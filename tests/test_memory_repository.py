import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_companion.memory import MemoryRepository
from hermes_companion.extraction import ExtractionResult, EpisodeCandidate, FollowupCandidate, TopicCandidate
from hermes_companion.extraction_store import ExtractionStore
from hermes_companion.store import EventStore
from hermes_companion.thought_queue import ThoughtQueueRepository
from hermes_companion.actions import ProactiveActionRepository


class MemoryRepositoryTests(unittest.TestCase):
    def test_duplicate_memory_is_not_written_twice(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            memories = MemoryRepository(store)
            first, created = memories.remember(
                user_id="primary-user",
                kind="fact",
                content="正在学习 ESP32",
                source_message_id="weixin:1",
                confidence=0.9,
                importance=0.8,
            )
            duplicate, created_again = memories.remember(
                user_id="primary-user",
                kind="fact",
                content="  正在学习 ESP32  ",
                source_message_id="weixin:2",
                confidence=0.9,
                importance=0.8,
            )

            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(first.id, duplicate.id)
            self.assertEqual(memories.count_active("primary-user"), 1)

    def test_search_and_user_delete_are_reflected_immediately(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            memories = MemoryRepository(store)
            record, _ = memories.remember(
                user_id="primary-user",
                kind="preference",
                content="喜欢讨论嵌入式和物联网项目",
                source_message_id="weixin:3",
                confidence=0.8,
                importance=0.7,
            )

            self.assertEqual(
                [item.id for item in memories.search("primary-user", "物联网")],
                [record.id],
            )
            self.assertTrue(memories.delete("primary-user", record.id))
            self.assertEqual(memories.search("primary-user", "物联网"), [])
            self.assertFalse(memories.delete("primary-user", record.id))

    def test_search_records_covers_memory_episode_event_and_topic_by_query_and_time(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            memories = MemoryRepository(store)
            memories.remember(
                user_id="primary-user", kind="fact", content="正在研究 ESP32 屏幕",
                source_message_id="weixin:search-1", confidence=0.9, importance=0.8,
            )
            db = store._db
            db.execute(
                "INSERT INTO episodes (id,user_id,source_message_id,occurred_at,summary,source,"
                "confidence,importance) VALUES ('ep-search','primary-user','weixin:search-1',"
                "'2026-10-17T10:00:00+08:00','ESP32 屏幕调试','weixin',0.9,0.8)"
            )
            db.execute(
                "INSERT INTO followup_events (id,user_id,source_message_id,event_type,title,"
                "remind_at) VALUES ('event-search','primary-user','weixin:search-1','project',"
                "'检查 ESP32 屏幕','2026-10-18T10:00:00+08:00')"
            )
            db.execute(
                "INSERT INTO topics (id,user_id,title,current_state,interest_score,last_active_at) "
                "VALUES ('topic-search','primary-user','ESP32 屏幕','调试中',0.9,"
                "'2026-10-17T10:00:00+08:00')"
            )
            db.commit()

            result = memories.search_records(
                "primary-user", query="ESP32",
                start_at="2026-10-16T00:00:00+08:00",
                end_at="2026-10-19T00:00:00+08:00",
            )

            self.assertEqual(len(result["memories"]), 1)
            self.assertEqual(len(result["episodes"]), 1)
            self.assertEqual(len(result["events"]), 1)
            self.assertEqual(len(result["topics"]), 1)
            outside = memories.search_records(
                "primary-user", query="ESP32",
                start_at="2026-10-19T00:00:00+08:00",
                end_at="2026-10-20T00:00:00+08:00",
            )
            self.assertTrue(all(not values for values in outside.values()))

    def test_related_memories_follow_shared_source_and_explicit_links(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            memories = MemoryRepository(store)
            first, _ = memories.remember(
                user_id="primary-user", kind="fact", content="使用 ESP32",
                source_message_id="weixin:related-1", confidence=0.9, importance=0.8,
            )
            second, _ = memories.remember(
                user_id="primary-user", kind="preference", content="喜欢嵌入式项目",
                source_message_id="weixin:related-1", confidence=0.9, importance=0.7,
            )
            linked, _ = memories.remember(
                user_id="primary-user", kind="fact", content="正在测试屏幕刷新率",
                source_message_id="weixin:related-2", confidence=0.9, importance=0.7,
            )
            store._db.execute(
                "INSERT INTO memory_links (id,source_memory_id,target_memory_id,relation) "
                "VALUES ('related-link',?,?, 'related')", (first.id, linked.id),
            )
            store._db.commit()

            related = memories.related_memories("primary-user", first.id)

            self.assertEqual({item["id"] for item in related}, {second.id, linked.id})

    def test_correction_supersedes_old_fact_and_invalidates_source_derivatives(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            memories = MemoryRepository(store)
            original, _ = memories.remember(
                user_id="primary-user", kind="fact", content="正在学习 ESP32",
                source_message_id="weixin:correct-1", confidence=0.9, importance=0.8,
            )
            store._db.execute(
                "INSERT INTO episodes (id, user_id, source_message_id, occurred_at, summary, "
                "source, confidence, importance) VALUES ('episode-1','primary-user',"
                "'weixin:correct-1','2026-10-17T08:00:00+00:00','学习 ESP32','weixin',0.9,0.8)"
            )
            store._db.execute(
                "INSERT INTO followup_events (id, user_id, source_message_id, event_type, title) "
                "VALUES ('event-1','primary-user','weixin:correct-1','project','ESP32 进展')"
            )
            store._db.commit()

            corrected = memories.correct("primary-user", original.id, "正在学习 STM32")

            self.assertIsNotNone(corrected)
            self.assertEqual(corrected.content, "正在学习 STM32")
            self.assertIsNone(corrected.source_message_id)
            self.assertEqual(
                store._db.execute("SELECT status FROM memories WHERE id=?", (original.id,)).fetchone()[0],
                "superseded",
            )
            self.assertEqual(memories.search("primary-user", "ESP32"), [])
            self.assertEqual(
                [item.id for item in memories.search("primary-user", "STM32")], [corrected.id]
            )
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 0)
            self.assertEqual(
                store._db.execute("SELECT status FROM followup_events WHERE id='event-1'").fetchone()[0],
                "cancelled",
            )
            retried = memories.correct("primary-user", original.id, "正在学习 STM32")
            self.assertEqual(retried.id, corrected.id)

    def test_export_contains_structured_companion_data_but_not_raw_inbox(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            memories = MemoryRepository(store)
            memories.remember(
                user_id="primary-user", kind="fact", content="正在做一个项目",
                source_message_id="wx-private-id", confidence=0.9, importance=0.7,
            )
            ThoughtQueueRepository(store).enqueue(
                user_id="primary-user", thought_type="research", topic="项目资料",
                reason="后续找相关资料", possible_action="research", priority=0.5,
                source_key="research:project", earliest_send_at=datetime(2026,10,17,tzinfo=timezone.utc),
                expires_at=datetime(2026,10,20,tzinfo=timezone.utc), now=datetime(2026,10,17,tzinfo=timezone.utc),
            )
            store._db.execute(
                "INSERT INTO event_outbox (event_id, event_type, user_id, event_timestamp, "
                "source, payload_json, correlation_id, schema_version) "
                "VALUES ('e1', 'USER_MESSAGE_RECEIVED', 'primary-user', '2026-10-17T00:00:00+00:00', "
                "'weixin', '{\"text\":\"private raw message\"}', 'c1', 1)"
            )
            store._db.commit()

            exported = memories.export_data("primary-user")

            self.assertEqual(exported["user_id"], "primary-user")
            self.assertEqual(exported["memories"][0]["content"], "正在做一个项目")
            self.assertEqual(exported["thought_queue"][0]["topic"], "项目资料")
            self.assertNotIn("event_outbox", exported)
            self.assertNotIn("private raw message", str(exported))

    def test_forget_invalidates_source_derived_records_and_only_orphaned_topics(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            repository = MemoryRepository(store)
            memory, _ = repository.remember(
                user_id="primary-user", kind="fact", content="正在准备 ESP32 展示",
                source_message_id="message-1", confidence=0.9, importance=0.8,
            )
            remaining_memory, _ = repository.remember(
                user_id="primary-user", kind="preference", content="喜欢嵌入式项目",
                source_message_id="message-2", confidence=0.9, importance=0.7,
            )
            db = store._db
            db.execute(
                "INSERT INTO episodes (id, user_id, source_message_id, occurred_at, summary, "
                "source, confidence, importance) VALUES ('ep-1', 'primary-user', 'message-1', "
                "'2026-10-17T08:00:00+00:00', '准备 ESP32 展示', 'weixin', 0.9, 0.8)"
            )
            db.execute(
                "INSERT INTO followup_events (id, user_id, source_message_id, event_type, title, "
                "remind_at) VALUES ('event-1', 'primary-user', 'message-1', 'project', '展示后跟进', "
                "'2026-10-18T08:00:00+00:00')"
            )
            db.commit()
            ThoughtQueueRepository(store).enqueue(
                user_id="primary-user", thought_type="event_follow_up", topic="展示后跟进",
                reason="展示后关心一下", possible_action="follow_up", priority=0.8,
                source_key="event:event-1", earliest_send_at=datetime(2026, 10, 18, 8, tzinfo=timezone.utc),
                expires_at=datetime(2026, 10, 21, 8, tzinfo=timezone.utc),
                related_memory_ids=[memory.id], now=datetime(2026, 10, 17, 8, tzinfo=timezone.utc),
            )
            extraction_store = ExtractionStore(store)
            extraction_store.persist(
                user_id="primary-user", source_message_id="message-1", source="weixin",
                source_timestamp=datetime.now().astimezone(),
                extraction=ExtractionResult((), (), (), (
                    TopicCandidate("ESP32 展示", "准备中", 0.8, True),
                )),
            )
            extraction_store.persist(
                user_id="primary-user", source_message_id="message-2", source="weixin",
                source_timestamp=datetime.now().astimezone(),
                extraction=ExtractionResult((), (), (), (
                    TopicCandidate("ESP32 展示", "喜欢这个项目", 0.8, True),
                )),
            )
            db.execute(
                "INSERT INTO memory_links (id, source_memory_id, target_memory_id, relation) "
                "VALUES ('link-1', ?, ?, 'related')",
                (memory.id, remaining_memory.id),
            )
            db.commit()
            action, _ = ProactiveActionRepository(store).create(
                idempotency_key="proactive:event:event-1",
                candidate_id="event:event-1", intent="event_follow_up",
                payload={"title": "展示后跟进"},
            )
            ProactiveActionRepository(store).transition(
                action.id, "draft_ready", draft_text="之前记得你准备展示，这次顺利吗？"
            )
            db.execute(
                "INSERT INTO social_decisions (evaluation_slot, evaluated_at, candidate_id, "
                "candidate_kind, selected, score, reason_codes_json, components_json, "
                "draft_status, draft_text, draft_basis_json) VALUES "
                "('slot-1','2026-10-17T10:00:00+00:00','event:event-1','event_follow_up',"
                "1,0.8,'[]','{}','generated','之前记得你准备展示，这次顺利吗？',?)",
                (f'["{memory.id}"]',),
            )
            db.commit()

            self.assertTrue(repository.delete("primary-user", memory.id))

            self.assertEqual(db.execute("SELECT COUNT(*) FROM episodes WHERE source_message_id='message-1'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM followup_events WHERE id='event-1'").fetchone()[0], "cancelled")
            self.assertEqual(db.execute("SELECT status FROM topics WHERE title='ESP32 展示'").fetchone()[0], "open")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_links WHERE id='link-1'").fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT status FROM thought_queue WHERE source_key='event:event-1'").fetchone()[0],
                "cancelled",
            )
            self.assertEqual(repository.count_active("primary-user"), 1)
            self.assertEqual(ProactiveActionRepository(store).get(action.id).state, "cancelled")
            draft = db.execute(
                "SELECT draft_text FROM social_decisions WHERE candidate_id='event:event-1'"
            ).fetchone()[0]
            self.assertIsNone(draft)


if __name__ == "__main__":
    unittest.main()
