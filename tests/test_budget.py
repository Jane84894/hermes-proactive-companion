import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from hermes_companion.budget import BudgetExceeded, BudgetedModel
from hermes_companion.store import EventStore


class FakeModel:
    def __init__(self, result, usage=None):
        self.result = result
        self.last_usage = usage
        self.calls = 0

    def generate_json(self, system_prompt, user_prompt, *, max_tokens):
        self.calls += 1
        return self.result


class BudgetTests(unittest.TestCase):
    def test_records_provider_usage_and_stays_within_daily_token_budget(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            model = FakeModel(
                {"ok": True},
                usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
            )
            guarded = BudgetedModel(
                model, store, daily_token_budget=1000, timezone="Asia/Shanghai"
            )
            now = datetime(2026, 10, 17, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

            self.assertEqual(
                guarded.generate_json("system", "short", max_tokens=100, now=now),
                {"ok": True},
            )
            totals = store.usage_between(
                datetime(2026, 10, 17, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                datetime(2026, 10, 18, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
            self.assertEqual(totals, 70)
            cost = store.usage_cost_between(
                datetime(2026, 10, 17, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                datetime(2026, 10, 18, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                input_usd_per_million=0.14,
                output_usd_per_million=0.28,
            )
            self.assertAlmostEqual(cost, 0.0000126)

    def test_exhausted_budget_defers_without_calling_model(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            model = FakeModel({"ok": True})
            guarded = BudgetedModel(
                model, store, daily_token_budget=5, timezone="Asia/Shanghai"
            )
            now = datetime(2026, 10, 17, 23, 45, tzinfo=ZoneInfo("Asia/Shanghai"))
            with self.assertRaises(BudgetExceeded) as raised:
                guarded.generate_json("system prompt", "message", max_tokens=100, now=now)
            self.assertEqual(model.calls, 0)
            self.assertEqual(
                raised.exception.retry_at,
                datetime(2026, 10, 18, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

    def test_monthly_cost_cap_preflights_using_conservative_model_rates(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            model = FakeModel({"ok": True})
            guarded = BudgetedModel(
                model, store, daily_token_budget=1000,
                monthly_cost_budget_usd=0.00001,
                timezone="Asia/Shanghai",
            )
            now = datetime(2026, 10, 17, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

            with self.assertRaises(BudgetExceeded) as raised:
                guarded.generate_json("system", "short", max_tokens=100, now=now)

            self.assertEqual(model.calls, 0)
            self.assertEqual(
                raised.exception.retry_at,
                datetime(2026, 11, 1, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

    def test_failed_response_still_consumes_token_and_cost_budgets(self):
        class InvalidJsonModel(FakeModel):
            def generate_json(self, system_prompt, user_prompt, *, max_tokens):
                self.calls += 1
                self.last_usage = {
                    "prompt_tokens": 40,
                    "completion_tokens": 10,
                    "total_tokens": 50,
                    "request_id": "req-1",
                }
                raise ValueError("invalid JSON response")

        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            model = InvalidJsonModel(None)
            guarded = BudgetedModel(
                model, store, daily_token_budget=1000,
                monthly_cost_budget_usd=1,
                timezone="Asia/Shanghai",
            )
            now = datetime(2026, 10, 17, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                guarded.generate_json("system", "short", max_tokens=100, now=now)

            day_start = datetime(2026, 10, 17, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertEqual(store.usage_between(day_start, day_start.replace(day=18)), 50)


if __name__ == "__main__":
    unittest.main()
