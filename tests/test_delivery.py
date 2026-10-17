import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from hermes_companion.actions import ProactiveActionRepository
from hermes_companion.events import CompanionEvent
from hermes_companion.delivery import (
    ActionDispatcher,
    DeliveryUncertain,
    HermesCLISender,
)
from hermes_companion.social import SocialCandidate, SocialPolicy
from hermes_companion.store import EventStore


ZONE = ZoneInfo("Asia/Shanghai")


def make_approved(store, now, *, approve=True):
    candidate = SocialCandidate(
        id="event:exam-1", kind="event_follow_up", title="考试",
        reason="考试后跟进", priority=.9, relevance=.9, timeliness=.9,
        novelty=.7, continuity=.9, relationship_fit=.8,
        interruption_cost=.2, repetition_penalty=.1,
        earliest_send_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(days=1),
    )
    action, _ = ProactiveActionRepository(store).create(
        idempotency_key=f"proactive:{candidate.id}",
        candidate_id=candidate.id,
        intent=candidate.kind,
        payload={
            "id": candidate.id, "kind": candidate.kind, "title": candidate.title,
            "reason": candidate.reason, "priority": candidate.priority,
            "relevance": candidate.relevance, "timeliness": candidate.timeliness,
            "novelty": candidate.novelty, "continuity": candidate.continuity,
            "relationship_fit": candidate.relationship_fit,
            "interruption_cost": candidate.interruption_cost,
            "repetition_penalty": candidate.repetition_penalty,
            "earliest_send_at": candidate.earliest_send_at.isoformat(),
            "expires_at": candidate.expires_at.isoformat(), "status": "candidate",
        },
    )
    actions = ProactiveActionRepository(store)
    actions.transition(action.id, "draft_ready", draft_text="考试考得怎么样？")
    if approve:
        actions.transition(action.id, "approved")
    return action.id


