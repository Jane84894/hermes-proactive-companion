"""Idempotent persistence for validated extraction candidates."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .extraction import ExtractionResult
from .memory import MemoryRepository
from .store import EventStore


@dataclass(frozen=True, slots=True)
class PersistReport:
    created: dict[str, int]


class ExtractionStore:
    def __init__(self, store: EventStore):
        self._store = store
        self._db = store._db
        self._memories = MemoryRepository(store)

    def persist(
        self,
        *,
        user_id: str,
        source_message_id: str,
        source: str,
        source_timestamp: datetime,
        extraction: ExtractionResult,
    ) -> PersistReport:
        if source_timestamp.tzinfo is None or source_timestamp.utcoffset() is None:
            raise ValueError("source_timestamp must be timezone-aware")
        counts = {"memories": 0, "episodes": 0, "events": 0, "topics": 0}
        source_stamp = source_timestamp.astimezone(timezone.utc).isoformat()
        for item in extraction.memories:
            _, created = self._memories.remember(
                user_id=user_id,
                kind=item.kind,
                content=item.content,
                source_message_id=source_message_id,
                confidence=item.confidence,
                importance=item.importance,
            )
            counts["memories"] += int(created)

        for item in extraction.episodes:
            occurred_at = (item.occurred_at or source_timestamp).isoformat()
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO episodes (
                    id, user_id, source_message_id, occurred_at, summary, source,
                    confidence, importance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), user_id, source_message_id, occurred_at,
                    item.summary, source, item.confidence, item.importance,
                ),
            )
            self._db.commit()
            counts["episodes"] += int(cursor.rowcount == 1)

        for item in extraction.events:
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO followup_events (
                    id, user_id, source_message_id, event_type, title, details,
                    occurred_at, remind_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    str(uuid4()), user_id, source_message_id, item.event_type,
                    item.title, item.details,
                    item.occurred_at.isoformat() if item.occurred_at else None,
                    item.remind_at.isoformat() if item.remind_at else None,
                ),
            )
            self._db.commit()
            counts["events"] += int(cursor.rowcount == 1)

        for item in extraction.topics:
            existing = self._find_topic(user_id, item.title)
            if existing is None:
                topic_id = str(uuid4())
                self._db.execute(
                    """
                    INSERT INTO topics (
                        id, user_id, title, current_state, interest_score,
                        followup_allowed, created_at, last_active_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        topic_id, user_id, item.title, item.current_state,
                        item.interest_score, int(item.followup_allowed), source_stamp, source_stamp,
                    ),
                )
                counts["topics"] += 1
            else:
                topic_id = str(existing["id"])
                existing_active = datetime.fromisoformat(str(existing["last_active_at"]))
                if existing_active.tzinfo is None or existing_active.utcoffset() is None:
                    existing_active = existing_active.replace(tzinfo=timezone.utc)
                source_active = source_timestamp.astimezone(timezone.utc)
                evidence_is_current = source_active >= existing_active.astimezone(timezone.utc)
                active_stamp = max(
                    existing_active.astimezone(timezone.utc),
                    source_active,
                ).isoformat()
                self._db.execute(
                    """
                    UPDATE topics SET
                        current_state=CASE WHEN ? AND ?<>'' THEN ? ELSE current_state END,
                        interest_score=CASE WHEN ? THEN ? ELSE interest_score END,
                        followup_allowed=CASE WHEN ? THEN ? ELSE followup_allowed END,
                        last_active_at=?
                    WHERE id=?
                    """,
                    (
                        int(evidence_is_current), item.current_state, item.current_state,
                        int(evidence_is_current), item.interest_score,
                        int(evidence_is_current), int(item.followup_allowed),
                        active_stamp, existing["id"],
                    ),
                )
            self._db.execute(
                "INSERT OR IGNORE INTO topic_sources(topic_id, user_id, source_message_id) "
                "VALUES (?, ?, ?)",
                (topic_id, user_id, source_message_id),
            )
            self._db.commit()

        return PersistReport(created=counts)

    def _find_topic(self, user_id: str, title: str) -> sqlite3.Row | None:
        rows = self._db.execute(
            "SELECT * FROM topics WHERE user_id=? AND status='open'",
            (user_id,),
        ).fetchall()
        normalized = " ".join(title.casefold().split())
        return next(
            (
                row for row in rows
                if " ".join(str(row["title"]).casefold().split()) == normalized
            ),
            None,
        )
