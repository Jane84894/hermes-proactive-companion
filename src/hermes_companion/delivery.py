"""Manually approved, allowlisted Hermes CLI delivery with no automatic retry."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .actions import ProactiveAction, ProactiveActionRepository
from .controls import CompanionControls
from .events import CompanionEvent
from .social import SocialCandidate, SocialContext, SocialEngine, SocialPolicy
from .store import EventStore


class DeliveryUncertain(RuntimeError):
    """Hermes may have delivered the message; never retry automatically."""


class DeliveryFailed(RuntimeError):
    """The CLI could not start, so no delivery request was made."""


class HermesCLISender:
    def __init__(
        self,
        executable: str | Path,
        *,
        target_user_id: str,
        allowlisted_user_id: str,
        timeout_seconds: int = 45,
    ):
        if not target_user_id or target_user_id != allowlisted_user_id:
            raise ValueError("delivery target must exactly match the allowlisted user")
        if ":" in target_user_id or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", target_user_id):
            raise ValueError("delivery target ID is invalid")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        self.executable = str(executable)
        self.target_user_id = target_user_id
        self.timeout_seconds = timeout_seconds

    def send(self, message: str) -> str:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must not be empty")
        try:
            result = subprocess.run(
                [self.executable, "send", "--to", f"weixin:{self.target_user_id}",
                 "--file", "-", "--quiet"],
                input=message,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeliveryUncertain("Hermes send timed out; delivery state is unknown") from exc
        except OSError as exc:
            raise DeliveryFailed("Hermes CLI could not be started") from exc
        if result.returncode != 0:
            raise DeliveryUncertain(
                f"Hermes send returned exit status {result.returncode}; delivery state is unknown"
            )
        return "hermes-send-exit-0"


class ActionDispatcher:
    def __init__(
        self,
        store: EventStore,
        policy: SocialPolicy,
        sender: HermesCLISender,
        *,
        enabled: bool,
        target_user_id: str,
        allowlisted_user_id: str,
        auto_approve: bool = False,
    ):
        if target_user_id != allowlisted_user_id:
            raise ValueError("delivery target must exactly match the allowlisted user")
        sender_target = getattr(sender, "target_user_id", target_user_id)
        if sender_target != target_user_id:
            raise ValueError("sender target must exactly match the allowlisted user")
        self.store = store
        self.policy = policy
        self.sender = sender
        self.enabled = bool(enabled)
        self.auto_approve = bool(auto_approve)
        self.target_user_id = target_user_id
        self.allowlisted_user_id = allowlisted_user_id
        self.actions = ProactiveActionRepository(store)
        self.controls = CompanionControls(store)
        self.engine = SocialEngine()

    def approve(self, action_id: str) -> ProactiveAction:
        action = self.actions.get(action_id)
        if action.state not in {"draft_ready", "send_failed"}:
            raise ValueError("only a reviewable draft or known failed send can be approved")
        if not action.draft_text or not action.draft_text.strip():
            raise ValueError("action has no message draft to approve")
        if not self.actions.transition(action_id, "approved"):
            raise RuntimeError("action changed before approval")
        return self.actions.get(action_id)

    def recover_interrupted_sends(self) -> int:
        """Mark sends left in-flight by a crash as uncertain; never replay them."""
        cursor = self.store._db.execute(
            "UPDATE proactive_actions SET state='delivery_unknown', updated_at=CURRENT_TIMESTAMP "
            "WHERE state='sending'"
        )
        self.store._db.commit()
        return cursor.rowcount

    def update_feedback_state(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        threshold = (current - timedelta(hours=48)).astimezone(timezone.utc)
        rows = self.store._db.execute(
            "SELECT id, candidate_id FROM proactive_actions WHERE state='delivered' "
            "AND delivered_at IS NOT NULL ORDER BY delivered_at"
        ).fetchall()
        ignored = 0
        for row in rows:
            if self._timestamp(self.actions.get(str(row["id"])).delivered_at) > threshold:
                continue
            if self.actions.transition(str(row["id"]), "ignored", occurred_at=current):
                self._set_discovery_state(str(row["candidate_id"]), "ignored")
                ignored += 1
        return ignored

    def dispatch_one(self, *, now: datetime | None = None) -> dict[str, object] | None:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        states = "'approved','draft_ready'" if self.auto_approve else "'approved'"
        row = self.store._db.execute(
            f"SELECT id FROM proactive_actions WHERE state IN ({states}) "
            "ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        action = self.actions.get(str(row["id"]))
        if not self.enabled:
            return {"action_id": action.id, "state": "delivery_disabled"}
        if self.target_user_id != self.allowlisted_user_id:
            return {"action_id": action.id, "state": "target_not_allowlisted"}
        if not action.draft_text or not action.draft_text.strip():
            return {"action_id": action.id, "state": "missing_draft"}
        if action.state == "draft_ready":
            if not self.enabled or not self.auto_approve:
                return {"action_id": action.id, "state": "approval_required"}
            if not self.actions.transition(action.id, "approved", occurred_at=current):
                return {"action_id": action.id, "state": "action_changed"}
            action = self.actions.get(action.id)
        try:
            candidate = self._candidate(action)
        except (KeyError, TypeError, ValueError):
            return {"action_id": action.id, "state": "invalid_candidate"}
        local_now = current.astimezone(ZoneInfo(self.policy.timezone))
        if candidate.expires_at <= local_now:
            self.actions.transition(action.id, "expired", occurred_at=current)
            return {"action_id": action.id, "state": "expired"}
        context = self._context(local_now)
        decision = self.engine.evaluate([candidate], context, self.policy)
        if decision.selected is None:
            return {
                "action_id": action.id,
                "state": "suppressed",
                "reason": decision.suppressed_reason,
            }
        # Recheck the state under a write lock before crossing the send boundary.
        if not self.actions.transition(action.id, "ready", occurred_at=current):
            return {"action_id": action.id, "state": "action_changed"}
        if not self.actions.transition(action.id, "sending", occurred_at=current):
            return {"action_id": action.id, "state": "action_changed"}
        try:
            receipt = self.sender.send(action.draft_text)
        except DeliveryUncertain:
            self.actions.transition(action.id, "delivery_unknown", occurred_at=current)
            self._publish_delivery_event(action, "PROACTIVE_MESSAGE_FAILED", "delivery_unknown", current)
            return {"action_id": action.id, "state": "delivery_unknown"}
        except DeliveryFailed:
            self.actions.transition(action.id, "send_failed", occurred_at=current)
            self._publish_delivery_event(action, "PROACTIVE_MESSAGE_FAILED", "send_failed", current)
            return {"action_id": action.id, "state": "send_failed"}
        except Exception:
            # An unexpected adapter error may have occurred after the request left.
            self.actions.transition(action.id, "delivery_unknown", occurred_at=current)
            self._publish_delivery_event(action, "PROACTIVE_MESSAGE_FAILED", "delivery_unknown", current)
            return {"action_id": action.id, "state": "delivery_unknown"}
        self.actions.transition(
            action.id, "delivered", delivery_receipt=receipt, occurred_at=current
        )
        self._set_discovery_state(action.candidate_id, "delivered")
        self._publish_delivery_event(action, "PROACTIVE_MESSAGE_DELIVERED", "delivered", current)
        return {"action_id": action.id, "state": "delivered"}

    def record_user_response(self, event: CompanionEvent) -> str | None:
        if event.event_type != "USER_MESSAGE_RECEIVED" or event.user_id != "primary-user":
            return None
        rows = self.store._db.execute(
            "SELECT id, delivered_at FROM proactive_actions WHERE state='delivered' "
            "AND delivered_at IS NOT NULL ORDER BY delivered_at DESC"
        ).fetchall()
        for row in rows:
            delivered = self._timestamp(row["delivered_at"])
            replied = event.timestamp.astimezone(timezone.utc)
            if delivered < replied <= delivered + timedelta(hours=24):
                action_id = str(row["id"])
                if self.actions.transition(action_id, "responded", occurred_at=replied):
                    candidate_id = self.actions.get(action_id).candidate_id
                    self._set_discovery_state(candidate_id, "responded")
                    return action_id
        return None

    def _context(self, now: datetime) -> SocialContext:
        zone = ZoneInfo(self.policy.timezone)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        delivered_rows = self.store._db.execute(
            "SELECT delivered_at FROM proactive_actions WHERE delivered_at IS NOT NULL "
            "AND state IN ('delivered','responded','ignored','closed')"
        ).fetchall()
        delivered = [self._timestamp(row[0]).astimezone(zone) for row in delivered_rows]
        today = [stamp for stamp in delivered if start <= stamp < start + timedelta(days=1)]
        last_proactive = max(delivered) if delivered else None
        activity = self.store._db.execute(
            "SELECT MAX(event_timestamp) FROM event_outbox WHERE event_type='USER_MESSAGE_RECEIVED'"
        ).fetchone()[0]
        last_activity = self._timestamp(activity) if activity else None
        state = self.controls.state("primary-user")
        preferences = self.controls.preferences("primary-user")
        ignored = max(0, min(100, self.controls.consecutive_ignored("primary-user")))
        return SocialContext(
            now=now,
            proactive_messages_today=len(today),
            last_proactive_at=last_proactive,
            last_user_activity_at=last_activity,
            consecutive_ignored=ignored,
            paused=bool(state.get("proactive_paused", False)),
            busy_until=CompanionControls._timestamp(state.get("busy_until")),
            frequency_multiplier=2.0 if preferences.get("proactive_frequency") == "reduced" else 1.0,
            preferred_contact_time=preferences.get("preferred_contact_time"),
            technical_news_preferred=preferences.get("discovery_technical_news") == "more",
        )

    def _set_discovery_state(self, candidate_id: str, state: str) -> None:
        prefix, separator, discovery_id = candidate_id.partition(":")
        if prefix == "discovery" and separator and state in {"delivered", "responded", "ignored"}:
            self.store._db.execute(
                "UPDATE discoveries SET status=? WHERE id=?", (state, discovery_id)
            )
            self.store._db.commit()

    def _publish_delivery_event(
        self, action: ProactiveAction, event_type: str, state: str, timestamp: datetime
    ) -> None:
        self.store.publish(CompanionEvent(
            event_type=event_type,
            user_id="primary-user",
            timestamp=timestamp,
            source="hermes_cli",
            payload={"action_id": action.id, "candidate_id": action.candidate_id, "state": state},
            causation_id=action.id,
            idempotency_key=f"{event_type.casefold()}:{action.id}",
        ))

    @staticmethod
    def _candidate(action: ProactiveAction) -> SocialCandidate:
        payload = action.payload
        return SocialCandidate(
            id=str(payload["id"]), kind=str(payload["kind"]),
            title=str(payload["title"]), reason=str(payload["reason"]),
            priority=float(payload["priority"]), relevance=float(payload["relevance"]),
            timeliness=float(payload["timeliness"]), novelty=float(payload["novelty"]),
            continuity=float(payload["continuity"]),
            relationship_fit=float(payload["relationship_fit"]),
            interruption_cost=float(payload["interruption_cost"]),
            repetition_penalty=float(payload["repetition_penalty"]),
            earliest_send_at=datetime.fromisoformat(str(payload["earliest_send_at"])),
            expires_at=datetime.fromisoformat(str(payload["expires_at"])),
            status=str(payload.get("status", "candidate")),
        )

    @staticmethod
    def _timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
