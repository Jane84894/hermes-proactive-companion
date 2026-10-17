import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_companion.actions import ProactiveActionRepository
from hermes_companion.companion_agent import DraftResult
from hermes_companion.delivery import ActionDispatcher
from hermes_companion.events import CompanionEvent
from hermes_companion.extraction import (
    EpisodeCandidate,
    ExtractionResult,
    FollowupCandidate,
    MemoryCandidate,
    TopicCandidate,
)
from hermes_companion.processor import EventProcessor
from hermes_companion.shadow import ShadowDecisionRunner
from hermes_companion.social import SocialPolicy
from hermes_companion.store import EventStore


ZONE = ZoneInfo("Asia/Shanghai")


class FakeExtractor:
    def __init__(self, result):
        self.result = result

    def extract(self, event):
        return self.result


class GroundedDraftAgent:
    def generate(self, candidate, memories, companion_state=None):
        self.candidate = candidate
        self.memory_ids = tuple(memory.id for memory in memories)
        return DraftResult(
            "generated", "数学考试出结果了吗？", self.memory_ids, "grounded_short_draft"
        )


class FakeSender:
    target_user_id = "wxid-primary"

    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return "mock-delivery-receipt"


class CompanionEndToEndTests(unittest.TestCase):
    def test_user_message_to_memory_shadow_draft_delivery_and_reply(self):
        now = datetime(2026, 10, 17, 16, 0, tzinfo=ZONE)
        source_time = now - timedelta(days=2)
        inbound = CompanionEvent(
            event_type="USER_MESSAGE_RECEIVED", user_id="primary-user",
            timestamp=source_time, source="hermes-sessiondb:weixin",
            payload={"text": "前天我参加了数学考试，今天会出结果。"},
            idempotency_key="hermes-weixin-message:e2e-1",
        )
        extraction = ExtractionResult(
            memories=(MemoryCandidate("fact", "用户参加了数学考试", 0.95, 0.8),),
            episodes=(EpisodeCandidate("参加数学考试，等待结果", source_time, 0.9, 0.8),),
            events=(FollowupCandidate(
                "exam_result", "数学考试", "等待考试结果", source_time,
                now - timedelta(minutes=5), 0.9,
            ),),
            topics=(TopicCandidate("数学考试", "等待结果", 0.8, True),),
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "companion.sqlite3"
            sender = FakeSender()
            draft_agent = GroundedDraftAgent()
            with EventStore(db_path) as store:
                store.publish(inbound)
                processor = EventProcessor(
                    store, FakeExtractor(extraction), worker_id="e2e-worker"
                )
                ingestion = processor.process_once(now=now)
                self.assertEqual(ingestion.processed, 1)

                runner = ShadowDecisionRunner(
                    store, SocialPolicy(timezone="Asia/Shanghai"), 30,
                    companion_agent=draft_agent,
                )
                decision = runner.evaluate(now=now)
                self.assertEqual(decision["selected_candidate"], "event:"
                    + store._db.execute("SELECT id FROM followup_events").fetchone()[0])
                self.assertEqual(decision["draft_status"], "generated")
                action_row = store._db.execute(
                    "SELECT id, state, draft_text FROM proactive_actions"
                ).fetchone()
                self.assertEqual(action_row["state"], "draft_ready")
                self.assertEqual(action_row["draft_text"], "数学考试出结果了吗？")
                self.assertTrue(draft_agent.memory_ids)

                dispatcher = ActionDispatcher(
                    store, SocialPolicy(timezone="Asia/Shanghai"), sender,
                    enabled=True, target_user_id="wxid-primary",
                    allowlisted_user_id="wxid-primary",
                )
                dispatcher.approve(str(action_row["id"]))
                result = dispatcher.dispatch_one(now=now + timedelta(minutes=2))
                self.assertEqual(result["state"], "delivered")
                self.assertEqual(sender.messages, ["数学考试出结果了吗？"])

                reply = CompanionEvent(
                    event_type="USER_MESSAGE_RECEIVED", user_id="primary-user",
                    timestamp=now + timedelta(minutes=10), source="weixin",
                    payload={"text": "出了，感觉还不错"},
                )
                self.assertEqual(
                    dispatcher.record_user_response(reply), str(action_row["id"])
                )
                self.assertEqual(
                    ProactiveActionRepository(store).get(str(action_row["id"])).state,
                    "responded",
                )
                raw = store._db.execute(
                    "SELECT payload_json FROM event_outbox WHERE event_id=?",
                    (inbound.event_id,),
                ).fetchone()[0]
                self.assertEqual(raw, "{}")

            with EventStore(db_path) as reopened:
                self.assertEqual(
                    ProactiveActionRepository(reopened).get(str(action_row["id"])).state,
                    "responded",
                )
                self.assertEqual(
                    reopened._db.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
