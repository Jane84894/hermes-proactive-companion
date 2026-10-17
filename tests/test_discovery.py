import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_companion.discovery import DiscoveryWorker
from hermes_companion.store import EventStore


class DiscoveryWorkerTests(unittest.TestCase):
    def test_github_metadata_is_deduplicated_and_worker_is_rate_limited(self):
        now = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
        response = io.BytesIO(json.dumps({"items": [{
            "id": 42,
            "full_name": "example/esp32-display",
            "html_url": "https://github.com/example/esp32-display",
            "updated_at": "2026-10-16T10:00:00Z",
            "description": "untrusted description is discarded",
        }]}).encode())
        with tempfile.TemporaryDirectory() as tmp, EventStore(Path(tmp) / "db.sqlite3") as store:
            store._db.execute(
                "INSERT INTO users(id) VALUES ('primary-user')"
            )
            store._db.execute(
                "INSERT INTO topics (id, user_id, title, interest_score) "
                "VALUES ('topic-1','primary-user','ESP32 display',0.8)"
            )
            store._db.commit()
            worker = DiscoveryWorker(store)
            with patch("hermes_companion.discovery.urlopen", return_value=response) as request:
                first = worker.run_once(now=now)
                second = worker.run_once(now=now)

            self.assertEqual(first, {"state": "completed", "created": 1})
            self.assertEqual(second, {"state": "not_due", "created": 0})
            self.assertEqual(request.call_count, 1)
            row = store._db.execute("SELECT * FROM discoveries").fetchone()
            self.assertEqual(row["source"], "github")
            self.assertEqual(row["status"], "new")
            self.assertEqual(row["summary"], "")

    def test_feed_configuration_rejects_non_https_and_embedded_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(Path(tmp) / "db.sqlite3") as store:
            with self.assertRaises(ValueError):
                DiscoveryWorker(store, rss_feeds=("http://example.com/feed.xml",))
            with self.assertRaises(ValueError):
                DiscoveryWorker(store, rss_feeds=("https://user:secret@example.com/feed.xml",))


if __name__ == "__main__":
    unittest.main()
