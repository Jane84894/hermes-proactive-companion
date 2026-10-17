import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hermes_companion.extraction import (
    EpisodeCandidate,
    ExtractionResult,
    FollowupCandidate,
    MemoryCandidate,
    TopicCandidate,
)
from hermes_companion.extraction_store import ExtractionStore
from hermes_companion.store import EventStore


class ExtractionStoreTests(unittest.TestCase):
    def test_persist_is_idempotent_and_uses_source_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            store._db.execute("INSERT INTO users(id) VALUES ('primary-user')")
            store._db.commit()
            persistence = ExtractionStore(store)
            source_time = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
            result = ExtractionResult(
                memories=(MemoryCandidate("fact", "周五有高数考试", 0.95, 0.8),),
                episodes=(EpisodeCandidate("提到高数考试", source_time, 0.9, 0.7),),
                events=(
                    FollowupCandidate(
                        "exam",
                        "高数考试",
                        "下午考试",
                        datetime(2026, 10, 23, 14, tzinfo=timezone.utc),
                        datetime(2026, 10, 23, 17, tzinfo=timezone.utc),
                        0.9,
                    ),
                ),
                topics=(TopicCandidate("高数", "正在备考", 0.8, True),),
            )

            first = persistence.persist(
                user_id="primary-user",
                source_message_id="hermes-weixin-message:10",
                source="hermes-sessiondb:weixin",
                source_timestamp=source_time,
                extraction=result,
            )
            second = persistence.persist(
                user_id="primary-user",
                source_message_id="hermes-weixin-message:10",
                source="hermes-sessiondb:weixin",
                source_timestamp=source_time,
                extraction=result,
            )

            self.assertEqual(first.created, {"memories": 1, "episodes": 1, "events": 1, "topics": 1})
            self.assertEqual(second.created, {"memories": 0, "episodes": 0, "events": 0, "topics": 0})
            episode = store._db.execute("SELECT occurred_at FROM episodes").fetchone()
            self.assertEqual(datetime.fromisoformat(episode[0]), source_time)
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM followup_events").fetchone()[0], 1)
            self.assertEqual(store._db.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 1)

    def test_newer_topic_evidence_can_lower_interest_and_reenable_followup(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            store._db.execute("INSERT INTO users(id) VALUES ('primary-user')")
            store._db.commit()
            persistence = ExtractionStore(store)
            topic_table = lambda score, followup: ExtractionResult(
                (), (), (), (TopicCandidate("ESP32 像素屏", "项目进展", score, followup),)
            )
            persistence.persist(
                user_id="primary-user", source_message_id="old", source="weixin",
                source_timestamp=datetime(2026, 10, 16, 10, tzinfo=timezone.utc),
                extraction=topic_table(0.95, False),
            )
            persistence.persist(
                user_id="primary-user", source_message_id="new", source="weixin",
                source_timestamp=datetime(2026, 10, 17, 10, tzinfo=timezone.utc),
                extraction=topic_table(0.25, True),
            )
            # A late retry of old evidence must not overwrite the newer state.
            persistence.persist(
                user_id="primary-user", source_message_id="old-retry", source="weixin",
                source_timestamp=datetime(2026, 10, 16, 10, tzinfo=timezone.utc),
                extraction=topic_table(1.0, False),
            )

            row = store._db.execute(
                "SELECT current_state, interest_score, followup_allowed, last_active_at FROM topics"
            ).fetchone()
            self.assertEqual(row["interest_score"], 0.25)
            self.assertEqual(row["followup_allowed"], 1)
            self.assertEqual(row["last_active_at"], "2026-10-17T10:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
