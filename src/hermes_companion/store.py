"""Durable, idempotent event outbox backed by SQLite."""

from __future__ import annotations

import json
import sqlite3
import os
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections.abc import Sequence

from .events import CompanionEvent


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(self.path, timeout=5.0)
        os.chmod(self.path, 0o600)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.execute("PRAGMA journal_mode = WAL")
        try:
            self._apply_migrations()
        except Exception:
            self._db.rollback()
            self._db.close()
            raise

    def _apply_migrations(self) -> None:
        migration_dir = Path(__file__).parent / "migrations"
        paths = sorted(migration_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        if not paths:
            raise RuntimeError("no database migrations are installed")
        for migration in paths:
            version = int(migration.name.split("_", 1)[0])
            try:
                current = self.schema_version
            except sqlite3.OperationalError:
                current = 0
            if version <= current:
                continue
            if version != current + 1:
                raise RuntimeError(
                    f"database migration gap: expected {current + 1}, found {version}"
                )
            script = migration.read_text(encoding="utf-8")
            self._db.executescript(
                "BEGIN IMMEDIATE;\n"
                + script
                + f"\nINSERT INTO schema_migrations(version) VALUES ({version});\nCOMMIT;"
            )

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    @property
    def schema_version(self) -> int:
        row = self._db.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        return int(row[0] or 0)

    def publish(self, event: CompanionEvent) -> bool:
        """Persist an event once; return False if its id or idempotency key exists."""
        try:
            self._db.execute("BEGIN IMMEDIATE")
            inserted = self._insert_event(event)
            self._db.commit()
            return inserted
        except Exception:
            self._db.rollback()
            raise

    def publish_and_advance(
        self, event: CompanionEvent, consumer_name: str, cursor_value: int
    ) -> bool:
        """Atomically persist an event and advance its source cursor."""
        if not consumer_name.strip():
            raise ValueError("consumer_name must not be empty")
        if cursor_value < 0:
            raise ValueError("cursor_value must not be negative")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            inserted = self._insert_event(event)
            self._db.execute(
                """
                INSERT INTO consumer_cursors (consumer_name, last_value)
                VALUES (?, ?)
                ON CONFLICT(consumer_name) DO UPDATE SET
                    last_value = MAX(consumer_cursors.last_value, excluded.last_value),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (consumer_name, cursor_value),
            )
            self._db.commit()
            return inserted
        except Exception:
            self._db.rollback()
            raise

    def _insert_event(self, event: CompanionEvent) -> bool:
        cursor = self._db.execute(
            """
            INSERT OR IGNORE INTO event_outbox (
                event_id, event_type, user_id, event_timestamp, source,
                payload_json, correlation_id, schema_version, causation_id,
                idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.event_type,
                event.user_id,
                event.timestamp.isoformat(),
                event.source,
                json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
                event.correlation_id,
                event.schema_version,
                event.causation_id,
                event.idempotency_key,
            ),
        )
        return cursor.rowcount == 1

    def get_cursor(self, consumer_name: str) -> int:
        row = self._db.execute(
            "SELECT last_value FROM consumer_cursors WHERE consumer_name=?",
            (consumer_name,),
        ).fetchone()
        return int(row[0]) if row else 0

    def record_model_usage(
        self,
        *,
        purpose: str,
        provider: str,
        model: str,
        request_id: str | None,
        occurred_at: datetime,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        estimated_cost_usd: float | None = None,
        cost_status: str = "unknown",
    ) -> None:
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if min(prompt_tokens, completion_tokens, total_tokens) < 0:
            raise ValueError("token counts must not be negative")
        if cost_status not in {"unknown", "estimated", "actual"}:
            raise ValueError("invalid cost_status")
        self._db.execute(
            """
            INSERT INTO model_usage (
                purpose, provider, model, request_id, occurred_at, prompt_tokens,
                completion_tokens, total_tokens, estimated_cost_usd, cost_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                purpose, provider, model, request_id, occurred_at.isoformat(),
                prompt_tokens, completion_tokens, total_tokens,
                estimated_cost_usd, cost_status,
            ),
        )
        self._db.commit()

    def usage_between(self, start: datetime, end: datetime) -> int:
        if any(value.tzinfo is None or value.utcoffset() is None for value in (start, end)):
            raise ValueError("usage range must be timezone-aware")
        if end <= start:
            raise ValueError("usage range end must follow start")
        row = self._db.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) FROM model_usage "
            "WHERE occurred_at>=? AND occurred_at<?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        return int(row[0])

    def usage_cost_between(
        self,
        start: datetime,
        end: datetime,
        *,
        input_usd_per_million: float,
        output_usd_per_million: float,
    ) -> float:
        if any(value.tzinfo is None or value.utcoffset() is None for value in (start, end)):
            raise ValueError("usage range must be timezone-aware")
        if end <= start:
            raise ValueError("usage range end must follow start")
        if min(input_usd_per_million, output_usd_per_million) < 0:
            raise ValueError("model prices must not be negative")
        rows = self._db.execute(
            "SELECT prompt_tokens, completion_tokens, estimated_cost_usd, cost_status "
            "FROM model_usage WHERE occurred_at>=? AND occurred_at<?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        total = 0.0
        for row in rows:
            recorded = row["estimated_cost_usd"]
            if recorded is not None and row["cost_status"] in {"estimated", "actual"}:
                total += float(recorded)
            else:
                total += (
                    int(row["prompt_tokens"]) * input_usd_per_million
                    + int(row["completion_tokens"]) * output_usd_per_million
                ) / 1_000_000
        return total

    def pending_events(self, limit: int = 100) -> list[CompanionEvent]:
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self._db.execute(
            "SELECT * FROM event_outbox WHERE status='pending' "
            "ORDER BY created_at, event_id LIMIT ?",
            (limit,),
        ).fetchall()
        return [CompanionEvent.from_storage(dict(row)) for row in rows]

    def claim_events(
        self,
        owner: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 300,
        limit: int = 1,
        event_types: Sequence[str] | None = None,
    ) -> list[CompanionEvent]:
        if not owner.strip():
            raise ValueError("owner must not be empty")
        if lease_seconds < 1 or limit < 1:
            raise ValueError("lease_seconds and limit must be positive")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        stamp = current.isoformat()
        expired_before = (current - timedelta(seconds=lease_seconds)).isoformat()
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "UPDATE event_outbox SET status='pending', claimed_at=NULL, claim_owner=NULL "
                "WHERE status='processing' AND claimed_at <= ?",
                (expired_before,),
            )
            type_filter = ""
            parameters: list[object] = [stamp]
            if event_types is not None:
                if not event_types:
                    self._db.commit()
                    return []
                type_filter = " AND event_type IN (" + ",".join("?" for _ in event_types) + ")"
                parameters.extend(event_types)
            parameters.append(limit)
            rows = self._db.execute(
                "SELECT event_id FROM event_outbox WHERE status='pending' "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                + type_filter
                + " ORDER BY created_at, event_id LIMIT ?",
                parameters,
            ).fetchall()
            ids = [row[0] for row in rows]
            for event_id in ids:
                self._db.execute(
                    "UPDATE event_outbox SET status='processing', claimed_at=?, "
                    "claim_owner=?, attempt_count=attempt_count+1 "
                    "WHERE event_id=? AND status='pending'",
                    (stamp, owner, event_id),
                )
            if ids:
                placeholders = ",".join("?" for _ in ids)
                claimed = self._db.execute(
                    "SELECT * FROM event_outbox WHERE event_id IN (" + placeholders + ") "
                    "AND claim_owner=? AND status='processing' "
                    "ORDER BY created_at, event_id",
                    (*ids, owner),
                ).fetchall()
            else:
                claimed = []
            self._db.commit()
            return [CompanionEvent.from_storage(dict(row)) for row in claimed]
        except Exception:
            self._db.rollback()
            raise

    def fail_claim(
        self,
        event_id: str,
        *,
        owner: str,
        error_code: str,
        now: datetime | None = None,
        max_attempts: int = 5,
        retry_delay_seconds: int = 60,
    ) -> bool:
        if max_attempts < 1 or retry_delay_seconds < 0:
            raise ValueError("retry limits are invalid")
        safe_code = re.sub(r"[^A-Za-z0-9_.-]", "", error_code)[:80] or "Error"
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        row = self._db.execute(
            "SELECT attempt_count FROM event_outbox WHERE event_id=? "
            "AND status='processing' AND claim_owner=?",
            (event_id, owner),
        ).fetchone()
        if row is None:
            return False
        terminal = int(row[0]) >= max_attempts
        retry_at = None if terminal else (
            current + timedelta(seconds=retry_delay_seconds)
        ).isoformat()
        cursor = self._db.execute(
            "UPDATE event_outbox SET status=?, claimed_at=NULL, claim_owner=NULL, "
            "next_attempt_at=?, last_error_code=? "
            "WHERE event_id=? AND status='processing' AND claim_owner=?",
            (
                "failed" if terminal else "pending",
                retry_at,
                safe_code,
                event_id,
                owner,
            ),
        )
        self._db.commit()
        return bool(cursor.rowcount and terminal)

    def defer_claim(
        self,
        event_id: str,
        *,
        owner: str,
        retry_at: datetime,
        reason_code: str,
    ) -> bool:
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            raise ValueError("retry_at must be timezone-aware")
        safe_code = re.sub(r"[^A-Za-z0-9_.-]", "", reason_code)[:80] or "Deferred"
        cursor = self._db.execute(
            "UPDATE event_outbox SET status='pending', claimed_at=NULL, claim_owner=NULL, "
            "attempt_count=MAX(0, attempt_count-1), next_attempt_at=?, last_error_code=? "
            "WHERE event_id=? AND status='processing' AND claim_owner=?",
            (retry_at.astimezone(timezone.utc).isoformat(), safe_code, event_id, owner),
        )
        self._db.commit()
        return cursor.rowcount == 1

    def mark_processed(
        self, event_id: str, *, redact_payload: bool = False, owner: str | None = None
    ) -> bool:
        cursor = self._db.execute(
            "UPDATE event_outbox SET status='processed', processed_at=CURRENT_TIMESTAMP, "
            "payload_json=CASE WHEN ? THEN '{}' ELSE payload_json END, "
            "claimed_at=NULL, claim_owner=NULL "
            "WHERE event_id=? AND (status='pending' OR "
            "(status='processing' AND claim_owner=?))",
            (int(redact_payload), event_id, owner),
        )
        self._db.commit()
        return cursor.rowcount == 1
