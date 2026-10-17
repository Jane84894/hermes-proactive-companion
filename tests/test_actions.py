import tempfile
import unittest
from pathlib import Path

from hermes_companion.actions import ProactiveActionRepository
from hermes_companion.store import EventStore


class ProactiveActionTests(unittest.TestCase):
    def test_creation_is_idempotent_and_unknown_delivery_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            actions = ProactiveActionRepository(store)
            action, created = actions.create(
                idempotency_key="proactive:event:exam-1",
                candidate_id="event:exam-1",
                intent="follow_up",
                payload={"title": "考试后跟进"},
            )
            duplicate, created_again = actions.create(
                idempotency_key="proactive:event:exam-1",
                candidate_id="event:exam-1",
                intent="follow_up",
                payload={"title": "考试后跟进"},
            )

            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(action.id, duplicate.id)
            self.assertTrue(actions.transition(action.id, "draft_ready"))
            self.assertTrue(actions.transition(action.id, "approved"))
            self.assertTrue(actions.transition(action.id, "generating"))
            self.assertTrue(actions.transition(action.id, "ready"))
            self.assertTrue(actions.transition(action.id, "sending"))
            self.assertTrue(actions.transition(action.id, "delivery_unknown"))
            self.assertFalse(actions.transition(action.id, "sending"))
            self.assertFalse(actions.transition(action.id, "delivered"))

    def test_rejects_transitions_that_skip_review_or_generation(self):
        with tempfile.TemporaryDirectory() as tmp, EventStore(
            Path(tmp) / "companion.db"
        ) as store:
            actions = ProactiveActionRepository(store)
            action, _ = actions.create(
                idempotency_key="proactive:topic:one",
                candidate_id="topic:one",
                intent="topic_follow_up",
                payload={"title": "延续话题"},
            )

            self.assertFalse(actions.transition(action.id, "sending"))
            self.assertEqual(actions.get(action.id).state, "candidate")


if __name__ == "__main__":
    unittest.main()
