"""Daily token enforcement and anonymous model usage accounting."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .store import EventStore


class BudgetExceeded(RuntimeError):
    def __init__(self, retry_at: datetime, budget: str = "daily token"):
        super().__init__(f"{budget} budget reached")
        self.retry_at = retry_at
        self.budget = budget


class BudgetedModel:
    # Conservative overseas pay-as-you-go rates for MiMo-V2.6-Flash. Input
    # is priced as a cache miss; actual cache-hit cost can only be lower.
    INPUT_USD_PER_MILLION = 0.14
    OUTPUT_USD_PER_MILLION = 0.28

    def __init__(
        self,
        model: Any,
        store: EventStore,
        *,
        daily_token_budget: int,
        monthly_cost_budget_usd: float | None = None,
        timezone: str = "Asia/Shanghai",
        purpose: str = "memory_extraction",
    ):
        if daily_token_budget < 1:
            raise ValueError("daily_token_budget must be positive")
        if monthly_cost_budget_usd is not None and monthly_cost_budget_usd <= 0:
            raise ValueError("monthly_cost_budget_usd must be positive")
        self.model = model
        self.store = store
        self.daily_token_budget = daily_token_budget
        self.monthly_cost_budget_usd = monthly_cost_budget_usd
        self.timezone = ZoneInfo(timezone)
        self.purpose = purpose

    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        day_start = datetime.combine(current.date(), time.min, tzinfo=self.timezone)
        day_end = day_start + timedelta(days=1)
        estimate_prompt = self._estimate_tokens(system_prompt + "\n" + user_prompt)
        already_used = self.store.usage_between(day_start, day_end)
        if already_used + estimate_prompt + max_tokens > self.daily_token_budget:
            raise BudgetExceeded(day_end, "daily token")

        month_end = None
        if self.monthly_cost_budget_usd is not None:
            month_start = datetime.combine(
                current.date().replace(day=1), time.min, tzinfo=self.timezone
            )
            next_month_date = (month_start.date().replace(day=28) + timedelta(days=4)).replace(day=1)
            month_end = datetime.combine(next_month_date, time.min, tzinfo=self.timezone)
            already_cost = self.store.usage_cost_between(
                month_start,
                month_end,
                input_usd_per_million=self.INPUT_USD_PER_MILLION,
                output_usd_per_million=self.OUTPUT_USD_PER_MILLION,
            )
            reserved_cost = self._cost(estimate_prompt, max_tokens)
            if already_cost + reserved_cost > self.monthly_cost_budget_usd:
                raise BudgetExceeded(month_end, "monthly USD")

        try:
            result = self.model.generate_json(
                system_prompt, user_prompt, max_tokens=max_tokens
            )
        except Exception:
            usage = getattr(self.model, "last_usage", None)
            prompt_tokens = self._usage_value(usage, "prompt_tokens")
            completion_tokens = self._usage_value(usage, "completion_tokens")
            total_tokens = self._usage_value(usage, "total_tokens")
            if total_tokens <= 0:
                # The request may have reached the provider without a usable
                # response. Charge the full reserved output allowance locally.
                prompt_tokens = estimate_prompt
                completion_tokens = max_tokens
                total_tokens = prompt_tokens + completion_tokens
            self._record_usage(current, usage, prompt_tokens, completion_tokens, total_tokens)
            raise

        usage = getattr(self.model, "last_usage", None)
        prompt_tokens = self._usage_value(usage, "prompt_tokens")
        completion_tokens = self._usage_value(usage, "completion_tokens")
        total_tokens = self._usage_value(usage, "total_tokens")
        if total_tokens <= 0:
            prompt_tokens = estimate_prompt
            completion_tokens = self._estimate_tokens(
                json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            )
            total_tokens = prompt_tokens + completion_tokens
        self._record_usage(current, usage, prompt_tokens, completion_tokens, total_tokens)
        return result

    def _record_usage(
        self,
        occurred_at: datetime,
        usage: Any,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        estimated_cost = self._cost(prompt_tokens, completion_tokens)
        model_name = str(getattr(self.model, "model", "mimo-v2.6-flash"))
        self.store.record_model_usage(
            purpose=self.purpose,
            provider="xiaomi-mimo",
            model=model_name,
            request_id=self._usage_value(usage, "request_id", default=None),
            occurred_at=occurred_at,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            cost_status="estimated",
        )

    @classmethod
    def _cost(cls, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * cls.INPUT_USD_PER_MILLION
            + completion_tokens * cls.OUTPUT_USD_PER_MILLION
        ) / 1_000_000

    @staticmethod
    def _usage_value(usage: Any, name: str, default: Any = 0):
        if usage is None:
            return default
        if isinstance(usage, dict):
            return usage.get(name, default) or default
        return getattr(usage, name, default) or default

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        # Chinese and other non-ASCII characters are conservatively counted one each;
        # ordinary Latin text is approximated at four characters per token.
        estimate = sum(1.0 if ord(char) > 127 else 0.25 for char in text)
        return max(1, int(estimate + 0.999))
