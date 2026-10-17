"""Bounded, persistent queue for explicit companion intentions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4

from .store import EventStore


class QueueLimitExceeded(RuntimeError):
    """Raised when enqueueing would exceed configured task budgets."""


_ACTIVE = ("candidate", "pending", "ready", "selected")
_THOUGHT_TYPES = {
    "event_follow_up", "topic_follow_up", "discovery_share", "research",
    "light_chat", "conversation_intent",
}
_TRANSITIONS = {
    "candidate": {"pending", "ready", "completed", "cancelled", "expired", "merged"},
    "pending": {"ready", "selected", "completed", "cancelled", "expired", "merged", "failed"},
    "ready": {"selected", "completed", "cancelled", "expired", "merged", "failed"},
    "selected": {"pending", "delivered", "completed", "cancelled", "expired", "failed"},
    "delivered": {"engaged", "ignored", "completed"},
    "engaged": {"completed"},
    "ignored": {"completed"},
    "failed": {"pending", "completed", "cancelled", "expired"},
    "completed": set(),
    "cancelled": set(),
    "expired": set(),
    "merged": set(),
}


@dataclass(frozen=True, slots=True)
class Thought:
    id: str
    user_id: str
    thought_type: str
    topic: str
    reason: str
    possible_action: str
    priority: float
    created_at: datetime
    earliest_send_at: datetime
    expires_at: datetime
    status: str
    attempt_count: int
    last_evaluated_at: datetime | None
    related_memory_ids: tuple[str, ...]
    source_key: str


class ThoughtQueueRepository:
    """Store bounded, deduplicated intentions without executing model tools."""

    def __init__(
        self,
        store: EventStore,
        *,
        max_queue_length: int = 100,
        max_tasks_per_day: int = 20,
        max_attempts: int = 3,
        timezone_name: str = "Asia/Shanghai",
    ):
        if min(max_queue_length, max_tasks_per_day, max_attempts) < 1:
            raise ValueError("thought queue limits must be positive")
        self._db = store._db
        self.max_queue_length = max_queue_length
        self.max_tasks_per_day = max_tasks_per_day
        self.max_attempts = max_attempts
        self.zone = ZoneInfo(timezone_name)

    def enqueue(
        self,
        *,
        user_id: str,
        thought_type: str,
        topic: str,
        reason: str,
        possible_action: str,
        priority: float,
        source_key: str,
        earliest_send_at: datetime,
        expires_at: datetime,
        related_memory_ids: tuple[str, ...] | list[str] = (),
        now: datetime | None = None,
    ) -> tuple[Thought, bool]:
        if not all(value.strip() for value in (user_id, thought_type, topic, source_key)):
            raise ValueError("user, thought type, topic, and source key are required")
        if thought_type not in _THOUGHT_TYPES:
            raise ValueError("unsupported thought type")
        if not 0.0 <= priority <= 1.0:
            raise ValueError("priority must be between 0 and 1")
        created = self._aware(now or datetime.now(timezone.utc), "now")
        earliest = self._aware(earliest_send_at, "earliest_send_at")
        expires = self._aware(expires_at, "expires_at")
        if expires <= created or expires <= earliest:
            raise ValueError("thought expiry must be after creation and earliest send time")
        ids = self._memory_ids(related_memory_ids)
        stamp = created.astimezone(timezone.utc).isoformat()
        source_key = source_key.strip()

        self._db.execute("BEGIN IMMEDIATE")
        try:
            existing = self._db.execute(
                "SELECT id FROM thought_queue WHERE user_id=? AND source_key=?",
                (user_id, source_key),
            ).fetchone()
            if existing:
                self._db.commit()
                return self.get(user_id, str(existing["id"])), False

            local_day = created.astimezone(self.zone).date()
            start = datetime.combine(local_day, datetime.min.time(), tzinfo=self.zone).astimezone(timezone.utc)
            end = datetime.combine(local_day.fromordinal(local_day.toordinal() + 1), datetime.min.time(), tzinfo=self.zone).astimezone(timezone.utc)
            active_marks = ",".join("?" for _ in _ACTIVE)
            active_count = int(self._db.execute(
                f"SELECT COUNT(*) FROM thought_queue WHERE user_id=? AND status IN ({active_marks})",
                (user_id, *_ACTIVE),
            ).fetchone()[0])
            daily_count = int(self._db.execute(
                "SELECT COUNT(*) FROM thought_queue WHERE user_id=? AND created_at>=? AND created_at<?",
                (user_id, start.isoformat(), end.isoformat()),
            ).fetchone()[0])
            if active_count >= self.max_queue_length:
                raise QueueLimitExceeded("thought queue is full")
            if daily_count >= self.max_tasks_per_day:
                raise QueueLimitExceeded("daily thought creation budget is exhausted")
            self._db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (user_id,))
            thought_id = str(uuid4())
            self._db.execute(
                "INSERT INTO thought_queue (id,user_id,thought_type,topic,reason,possible_action,"
                "priority,created_at,earliest_send_at,expires_at,status,related_memory_ids_json,"
                "source_key,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (thought_id, user_id, thought_type.strip(), topic.strip(), reason.strip(),
                 possible_action.strip(), priority, stamp, earliest.astimezone(timezone.utc).isoformat(),
                 expires.astimezone(timezone.utc).isoformat(), json.dumps(ids), source_key, stamp),
            )
            self._db.commit()
            return self.get(user_id, thought_id), True
        except Exception:
            self._db.rollback()
            raise

    def get(self, user_id: str, thought_id: str) -> Thought:
        row = self._db.execute(
            "SELECT * FROM thought_queue WHERE user_id=? AND id=?", (user_id, thought_id)
        ).fetchone()
        if row is None:
            raise KeyError(thought_id)
        return self._record(row)

    def get_by_source_key(self, user_id: str, source_key: str) -> Thought | None:
        row = self._db.execute(
            "SELECT * FROM thought_queue WHERE user_id=? AND source_key=?",
            (user_id, source_key),
        ).fetchone()
        return self._record(row) if row else None

    def list(
        self, user_id: str, *, statuses: tuple[str, ...] | list[str] | None = None,
        limit: int = 100,
    ) -> list[Thought]:
        if limit < 1 or limit > 1000:
            raise ValueError("thought list limit must be between 1 and 1000")
        if statuses:
            marks = ",".join("?" for _ in statuses)
            rows = self._db.execute(
                f"SELECT * FROM thought_queue WHERE user_id=? AND status IN ({marks}) "
                "ORDER BY priority DESC, earliest_send_at, created_at LIMIT ?",
                (user_id, *statuses, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM thought_queue WHERE user_id=? "
                "ORDER BY created_at DESC LIMIT ?", (user_id, limit)
            ).fetchall()
        return [self._record(row) for row in rows]

    def search(
        self, user_id: str, *, query: str = "", statuses: tuple[str, ...] | None = None,
        created_after: datetime | None = None, created_before: datetime | None = None,
        limit: int = 20,
    ) -> list[Thought]:
        if limit < 1 or limit > 1000:
            raise ValueError("thought search limit must be between 1 and 1000")
        clauses = ["user_id=?"]
        params: list[object] = [user_id]
        if query.strip():
            clauses.append("(topic LIKE ? OR reason LIKE ? OR possible_action LIKE ?)")
            pattern = f"%{query.strip()}%"
            params.extend((pattern, pattern, pattern))
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(statuses)
        if created_after:
            clauses.append("created_at>=?")
            params.append(self._aware(created_after, "created_after").astimezone(timezone.utc).isoformat())
        if created_before:
            clauses.append("created_at<?")
            params.append(self._aware(created_before, "created_before").astimezone(timezone.utc).isoformat())
        rows = self._db.execute(
            "SELECT * FROM thought_queue WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT ?", (*params, limit)
        ).fetchall()
        return [self._record(row) for row in rows]

    def update(self, user_id: str, thought_id: str, **changes: object) -> Thought:
        allowed = {"thought_type", "topic", "reason", "possible_action", "priority", "earliest_send_at", "expires_at", "related_memory_ids"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("unsupported thought fields: " + ", ".join(sorted(unknown)))
        current = self.get(user_id, thought_id)
        if current.status not in _ACTIVE:
            raise ValueError("only active thoughts can be updated")
        values: dict[str, object] = {}
        for key, value in changes.items():
            if key in {"thought_type", "topic"}:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{key} must not be empty")
                if key == "thought_type" and value not in _THOUGHT_TYPES:
                    raise ValueError("unsupported thought type")
                values[key] = value.strip()
            elif key in {"reason", "possible_action"}:
                if not isinstance(value, str):
                    raise ValueError(f"{key} must be text")
                values[key] = value.strip()
            elif key == "priority":
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
                    raise ValueError("priority must be between 0 and 1")
                values[key] = float(value)
            elif key in {"earliest_send_at", "expires_at"}:
                values[key] = self._aware(value, key).astimezone(timezone.utc).isoformat()
            elif key == "related_memory_ids":
                values["related_memory_ids_json"] = json.dumps(self._memory_ids(value))
        earliest = datetime.fromisoformat(str(values.get("earliest_send_at", current.earliest_send_at.isoformat())))
        expires = datetime.fromisoformat(str(values.get("expires_at", current.expires_at.isoformat())))
        if expires <= earliest:
            raise ValueError("thought expiry must be after earliest send time")
        if values:
            assignments = ",".join(f"{key}=?" for key in values)
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    f"UPDATE thought_queue SET {assignments}, updated_at=? WHERE user_id=? AND id=?",
                    (*values.values(), datetime.now(timezone.utc).isoformat(), user_id, thought_id),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return self.get(user_id, thought_id)

    def merge(self, user_id: str, target_id: str, source_ids: list[str]) -> Thought:
        ids = list(dict.fromkeys(source_ids))
        if target_id in ids:
            ids.remove(target_id)
        target = self.get(user_id, target_id)
        if target.status not in _ACTIVE:
            raise ValueError("merge target must be active")
        sources = [self.get(user_id, item_id) for item_id in ids]
        if not sources:
            return target
        if any(item.status not in _ACTIVE for item in sources):
            raise ValueError("merge sources must be active")
        memories = list(target.related_memory_ids)
        reasons = [target.reason] if target.reason else []
        actions = [target.possible_action] if target.possible_action else []
        earliest = target.earliest_send_at
        expiry = target.expires_at
        priority = target.priority
        for item in sources:
            memories.extend(item.related_memory_ids)
            if item.reason and item.reason not in reasons:
                reasons.append(item.reason)
            if item.possible_action and item.possible_action not in actions:
                actions.append(item.possible_action)
            earliest = min(earliest, item.earliest_send_at)
            expiry = max(expiry, item.expires_at)
            priority = max(priority, item.priority)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE thought_queue SET reason=?, possible_action=?, priority=?, earliest_send_at=?, "
                "expires_at=?, related_memory_ids_json=?, updated_at=? WHERE user_id=? AND id=?",
                ("; ".join(reasons), "; ".join(actions), priority, earliest.astimezone(timezone.utc).isoformat(),
                 expiry.astimezone(timezone.utc).isoformat(), json.dumps(self._memory_ids(memories)),
                 datetime.now(timezone.utc).isoformat(), user_id, target_id),
            )
            self._db.executemany(
                "UPDATE thought_queue SET status='merged', updated_at=? WHERE user_id=? AND id=?",
                [(datetime.now(timezone.utc).isoformat(), user_id, item.id) for item in sources],
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get(user_id, target_id)

    def delay(self, user_id: str, thought_id: str, earliest_send_at: datetime) -> Thought:
        item = self.get(user_id, thought_id)
        new_time = self._aware(earliest_send_at, "earliest_send_at")
        if item.status not in {"candidate", "pending", "ready"}:
            raise ValueError("only unselected thoughts can be delayed")
        if new_time <= item.earliest_send_at:
            raise ValueError("delay time must move later")
        if new_time >= item.expires_at:
            if not self.transition(user_id, thought_id, "expired"):
                raise ValueError("thought can no longer be expired")
            return self.get(user_id, thought_id)
        return self.update(user_id, thought_id, earliest_send_at=new_time)

    def transition(self, user_id: str, thought_id: str, new_status: str) -> bool:
        item = self.get(user_id, thought_id)
        if new_status not in _TRANSITIONS.get(item.status, set()):
            return False
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE thought_queue SET status=?, updated_at=? WHERE user_id=? AND id=?",
                (new_status, datetime.now(timezone.utc).isoformat(), user_id, thought_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return True

    def complete(self, user_id: str, thought_id: str) -> bool:
        return self.transition(user_id, thought_id, "completed")

    def cancel(self, user_id: str, thought_id: str) -> bool:
        return self.transition(user_id, thought_id, "cancelled")

    def expire_due(self, *, now: datetime | None = None) -> int:
        current = self._aware(now or datetime.now(timezone.utc), "now").astimezone(timezone.utc)
        marks = ",".join("?" for _ in _ACTIVE)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._db.execute(
                f"UPDATE thought_queue SET status='expired', updated_at=? "
                f"WHERE status IN ({marks}) AND expires_at<=?",
                (current.isoformat(), *_ACTIVE, current.isoformat()),
            )
            self._db.commit()
            return int(cursor.rowcount)
        except Exception:
            self._db.rollback()
            raise

    def mark_evaluated(self, user_id: str, thought_id: str, *, now: datetime) -> Thought:
        current = self._aware(now, "now").astimezone(timezone.utc).isoformat()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE thought_queue SET last_evaluated_at=?, updated_at=? WHERE user_id=? AND id=?",
                (current, current, user_id, thought_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get(user_id, thought_id)

    def record_attempt(self, user_id: str, thought_id: str, *, now: datetime | None = None) -> Thought:
        current = self._aware(now or datetime.now(timezone.utc), "now").astimezone(timezone.utc).isoformat()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            item = self.get(user_id, thought_id)
            if item.attempt_count >= self.max_attempts:
                raise ValueError("thought attempt budget is exhausted")
            if item.status not in {"selected", "ready", "pending"}:
                raise ValueError("only active thoughts can record an attempt")
            count = item.attempt_count + 1
            new_status = "failed" if count >= self.max_attempts else item.status
            self._db.execute(
                "UPDATE thought_queue SET attempt_count=?, status=?, updated_at=? WHERE user_id=? AND id=?",
                (count, new_status, current, user_id, thought_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get(user_id, thought_id)

    @staticmethod
    def _memory_ids(values: object) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)) or any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError("related_memory_ids must contain non-empty strings")
        return tuple(dict.fromkeys(item.strip() for item in values))

    @staticmethod
    def _aware(value: object, name: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be a timezone-aware datetime")
        return value

    @staticmethod
    def _record(row: sqlite3.Row) -> Thought:
        parse = lambda value: datetime.fromisoformat(value) if value else None
        return Thought(
            id=str(row["id"]), user_id=str(row["user_id"]), thought_type=str(row["thought_type"]),
            topic=str(row["topic"]), reason=str(row["reason"]),
            possible_action=str(row["possible_action"]), priority=float(row["priority"]),
            created_at=parse(row["created_at"]), earliest_send_at=parse(row["earliest_send_at"]),
            expires_at=parse(row["expires_at"]), status=str(row["status"]),
            attempt_count=int(row["attempt_count"]), last_evaluated_at=parse(row["last_evaluated_at"]),
            related_memory_ids=tuple(json.loads(row["related_memory_ids_json"])),
            source_key=str(row["source_key"]),
        )
