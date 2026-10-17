"""Build a small persistent companion context from authoritative user records."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .store import EventStore


class CompanionStateRepository:
    """Refresh prompt context without overwriting explicit social controls."""

    def __init__(self, store: EventStore):
        self._db = store._db

    def get(self, user_id: str) -> dict[str, object]:
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        row = self._db.execute(
            "SELECT state_json FROM companion_states WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            return self._defaults()
        try:
            state = json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError):
            return {**self._defaults(), "proactive_paused": True}
        if not isinstance(state, dict):
            return {**self._defaults(), "proactive_paused": True}
        return {**self._defaults(), **state}

    def refresh(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
        persona_version: str | None = None,
    ) -> dict[str, object]:
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current_utc = current.astimezone(timezone.utc)

        interests = []
        for row in self._db.execute(
            "SELECT title, current_state, interest_score, last_active_at FROM topics "
            "WHERE user_id=? AND status='open' ORDER BY interest_score DESC, last_active_at DESC",
            (user_id,),
        ).fetchall():
            last_active = self._timestamp(row["last_active_at"])
            if last_active is None or last_active < current_utc - timedelta(days=90):
                continue
            interests.append({
                "title": row["title"],
                "current_state": row["current_state"],
                "interest_score": float(row["interest_score"]),
                "last_active_at": last_active.isoformat(),
            })
            if len(interests) == 5:
                break

        thoughts = []
        event_rows = self._db.execute(
            "SELECT event_type, title, details, remind_at FROM followup_events "
            "WHERE user_id=? AND status IN ('pending','scheduled') AND remind_at IS NOT NULL",
            (user_id,),
        ).fetchall()
        parsed_events = []
        for row in event_rows:
            remind_at = self._timestamp(row["remind_at"])
            if remind_at is None or remind_at < current_utc - timedelta(days=3):
                continue
            parsed_events.append((remind_at, row))
        for remind_at, row in sorted(parsed_events, key=lambda item: item[0])[:5]:
            thoughts.append({
                "event_type": row["event_type"],
                "title": row["title"],
                "details": row["details"],
                "remind_at": remind_at.isoformat(),
            })
        queued_rows = self._db.execute(
            "SELECT thought_type, topic, reason, possible_action, priority, earliest_send_at "
            "FROM thought_queue WHERE user_id=? AND status IN ('candidate','pending','ready') "
            "AND earliest_send_at<=? AND expires_at>? "
            "ORDER BY priority DESC, earliest_send_at LIMIT ?",
            (user_id, current_utc.isoformat(), current_utc.isoformat(), max(0, 5 - len(thoughts))),
        ).fetchall()
        known_titles = {str(item["title"]).casefold() for item in thoughts}
        for row in queued_rows:
            if str(row["topic"]).casefold() in known_titles:
                continue
            thoughts.append({
                "type": row["thought_type"],
                "title": row["topic"],
                "reason": row["reason"],
                "possible_action": row["possible_action"],
                "priority": float(row["priority"]),
                "earliest_send_at": row["earliest_send_at"],
            })
            known_titles.add(str(row["topic"]).casefold())
            if len(thoughts) == 5:
                break

        latest_episode = self._db.execute(
            "SELECT summary FROM episodes WHERE user_id=? ORDER BY occurred_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        preferences = [
            {"category": str(row["category"]), "value": str(row["value"]),
             "confidence": float(row["confidence"]), "evidence_count": int(row["evidence_count"])}
            for row in self._db.execute(
                "SELECT category, value, confidence, evidence_count FROM interaction_preferences "
                "WHERE user_id=? ORDER BY confidence DESC, evidence_count DESC LIMIT 8",
                (user_id,),
            ).fetchall()
        ]

        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (user_id,))
            row = self._db.execute(
                "SELECT state_json FROM companion_states WHERE user_id=?", (user_id,)
            ).fetchone()
            previous = self._decode(row["state_json"] if row else None)
            state = {**self._defaults(), **previous}
            state["current_interests"] = interests
            state["pending_thoughts"] = thoughts
            state["last_interaction_summary"] = latest_episode["summary"] if latest_episode else ""
            state["interaction_preferences"] = preferences
            if persona_version is not None:
                if not persona_version.strip() or len(persona_version) > 120:
                    raise ValueError("persona_version must be a short non-empty string")
                state["persona_version"] = persona_version
            serialized = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            if row is None:
                self._db.execute(
                    "INSERT INTO companion_states(user_id, state_json) VALUES (?, ?)",
                    (user_id, serialized),
                )
            elif serialized != row["state_json"]:
                self._db.execute(
                    "UPDATE companion_states SET state_json=?, revision=revision+1, "
                    "updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                    (serialized, user_id),
                )
            self._db.commit()
            return state
        except Exception:
            self._db.rollback()
            raise

    @staticmethod
    def _defaults() -> dict[str, object]:
        return {
            "current_interests": [],
            "recent_discoveries": [],
            "pending_thoughts": [],
            "conversation_goals": [],
            "last_interaction_summary": "",
            "persona_version": "",
            "interaction_preferences": [],
        }

    @classmethod
    def _decode(cls, raw: str | None) -> dict[str, object]:
        if raw is None:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            # Fail closed for corrupted controls and rebuild only derived fields.
            return {"proactive_paused": True}
        return value if isinstance(value, dict) else {"proactive_paused": True}

    @staticmethod
    def _timestamp(raw: str) -> datetime | None:
        try:
            value = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
