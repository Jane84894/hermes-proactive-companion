import sqlite3
import tempfile
import unittest
from pathlib import Path

from hermes_companion.hermes_sessiondb import HermesSessionReader


class HermesSessionReaderTests(unittest.TestCase):
    def test_reads_only_allowlisted_weixin_user_and_assistant_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, source TEXT, user_id TEXT, chat_id TEXT
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, timestamp REAL, platform_message_id TEXT
                );
                INSERT INTO sessions VALUES ('s1', 'weixin', 'u1', 'u1');
                INSERT INTO sessions VALUES ('s2', 'weixin', 'u2', 'u2');
                INSERT INTO sessions VALUES ('s3', 'telegram', 'u1', 'u1');
                INSERT INTO messages VALUES (10, 's1', 'user', '考试在周五', 1781647200, 'wx-10');
                INSERT INTO messages VALUES (11, 's1', 'assistant', '记下了', 1781647201, 'wx-11');
                INSERT INTO messages VALUES (14, 's1', 'tool', '不得读取', 1781647204, 'wx-14');
                INSERT INTO messages VALUES (12, 's2', 'user', '不应读取', 1781647202, 'wx-12');
                INSERT INTO messages VALUES (13, 's3', 'user', '不应读取', 1781647203, 'tg-13');
                """
            )
            conn.commit()
            conn.close()

            messages = HermesSessionReader(db, target_user_id="u1").read_after(0)

            self.assertEqual([item.row_id for item in messages], [10, 11])
            self.assertEqual(messages[0].platform_message_id, "wx-10")
            self.assertEqual(messages[0].role, "user")
            self.assertEqual(messages[0].text, "考试在周五")
            self.assertEqual(messages[0].timestamp, 1781647200)
            self.assertEqual(messages[1].role, "assistant")

    def test_reader_uses_sqlite_read_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE sessions (id TEXT, source TEXT, user_id TEXT, chat_id TEXT);"
                "CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT, timestamp REAL, platform_message_id TEXT);"
            )
            conn.commit()
            conn.close()

            reader = HermesSessionReader(db, target_user_id="u1")
            with self.assertRaises(sqlite3.OperationalError):
                reader._connect().execute("CREATE TABLE must_not_exist (id INTEGER)")


if __name__ == "__main__":
    unittest.main()
