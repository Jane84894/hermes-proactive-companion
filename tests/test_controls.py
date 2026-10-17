import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_companion.controls import CompanionControls
from hermes_companion.events import CompanionEvent
from hermes_companion.shadow import ShadowDecisionRunner
from hermes_companion.social import SocialPolicy
from hermes_companion.store import EventStore


class CompanionControlTests(unittest.TestCase):
    def test_explicit_stop_and_resume_are_persisted(self):
        now = datetime(2026, 10, 17, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.sqlite3"
        ) as store:
            controls = CompanionControls(store)
            stop = CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED", user_id="primary-user",
                timestamp=now, source="test", payload={"text": "先别主动联系我"},
            )
            resume = CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED", user_id="primary-user",
                timestamp=now + timedelta(minutes=1), source="test",
                payload={"text": "恢复主动联系"},
            )

            self.assertEqual(controls.apply_message(stop), "paused")
            self.assertTrue(controls.state("primary-user")["proactive_paused"])
            self.assertEqual(controls.apply_message(resume), "resumed")
            self.assertFalse(controls.state("primary-user")["proactive_paused"])

    def test_old_history_does_not_reapply_a_control_as_current(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.sqlite3"
        ) as store:
            event = CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED", user_id="primary-user",
                timestamp=now - timedelta(days=30), source="test",
                payload={"text": "先别主动联系我"},
            )
            controls = CompanionControls(store)

            self.assertIsNone(controls.apply_message(event))
            self.assertFalse(controls.state("primary-user").get("proactive_paused", False))

    def test_explicit_preferences_are_saved_once_and_less_chat_is_not_a_pause(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.sqlite3"
        ) as store:
            controls = CompanionControls(store)
            event = CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED", user_id="primary-user",
                timestamp=now, source="test", payload={"text": "少找我聊天"},
                idempotency_key="message-less-chat",
            )

            self.assertEqual(controls.apply_message(event), "preference_updated")
            self.assertEqual(controls.apply_message(event), "preference_updated")
            self.assertFalse(controls.state("primary-user").get("proactive_paused", False))
            row = store._db.execute(
                "SELECT value, evidence_count FROM interaction_preferences "
                "WHERE user_id='primary-user' AND category='proactive_frequency'"
            ).fetchone()
            self.assertEqual(tuple(row), ("reduced", 1))

    def test_explicit_discovery_and_time_preferences_are_persisted(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.sqlite3"
        ) as store:
            controls = CompanionControls(store)
            for index, text in enumerate(("以后晚上多联系", "技术新闻可以多分享")):
                event = CompanionEvent(
                    event_type="USER_MESSAGE_RECEIVED", user_id="primary-user",
                    timestamp=now, source="test", payload={"text": text},
                    idempotency_key=f"message-pref-{index}",
                )
                self.assertEqual(controls.apply_message(event), "preference_updated")
            rows = store._db.execute(
                "SELECT category, value FROM interaction_preferences ORDER BY category"
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows],
                [("discovery_technical_news", "more"), ("preferred_contact_time", "evening")],
            )

    def test_busy_message_blocks_due_shadow_candidate(self):
        zone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 10, 17, 14, 0, tzinfo=zone)
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.sqlite3"
        ) as store:
            db = store._db
            db.execute("INSERT INTO users(id) VALUES ('primary-user')")
            db.execute(
                "INSERT INTO followup_events (id, user_id, source_message_id, event_type, "
                "title, remind_at) VALUES ('exam-busy', 'primary-user', 'msg-busy', 'exam', "
                "'询问考试情况', ?)",
                ((now - timedelta(minutes=30)).isoformat(),),
            )
            db.commit()
            message = CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED", user_id="primary-user",
                timestamp=now - timedelta(minutes=5), source="test",
                payload={"text": "我最近很忙"},
            )
            controls = CompanionControls(store)
            self.assertEqual(controls.apply_message(message), "busy")

            runner = ShadowDecisionRunner(
                store, SocialPolicy(timezone="Asia/Shanghai"), interval_minutes=30
            )
            decision = runner.evaluate(now=now)

            self.assertEqual(decision["selected_candidate"], None)
            self.assertEqual(decision["suppressed_reason"], "user_busy")


if __name__ == "__main__":
    unittest.main()
