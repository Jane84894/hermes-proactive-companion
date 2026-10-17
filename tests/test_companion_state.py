import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_companion.companion_state import CompanionStateRepository
from hermes_companion.store import EventStore
from hermes_companion.thought_queue import ThoughtQueueRepository


class CompanionStateTests(unittest.TestCase):
    def test_refresh_derives_context_and_preserves_user_controls(self):
        now = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            db = store._db
            db.execute("INSERT INTO users(id) VALUES ('primary-user')")
            db.execute(
                "INSERT INTO topics (id, user_id, title, current_state, interest_score, last_active_at) "
                "VALUES ('topic-1', 'primary-user', 'ESP32 屏幕', '正在优化动画', 0.9, ?)",
                ((now - timedelta(hours=2)).isoformat(),),
            )
            db.execute(
                "INSERT INTO followup_events (id, user_id, source_message_id, event_type, title, "
                "remind_at) VALUES ('exam-1', 'primary-user', 'm1', 'exam', '高数考试', ?)",
                ((now + timedelta(hours=3)).isoformat(),),
            )
            db.execute(
                "INSERT INTO episodes (id, user_id, source_message_id, occurred_at, summary, "
                "source, confidence, importance) VALUES ('episode-1', 'primary-user', 'm1', ?, "
                "'用户最近在优化 ESP32 屏幕动画', 'weixin', 0.9, 0.8)",
                (now.isoformat(),),
            )
            db.execute(
                "INSERT INTO companion_states (user_id, state_json) VALUES (?, ?)",
                ("primary-user", json.dumps({
                    "proactive_paused": True,
                    "busy_until": (now + timedelta(days=1)).isoformat(),
                    "recent_discoveries": [{"title": "保留发现"}],
                })),
            )
            db.commit()

            state = CompanionStateRepository(store).refresh(
                "primary-user", now=now, persona_version="persona-v2"
            )

            self.assertTrue(state["proactive_paused"])
            self.assertEqual(state["busy_until"], (now + timedelta(days=1)).isoformat())
            self.assertEqual(state["current_interests"][0]["title"], "ESP32 屏幕")
            self.assertEqual(state["pending_thoughts"][0]["title"], "高数考试")
            self.assertEqual(state["last_interaction_summary"], "用户最近在优化 ESP32 屏幕动画")
            self.assertEqual(state["persona_version"], "persona-v2")
            self.assertEqual(state["recent_discoveries"], [{"title": "保留发现"}])

    def test_expired_topics_and_events_are_not_prompt_context(self):
        now = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            db = store._db
            db.execute("INSERT INTO users(id) VALUES ('primary-user')")
            db.execute(
                "INSERT INTO topics (id, user_id, title, interest_score, last_active_at) "
                "VALUES ('old-topic', 'primary-user', '旧兴趣', 1.0, ?)",
                ((now - timedelta(days=120)).isoformat(),),
            )
            db.execute(
                "INSERT INTO followup_events (id, user_id, source_message_id, event_type, title, "
                "remind_at, status) VALUES ('old-event', 'primary-user', 'm1', 'exam', '过期考试', ?, 'expired')",
                ((now - timedelta(days=30)).isoformat(),),
            )
            db.commit()

            state = CompanionStateRepository(store).refresh("primary-user", now=now)

            self.assertEqual(state["current_interests"], [])
            self.assertEqual(state["pending_thoughts"], [])

    def test_refresh_includes_bounded_pending_thought_queue_items(self):
        now = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            queue = ThoughtQueueRepository(store)
            queue.enqueue(
                user_id="primary-user", thought_type="research",
                topic="查一下 ESP32 屏幕驱动资料", reason="最近讨论过刷新率",
                possible_action="find_documentation", priority=0.7,
                source_key="research:esp32-display",
                earliest_send_at=now, expires_at=now + timedelta(days=2), now=now,
            )

            state = CompanionStateRepository(store).refresh("primary-user", now=now)

            self.assertEqual(state["pending_thoughts"][0]["title"], "查一下 ESP32 屏幕驱动资料")
            self.assertEqual(state["pending_thoughts"][0]["type"], "research")


if __name__ == "__main__":
    unittest.main()