class FakeSender:
    def __init__(self, result="receipt-1", error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def send(self, message):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class DeliveryTests(unittest.TestCase):
    def test_live_mode_dispatches_generated_draft_without_manual_approval(self):
        now = datetime(2026, 10, 17, 14, 0, tzinfo=ZONE)
        with tempfile.TemporaryDirectory() as tmp, EventStore(Path(tmp) / "db.sqlite3") as store:
            action_id = make_approved(store, now, approve=False)
            sender = FakeSender()
            dispatcher = ActionDispatcher(
                store, SocialPolicy(timezone="Asia/Shanghai"), sender,
                enabled=True, target_user_id="wxid-primary", allowlisted_user_id="wxid-primary",
                auto_approve=True,
            )

            result = dispatcher.dispatch_one(now=now)

            self.assertEqual(result["state"], "delivered")
            self.assertEqual(sender.calls, 1)
            self.assertEqual(ProactiveActionRepository(store).get(action_id).state, "delivered")

    def test_approved_action_sends_once_and_records_delivery(self):
        now = datetime(2026, 10, 17, 14, 0, tzinfo=ZONE)
        with tempfile.TemporaryDirectory() as tmp, EventStore(Path(tmp) / "db.sqlite3") as store:
            action_id = make_approved(store, now)
            sender = FakeSender()
            dispatcher = ActionDispatcher(
                store, SocialPolicy(timezone="Asia/Shanghai"), sender,
                enabled=True, target_user_id="wxid-primary", allowlisted_user_id="wxid-primary",
            )

            result = dispatcher.dispatch_one(now=now)
            repeated = dispatcher.dispatch_one(now=now + timedelta(minutes=1))

            self.assertEqual(result["state"], "delivered")
            self.assertIsNone(repeated)
            self.assertEqual(sender.calls, 1)
            action = ProactiveActionRepository(store).get(action_id)
            self.assertEqual(action.state, "delivered")
            self.assertEqual(action.delivery_receipt, "receipt-1")
            self.assertEqual(action.attempt_count, 1)

    def test_unknown_delivery_is_terminal_and_never_retried(self):
        now = datetime(2026, 10, 17, 14, 0, tzinfo=ZONE)
        with tempfile.TemporaryDirectory() as tmp, EventStore(Path(tmp) / "db.sqlite3") as store:
            action_id = make_approved(store, now)
            sender = FakeSender(error=DeliveryUncertain())
            dispatcher = ActionDispatcher(
                store, SocialPolicy(timezone="Asia/Shanghai"), sender,
                enabled=True, target_user_id="wxid-primary", allowlisted_user_id="wxid-primary",
            )

            result = dispatcher.dispatch_one(now=now)
            repeated = dispatcher.dispatch_one(now=now + timedelta(minutes=1))

            self.assertEqual(result["state"], "delivery_unknown")
            self.assertIsNone(repeated)
            self.assertEqual(sender.calls, 1)
            self.assertEqual(ProactiveActionRepository(store).get(action_id).attempt_count, 1)

    def test_disabled_delivery_does_not_call_sender(self):
        now = datetime(2026, 10, 17, 14, 0, tzinfo=ZONE)
        with tempfile.TemporaryDirectory() as tmp, EventStore(Path(tmp) / "db.sqlite3") as store:
            action_id = make_approved(store, now)
            sender = FakeSender()
            dispatcher = ActionDispatcher(
                store, SocialPolicy(timezone="Asia/Shanghai"), sender,
                enabled=False, target_user_id="wxid-primary", allowlisted_user_id="wxid-primary",
            )

            result = dispatcher.dispatch_one(now=now)

            self.assertEqual(result["state"], "delivery_disabled")
            self.assertEqual(sender.calls, 0)
            self.assertEqual(ProactiveActionRepository(store).get(action_id).state, "approved")

    def test_hermes_cli_sender_uses_allowlisted_weixin_target_and_stdin(self):
        sender = HermesCLISender(
            "/venv/bin/hermes", target_user_id="wxid-primary",
            allowlisted_user_id="wxid-primary",
        )
        with patch("hermes_companion.delivery.subprocess.run") as run:
            run.return_value.returncode = 0
            receipt = sender.send("审阅通过的短消息")

        self.assertEqual(receipt, "hermes-send-exit-0")
        args, kwargs = run.call_args
        self.assertEqual(args[0][:4], ["/venv/bin/hermes", "send", "--to", "weixin:wxid-primary"])
        self.assertEqual(kwargs["input"], "审阅通过的短消息")
        self.assertNotIn("审阅通过的短消息", args[0])
        self.assertNotIn("shell", kwargs)

    def test_hermes_cli_nonzero_result_is_uncertain(self):
        sender = HermesCLISender(
            "/venv/bin/hermes", target_user_id="wxid-primary",
            allowlisted_user_id="wxid-primary",
        )
        with patch("hermes_companion.delivery.subprocess.run") as run:
            run.return_value.returncode = 1
            with self.assertRaises(DeliveryUncertain):
                sender.send("message")

    def test_silent_delivery_becomes_one_weak_ignore_signal_after_48_hours(self):
        now = datetime(2026, 10, 17, 14, 0, tzinfo=ZONE)
        with tempfile.TemporaryDirectory() as tmp, EventStore(Path(tmp) / "db.sqlite3") as store:
            action_id = make_approved(store, now)
            dispatcher = ActionDispatcher(
                store, SocialPolicy(timezone="Asia/Shanghai"), FakeSender(),
                enabled=True, target_user_id="wxid-primary", allowlisted_user_id="wxid-primary",
            )
            dispatcher.dispatch_one(now=now)

            count = dispatcher.update_feedback_state(now=now + timedelta(hours=49))

            self.assertEqual(count, 1)
            self.assertEqual(ProactiveActionRepository(store).get(action_id).state, "ignored")
            self.assertEqual(dispatcher.controls.consecutive_ignored("primary-user"), 1)

    def test_later_user_activity_resets_the_ignored_streak(self):
        now = datetime(2026, 10, 17, 14, 0, tzinfo=ZONE)
        with tempfile.TemporaryDirectory() as tmp, EventStore(Path(tmp) / "db.sqlite3") as store:
            action_id = make_approved(store, now)
            dispatcher = ActionDispatcher(
                store, SocialPolicy(timezone="Asia/Shanghai"), FakeSender(),
                enabled=True, target_user_id="wxid-primary", allowlisted_user_id="wxid-primary",
            )
            dispatcher.dispatch_one(now=now)
            later = now + timedelta(hours=49)
            dispatcher.update_feedback_state(now=later)
            store.publish(CompanionEvent(
                event_type="USER_MESSAGE_RECEIVED", user_id="primary-user",
                timestamp=later + timedelta(minutes=1), source="test", payload={"text": "最近在忙"},
            ))

            self.assertEqual(dispatcher.controls.consecutive_ignored("primary-user"), 0)
            self.assertEqual(ProactiveActionRepository(store).get(action_id).state, "ignored")


if __name__ == "__main__":
    unittest.main()
