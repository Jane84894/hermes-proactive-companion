"""Deterministic social gates and explainable candidate scoring."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class SocialCandidate:
    id: str
    kind: str
    title: str
    reason: str
    priority: float
    relevance: float
    timeliness: float
    novelty: float
    continuity: float
    relationship_fit: float
    interruption_cost: float
    repetition_penalty: float
    earliest_send_at: datetime
    expires_at: datetime
    status: str = "pending"

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.kind.strip() or not self.title.strip():
            raise ValueError("candidate id, kind, and title must not be empty")
        for name in (
            "priority", "relevance", "timeliness", "novelty", "continuity",
            "relationship_fit", "interruption_cost", "repetition_penalty",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        for name in ("earliest_send_at", "expires_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SocialContext:
    now: datetime
    proactive_messages_today: int = 0
    last_proactive_at: datetime | None = None
    last_user_activity_at: datetime | None = None
    consecutive_ignored: int = 0
    paused: bool = False
    busy_until: datetime | None = None
    frequency_multiplier: float = 1.0
    preferred_contact_time: str | None = None
    technical_news_preferred: bool = False


@dataclass(frozen=True, slots=True)
class SocialPolicy:
    timezone: str = "Asia/Shanghai"
    quiet_hours_start: time = time(23, 30)
    quiet_hours_end: time = time(8, 30)
    max_daily_messages: int = 5
    min_interval_minutes: int = 90
    post_reply_cooldown_minutes: int = 60
    light_chat_after_hours: int = 24
    ignored_streak_pause_after: int = 2
    minimum_score: float = 0.55

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)
        if self.max_daily_messages < 0:
            raise ValueError("max_daily_messages must not be negative")
        if min(self.min_interval_minutes, self.post_reply_cooldown_minutes) < 0:
            raise ValueError("intervals must not be negative")
        if self.light_chat_after_hours < 1:
            raise ValueError("light_chat_after_hours must be positive")
        if self.ignored_streak_pause_after < 0:
            raise ValueError("ignored_streak_pause_after must not be negative")
        if not 0 <= self.minimum_score <= 1:
            raise ValueError("minimum_score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class CandidateScore:
    score: float
    components: dict[str, float]
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SocialDecision:
    selected: SocialCandidate | None
    scored: dict[str, CandidateScore] = field(default_factory=dict)
    suppressed_reason: str | None = None
    reason_codes: tuple[str, ...] = ()


class SocialEngine:
    _LOW_PRIORITY_KINDS = {"light_chat", "discovery_share", "topic_follow_up"}

    def evaluate(
        self,
        candidates: list[SocialCandidate],
        context: SocialContext,
        policy: SocialPolicy,
    ) -> SocialDecision:
        now = self._aware(context.now, "now").astimezone(ZoneInfo(policy.timezone))
        if context.frequency_multiplier < 1.0:
            raise ValueError("frequency_multiplier must be at least one")
        daily_limit = policy.max_daily_messages
        if context.frequency_multiplier > 1.0 and daily_limit > 0:
            daily_limit = max(1, (daily_limit + 1) // 2)
        effective_policy = replace(
            policy,
            max_daily_messages=daily_limit,
            min_interval_minutes=int(policy.min_interval_minutes * context.frequency_multiplier),
        )
        gates = (
            (context.paused, "user_paused"),
            (self._is_quiet(now.timetz().replace(tzinfo=None), policy), "quiet_hours"),
            (self._is_busy(context.busy_until, now), "user_busy"),
            (
                context.proactive_messages_today >= effective_policy.max_daily_messages,
                "daily_limit",
            ),
            (self._within_minimum_interval(context.last_proactive_at, now, effective_policy), "minimum_interval"),
            (self._within_reply_cooldown(context.last_user_activity_at, now, effective_policy), "recent_user_activity"),
        )
        for blocked, reason in gates:
            if blocked:
                return SocialDecision(selected=None, suppressed_reason=reason, reason_codes=(reason,))

        scored: dict[str, CandidateScore] = {}
        eligible: list[tuple[float, float, datetime, SocialCandidate]] = []
        for item in candidates:
            if item.id in scored:
                raise ValueError(f"duplicate candidate id: {item.id}")
            local_earliest = item.earliest_send_at.astimezone(ZoneInfo(policy.timezone))
            local_expiry = item.expires_at.astimezone(ZoneInfo(policy.timezone))
            candidate_reasons: list[str] = []
            if item.status not in {"candidate", "pending", "ready"}:
                candidate_reasons.append("inactive_candidate")
            if local_earliest > now:
                candidate_reasons.append("not_ready")
            if local_expiry <= now:
                candidate_reasons.append("expired")
            if (
                context.consecutive_ignored >= policy.ignored_streak_pause_after
                and item.kind in self._LOW_PRIORITY_KINDS
            ):
                candidate_reasons.append("ignored_streak_pause")
            score = self._score(item, context.consecutive_ignored, effective_policy)
            preference_boost = 0.0
            local_hour = now.hour
            if context.preferred_contact_time == "evening" and 18 <= local_hour < 23:
                preference_boost += 0.08
            if context.technical_news_preferred and item.kind == "discovery_share":
                preference_boost += 0.08
            score = max(0.0, min(1.0, score + preference_boost))
            scored[item.id] = CandidateScore(
                score=score,
                components={
                    "priority": item.priority,
                    "relevance": item.relevance,
                    "timeliness": item.timeliness,
                    "novelty": item.novelty,
                    "continuity": item.continuity,
                    "relationship_fit": item.relationship_fit,
                    "interruption_cost": item.interruption_cost,
                    "repetition_penalty": item.repetition_penalty,
                    "explicit_preference_boost": preference_boost,
                },
                reason_codes=tuple(candidate_reasons),
            )
            if not candidate_reasons:
                eligible.append((score, item.priority, local_expiry, item))

        if not eligible:
            return SocialDecision(
                selected=None,
                scored=scored,
                suppressed_reason="no_eligible_candidates",
                reason_codes=("no_eligible_candidates",),
            )

        eligible.sort(key=lambda row: (row[0], row[1], -row[2].timestamp(), row[3].id), reverse=True)
        score, _, _, selected = eligible[0]
        if score < policy.minimum_score:
            return SocialDecision(
                selected=None,
                scored=scored,
                suppressed_reason="below_score_threshold",
                reason_codes=("below_score_threshold",),
            )
        return SocialDecision(
            selected=selected,
            scored=scored,
            reason_codes=("selected_highest_score",),
        )

    @staticmethod
    def _aware(value: datetime, name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value

    @staticmethod
    def _is_quiet(now: time, policy: SocialPolicy) -> bool:
        start, end = policy.quiet_hours_start, policy.quiet_hours_end
        if start == end:
            return False
        if start < end:
            return start <= now < end
        return now >= start or now < end

    @classmethod
    def _is_busy(cls, busy_until: datetime | None, now: datetime) -> bool:
        if busy_until is None:
            return False
        return cls._aware(busy_until, "busy_until") > now

    @classmethod
    def _within_minimum_interval(
        cls, last_proactive_at: datetime | None, now: datetime, policy: SocialPolicy
    ) -> bool:
        if last_proactive_at is None:
            return False
        last = cls._aware(last_proactive_at, "last_proactive_at")
        return (now - last).total_seconds() < policy.min_interval_minutes * 60

    @classmethod
    def _within_reply_cooldown(
        cls, last_user_activity_at: datetime | None, now: datetime, policy: SocialPolicy
    ) -> bool:
        if last_user_activity_at is None:
            return False
        last = cls._aware(last_user_activity_at, "last_user_activity_at")
        elapsed = (now - last).total_seconds()
        return 0 <= elapsed < policy.post_reply_cooldown_minutes * 60

    @staticmethod
    def _score(
        item: SocialCandidate, consecutive_ignored: int, policy: SocialPolicy
    ) -> float:
        score = (
            0.10 * item.priority
            + 0.20 * item.relevance
            + 0.20 * item.timeliness
            + 0.15 * item.novelty
            + 0.20 * item.continuity
            + 0.15 * item.relationship_fit
            - 0.10 * item.interruption_cost
            - 0.25 * item.repetition_penalty
        )
        if item.kind in SocialEngine._LOW_PRIORITY_KINDS:
            score -= min(consecutive_ignored, 3) * 0.08
        return max(0.0, min(1.0, score))
