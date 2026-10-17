"""Deterministic, token-free user controls for proactive behavior."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .events import CompanionEvent
from .store import EventStore


class CompanionControls:
    def __init__(self, store: EventStore):
        self.store = store
        self._db = store._db

    def apply_message(self, event: CompanionEvent) -> str | None:
        if event.event_type != "USER_MESSAGE_RECEIVED":
            return None
        if event.timestamp.tzinfo is None or event.timestamp.utcoffset() is None:
            return None
        age = datetime.now(event.timestamp.tzinfo) - event.timestamp
        if age > timedelta(days=7) or age < -timedelta(minutes=5):
            return None
        text = event.payload.get("text")
        if not isinstance(text, str):
            return None
        normalized = " ".join(text.casefold().split())
        if any(phrase in normalized for phrase in (
            "恢复主动联系", "恢复主动", "继续主动联系", "可以主动联系我",
        )):
            self.set_paused(event.user_id, False)
            return "resumed"
        if any(phrase in normalized for phrase in (
            "停止主动联系", "先别主动联系我", "不要主动联系我",
            "别主动联系我", "暂停主动模式",
        )):
            self.set_paused(event.user_id, True)
            return "paused"
        preference = self._preference_intent(normalized)
        if preference is not None:
            category, value = preference
            source_id = event.idempotency_key or event.event_id
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (event.user_id,))
                evidence = self._db.execute(
                    "INSERT OR IGNORE INTO preference_evidence "
                    "(id, user_id, category, value, source_message_id) VALUES (?, ?, ?, ?, ?)",
                    (str(uuid4()), event.user_id, category, value, source_id),
                )
                if evidence.rowcount:
                    self._db.execute(
                        "INSERT INTO interaction_preferences "
                        "(id, user_id, category, value, confidence, evidence_count) "
                        "VALUES (?, ?, ?, ?, 1.0, 1) ON CONFLICT(user_id, category) DO UPDATE SET "
                        "value=excluded.value, confidence=1.0, "
                        "evidence_count=interaction_preferences.evidence_count+1, "
                        "updated_at=CURRENT_TIMESTAMP",
                        (str(uuid4()), event.user_id, category, value),
                    )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            from .companion_state import CompanionStateRepository

            CompanionStateRepository(self.store).refresh(event.user_id, now=event.timestamp)
            return "preference_updated"
        if any(phrase in normalized for phrase in (
            "我最近很忙", "最近很忙", "最近比较忙", "这几天很忙",
        )):
            duration = timedelta(days=3)
        elif any(phrase in normalized for phrase in (
            "我今天很忙", "今天很忙", "我有点忙", "最近有点忙",
        )):
            duration = timedelta(hours=24)
        else:
            return None
        state = self.state(event.user_id)
        existing = self._timestamp(state.get("busy_until"))
        busy_until = event.timestamp + duration
        if existing and existing > busy_until:
            busy_until = existing
        state["busy_until"] = busy_until.isoformat()
        self._save_state(event.user_id, state)
        return "busy"

    @staticmethod
    def _preference_intent(normalized: str) -> tuple[str, str] | None:
        if any(phrase in normalized for phrase in (
            "少找我聊天", "少主动找我", "少联系我", "降低主动联系频率",
        )):
            return ("proactive_frequency", "reduced")
        if any(phrase in normalized for phrase in (
            "以后晚上多联系", "晚上可以多联系我", "晚上多联系我",
        )):
            return ("preferred_contact_time", "evening")
        if any(phrase in normalized for phrase in (
            "技术新闻可以多分享", "多分享技术新闻", "多分享科技新闻",
        )):
            return ("discovery_technical_news", "more")
        return None

    def preferences(self, user_id: str) -> dict[str, str]:
        rows = self._db.execute(
            "SELECT category, value FROM interaction_preferences WHERE user_id=?",
            (user_id,),
        ).fetchall()
        return {str(row["category"]): str(row["value"]) for row in rows}

    def consecutive_ignored(self, user_id: str) -> int:
        activity_row = self._db.execute(
            "SELECT MAX(event_timestamp) FROM event_outbox WHERE user_id=? "
            "AND event_type='USER_MESSAGE_RECEIVED'",
            (user_id,),
        ).fetchone()
        last_activity = self._timestamp(activity_row[0]) if activity_row and activity_row[0] else None
        rows = self._db.execute(
            "SELECT state, delivered_at FROM proactive_actions WHERE delivered_at IS NOT NULL "
            "ORDER BY delivered_at DESC LIMIT 20"
        ).fetchall()
        count = 0
        for row in rows:
            delivered_at = self._timestamp(row["delivered_at"])
            if last_activity and delivered_at and last_activity > delivered_at:
                break
            if row["state"] != "ignored":
                break
            count += 1
        return count

    def set_paused(self, user_id: str, paused: bool) -> None:
        state = self.state(user_id)
        state["proactive_paused"] = bool(paused)
        self._save_state(user_id, state)

    def state(self, user_id: str) -> dict:
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        row = self._db.execute(
            "SELECT state_json FROM companion_states WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            return {}
        try:
            state = json.loads(row[0])
            if not isinstance(state, dict):
                return {"proactive_paused": True}
            return state
        except (TypeError, json.JSONDecodeError):
            return {"proactive_paused": True}

    def _save_state(self, user_id: str, state: dict) -> None:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (user_id,))
            self._db.execute(
                "INSERT INTO companion_states(user_id, state_json) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET state_json=excluded.state_json, "
                "revision=companion_states.revision+1, updated_at=CURRENT_TIMESTAMP",
                (user_id, json.dumps(state, ensure_ascii=False, separators=(",", ":"))),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
