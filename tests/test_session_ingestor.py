import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_companion.hermes_sessiondb import HermesSessionReader
from hermes_companion.controls import CompanionControls
from hermes_companion.ingest import SessionDbIngestor
from hermes_companion.store import EventStore


class SessionDbIngestorTests(unittest.TestCase):
    def test_poll_is_idempotent_and_commits_event_with_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "hermes.db"
            conn = sqlite3.connect(source)
            conn.executescript(
                """
                CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT, chat_id TEXT);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                    timestamp REAL, platform_message_id TEXT
                );
                INSERT INTO sessions VALUES ('s1', 'weixin', 'u1', 'u1');
                INSERT INTO messages VALUES (10, 's1', 'user', '周五考试', 1781647200, 'wx-10');
                INSERT INTO messages VALUES (11, 's1', 'assistant', '这段回复只用于事件跟踪', 1781647201, 'wx-11');
                """
            )
            conn.commit()
            conn.close()

            with EventStore(Path(tmp) / "companion.db") as store:
                ingestor = SessionDbIngestor(
                    HermesSessionReader(source, target_user_id="u1"), store
                )
                self.assertEqual(ingestor.poll_once(), 2)
                self.assertEqual(ingestor.poll_once(), 0)
                self.assertEqual(store.get_cursor("hermes-weixin"), 11)
                pending = store.pending_events()

            self.assertEqual(len(pending), 2)
            by_type = {item.event_type: item for item in pending}
            self.assertEqual(by_type["USER_MESSAGE_RECEIVED"].timestamp.timestamp(), 1781647200)
            self.assertEqual(by_type["USER_MESSAGE_RECEIVED"].idempotency_key, "hermes-weixin-message:10")
            self.assertEqual(by_type["ASSISTANT_MESSAGE_SENT"].payload, {"platform_message_id": "wx-11"})

    def test_recent_control_message_is_applied_during_ingestion_without_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "hermes.db"
            timestamp = datetime.now(timezone.utc).timestamp()
            conn = sqlite3.connect(source)
            conn.executescript(
                """
                CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, user_id TEXT, chat_id TEXT);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                    timestamp REAL, platform_message_id TEXT
                );
                INSERT INTO sessions VALUES ('s1', 'weixin', 'u1', 'u1');
                """
            )
            conn.execute(
                "INSERT INTO messages VALUES (11, 's1', 'user', ?, ?, 'wx-11')",
                ("先别主动联系我", timestamp),
            )
            conn.commit()
            conn.close()

            with EventStore(Path(tmp) / "companion.db") as store:
                controls = CompanionControls(store)
                ingestor = SessionDbIngestor(
                    HermesSessionReader(source, target_user_id="u1"), store,
                    controls=controls,
                )
                self.assertEqual(ingestor.poll_once(), 1)
                self.assertTrue(controls.state("primary-user")["proactive_paused"])


if __name__ == "__main__":
    unittest.main()
