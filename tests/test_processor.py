import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_companion.events import CompanionEvent
from hermes_companion.budget import BudgetExceeded
from hermes_companion.extraction import (
    EpisodeCandidate,
    ExtractionResult,
    FollowupCandidate,
    MemoryCandidate,
    TopicCandidate,
)
from hermes_companion.processor import EventProcessor
from hermes_companion.store import EventStore


class FakeExtractor:
    def __init__(self, result=None, error=None):
        self.result = result or ExtractionResult((), (), (), ())
        self.error = error
        self.calls = 0

    def extract(self, event):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class EventProcessorTests(unittest.TestCase):
    def _event(self):
        return CompanionEvent(
            event_type="USER_MESSAGE_RECEIVED",
            user_id="primary-user",
            timestamp=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc),
            source="hermes-sessiondb:weixin",
            payload={"text": "我周五有考试"},
            idempotency_key="hermes-weixin-message:10",
        )

    def test_process_persists_memories_and_redacts_raw_event_text(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            event = self._event()
            store.publish(event)
            extractor = FakeExtractor(
                ExtractionResult(
                    memories=(MemoryCandidate("fact", "周五有考试", 0.9, 0.7),),
                    episodes=(EpisodeCandidate("周五准备考试", None, 0.9, 0.7),),
                    events=(FollowupCandidate(
                        "exam", "周五考试", "", None,
                        datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc), 0.9,
                    ),),
                    topics=(TopicCandidate("考试准备", "周五考试", 0.8, True),),
                )
            )
            processor = EventProcessor(store, extractor, worker_id="memory-worker")

            report = processor.process_once(
                now=datetime(2026, 10, 17, 8, 1, tzinfo=timezone.utc)
            )
            row = store._db.execute(
                "SELECT status, payload_json FROM event_outbox WHERE event_id=?",
                (event.event_id,),
            ).fetchone()

            self.assertEqual(report.processed, 1)
            self.assertEqual(extractor.calls, 1)
            self.assertEqual(row["status"], "processed")
            self.assertEqual(row["payload_json"], "{}")
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)
            state = processor.companion_state.get("primary-user")
            self.assertEqual(state["current_interests"][0]["title"], "考试准备")
            self.assertEqual(state["pending_thoughts"][0]["title"], "周五考试")
            self.assertEqual(state["last_interaction_summary"], "周五准备考试")
            self.assertEqual(
                processor.process_once(now=datetime(2026, 10, 17, 8, 2, tzinfo=timezone.utc)).processed,
                0,
            )

    def test_exact_acknowledgement_skips_model_but_is_processed_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            event = CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED", user_id="primary-user",
                timestamp=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc),
                source="hermes-sessiondb:weixin", payload={"text": " 好的！ "},
                idempotency_key="hermes-weixin-message:11",
            )
            store.publish(event)
            extractor = FakeExtractor()
            processor = EventProcessor(store, extractor, worker_id="memory-worker")

            report = processor.process_once(
                now=datetime(2026, 10, 17, 8, 1, tzinfo=timezone.utc)
            )

            row = store._db.execute(
                "SELECT status, payload_json FROM event_outbox WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            self.assertEqual(report.processed, 1)
            self.assertEqual(extractor.calls, 0)
            self.assertEqual(row["status"], "processed")
            self.assertEqual(row["payload_json"], "{}")
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)

    def test_short_but_substantive_message_still_uses_extractor(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            event = self._event()
            event = CompanionEvent(
                event_type=event.event_type, user_id=event.user_id, timestamp=event.timestamp,
                source=event.source, payload={"text": "我失业了"},
                idempotency_key=event.idempotency_key,
            )
            store.publish(event)
            extractor = FakeExtractor()
            processor = EventProcessor(store, extractor, worker_id="memory-worker")

            processor.process_once(now=datetime(2026, 10, 17, 8, 1, tzinfo=timezone.utc))

            self.assertEqual(extractor.calls, 1)

    def test_assistant_event_is_redacted_without_extraction_or_control_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            event = CompanionEvent(
                event_type="ASSISTANT_MESSAGE_SENT", user_id="primary-user",
                timestamp=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc),
                source="hermes-sessiondb:weixin",
                payload={"text": "先别主动联系我", "platform_message_id": "wx-a1"},
                idempotency_key="hermes-weixin-message:12",
            )
            store.publish(event)
            extractor = FakeExtractor()
            processor = EventProcessor(store, extractor, worker_id="memory-worker")

            report = processor.process_once(
                now=datetime(2026, 10, 17, 8, 1, tzinfo=timezone.utc)
            )

            row = store._db.execute(
                "SELECT status, payload_json FROM event_outbox WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            self.assertEqual(report.processed, 1)
            self.assertEqual(extractor.calls, 0)
            self.assertEqual(row["status"], "processed")
            self.assertEqual(row["payload_json"], "{}")
            self.assertFalse(processor.controls.state("primary-user").get("proactive_paused", False))

    def test_model_failure_retries_without_persisting_error_text(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            event = self._event()
            store.publish(event)
            processor = EventProcessor(
                store,
                FakeExtractor(error=RuntimeError("sensitive content in provider exception")),
                worker_id="memory-worker",
                retry_delay_seconds=60,
            )
            now = datetime(2026, 10, 17, 8, 1, tzinfo=timezone.utc)

            report = processor.process_once(now=now)
            row = store._db.execute(
                "SELECT status, payload_json, last_error_code FROM event_outbox WHERE event_id=?",
                (event.event_id,),
            ).fetchone()

            self.assertEqual(report.failed, 1)
            self.assertEqual(row["status"], "pending")
            self.assertIn("我周五有考试", row["payload_json"])
            self.assertEqual(row["last_error_code"], "RuntimeError")
            self.assertNotIn("sensitive content", row["last_error_code"])
            self.assertEqual(
                processor.process_once(now=now + timedelta(seconds=59)).processed,
                0,
            )

    def test_output_validation_errors_use_slower_bounded_retry(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            event = self._event()
            store.publish(event)
            processor = EventProcessor(
                store, FakeExtractor(error=ValueError("invalid model field")),
                worker_id="memory-worker",
            )
            now = datetime(2026, 10, 17, 8, 1, tzinfo=timezone.utc)

            processor.process_once(now=now)
            row = store._db.execute(
                "SELECT last_error_code, next_attempt_at FROM event_outbox WHERE event_id=?",
                (event.event_id,),
            ).fetchone()

            self.assertEqual(row["last_error_code"], "OutputValidationError")
            self.assertEqual(
                datetime.fromisoformat(row["next_attempt_at"]),
                now + timedelta(minutes=15),
            )

    def test_pause_instruction_takes_effect_even_when_model_budget_is_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            event = self._event()
            event = CompanionEvent(
                event_type=event.event_type, user_id=event.user_id,
                timestamp=event.timestamp, source=event.source,
                payload={"text": "先别主动联系我"}, idempotency_key=event.idempotency_key,
            )
            store.publish(event)
            retry_at = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
            processor = EventProcessor(
                store, FakeExtractor(error=BudgetExceeded(retry_at)), worker_id="control-worker"
            )

            report = processor.process_once(now=datetime(2026, 10, 17, 8, 1, tzinfo=timezone.utc))
            state = processor.controls.state("primary-user")

            self.assertEqual(report.deferred, 1)
            self.assertTrue(state["proactive_paused"])


if __name__ == "__main__":
    unittest.main()
