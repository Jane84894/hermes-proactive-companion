"""Persistent, idempotent state machine for proactive message actions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .store import EventStore


_TRANSITIONS = {
    "candidate": {"draft_ready", "cancelled", "expired"},
    "draft_ready": {"approved", "cancelled", "expired", "closed"},
    "approved": {"generating", "ready", "cancelled", "expired"},
    "generating": {"ready", "generation_failed", "cancelled"},
    "ready": {"sending", "cancelled", "expired"},
    "sending": {"delivered", "send_failed", "delivery_unknown"},
    "delivered": {"responded", "ignored", "closed"},
    "generation_failed": {"generating", "cancelled", "expired"},
    # A retry after a known failure requires another explicit approval.
    "send_failed": {"approved", "cancelled", "expired"},
    "delivery_unknown": set(),
    "cancelled": set(),
    "expired": set(),
    "responded": {"closed"},
    "ignored": {"closed"},
    "closed": set(),
}


@dataclass(frozen=True, slots=True)
class ProactiveAction:
    id: str
    idempotency_key: str
    candidate_id: str
    intent: str
    state: str
    payload: dict[str, object]
    draft_text: str | None
    attempt_count: int
    delivery_receipt: str | None
    delivered_at: str | None
    responded_at: str | None


class ProactiveActionRepository:
    def __init__(self, store: EventStore):
        self._db = store._db

    def create(
        self,
        *,
        idempotency_key: str,
        candidate_id: str,
        intent: str,
        payload: dict[str, object],
    ) -> tuple[ProactiveAction, bool]:
        if not all(value.strip() for value in (idempotency_key, candidate_id, intent)):
            raise ValueError("action idempotency key, candidate ID, and intent are required")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "INSERT OR IGNORE INTO proactive_actions "
                "(id, idempotency_key, candidate_id, intent, payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid4()), idempotency_key, candidate_id, intent,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            row = self._db.execute(
                "SELECT * FROM proactive_actions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row["candidate_id"] != candidate_id or row["intent"] != intent:
                raise ValueError("idempotency key is already bound to a different action")
            created = self._db.execute("SELECT changes()").fetchone()[0] == 1
            self._db.commit()
            return self._record(row), created
        except Exception:
            self._db.rollback()
            raise

    def get(self, action_id: str) -> ProactiveAction:
        row = self._db.execute(
            "SELECT * FROM proactive_actions WHERE id=?", (action_id,)
        ).fetchone()
        if row is None:
            raise KeyError(action_id)
        return self._record(row)

    def transition(
        self,
        action_id: str,
        new_state: str,
        *,
        draft_text: str | None = None,
        delivery_receipt: str | None = None,
        occurred_at: datetime | None = None,
    ) -> bool:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT state FROM proactive_actions WHERE id=?", (action_id,)
            ).fetchone()
            if row is None:
                raise KeyError(action_id)
            old_state = str(row["state"])
            if new_state not in _TRANSITIONS.get(old_state, set()):
                self._db.rollback()
                return False
            stamp = (occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
            self._db.execute(
                "UPDATE proactive_actions SET state=?, "
                "draft_text=COALESCE(?, draft_text), "
                "delivery_receipt=COALESCE(?, delivery_receipt), "
                "delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END, "
                "responded_at=CASE WHEN ?='responded' THEN ? ELSE responded_at END, "
                "attempt_count=attempt_count + ?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_state, draft_text, delivery_receipt, new_state, stamp,
                 new_state, stamp, int(new_state == "sending"), action_id),
            )
            self._db.commit()
            return True
        except Exception:
            self._db.rollback()
            raise

    @staticmethod
    def _record(row: sqlite3.Row) -> ProactiveAction:
        return ProactiveAction(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            candidate_id=row["candidate_id"],
            intent=row["intent"],
            state=row["state"],
            payload=json.loads(row["payload_json"]),
            draft_text=row["draft_text"],
            attempt_count=int(row["attempt_count"]),
            delivery_receipt=row["delivery_receipt"],
            delivered_at=row["delivered_at"],
            responded_at=row["responded_at"],
        )
