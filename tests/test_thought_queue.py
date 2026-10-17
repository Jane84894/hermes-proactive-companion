import tempfile
import unittest
from pathlib import Path

from hermes_companion.store import EventStore
from hermes_companion.thought_queue import QueueLimitExceeded, ThoughtQueueRepository

from datetime import datetime, timedelta, timezone


class ThoughtQueueSchemaTests(unittest.TestCase):
    def test_database_migrates_a_persistent_thought_queue(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            columns = {
                row["name"]
                for row in store._db.execute('PRAGMA table_info("thought_queue")')
            }

            self.assertEqual(store.schema_version, 9)
            self.assertTrue({
                "id", "user_id", "thought_type", "topic", "reason",
                "possible_action", "priority", "created_at", "earliest_send_at",
                "expires_at", "status", "attempt_count", "last_evaluated_at",
                "related_memory_ids_json", "source_key",
            }.issubset(columns))


class ThoughtQueueRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.tmp.name) / "companion.db")
        self.queue = ThoughtQueueRepository(
            self.store, max_queue_length=3, max_tasks_per_day=10
        )
        self.now = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def enqueue(self, key, topic=None, now=None):
        return self.queue.enqueue(
            user_id="primary-user", thought_type="topic_follow_up",
            topic=topic or key, reason="继续上次的话题", possible_action="follow_up",
            priority=0.7, source_key=key,
            earliest_send_at=self.now + timedelta(hours=1),
            expires_at=self.now + timedelta(days=7), now=now or self.now,
        )

    def test_enqueue_is_idempotent_and_persists_related_memories(self):
        thought, created = self.queue.enqueue(
            user_id="primary-user", thought_type="event_follow_up", topic="考试",
            reason="问问考试情况", possible_action="follow_up", priority=0.9,
            source_key="event:exam-1", earliest_send_at=self.now,
            expires_at=self.now + timedelta(days=2), related_memory_ids=["m1"], now=self.now,
        )
        duplicate, duplicate_created = self.queue.enqueue(
            user_id="primary-user", thought_type="event_follow_up", topic="考试",
            reason="问问考试情况", possible_action="follow_up", priority=0.9,
            source_key="event:exam-1", earliest_send_at=self.now,
            expires_at=self.now + timedelta(days=2), related_memory_ids=["m1"], now=self.now,
        )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.id, thought.id)
        self.assertEqual(duplicate.related_memory_ids, ("m1",))
        self.assertEqual(self.queue.get("primary-user", thought.id).topic, "考试")

        db_path = self.store.path
        self.store.close()
        self.store = EventStore(db_path)
        self.queue = ThoughtQueueRepository(self.store)
        restored = self.queue.get("primary-user", thought.id)
        self.assertEqual(restored.status, "pending")
        self.assertEqual(restored.related_memory_ids, ("m1",))

    def test_queue_and_daily_creation_budgets_are_enforced(self):
        limited = ThoughtQueueRepository(
            self.store, max_queue_length=10, max_tasks_per_day=2
        )
        original = self.queue
        self.queue = limited
        self.enqueue("one")
        self.enqueue("two")
        with self.assertRaises(QueueLimitExceeded):
            self.enqueue("three")

        # Completing an item frees queue capacity, but not today's creation budget.
        first = self.queue.search("primary-user", query="one")[0]
        self.queue.complete("primary-user", first.id)
        with self.assertRaises(QueueLimitExceeded):
            self.enqueue("three")
        self.queue = original

    def test_update_merge_delay_and_lifecycle_operations(self):
        first, _ = self.queue.enqueue(
            user_id="primary-user", thought_type="topic_follow_up", topic="屏幕动画",
            reason="旧状态", possible_action="follow_up", priority=0.6,
            source_key="topic:a", earliest_send_at=self.now,
            expires_at=self.now + timedelta(days=3), related_memory_ids=["m1"], now=self.now,
        )
        second, _ = self.queue.enqueue(
            user_id="primary-user", thought_type="topic_follow_up", topic="像素屏",
            reason="补充状态", possible_action="分享资料", priority=0.8,
            source_key="topic:b", earliest_send_at=self.now,
            expires_at=self.now + timedelta(days=5), related_memory_ids=["m2"], now=self.now,
        )
        third, _ = self.queue.enqueue(
            user_id="primary-user", thought_type="conversation_intent", topic="后续聊聊",
            reason="可选话题", possible_action="follow_up", priority=0.4,
            source_key="topic:c", earliest_send_at=self.now,
            expires_at=self.now + timedelta(days=5), now=self.now,
        )
        updated = self.queue.update("primary-user", first.id, reason="最新状态", priority=0.9)
        merged = self.queue.merge("primary-user", first.id, [second.id])
        delayed_to = self.now + timedelta(hours=5)
        delayed = self.queue.delay("primary-user", first.id, delayed_to)

        self.assertEqual(updated.reason, "最新状态")
        self.assertEqual(merged.related_memory_ids, ("m1", "m2"))
        self.assertEqual(self.queue.get("primary-user", second.id).status, "merged")
        self.assertEqual(delayed.earliest_send_at, delayed_to)
        self.assertTrue(self.queue.complete("primary-user", first.id))
        self.assertEqual(self.queue.get("primary-user", first.id).status, "completed")
        self.assertTrue(self.queue.cancel("primary-user", third.id))

    def test_search_filters_by_text_and_time_and_expiration_is_persisted(self):
        thought, _ = self.queue.enqueue(
            user_id="primary-user", thought_type="discovery_share", topic="开源 Agent 项目",
            reason="与兴趣相关", possible_action="分享来源", priority=0.8,
            source_key="discovery:repo-1", earliest_send_at=self.now,
            expires_at=self.now + timedelta(hours=1), now=self.now,
        )
        results = self.queue.search(
            "primary-user", query="开源", created_after=self.now - timedelta(minutes=1),
            created_before=self.now + timedelta(minutes=1),
        )
        self.assertEqual([item.id for item in results], [thought.id])
        self.assertEqual(self.queue.expire_due(now=self.now + timedelta(hours=2)), 1)
        self.assertEqual(self.queue.get("primary-user", thought.id).status, "expired")

    def test_attempt_budget_stops_repeated_task_execution(self):
        self.queue = ThoughtQueueRepository(self.store, max_attempts=2)
        thought, _ = self.enqueue("bounded", now=self.now)

        first = self.queue.record_attempt("primary-user", thought.id, now=self.now)
        second = self.queue.record_attempt("primary-user", thought.id, now=self.now)

        self.assertEqual(first.attempt_count, 1)
        self.assertEqual(second.attempt_count, 2)
        self.assertEqual(second.status, "failed")
        with self.assertRaisesRegex(ValueError, "attempt budget"):
            self.queue.record_attempt("primary-user", thought.id, now=self.now)


if __name__ == "__main__":
    unittest.main()
