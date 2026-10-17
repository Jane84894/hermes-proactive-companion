import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_companion.shadow import ShadowDecisionRunner
from hermes_companion.social import SocialPolicy
from hermes_companion.events import CompanionEvent
from hermes_companion.store import EventStore
from hermes_companion.companion_agent import DraftResult
from hermes_companion.thought_queue import ThoughtQueueRepository


class ShadowDecisionTests(unittest.TestCase):
    def test_selected_topic_is_not_repeated_as_a_candidate(self):
        zone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 10, 17, 14, 0, tzinfo=zone)
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            db = store._db
            db.execute("INSERT INTO users(id) VALUES ('primary-user')")
            db.execute(
                "INSERT INTO topics (id, user_id, title, interest_score, last_active_at) "
                "VALUES ('topic-1', 'primary-user', '准备旅行', 0.95, ?)",
                ((now - timedelta(hours=2)).isoformat(),),
            )
            db.commit()
            runner = ShadowDecisionRunner(
                store, SocialPolicy(timezone="Asia/Shanghai"), interval_minutes=30
            )

            first = runner.evaluate(now=now)
            second = runner.evaluate(now=now + timedelta(minutes=30))

            self.assertEqual(first["selected_candidate"], "topic:topic-1")
            self.assertEqual(second["candidate_count"], 0)
            thought = ThoughtQueueRepository(store).search(
                "primary-user", query="准备旅行"
            )[0]
            self.assertEqual(thought.source_key, "topic:topic-1")
            self.assertEqual(thought.status, "selected")

    def test_cancelled_queued_thought_is_not_evaluated_again(self):
        zone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 10, 17, 14, 0, tzinfo=zone)
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            db = store._db
            db.execute("INSERT INTO users(id) VALUES ('primary-user')")
            db.execute(
                "INSERT INTO topics (id, user_id, title, interest_score, last_active_at) "
                "VALUES ('topic-cancel', 'primary-user', '想聊的项目', 0.95, ?)",
                ((now - timedelta(hours=2)).isoformat(),),
            )
            db.commit()
            runner = ShadowDecisionRunner(
                store, SocialPolicy(timezone="Asia/Shanghai"), interval_minutes=30
            )
            first_candidates = runner._candidates(now)
            self.assertEqual(len(first_candidates), 1)
            thought = ThoughtQueueRepository(store).search(
                "primary-user", query="想聊的项目"
            )[0]
            self.assertTrue(ThoughtQueueRepository(store).cancel("primary-user", thought.id))

            later_candidates = runner._candidates(now + timedelta(minutes=30))

            self.assertEqual(later_candidates, [])

    def test_queue_refreshes_candidate_interest_without_overriding_user_delay(self):
        zone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 10, 17, 14, 0, tzinfo=zone)
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            db = store._db
            db.execute("INSERT INTO users(id) VALUES ('primary-user')")
            db.execute(
                "INSERT INTO topics (id, user_id, title, interest_score, last_active_at) "
                "VALUES ('topic-change', 'primary-user', '学习的主题', 0.95, ?)",
                ((now - timedelta(hours=2)).isoformat(),),
            )
            db.commit()
            runner = ShadowDecisionRunner(
                store, SocialPolicy(timezone="Asia/Shanghai"), interval_minutes=30
            )
            candidate = runner._candidates(now)[0]
            thought = runner.thoughts.get_by_source_key("primary-user", candidate.id)
            delayed_until = now + timedelta(hours=4)
            runner.thoughts.delay("primary-user", thought.id, delayed_until)
            updated_candidate = replace(candidate, priority=0.2, relevance=0.1)

            ready = runner._enqueue_candidates([updated_candidate], now + timedelta(minutes=30))
            refreshed = runner.thoughts.get("primary-user", thought.id)

            self.assertEqual(ready, [])
            self.assertEqual(refreshed.priority, 0.2)
            self.assertEqual(refreshed.earliest_send_at, delayed_until)

    def test_due_event_is_logged_once_and_never_sent(self):
        zone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 10, 17, 14, 0, tzinfo=zone)
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            db = store._db
            db.execute("INSERT INTO users(id) VALUES ('primary-user')")
            db.execute(
                "INSERT INTO followup_events (id, user_id, source_message_id, event_type, "
                "title, remind_at) VALUES ('exam-1', 'primary-user', 'msg-1', 'exam', "
                "'询问考试情况', ?)",
                ((now - timedelta(minutes=30)).isoformat(),),
            )
            db.commit()
            runner = ShadowDecisionRunner(
                store, SocialPolicy(timezone="Asia/Shanghai"), interval_minutes=30
            )

            first = runner.evaluate(now=now)
            second = runner.evaluate(now=now + timedelta(minutes=2))

            self.assertIsNotNone(first)
            self.assertEqual(first["candidate_count"], 1)
            self.assertEqual(first["selected_candidate"], "event:exam-1")
            self.assertEqual(first["outbound_delivery"], "disabled")
            self.assertIsNone(second)
            row = db.execute(
                "SELECT selected, reason_codes_json FROM social_decisions"
            ).fetchone()
            self.assertEqual(row["selected"], 1)
            self.assertIn("selected_highest_score", row["reason_codes_json"])

            runner.set_paused(True)
            paused = runner.evaluate(now=now + timedelta(minutes=30))
            self.assertEqual(paused["selected_candidate"], None)
            self.assertEqual(paused["suppressed_reason"], "user_paused")
            runner.set_paused(False)

    def test_light_chat_waits_24_hours_then_becomes_candidate_without_other_topics(self):
        zone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 10, 17, 18, 0, tzinfo=zone)
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.sqlite3"
        ) as store:
            store.publish(CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED", user_id="primary-user",
                timestamp=now - timedelta(hours=23), source="test", payload={"text": "你好"},
            ))
            runner = ShadowDecisionRunner(
                store, SocialPolicy(timezone="Asia/Shanghai"), interval_minutes=30,
                companion_agent=None,
            )

            before_threshold = runner._candidates(now)
            later = runner._candidates(now + timedelta(hours=2))

            self.assertEqual(before_threshold, [])
            self.assertEqual(len(later), 1)
            self.assertEqual(later[0].kind, "light_chat")

    def test_selected_candidate_gets_reviewable_draft(self):
        class FakeAgent:
            def generate(self, candidate, memories, companion_state=None):
                return DraftResult("generated", "考试感觉怎么样？", (), "grounded_short_draft")

        zone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 10, 17, 14, 0, tzinfo=zone)
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            db = store._db
            db.execute("INSERT INTO users(id) VALUES ('primary-user')")
            db.execute(
                "INSERT INTO followup_events (id, user_id, source_message_id, event_type, "
                "title, remind_at) VALUES ('exam-2', 'primary-user', 'msg-2', 'exam', "
                "'询问考试情况', ?)",
                ((now - timedelta(minutes=30)).isoformat(),),
            )
            db.commit()
            runner = ShadowDecisionRunner(
                store, SocialPolicy(timezone="Asia/Shanghai"), interval_minutes=30,
                companion_agent=FakeAgent(),
            )

            report = runner.evaluate(now=now)
            row = db.execute(
                "SELECT candidate_title, draft_status, draft_text FROM social_decisions "
                "WHERE selected=1"
            ).fetchone()

            self.assertEqual(report["draft_status"], "generated")
            self.assertEqual(row["candidate_title"], "询问考试情况")
            self.assertEqual(row["draft_status"], "generated")
            self.assertEqual(row["draft_text"], "考试感觉怎么样？")
            action = db.execute(
                "SELECT state, draft_text FROM proactive_actions WHERE candidate_id=?",
                ("event:exam-2",),
            ).fetchone()
            self.assertEqual(action["state"], "draft_ready")
            self.assertEqual(action["draft_text"], "考试感觉怎么样？")


if __name__ == "__main__":
    unittest.main()
