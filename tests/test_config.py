import tempfile
import unittest
from pathlib import Path

from hermes_companion.config import AppConfig


class AppConfigTests(unittest.TestCase):
    def test_example_config_uses_higher_proactive_frequency(self):
        example = Path(__file__).resolve().parents[1] / "config.example.toml"

        config = AppConfig.load(example)

        self.assertEqual(config.max_daily_proactive_messages, 8)
        self.assertEqual(config.min_message_interval_minutes, 45)
        self.assertEqual(config.post_reply_cooldown_minutes, 30)
        self.assertEqual(config.light_chat_after_hours, 6)

    def test_loads_shadow_defaults_and_spec_social_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companion.toml"
            path.write_text(
                """
                [companion]
                enabled = true
                mode = "shadow"
                timezone = "Asia/Shanghai"
                [paths]
                hermes_state_db = "~/state.db"
                hermes_env_file = "~/.hermes/.env"
                companion_db = "~/companion/data.db"
                [social]
                quiet_hours_start = "23:30"
                quiet_hours_end = "08:30"
                max_daily_proactive_messages = 8
                min_message_interval_minutes = 45
                post_reply_cooldown_minutes = 30
                light_chat_after_hours = 6
                """
            )

            config = AppConfig.load(path)

            self.assertTrue(config.enabled)
            self.assertEqual(config.mode, "shadow")
            self.assertEqual(config.timezone, "Asia/Shanghai")
            self.assertEqual(config.quiet_hours_start, "23:30")
            self.assertEqual(config.max_daily_proactive_messages, 8)
            self.assertEqual(config.min_message_interval_minutes, 45)
            self.assertEqual(config.post_reply_cooldown_minutes, 30)
            self.assertEqual(config.light_chat_after_hours, 6)
            self.assertFalse(config.discovery_enabled)
            self.assertFalse(config.outbound_delivery_enabled)
            self.assertEqual(config.max_thought_queue_items, 100)
            self.assertEqual(config.max_thought_tasks_per_day, 20)
            self.assertEqual(config.max_thought_attempts, 3)
            self.assertEqual(config.hermes_state_db, Path.home() / "state.db")

    def test_thought_queue_limits_are_configurable_and_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companion.toml"
            path.write_text('[thoughts]\nmax_queue_length=40\nmax_tasks_per_day=8\nmax_attempts=2\n')
            config = AppConfig.load(path)
            self.assertEqual(config.max_thought_queue_items, 40)
            self.assertEqual(config.max_thought_tasks_per_day, 8)
            self.assertEqual(config.max_thought_attempts, 2)

            path.write_text('[thoughts]\nmax_queue_length=0\n')
            with self.assertRaisesRegex(ValueError, "max_queue_length"):
                AppConfig.load(path)

    def test_live_mode_requires_outbound_delivery_and_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companion.toml"
            path.write_text('[companion]\nmode = "live"\n')
            with self.assertRaisesRegex(ValueError, "requires outbound delivery"):
                AppConfig.load(path)

            path.write_text(
                '[companion]\nmode = "live"\noutbound_delivery_enabled = true\n'
            )
            config = AppConfig.load(path)
            self.assertEqual(config.mode, "live")
            self.assertTrue(config.outbound_delivery_enabled)

    def test_rejects_invalid_quiet_time_and_negative_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "companion.toml"
            path.write_text(
                '[social]\nquiet_hours_start="25:90"\nmax_daily_proactive_messages=-1\n'
            )
            with self.assertRaises(ValueError):
                AppConfig.load(path)


if __name__ == "__main__":
    unittest.main()
