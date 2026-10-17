"""Structured memory persistence and keyword retrieval."""

from __future__ import annotations

import json
from datetime import datetime, timezone
import re
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from .store import EventStore

_KINDS = {"profile", "fact", "preference", "role"}


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    user_id: str
    kind: str
    content: str
    source_message_id: str | None
    confidence: float
    importance: float
    status: str
    created_at: str


class MemoryRepository:
    def __init__(self, store: EventStore):
        self.store = store
        self._db = store._db

    def remember(
        self,
        *,
        user_id: str,
        kind: str,
        content: str,
        source_message_id: str | None,
        confidence: float,
        importance: float,
    ) -> tuple[MemoryRecord, bool]:
        content = content.strip()
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        if kind not in _KINDS:
            raise ValueError(f"unsupported memory kind: {kind}")
        if not content:
            raise ValueError("content must not be empty")
        for name, score in (("confidence", confidence), ("importance", importance)):
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")

        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (user_id,))
            existing = self._find_duplicate(user_id, kind, content)
            if existing is not None:
                self._db.execute(
                    "UPDATE memories SET confidence=MAX(confidence, ?), "
                    "importance=MAX(importance, ?), updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=?",
                    (confidence, importance, existing["id"]),
                )
                record = self._get(str(existing["id"]))
                self._db.commit()
                return record, False

            memory_id = str(uuid4())
            self._db.execute(
                """
                INSERT INTO memories (
                    id, user_id, kind, content, source_message_id,
                    confidence, importance
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    user_id,
                    kind,
                    content,
                    source_message_id,
                    confidence,
                    importance,
                ),
            )
            self._record_operation(user_id, "created", {"memory_id": memory_id})
            record = self._get(memory_id)
            self._db.commit()
            return record, True
        except Exception:
            self._db.rollback()
            raise

    def search(self, user_id: str, query: str, limit: int = 10) -> list[MemoryRecord]:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self._db.execute(
            """
            SELECT * FROM memories
            WHERE user_id=? AND status='active' AND content LIKE ?
            ORDER BY importance * confidence DESC, updated_at DESC
            LIMIT ?
            """,
            (user_id, f"%{query}%", limit),
        ).fetchall()
        return [self._record(row) for row in rows]

    def search_records(
        self,
        user_id: str,
        *,
        query: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: int = 10,
    ) -> dict[str, list[dict[str, object]]]:
        """Search active memories and associated episodes, events, and topics."""
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        if limit < 1:
            raise ValueError("limit must be positive")
        query = query.strip() if query is not None else None
        if query == "":
            query = None
        start = self._parse_range_bound(start_at, "start_at")
        end = self._parse_range_bound(end_at, "end_at")
        if query is None and start is None and end is None:
            raise ValueError("provide a query or time range")
        if start is not None and end is not None and start >= end:
            raise ValueError("start_at must be before end_at")

        specs = {
            "memories": (
                "SELECT id, kind, content, confidence, importance, created_at "
                "FROM memories WHERE status='active'",
                ("kind", "content"), "created_at", "importance DESC, confidence DESC, created_at DESC",
            ),
            "episodes": (
                "SELECT id, summary, occurred_at, confidence, importance FROM episodes WHERE 1=1",
                ("summary",), "occurred_at", "occurred_at DESC",
            ),
            "events": (
                "SELECT id, event_type, title, details, occurred_at, remind_at, status "
                "FROM followup_events WHERE 1=1",
                ("event_type", "title", "details"),
                "COALESCE(remind_at, occurred_at, created_at)", "COALESCE(remind_at, occurred_at, created_at)",
            ),
            "topics": (
                "SELECT id, title, current_state, interest_score, status, last_active_at "
                "FROM topics WHERE 1=1",
                ("title", "current_state"), "last_active_at", "interest_score DESC, last_active_at DESC",
            ),
        }
        result: dict[str, list[dict[str, object]]] = {}
        term = None
        if query is not None:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            term = f"%{escaped}%"
        for name, (base_sql, search_fields, time_field, order_by) in specs.items():
            clauses = ["user_id=?"]
            parameters: list[object] = [user_id]
            if name == "memories":
                clauses.append("status='active'")
            if term is not None:
                clauses.append("(" + " OR ".join(
                    f"{field} LIKE ? ESCAPE '\\'" for field in search_fields
                ) + ")")
                parameters.extend([term] * len(search_fields))
            if start is not None:
                clauses.append(f"julianday({time_field}) >= julianday(?)")
                parameters.append(start.isoformat())
            if end is not None:
                clauses.append(f"julianday({time_field}) < julianday(?)")
                parameters.append(end.isoformat())
            sql = f"{base_sql} AND " + " AND ".join(clauses) + f" ORDER BY {order_by} LIMIT ?"
            parameters.append(limit)
            rows = self._db.execute(sql, parameters).fetchall()
            result[name] = [dict(row) for row in rows]
        return result

    def related_memories(
        self, user_id: str, memory_id: str, limit: int = 10
    ) -> list[dict[str, object]]:
        if not user_id.strip() or not memory_id.strip():
            raise ValueError("user_id and memory_id must not be empty")
        if limit < 1:
            raise ValueError("limit must be positive")
        base = self._db.execute(
            "SELECT source_message_id FROM memories WHERE id=? AND user_id=? AND status='active'",
            (memory_id, user_id),
        ).fetchone()
        if base is None:
            return []
        rows = self._db.execute(
            "SELECT DISTINCT id, kind, content, confidence, importance, created_at, relation FROM ("
            "SELECT m.id, m.kind, m.content, m.confidence, m.importance, m.created_at, "
            "'same_source' AS relation FROM memories m WHERE m.user_id=? AND m.status='active' "
            "AND m.source_message_id=? AND m.id<>? "
            "UNION ALL SELECT m.id, m.kind, m.content, m.confidence, m.importance, m.created_at, "
            "l.relation FROM memory_links l JOIN memories m ON m.id=l.target_memory_id "
            "WHERE l.source_memory_id=? AND m.user_id=? AND m.status='active' "
            "UNION ALL SELECT m.id, m.kind, m.content, m.confidence, m.importance, m.created_at, "
            "l.relation FROM memory_links l JOIN memories m ON m.id=l.source_memory_id "
            "WHERE l.target_memory_id=? AND m.user_id=? AND m.status='active'"
            ") ORDER BY importance DESC, confidence DESC, created_at DESC LIMIT ?",
            (user_id, base["source_message_id"], memory_id, memory_id, user_id,
             memory_id, user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _parse_range_bound(value: str | None, name: str) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an ISO-8601 datetime") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{name} must include a timezone")
        return parsed

    def list_active(self, user_id: str, limit: int = 100) -> list[MemoryRecord]:
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self._db.execute(
            "SELECT * FROM memories WHERE user_id=? AND status='active' "
            "ORDER BY importance DESC, confidence DESC, updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [self._record(row) for row in rows]

    def export_data(self, user_id: str) -> dict[str, object]:
        """Export structured companion records, excluding raw inbox and credentials."""
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        result: dict[str, object] = {
            "format_version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
        }
        for table in (
            "memories", "episodes", "followup_events", "topics", "relationships",
            "interaction_preferences", "companion_states", "memory_operations",
            "topic_sources", "discoveries", "preference_evidence", "thought_queue",
        ):
            rows = self._db.execute(
                f"SELECT * FROM {table} WHERE user_id=? ORDER BY rowid", (user_id,)
            ).fetchall()
            result[table] = [dict(row) for row in rows]
        result["memory_links"] = [
            dict(row) for row in self._db.execute(
                "SELECT l.* FROM memory_links l "
                "JOIN memories m ON m.id=l.source_memory_id "
                "WHERE m.user_id=? ORDER BY l.rowid", (user_id,)
            ).fetchall()
        ]
        # The deployment is single-user; decisions contain only candidate metadata and drafts.
        result["social_decisions"] = [
            dict(row) for row in self._db.execute(
                "SELECT evaluated_at, candidate_id, candidate_kind, selected, score, "
                "reason_codes_json, components_json, candidate_title, draft_status, "
                "draft_text, draft_basis_json FROM social_decisions ORDER BY id"
            ).fetchall()
        ]
        result["proactive_actions"] = [
            dict(row) for row in self._db.execute(
                "SELECT id, idempotency_key, candidate_id, intent, state, payload_json, "
                "draft_text, attempt_count, delivery_receipt, delivered_at, responded_at, "
                "created_at, updated_at "
                "FROM proactive_actions ORDER BY created_at, id"
            ).fetchall()
        ]
        return result

    def delete(self, user_id: str, memory_id: str) -> bool:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            source_row = self._db.execute(
                "SELECT source_message_id FROM memories WHERE id=? AND user_id=? AND status='active'",
                (memory_id, user_id),
            ).fetchone()
            cursor = self._db.execute(
                "UPDATE memories SET status='deleted', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND user_id=? AND status='active'",
                (memory_id, user_id),
            )
            if cursor.rowcount:
                source_message_id = source_row[0] if source_row else None
                self._cancel_thoughts_for_memory(user_id, memory_id, source_message_id)
                if source_message_id:
                    impacted_topics = [row[0] for row in self._db.execute(
                        "SELECT topic_id FROM topic_sources WHERE user_id=? AND source_message_id=?",
                        (user_id, source_message_id),
                    ).fetchall()]
                    self._db.execute(
                        "DELETE FROM episodes WHERE user_id=? AND source_message_id=?",
                        (user_id, source_message_id),
                    )
                    self._db.execute(
                        "UPDATE followup_events SET status='cancelled', updated_at=CURRENT_TIMESTAMP "
                        "WHERE user_id=? AND source_message_id=? AND status IN ('pending','scheduled')",
                        (user_id, source_message_id),
                    )
                    self._db.execute(
                        "DELETE FROM topic_sources WHERE user_id=? AND source_message_id=?",
                        (user_id, source_message_id),
                    )
                    preference_rows = self._db.execute(
                        "SELECT DISTINCT category FROM preference_evidence "
                        "WHERE user_id=? AND source_message_id=?",
                        (user_id, source_message_id),
                    ).fetchall()
                    self._db.execute(
                        "DELETE FROM preference_evidence WHERE user_id=? AND source_message_id=?",
                        (user_id, source_message_id),
                    )
                    for preference_row in preference_rows:
                        category = str(preference_row["category"])
                        latest = self._db.execute(
                            "SELECT value, COUNT(*) OVER() AS evidence_count FROM preference_evidence "
                            "WHERE user_id=? AND category=? ORDER BY created_at DESC LIMIT 1",
                            (user_id, category),
                        ).fetchone()
                        if latest is None:
                            self._db.execute(
                                "DELETE FROM interaction_preferences WHERE user_id=? AND category=?",
                                (user_id, category),
                            )
                        else:
                            self._db.execute(
                                "UPDATE interaction_preferences SET value=?, evidence_count=?, "
                                "updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND category=?",
                                (latest["value"], latest["evidence_count"], user_id, category),
                            )
                    for topic_id in impacted_topics:
                        remaining = self._db.execute(
                            "SELECT 1 FROM topic_sources WHERE topic_id=? LIMIT 1", (topic_id,)
                        ).fetchone()
                        if remaining is None:
                            self._db.execute(
                                "UPDATE topics SET status='closed', followup_allowed=0 "
                                "WHERE id=? AND user_id=?",
                                (topic_id, user_id),
                            )
                self._db.execute(
                    "DELETE FROM memory_links WHERE source_memory_id=? OR target_memory_id=?",
                    (memory_id, memory_id),
                )
                for decision in self._db.execute(
                    "SELECT candidate_id, draft_basis_json FROM social_decisions "
                    "WHERE draft_text IS NOT NULL"
                ).fetchall():
                    try:
                        basis_ids = json.loads(decision["draft_basis_json"])
                    except (TypeError, json.JSONDecodeError):
                        basis_ids = []
                    if memory_id not in basis_ids:
                        continue
                    candidate_id = str(decision["candidate_id"])
                    self._db.execute(
                        "UPDATE social_decisions SET draft_status='failed', draft_text=NULL, "
                        "draft_basis_json='[]' WHERE candidate_id=?",
                        (candidate_id,),
                    )
                    self._db.execute(
                        "UPDATE proactive_actions SET state='cancelled', draft_text=NULL, "
                        "updated_at=CURRENT_TIMESTAMP WHERE candidate_id=? "
                        "AND state IN ('candidate','draft_ready','approved','ready','generation_failed','send_failed')",
                        (candidate_id,),
                    )
                self._record_operation(user_id, "deleted", {"memory_id": memory_id})
            self._db.commit()
            if cursor.rowcount:
                from .companion_state import CompanionStateRepository

                CompanionStateRepository(self.store).refresh(user_id)
            return cursor.rowcount == 1
        except Exception:
            self._db.rollback()
            raise

    def correct(self, user_id: str, memory_id: str, content: str) -> MemoryRecord | None:
        """Supersede one active memory and atomically store its correction."""
        content = content.strip()
        if not user_id.strip() or not memory_id.strip():
            raise ValueError("user_id and memory_id must not be empty")
        if not content or len(content) > 600:
            raise ValueError("corrected memory must contain 1 to 600 characters")

        self._db.execute("BEGIN IMMEDIATE")
        try:
            old = self._db.execute(
                "SELECT * FROM memories WHERE id=? AND user_id=? AND status='active'",
                (memory_id, user_id),
            ).fetchone()
            if old is None:
                # Make retrying the same correction command safe.
                operation = self._db.execute(
                    "SELECT details_json FROM memory_operations WHERE user_id=? "
                    "AND operation='corrected' ORDER BY rowid DESC",
                    (user_id,),
                ).fetchall()
                for row in operation:
                    try:
                        details = json.loads(row["details_json"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if details.get("old_memory_id") == memory_id:
                        corrected = self._db.execute(
                            "SELECT * FROM memories WHERE id=? AND user_id=? AND status='active'",
                            (details.get("new_memory_id"), user_id),
                        ).fetchone()
                        if corrected is not None:
                            self._db.commit()
                            return self._record(corrected)
                self._db.commit()
                return None

            if self._normalize(old["content"]) == self._normalize(content):
                result = self._record(old)
                self._db.commit()
                return result

            self._db.execute(
                "UPDATE memories SET status='superseded', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND user_id=? AND status='active'",
                (memory_id, user_id),
            )
            duplicate = self._find_duplicate(user_id, old["kind"], content)
            if duplicate is None:
                corrected_id = str(uuid4())
                self._db.execute(
                    "INSERT INTO memories (id, user_id, kind, content, source_message_id, "
                    "confidence, importance) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                    (corrected_id, user_id, old["kind"], content,
                     old["confidence"], old["importance"]),
                )
            else:
                corrected_id = str(duplicate["id"])

            source_message_id = old["source_message_id"]
            self._cancel_thoughts_for_memory(user_id, memory_id, source_message_id)
            if source_message_id:
                impacted_topics = [row[0] for row in self._db.execute(
                    "SELECT topic_id FROM topic_sources WHERE user_id=? AND source_message_id=?",
                    (user_id, source_message_id),
                ).fetchall()]
                self._db.execute(
                    "DELETE FROM episodes WHERE user_id=? AND source_message_id=?",
                    (user_id, source_message_id),
                )
                self._db.execute(
                    "UPDATE followup_events SET status='cancelled', updated_at=CURRENT_TIMESTAMP "
                    "WHERE user_id=? AND source_message_id=? AND status IN ('pending','scheduled')",
                    (user_id, source_message_id),
                )
                self._db.execute(
                    "DELETE FROM topic_sources WHERE user_id=? AND source_message_id=?",
                    (user_id, source_message_id),
                )
                preference_rows = self._db.execute(
                    "SELECT DISTINCT category FROM preference_evidence "
                    "WHERE user_id=? AND source_message_id=?", (user_id, source_message_id),
                ).fetchall()
                self._db.execute(
                    "DELETE FROM preference_evidence WHERE user_id=? AND source_message_id=?",
                    (user_id, source_message_id),
                )
                for preference_row in preference_rows:
                    category = str(preference_row["category"])
                    latest = self._db.execute(
                        "SELECT value, COUNT(*) OVER() AS evidence_count FROM preference_evidence "
                        "WHERE user_id=? AND category=? ORDER BY created_at DESC LIMIT 1",
                        (user_id, category),
                    ).fetchone()
                    if latest is None:
                        self._db.execute(
                            "DELETE FROM interaction_preferences WHERE user_id=? AND category=?",
                            (user_id, category),
                        )
                    else:
                        self._db.execute(
                            "UPDATE interaction_preferences SET value=?, evidence_count=?, "
                            "updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND category=?",
                            (latest["value"], latest["evidence_count"], user_id, category),
                        )
                for topic_id in impacted_topics:
                    remaining = self._db.execute(
                        "SELECT 1 FROM topic_sources WHERE topic_id=? LIMIT 1", (topic_id,)
                    ).fetchone()
                    if remaining is None:
                        self._db.execute(
                            "UPDATE topics SET status='closed', followup_allowed=0 "
                            "WHERE id=? AND user_id=?", (topic_id, user_id),
                        )

            self._db.execute(
                "DELETE FROM memory_links WHERE source_memory_id=? OR target_memory_id=?",
                (memory_id, memory_id),
            )
            for decision in self._db.execute(
                "SELECT candidate_id, draft_basis_json FROM social_decisions "
                "WHERE draft_text IS NOT NULL"
            ).fetchall():
                try:
                    basis_ids = json.loads(decision["draft_basis_json"])
                except (TypeError, json.JSONDecodeError):
                    basis_ids = []
                if memory_id not in basis_ids:
                    continue
                candidate_id = str(decision["candidate_id"])
                self._db.execute(
                    "UPDATE social_decisions SET draft_status='failed', draft_text=NULL, "
                    "draft_basis_json='[]' WHERE candidate_id=?", (candidate_id,),
                )
                self._db.execute(
                    "UPDATE proactive_actions SET state='cancelled', draft_text=NULL, "
                    "updated_at=CURRENT_TIMESTAMP WHERE candidate_id=? "
                    "AND state IN ('candidate','draft_ready','approved','ready','generation_failed','send_failed')",
                    (candidate_id,),
                )
            self._record_operation(user_id, "corrected", {
                "old_memory_id": memory_id, "new_memory_id": corrected_id,
            })
            self._db.commit()
            from .companion_state import CompanionStateRepository

            CompanionStateRepository(self.store).refresh(user_id)
            return self._get(corrected_id)
        except Exception:
            self._db.rollback()
            raise

    def count_active(self, user_id: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM memories WHERE user_id=? AND status='active'",
            (user_id,),
        ).fetchone()
        return int(row[0])

    def _find_duplicate(self, user_id: str, kind: str, content: str):
        normalized = self._normalize(content)
        rows = self._db.execute(
            "SELECT * FROM memories WHERE user_id=? AND kind=? AND status='active'",
            (user_id, kind),
        ).fetchall()
        return next(
            (row for row in rows if self._normalize(row["content"]) == normalized),
            None,
        )

    @staticmethod
    def _normalize(content: str) -> str:
        return re.sub(r"\s+", " ", content).strip().casefold()

    def _record_operation(self, user_id: str, operation: str, details: dict) -> None:
        self._db.execute(
            "INSERT INTO memory_operations(id, user_id, operation, details_json) "
            "VALUES (?, ?, ?, ?)",
            (str(uuid4()), user_id, operation, json.dumps(details, ensure_ascii=False)),
        )

    def _cancel_thoughts_for_memory(
        self, user_id: str, memory_id: str, source_message_id: str | None
    ) -> None:
        source_keys: set[str] = set()
        if source_message_id:
            event_ids = self._db.execute(
                "SELECT id FROM followup_events WHERE user_id=? AND source_message_id=?",
                (user_id, source_message_id),
            ).fetchall()
            source_keys.update(f"event:{row['id']}" for row in event_ids)
            topic_ids = self._db.execute(
                "SELECT topic_id FROM topic_sources WHERE user_id=? AND source_message_id=?",
                (user_id, source_message_id),
            ).fetchall()
            source_keys.update(f"topic:{row['topic_id']}" for row in topic_ids)
            if topic_ids:
                marks = ",".join("?" for _ in topic_ids)
                discovery_rows = self._db.execute(
                    f"SELECT id FROM discoveries WHERE topic_id IN ({marks})",
                    tuple(row["topic_id"] for row in topic_ids),
                ).fetchall()
                source_keys.update(f"discovery:{row['id']}" for row in discovery_rows)

        rows = self._db.execute(
            "SELECT id, source_key, related_memory_ids_json FROM thought_queue "
            "WHERE user_id=? AND status IN ('candidate','pending','ready','selected')",
            (user_id,),
        ).fetchall()
        for row in rows:
            try:
                memory_ids = json.loads(row["related_memory_ids_json"])
            except (TypeError, json.JSONDecodeError):
                memory_ids = []
            if row["source_key"] not in source_keys and memory_id not in memory_ids:
                continue
            self._db.execute(
                "UPDATE thought_queue SET status='cancelled', updated_at=CURRENT_TIMESTAMP "
                "WHERE user_id=? AND id=?",
                (user_id, row["id"]),
            )
            self._db.execute(
                "UPDATE social_decisions SET draft_status='failed', draft_text=NULL, "
                "draft_basis_json='[]' WHERE candidate_id=?",
                (row["source_key"],),
            )
            self._db.execute(
                "UPDATE proactive_actions SET state='cancelled', draft_text=NULL, "
                "updated_at=CURRENT_TIMESTAMP WHERE candidate_id=? "
                "AND state IN ('candidate','draft_ready','approved','ready','generation_failed','send_failed')",
                (row["source_key"],),
            )

    def _get(self, memory_id: str) -> MemoryRecord:
        row = self._db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._record(row)

    @staticmethod
    def _record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            user_id=row["user_id"],
            kind=row["kind"],
            content=row["content"],
            source_message_id=row["source_message_id"],
            confidence=float(row["confidence"]),
            importance=float(row["importance"]),
            status=row["status"],
            created_at=row["created_at"],
        )
