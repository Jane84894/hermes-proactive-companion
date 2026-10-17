import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from hermes_companion.social import SocialCandidate, SocialContext, SocialEngine, SocialPolicy


DATASET = Path(__file__).parents[1] / "eval" / "companion-quality-v1.json"


class CompanionQualityDatasetTests(unittest.TestCase):
    def test_dataset_has_eight_required_human_review_categories(self):
        dataset = json.loads(DATASET.read_text(encoding="utf-8"))
        cases = dataset["cases"]
        self.assertEqual(len(cases), 8)
        self.assertEqual(len({case["id"] for case in cases}), 8)
        self.assertEqual(
            {case["category"] for case in cases},
            {
                "reasonable_concern", "boring_greeting", "repeated_message",
                "inappropriate_interruption", "topic_continuation",
                "technical_discovery_share", "long_no_reply", "explicit_stop",
            },
        )
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertIn("draft_input", case)
                self.assertIn("reference_draft", case)
                review = case["human_review"]
                self.assertEqual(review["reviewer"], "")
                self.assertIsNone(review["pass"])
                self.assertIn("naturalness_1_to_5", review)
                self.assertIn("grounding_1_to_5", review)
                self.assertIn("pressure_1_to_5", review)

    def test_social_engine_matches_expected_decision_for_every_fixture(self):
        dataset = json.loads(DATASET.read_text(encoding="utf-8"))
        engine = SocialEngine()
        for case in dataset["cases"]:
            fixture = case["evaluation"]
            now = datetime.fromisoformat(fixture["now"])
            source = fixture["candidate"]
            candidate = SocialCandidate(
                **source,
                earliest_send_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(hours=12),
            )
            context_values = dict(fixture["context"])
            for key in ("busy_until", "last_proactive_at", "last_user_activity_at"):
                if key in context_values:
                    context_values[key] = datetime.fromisoformat(context_values[key])
            decision = engine.evaluate(
                [candidate], SocialContext(now=now, **context_values), SocialPolicy()
            )
            expected = fixture["expected"]
            with self.subTest(case=case["id"]):
                self.assertEqual(decision.selected is not None, expected["selected"])
                if expected["selected"]:
                    self.assertEqual(decision.selected.id, source["id"])
                else:
                    self.assertEqual(decision.suppressed_reason, expected["reason"])
                    if "candidate_reason" in expected:
                        self.assertIn(
                            expected["candidate_reason"],
                            decision.scored[source["id"]].reason_codes,
                        )


if __name__ == "__main__":
    unittest.main()
