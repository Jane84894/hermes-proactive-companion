import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_companion.backup import backup_database


class BackupTests(unittest.TestCase):
    def test_creates_private_consistent_backup_and_applies_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "companion.sqlite3"
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE values_table (value TEXT)")
                db.execute("INSERT INTO values_table VALUES ('kept')")
            backup_dir = root / "backups"
            first = backup_database(
                source, backup_dir, now=datetime(2026, 10, 1, tzinfo=timezone.utc), keep=1
            )
            second = backup_database(
                source, backup_dir, now=datetime(2026, 10, 2, tzinfo=timezone.utc), keep=1
            )
            self.assertTrue(first.exists() is False)
            self.assertEqual(os.stat(second).st_mode & 0o777, 0o600)
            with sqlite3.connect(second) as db:
                self.assertEqual(db.execute("SELECT value FROM values_table").fetchone()[0], "kept")


if __name__ == "__main__":
    unittest.main()
