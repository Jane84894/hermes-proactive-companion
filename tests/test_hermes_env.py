import tempfile
import unittest
from pathlib import Path

from hermes_companion.hermes_env import load_hermes_environment


class HermesEnvironmentTests(unittest.TestCase):
    def test_loads_only_required_settings_and_requires_target_allowlist_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "WEIXIN_HOME_CHANNEL=wx-user\n"
                "WEIXIN_ALLOWED_USERS=wx-user,other-user\n"
                "XIAOMI_API_KEY='private-key-value'\n"
                "UNRELATED_SECRET=must-not-be-loaded\n"
            )
            environment = load_hermes_environment(path)

            self.assertEqual(environment.target_user_id, "wx-user")
            self.assertEqual(environment.api_key, "private-key-value")
            self.assertNotIn("private-key-value", repr(environment))
            self.assertNotIn("must-not-be-loaded", repr(environment))

            path.write_text(
                "WEIXIN_HOME_CHANNEL=wx-user\nWEIXIN_ALLOWED_USERS=another-user\n"
            )
            with self.assertRaisesRegex(ValueError, "not in the Weixin allowlist"):
                load_hermes_environment(path)


if __name__ == "__main__":
    unittest.main()
