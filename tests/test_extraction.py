import unittest
from datetime import datetime, timezone

from hermes_companion.events import CompanionEvent
from hermes_companion.extraction import MemoryExtractor


class FakeJsonModel:
    def __init__(self, response):
        self.response = response
        self.prompts = []

    def generate_json(self, system_prompt, user_prompt, *, max_tokens):
        self.prompts.append((system_prompt, user_prompt, max_tokens))
        return self.response


class MemoryExtractorTests(unittest.TestCase):
    def test_rejects_missing_or_unexpected_top_level_fields(self):
        extractor = MemoryExtractor(FakeJsonModel({}))

        with self.assertRaisesRegex(ValueError, "exactly these fields"):
            extractor._parse({})
        with self.assertRaisesRegex(ValueError, "exactly these fields"):
            extractor._parse({
                "memories": [], "episodes": [], "events": [], "topics": [], "extra": "ignored"
            })

    def test_extracts_typed_candidates_and_anchors_relative_dates_to_source_time(self):
        model = FakeJsonModel(
            {
                "memories": [
                    {
                        "kind": "fact",
                        "content": "用户正在准备高数考试",
                        "confidence": 0.95,
                        "importance": 0.8,
                    }
                ],
                "episodes": [],
                "events": [
                    {
                        "event_type": "exam",
                        "title": "高数考试",
                        "details": "下午考试",
                        "occurred_at": "2026-10-18T14:00:00+08:00",
                        "remind_at": "2026-10-18T17:00:00+08:00",
                        "confidence": 0.9,
                    }
                ],
                "topics": [],
            }
        )
        event = CompanionEvent(
            event_type="USER_MESSAGE_RECEIVED",
            user_id="primary-user",
            timestamp=datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc),
            source="hermes-sessiondb:weixin",
            payload={"text": "明天下午我要考高数"},
            idempotency_key="hermes-weixin-message:99",
        )

        extracted = MemoryExtractor(model).extract(event)

        self.assertEqual(extracted.memories[0].kind, "fact")
        self.assertEqual(extracted.events[0].remind_at.isoformat(), "2026-10-18T17:00:00+08:00")
        self.assertIn("2026-10-17T20:00:00+08:00", model.prompts[0][1])
        self.assertIn("not use the current runtime date", model.prompts[0][0])
        self.assertIn('"remind_at"', model.prompts[0][0])
        self.assertIn("Every listed field is required", model.prompts[0][0])
        self.assertIn("timezone offset", model.prompts[0][0])
        self.assertEqual(model.prompts[0][2], 1200)

    def test_rejects_invalid_memory_types_and_unzoned_event_times(self):
        model = FakeJsonModel(
            {
                "memories": [
                    {"kind": "guess", "content": "x", "confidence": 0.9, "importance": 0.8}
                ],
                "episodes": [],
                "events": [
                    {
                        "event_type": "exam",
                        "title": "考试",
                        "details": "",
                        "occurred_at": "2026-10-18T14:00:00",
                        "remind_at": None,
                        "confidence": 0.9,
                    }
                ],
                "topics": [],
            }
        )
        event = CompanionEvent(
            event_type="USER_MESSAGE_RECEIVED",
            user_id="primary-user",
            timestamp=datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc),
            source="test",
            payload={"text": "考试"},
        )
        with self.assertRaisesRegex(ValueError, "memory kind"):
            MemoryExtractor(model).extract(event)

        model.response["memories"][0]["kind"] = "fact"
        with self.assertRaisesRegex(ValueError, "must include a timezone"):
            MemoryExtractor(model).extract(event)

    def test_empty_or_non_user_event_is_not_sent_to_model(self):
        model = FakeJsonModel({})
        event = CompanionEvent(
            event_type="ASSISTANT_MESSAGE_SENT",
            user_id="primary-user",
            timestamp=datetime.now(timezone.utc),
            source="test",
            payload={"text": "hello"},
        )
        with self.assertRaisesRegex(ValueError, "USER_MESSAGE_RECEIVED"):
            MemoryExtractor(model).extract(event)
        self.assertEqual(model.prompts, [])


if __name__ == "__main__":
    unittest.main()
