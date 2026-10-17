import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hermes_companion.social import (
    SocialCandidate,
    SocialContext,
    SocialEngine,
    SocialPolicy,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def candidate(kind="event_follow_up", **values):
    defaults = dict(
        id="c1",
        kind=kind,
        title="考试后跟进",
        reason="用户提到今天有考试",
        priority=0.9,
        relevance=0.9,
        timeliness=0.9,
        novelty=0.5,
        continuity=0.9,
        relationship_fit=0.8,
        interruption_cost=0.1,
        repetition_penalty=0.0,
        earliest_send_at=datetime(2026, 10, 17, 18, 0, tzinfo=SHANGHAI),
        expires_at=datetime(2026, 10, 18, 12, 0, tzinfo=SHANGHAI),
    )
    defaults.update(values)
    return SocialCandidate(**defaults)


class SocialEngineTests(unittest.TestCase):
    def test_quiet_hours_suppress_even_high_priority_candidate(self):
        now = datetime(2026, 10, 17, 23, 45, tzinfo=SHANGHAI)
        decision = SocialEngine().evaluate(
            [candidate()], SocialContext(now=now), SocialPolicy()
        )
        self.assertIsNone(decision.selected)
        self.assertEqual(decision.suppressed_reason, "quiet_hours")

    def test_daily_cap_and_minimum_interval_are_hard_limits(self):
        now = datetime(2026, 10, 17, 18, 0, tzinfo=SHANGHAI)
        policy = SocialPolicy(max_daily_messages=3, min_interval_minutes=180)
        capped = SocialEngine().evaluate(
            [candidate()], SocialContext(now=now, proactive_messages_today=3), policy
        )
        recent = SocialEngine().evaluate(
            [candidate()],
            SocialContext(
                now=now,
                proactive_messages_today=1,
                last_proactive_at=now - timedelta(minutes=179),
            ),
            policy,
        )
        self.assertEqual(capped.suppressed_reason, "daily_limit")
        self.assertEqual(recent.suppressed_reason, "minimum_interval")

    def test_default_reply_cooldown_is_one_hour(self):
        now = datetime(2026, 10, 17, 18, 0, tzinfo=SHANGHAI)
        decision = SocialEngine().evaluate(
            [candidate()],
            SocialContext(now=now, last_user_activity_at=now - timedelta(minutes=61)),
            SocialPolicy(),
        )
        self.assertEqual(decision.selected.id, "c1")

    def test_ignored_messages_pause_only_low_priority_social_chat(self):
        now = datetime(2026, 10, 17, 18, 0, tzinfo=SHANGHAI)
        context = SocialContext(now=now, consecutive_ignored=2)
        casual = candidate(kind="light_chat", id="casual", priority=0.5)
        event = candidate(kind="event_follow_up", id="event")
        decision = SocialEngine().evaluate([casual, event], context, SocialPolicy())
        self.assertEqual(decision.selected.id, "event")
        self.assertIn("ignored_streak_pause", decision.scored["casual"].reason_codes)

    def test_selects_highest_eligible_score_and_explains_it(self):
        now = datetime(2026, 10, 17, 18, 0, tzinfo=SHANGHAI)
        low = candidate(id="low", relevance=0.2, timeliness=0.1, priority=0.2)
        high = candidate(id="high", relevance=0.95, continuity=0.95, priority=1.0)
        decision = SocialEngine().evaluate(
            [low, high], SocialContext(now=now), SocialPolicy()
        )
        self.assertEqual(decision.selected.id, "high")
        self.assertGreaterEqual(decision.scored["high"].score, 0.55)
        self.assertIn("selected_highest_score", decision.reason_codes)

    def test_expired_and_not_yet_ready_candidates_are_excluded(self):
        now = datetime(2026, 10, 17, 18, 0, tzinfo=SHANGHAI)
        expired = candidate(id="expired", expires_at=now - timedelta(seconds=1))
        later = candidate(id="later", earliest_send_at=now + timedelta(hours=2))
        decision = SocialEngine().evaluate(
            [expired, later], SocialContext(now=now), SocialPolicy()
        )
        self.assertIsNone(decision.selected)
        self.assertEqual(decision.suppressed_reason, "no_eligible_candidates")

    def test_reduced_frequency_extends_cooldown_and_explicit_preferences_boost_matching_items(self):
        now = datetime(2026, 10, 17, 19, 0, tzinfo=SHANGHAI)
        policy = SocialPolicy(max_daily_messages=3, min_interval_minutes=180)
        reduced = SocialEngine().evaluate(
            [candidate()],
            SocialContext(
                now=now, last_proactive_at=now - timedelta(minutes=200),
                frequency_multiplier=2.0,
            ),
            policy,
        )
        discovery = candidate(kind="discovery_share", id="discovery")
        preferred = SocialEngine().evaluate(
            [discovery],
            SocialContext(
                now=now, preferred_contact_time="evening", technical_news_preferred=True,
            ),
            policy,
        )

        self.assertEqual(reduced.suppressed_reason, "minimum_interval")
        self.assertEqual(preferred.scored["discovery"].components["explicit_preference_boost"], 0.16)


if __name__ == "__main__":
    unittest.main()
