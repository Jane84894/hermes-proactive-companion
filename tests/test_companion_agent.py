import unittest
import json
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path

from hermes_companion.companion_agent import CompanionAgent
from hermes_companion.social import SocialCandidate


class FakeModel:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def generate_json(self, system_prompt, user_prompt, *, max_tokens):
        self.calls += 1
        self.last_prompt = user_prompt
        self.system_prompt = system_prompt
        return self.result


def candidate():
    now = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)
    return SocialCandidate(
        id="event:1", kind="event_follow_up", title="考试",
        reason="到期事件跟进", priority=.9, relevance=.8, timeliness=.9,
        novelty=.6, continuity=.8, relationship_fit=.8,
        interruption_cost=.2, repetition_penalty=.1,
        earliest_send_at=now, expires_at=now + timedelta(days=1),
    )


class CompanionAgentTests(unittest.TestCase):
    def test_generates_short_reviewable_draft_without_tools(self):
        model = FakeModel({
            "message": "考试考完了吗？如果你愿意，可以说说感觉怎么样。",
            "basis_ids": [],
            "grounded": True,
        })
        agent = CompanionAgent(model)

        result = agent.generate(candidate(), [])

        self.assertEqual(result.status, "generated")
        self.assertIn("考试", result.message)
        self.assertEqual(model.calls, 1)
        self.assertIn("must not call tools", model.system_prompt)

    def test_rejects_long_or_ungrounded_drafts(self):
        for response in (
            {"message": "今天我刚去了学校，看到很多新东西。", "basis_ids": [], "grounded": False},
            {"message": "这是一段很长的草稿。" * 80, "basis_ids": [], "grounded": True},
        ):
            with self.subTest(response=response["grounded"]):
                result = CompanionAgent(FakeModel(response)).generate(candidate(), [])
                self.assertEqual(result.status, "rejected")
                self.assertEqual(result.message, "")

    def test_uses_persona_profile_and_enforces_sentence_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            persona = Path(tmp) / "persona.toml"
            persona.write_text(
                '[persona]\nname="阿禾"\ndescription="年轻女性虚拟角色，好奇、开朗，偶尔调皮。"\n'
                '[style]\nlanguage="zh-CN"\ntone="温柔克制"\nmax_sentences=1\n'
                'avoid_repeated_greetings=true\navoid_emotional_pressure=true\n'
                'never_claim_real_world_experiences=true\n',
                encoding="utf-8",
            )
            model = FakeModel({
                "message": "考试考完了吗？感觉怎么样？",
                "basis_ids": [],
                "grounded": True,
            })
            result = CompanionAgent(model, persona).generate(candidate(), [])

            self.assertEqual(result.status, "rejected")
            self.assertIn("温柔克制", model.last_prompt)
            self.assertIn("年轻女性虚拟角色", model.last_prompt)
            self.assertIn("avoid_repeated_greetings", model.last_prompt)

    def test_default_persona_matches_companion_spec(self):
        persona = CompanionAgent._load_persona(None)

        self.assertIn("年轻女性虚拟角色", persona["description"])
        self.assertEqual(persona["language"], "zh-CN")
        self.assertTrue(persona["avoid_repeated_greetings"])
        self.assertTrue(persona["avoid_emotional_pressure"])
        self.assertTrue(persona["never_claim_real_world_experiences"])

    def test_includes_only_bounded_dynamic_state_in_grounded_prompt(self):
        model = FakeModel({
            "message": "ESP32 屏幕动画后来优化得怎么样了？",
            "basis_ids": [],
            "grounded": True,
        })
        agent = CompanionAgent(model)

        result = agent.generate(candidate(), [], {
            "current_interests": [{"title": "ESP32 屏幕", "current_state": "优化动画", "interest_score": 0.9}],
            "pending_thoughts": [{"title": "考试后跟进", "details": "后续询问"}],
            "proactive_paused": True,
            "private_internal_field": "must not be included",
        })

        prompt_data = json.loads(model.last_prompt.split("\n", 1)[1])
        self.assertEqual(result.status, "generated")
        self.assertEqual(prompt_data["companion_state"]["current_interests"][0]["title"], "ESP32 屏幕")
        self.assertNotIn("proactive_paused", prompt_data["companion_state"])
        self.assertNotIn("private_internal_field", prompt_data["companion_state"])
        self.assertIn("untrusted quoted data", model.system_prompt)

    def test_discovery_draft_appends_only_the_verified_source_url(self):
        source_url = "https://github.com/example/esp32-display"
        discovery = SocialCandidate(
            id="discovery:repo-1", kind="discovery_share", title="example/esp32-display",
            reason=f"与 ESP32 话题相关，来源链接：{source_url}",
            priority=.6, relevance=.8, timeliness=.7, novelty=.9, continuity=.6,
            relationship_fit=.7, interruption_cost=.3, repetition_penalty=0,
            earliest_send_at=datetime(2026, 10, 17, 10, tzinfo=timezone.utc),
            expires_at=datetime(2026, 10, 24, 10, tzinfo=timezone.utc),
        )
        result = CompanionAgent(FakeModel({
            "message": "看到一个和 ESP32 显示相关的开源项目，可能对你有参考。",
            "basis_ids": [], "grounded": True,
        })).generate(discovery, [])

        self.assertEqual(result.status, "generated")
        self.assertTrue(result.message.endswith(source_url))


if __name__ == "__main__":
    unittest.main()
