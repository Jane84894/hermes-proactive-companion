import tempfile
import unittest
import stat
import sqlite3
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_companion.events import CompanionEvent
from hermes_companion.store import EventStore


class EventStoreTests(unittest.TestCase):
    def test_store_creates_private_database_with_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "private" / "companion.sqlite3"
            with EventStore(db) as store:
                self.assertEqual(store.schema_version, 9)
                tables = {
                    row[0]
                    for row in store._db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(
                    {
                        "users", "memories", "episodes", "followup_events",
                        "topics", "relationships", "interaction_preferences",
                        "companion_states", "memory_links", "memory_operations",
                        "event_outbox", "consumer_cursors", "topic_sources",
                        "proactive_actions", "thought_queue",
                    }.issubset(tables)
                )
            self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(db.parent.stat().st_mode), 0o700)

    def test_existing_schema_two_database_migrates_to_current_version(self):
        migration_dir = Path(__file__).parents[1] / "src/hermes_companion/migrations"
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "existing.db"
            with sqlite3.connect(db_path) as db:
                for version in (1, 2):
                    migration = sorted(migration_dir.glob(f"{version:04d}_*.sql"))[0]
                    db.executescript(migration.read_text(encoding="utf-8"))
                    db.execute(
                        "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                    )
            with EventStore(db_path) as store:
                self.assertEqual(store.schema_version, 9)
                columns = {
                    row["name"]
                    for row in store._db.execute("PRAGMA table_info(social_decisions)")
                }
                self.assertIn("draft_text", columns)

    def test_publish_is_idempotent_and_event_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "companion.sqlite3"
            event = CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED",
                user_id="primary-user",
                timestamp=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc),
                source="hermes-sessiondb",
                payload={"message_id": "weixin:42", "text": "今天考试"},
                idempotency_key="weixin:42",
            )

            with EventStore(db) as store:
                self.assertTrue(store.publish(event))
                self.assertFalse(store.publish(event))

            with EventStore(db) as store:
                pending = store.pending_events(limit=10)

            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].event_id, event.event_id)
            self.assertEqual(pending[0].timestamp, event.timestamp)
            self.assertEqual(pending[0].payload["text"], "今天考试")

    def test_failed_publish_rolls_back_transaction_and_cursor(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.sqlite3"
        ) as store:
            invalid = CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED",
                user_id="primary-user",
                timestamp=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc),
                source="test",
                payload={"not_json": object()},
                idempotency_key="invalid",
            )
            with self.assertRaises(TypeError):
                store.publish_and_advance(invalid, "reader", 10)

            self.assertEqual(store.get_cursor("reader"), 0)
            self.assertEqual(store.pending_events(), [])
            self.assertTrue(
                store.publish_and_advance(
                    CompanionEvent(
                        event_type="USER_MESSAGE_RECEIVED",
                        user_id="primary-user",
                        timestamp=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc),
                        source="test",
                        payload={},
                    ),
                    "reader",
                    11,
                )
            )

    def test_processed_event_can_redact_raw_message_payload(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.sqlite3"
        ) as store:
            event = CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED",
                user_id="primary-user",
                timestamp=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc),
                source="hermes-sessiondb:weixin",
                payload={"text": "私密消息"},
            )
            store.publish(event)

            self.assertTrue(store.mark_processed(event.event_id, redact_payload=True))
            row = store._db.execute(
                "SELECT status, payload_json FROM event_outbox WHERE event_id=?",
                (event.event_id,),
            ).fetchone()

            self.assertEqual(row["status"], "processed")
            self.assertEqual(row["payload_json"], "{}")

    def test_claim_is_exclusive_and_expired_claim_can_be_recovered(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.sqlite3"
        ) as store:
            now = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
            event = CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED",
                user_id="primary-user",
                timestamp=now,
                source="test",
                payload={"text": "x"},
            )
            store.publish(event)

            claimed = store.claim_events("worker-a", now=now, lease_seconds=30)
            second_claim = store.claim_events("worker-b", now=now, lease_seconds=30)
            recovered = store.claim_events(
                "worker-b", now=now + timedelta(seconds=31), lease_seconds=30
            )

            self.assertEqual([row.event_id for row in claimed], [event.event_id])
            self.assertEqual(second_claim, [])
            self.assertEqual([row.event_id for row in recovered], [event.event_id])
            record = store._db.execute(
                "SELECT attempt_count, claim_owner FROM event_outbox WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            self.assertEqual(record["attempt_count"], 2)
            self.assertEqual(record["claim_owner"], "worker-b")

    def test_failed_claim_uses_bounded_retry_and_becomes_terminal(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.sqlite3"
        ) as store:
            now = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
            event = CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED",
                user_id="primary-user",
                timestamp=now,
                source="test",
                payload={"text": "x"},
            )
            store.publish(event)
            for attempt in range(1, 4):
                claimed = store.claim_events(
                    "worker", now=now + timedelta(days=attempt), lease_seconds=30
                )
                self.assertEqual(len(claimed), 1)
                terminal = store.fail_claim(
                    event.event_id,
                    owner="worker",
                    error_code="TimeoutError",
                    now=now + timedelta(days=attempt),
                    max_attempts=3,
                    retry_delay_seconds=60,
                )
            self.assertTrue(terminal)
            self.assertEqual(store.claim_events("worker", now=now + timedelta(days=4)), [])
            status = store._db.execute(
                "SELECT status, last_error_code FROM event_outbox WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["last_error_code"], "TimeoutError")

    def test_deferred_claim_with_non_utc_deadline_is_claimable_at_its_due_time(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.sqlite3"
        ) as store:
            now_utc = datetime(2026, 10, 17, 11, 0, tzinfo=timezone.utc)
            event = CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED",
                user_id="primary-user",
                timestamp=now_utc,
                source="test",
                payload={"text": "x"},
            )
            store.publish(event)
            claimed = store.claim_events("worker", now=now_utc)
            retry_local = datetime(
                2026, 10, 17, 19, 1, tzinfo=ZoneInfo("Asia/Shanghai")
            )
            self.assertEqual(len(claimed), 1)
            self.assertTrue(
                store.defer_claim(
                    event.event_id,
                    owner="worker",
                    retry_at=retry_local,
                    reason_code="BudgetExceeded",
                )
            )

            stored_deadline = store._db.execute(
                "SELECT next_attempt_at FROM event_outbox WHERE event_id=?",
                (event.event_id,),
            ).fetchone()[0]
            due = store.claim_events(
                "worker-2", now=datetime(2026, 10, 17, 11, 1, 1, tzinfo=timezone.utc)
            )

            self.assertEqual(stored_deadline, "2026-10-17T11:01:00+00:00")
            self.assertEqual([row.event_id for row in due], [event.event_id])

    def test_event_rejects_naive_timestamp(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED",
                user_id="primary-user",
                timestamp=datetime(2026, 10, 17, 8, 0),
                source="test",
                payload={},
            )


if __name__ == "__main__":
    unittest.main()
