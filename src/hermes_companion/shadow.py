"""Persist explainable shadow-mode social decisions without sending messages."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .actions import ProactiveActionRepository
from .budget import BudgetExceeded
from .companion_agent import CompanionAgent
from .companion_state import CompanionStateRepository
from .controls import CompanionControls
from .memory import MemoryRepository
from .social import (
    SocialCandidate,
    SocialContext,
    SocialEngine,
    SocialPolicy,
)
from .store import EventStore
from .thought_queue import QueueLimitExceeded, ThoughtQueueRepository


class ShadowDecisionRunner:
    def __init__(
        self,
        store: EventStore,
        policy: SocialPolicy,
        interval_minutes: int,
        *,
        companion_agent: CompanionAgent | None = None,
        max_thought_queue_items: int = 100,
        max_thought_tasks_per_day: int = 20,
        max_thought_attempts: int = 3,
    ):
        if interval_minutes < 1:
            raise ValueError("interval_minutes must be positive")
        self.store = store
        self.policy = policy
        self.interval_minutes = interval_minutes
        self.engine = SocialEngine()
        self.companion_agent = companion_agent
        self.memories = MemoryRepository(store)
        self.companion_state = CompanionStateRepository(store)
        self.controls = CompanionControls(store)
        self.actions = ProactiveActionRepository(store)
        self.thoughts = ThoughtQueueRepository(
            store, max_queue_length=max_thought_queue_items,
            max_tasks_per_day=max_thought_tasks_per_day,
            max_attempts=max_thought_attempts,
            timezone_name=policy.timezone,
        )

    def evaluate(self, *, now: datetime) -> dict[str, object] | None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        local_now = now.astimezone(ZoneInfo(self.policy.timezone))
        self._sync_thought_states(local_now)
        slot = local_now.replace(minute=(local_now.minute // self.interval_minutes) * self.interval_minutes,
                                 second=0, microsecond=0).isoformat()
        db = self.store._db
        existing = db.execute(
            "SELECT 1 FROM social_decisions WHERE evaluation_slot=? LIMIT 1", (slot,)
        ).fetchone()
        if existing:
            return None

        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        selected_rows = db.execute(
            "SELECT evaluated_at FROM social_decisions WHERE selected=1 AND evaluated_at>=? "
            "AND evaluated_at<? ORDER BY evaluated_at DESC",
            (day_start.isoformat(), (day_start + timedelta(days=1)).isoformat()),
        ).fetchall()
        last_proactive = (
            datetime.fromisoformat(selected_rows[0][0]) if selected_rows else None
        )
        last_user_row = db.execute(
            "SELECT MAX(event_timestamp) FROM event_outbox WHERE event_type='USER_MESSAGE_RECEIVED'"
        ).fetchone()
        last_activity = self._timestamp(last_user_row[0]) if last_user_row and last_user_row[0] else None
        state = self.controls.state("primary-user")
        preferences = self.controls.preferences("primary-user")
        consecutive_ignored = max(0, min(100, self.controls.consecutive_ignored("primary-user")))
        context = SocialContext(
            now=local_now,
            proactive_messages_today=len(selected_rows),
            last_proactive_at=last_proactive,
            last_user_activity_at=last_activity,
            paused=bool(state.get("proactive_paused", False)),
            busy_until=CompanionControls._timestamp(state.get("busy_until")),
            consecutive_ignored=consecutive_ignored,
            frequency_multiplier=2.0 if preferences.get("proactive_frequency") == "reduced" else 1.0,
            preferred_contact_time=preferences.get("preferred_contact_time"),
            technical_news_preferred=preferences.get("discovery_technical_news") == "more",
        )
        candidates = self._candidates(local_now)
        decision = self.engine.evaluate(candidates, context, self.policy)
        selected_id = decision.selected.id if decision.selected else None
        action = None
        if decision.selected:
            selected = decision.selected
            action, _ = self.actions.create(
                idempotency_key=f"proactive:{selected.id}",
                candidate_id=selected.id,
                intent=selected.kind,
                payload={
                    "id": selected.id,
                    "kind": selected.kind,
                    "title": selected.title,
                    "reason": selected.reason,
                    "priority": selected.priority,
                    "relevance": selected.relevance,
                    "timeliness": selected.timeliness,
                    "novelty": selected.novelty,
                    "continuity": selected.continuity,
                    "relationship_fit": selected.relationship_fit,
                    "interruption_cost": selected.interruption_cost,
                    "repetition_penalty": selected.repetition_penalty,
                    "earliest_send_at": selected.earliest_send_at.isoformat(),
                    "expires_at": selected.expires_at.isoformat(),
                    "status": selected.status,
                },
            )
            thought = self.thoughts.get_by_source_key("primary-user", selected.id)
            if thought is not None:
                self.thoughts.transition("primary-user", thought.id, "selected")
        for candidate in candidates:
            scored = decision.scored.get(candidate.id)
            candidate_reasons = list(scored.reason_codes if scored else decision.reason_codes)
            if candidate.id == selected_id:
                candidate_reasons = list(decision.reason_codes)
            db.execute(
                "INSERT OR IGNORE INTO social_decisions (evaluation_slot, evaluated_at, "
                "candidate_id, candidate_kind, selected, score, reason_codes_json, "
                "components_json, candidate_title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    slot, local_now.isoformat(), candidate.id, candidate.kind,
                    int(candidate.id == selected_id), scored.score if scored else None,
                    json.dumps(candidate_reasons, separators=(",", ":")),
                    json.dumps(scored.components if scored else {}, separators=(",", ":")),
                    candidate.title,
                ),
            )
        if not candidates:
            db.execute(
                "INSERT OR IGNORE INTO social_decisions (evaluation_slot, evaluated_at, "
                "candidate_id, candidate_kind, selected, score, reason_codes_json, components_json) "
                "VALUES (?, ?, ?, 'none', 0, NULL, ?, '{}')",
                (slot, local_now.isoformat(), f"none:{slot}",
                 json.dumps([decision.suppressed_reason or "no_candidates"])),
            )
        db.commit()
        draft_result = None
        if decision.selected and self.companion_agent:
            selected = decision.selected
            memory_candidates = self.memories.search("primary-user", selected.title, limit=5)
            persona = getattr(self.companion_agent, "persona", {})
            persona_version = persona.get("version") if isinstance(persona, dict) else None
            state = self.companion_state.refresh(
                "primary-user", now=local_now,
                persona_version=persona_version if isinstance(persona_version, str) else None,
            )
            try:
                draft_result = self.companion_agent.generate(
                    selected, memory_candidates, state
                )
            except BudgetExceeded:
                draft_result = None
                draft_status = "budget_deferred"
            except Exception as exc:
                draft_result = None
                draft_status = "failed"
                failure_code = type(exc).__name__
            else:
                draft_status = draft_result.status
                failure_code = draft_result.reason_code
            db.execute(
                "UPDATE social_decisions SET draft_status=?, draft_text=?, draft_basis_json=? "
                "WHERE evaluation_slot=? AND candidate_id=?",
                (
                    draft_status,
                    draft_result.message if draft_result else None,
                    json.dumps(draft_result.basis_ids if draft_result else [], separators=(",", ":")),
                    slot, selected.id,
                ),
            )
            if draft_status in {"failed", "rejected"}:
                db.execute(
                    "UPDATE social_decisions SET reason_codes_json=? "
                    "WHERE evaluation_slot=? AND candidate_id=?",
                    (json.dumps([*decision.reason_codes, f"draft_{failure_code}"]), slot, selected.id),
                )
            # Persist decision/draft updates before the action repository opens
            # its own BEGIN IMMEDIATE transaction on this shared connection.
            db.commit()
            if draft_status in {"failed", "rejected"}:
                self.actions.transition(action.id, "generation_failed")
                thought = self.thoughts.get_by_source_key("primary-user", selected.id)
                if thought is not None:
                    self.thoughts.record_attempt("primary-user", thought.id)
                    self.thoughts.transition("primary-user", thought.id, "failed")
            elif draft_status == "budget_deferred":
                thought = self.thoughts.get_by_source_key("primary-user", selected.id)
                if thought is not None:
                    self.thoughts.transition("primary-user", thought.id, "pending")
            elif draft_status == "generated":
                self.actions.transition(
                    action.id, "draft_ready", draft_text=draft_result.message
                )
            db.commit()
        return {
            "evaluated_at": local_now.isoformat(),
            "candidate_count": len(candidates),
            "selected_candidate": selected_id,
            "suppressed_reason": decision.suppressed_reason,
            "outbound_delivery": "disabled",
            "draft_status": draft_status if decision.selected and self.companion_agent else "not_generated",
            "action_state": self.actions.get(action.id).state if action else "none",
        }

    def set_paused(self, paused: bool) -> None:
        self.controls.set_paused("primary-user", paused)

    def _candidates(self, now: datetime) -> list[SocialCandidate]:
        db = self.store._db
        rows = db.execute(
            "SELECT id, event_type, title, remind_at FROM followup_events "
            "WHERE status IN ('pending','scheduled') AND remind_at IS NOT NULL",
        ).fetchall()
        result: list[SocialCandidate] = []
        for row in rows:
            candidate_id = f"event:{row['id']}"
            if not self._candidate_retryable(candidate_id, now):
                continue
            remind_at = self._timestamp(row["remind_at"])
            if remind_at > now or remind_at <= now - timedelta(days=3):
                continue
            result.append(SocialCandidate(
                id=f"event:{row['id']}", kind="event_follow_up", title=row["title"],
                reason=f"到期事件跟进：{row['event_type']}", priority=0.9,
                relevance=0.85, timeliness=0.95, novelty=0.7, continuity=0.9,
                relationship_fit=0.8, interruption_cost=0.25, repetition_penalty=0.1,
                earliest_send_at=remind_at, expires_at=remind_at + timedelta(days=3),
            ))
        discovery_rows = db.execute(
            "SELECT id, title, url, topic_id, interest_score, created_at, expires_at "
            "FROM discoveries WHERE status='new' "
            "ORDER BY interest_score DESC, created_at DESC LIMIT 20"
        ).fetchall()
        for row in discovery_rows:
            candidate_id = f"discovery:{row['id']}"
            if not self._candidate_retryable(candidate_id, now):
                continue
            created_at = self._timestamp(row["created_at"])
            expires_at = self._timestamp(row["expires_at"])
            if expires_at <= now:
                db.execute("UPDATE discoveries SET status='expired' WHERE id=?", (row["id"],))
                continue
            result.append(SocialCandidate(
                id=candidate_id, kind="discovery_share", title=row["title"],
                reason=f"与近期话题相关的新项目，来源链接：{row['url']}",
                priority=0.55, relevance=float(row["interest_score"]),
                timeliness=0.7, novelty=0.9, continuity=0.65,
                relationship_fit=0.7, interruption_cost=0.35,
                repetition_penalty=0.0, earliest_send_at=created_at,
                expires_at=expires_at,
            ))
        topic_rows = db.execute(
            "SELECT id, title, interest_score, last_active_at FROM topics "
            "WHERE status='open' AND followup_allowed=1",
        ).fetchall()
        for row in topic_rows:
            candidate_id = f"topic:{row['id']}"
            if not self._candidate_retryable(candidate_id, now):
                continue
            last_active = self._timestamp(row["last_active_at"])
            if last_active + timedelta(minutes=self.interval_minutes) > now:
                continue
            if last_active + timedelta(days=14) <= now:
                continue
            result.append(SocialCandidate(
                id=f"topic:{row['id']}", kind="topic_follow_up", title=row["title"],
                reason="延续近期仍开放的话题", priority=0.5,
                relevance=float(row["interest_score"]), timeliness=0.45,
                novelty=0.35, continuity=0.8, relationship_fit=0.7,
                interruption_cost=0.45, repetition_penalty=0.15,
                earliest_send_at=last_active + timedelta(minutes=self.interval_minutes),
                expires_at=last_active + timedelta(days=14),
            ))
        last_activity_row = db.execute(
            "SELECT MAX(event_timestamp) FROM event_outbox "
            "WHERE event_type='USER_MESSAGE_RECEIVED'"
        ).fetchone()
        if last_activity_row and last_activity_row[0]:
            last_activity = self._timestamp(last_activity_row[0])
            light_chat_id = f"light-chat:{now.date().isoformat()}"
            recent_light_chat = db.execute(
                "SELECT 1 FROM social_decisions WHERE candidate_kind='light_chat' "
                "AND selected=1 AND evaluated_at>=? LIMIT 1",
                ((now - timedelta(days=1)).isoformat(),),
            ).fetchone()
            earliest = last_activity + timedelta(hours=self.policy.light_chat_after_hours)
            if (earliest <= now and earliest + timedelta(days=14) > now
                    and recent_light_chat is None and self._candidate_retryable(light_chat_id, now)):
                result.append(SocialCandidate(
                    id=light_chat_id, kind="light_chat", title="轻松聊聊",
                    reason="闲置时间已到，加入轻聊天候选",
                    priority=0.55, relevance=0.7, timeliness=0.6, novelty=0.8,
                    continuity=0.3, relationship_fit=0.75, interruption_cost=0.3,
                    repetition_penalty=0.0, earliest_send_at=earliest,
                    expires_at=earliest + timedelta(days=14),
                ))
        return self._enqueue_candidates(result, now)

    def _enqueue_candidates(
        self, candidates: list[SocialCandidate], now: datetime
    ) -> list[SocialCandidate]:
        self.thoughts.expire_due(now=now)
        ready: list[SocialCandidate] = []
        for candidate in candidates:
            try:
                thought = self.thoughts.get_by_source_key("primary-user", candidate.id)
                if thought is None:
                    thought, _ = self.thoughts.enqueue(
                        user_id="primary-user", thought_type=candidate.kind,
                        topic=candidate.title, reason=candidate.reason,
                        possible_action=candidate.kind, priority=candidate.priority,
                        source_key=candidate.id, earliest_send_at=candidate.earliest_send_at,
                        expires_at=candidate.expires_at, now=now,
                    )
                elif thought.status in {"candidate", "pending", "ready"}:
                    earliest = max(thought.earliest_send_at, candidate.earliest_send_at)
                    if candidate.expires_at <= earliest:
                        self.thoughts.transition("primary-user", thought.id, "expired")
                        continue
                    thought = self.thoughts.update(
                        "primary-user", thought.id,
                        thought_type=candidate.kind, topic=candidate.title,
                        reason=candidate.reason, possible_action=candidate.kind,
                        priority=candidate.priority, earliest_send_at=earliest,
                        expires_at=candidate.expires_at,
                    )
            except QueueLimitExceeded:
                continue
            if thought.status not in {"pending", "ready"}:
                continue
            if thought.earliest_send_at > now or thought.expires_at <= now:
                continue
            self.thoughts.mark_evaluated("primary-user", thought.id, now=now)
            ready.append(candidate)
        return ready

    def _sync_thought_states(self, now: datetime) -> None:
        transitions = {
            "delivered": "delivered", "responded": "engaged", "ignored": "ignored",
            "cancelled": "cancelled", "expired": "expired", "closed": "completed",
            "generation_failed": "failed", "send_failed": "failed",
            "delivery_unknown": "failed",
        }
        rows = self.store._db.execute(
            "SELECT t.id, a.state FROM thought_queue t JOIN proactive_actions a "
            "ON a.candidate_id=t.source_key WHERE t.user_id='primary-user' AND t.status='selected'"
        ).fetchall()
        for row in rows:
            state = transitions.get(str(row["state"]))
            if state:
                self.thoughts.transition("primary-user", str(row["id"]), state)

    def _candidate_retryable(self, candidate_id: str, now: datetime) -> bool:
        action = self.store._db.execute(
            "SELECT state FROM proactive_actions WHERE idempotency_key=?",
            (f"proactive:{candidate_id}",),
        ).fetchone()
        if action is not None and action["state"] != "candidate":
            return False
        if action is None:
            previously_selected = self.store._db.execute(
                "SELECT 1 FROM social_decisions WHERE selected=1 AND candidate_id=? LIMIT 1",
                (candidate_id,),
            ).fetchone()
            if previously_selected:
                return False
        local_now = now.astimezone(ZoneInfo(self.policy.timezone))
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        selected_today = self.store._db.execute(
            "SELECT 1 FROM social_decisions WHERE selected=1 AND candidate_id=? "
            "AND evaluated_at>=? LIMIT 1",
            (candidate_id, day_start.isoformat()),
        ).fetchone()
        return selected_today is None

    @staticmethod
    def _timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        # SQLite CURRENT_TIMESTAMP values in the source schema are UTC without a suffix.
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
