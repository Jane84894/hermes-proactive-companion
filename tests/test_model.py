import unittest
from types import SimpleNamespace

from hermes_companion.model import MimoModel


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class MimoModelTests(unittest.TestCase):
    def test_requests_json_without_tools_using_the_configured_model(self):
        completions = FakeCompletions('{"memories": [], "events": []}')
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        model = MimoModel(
            api_key="test-secret",
            base_url="https://api.xiaomimimo.com/v1",
            model="mimo-v2.6-flash",
            client=client,
        )

        result = model.generate_json("system", "user text", max_tokens=321)

        self.assertEqual(result, {"memories": [], "events": []})
        self.assertEqual(completions.kwargs["model"], "mimo-v2.6-flash")
        self.assertEqual(completions.kwargs["max_completion_tokens"], 321)
        self.assertEqual(completions.kwargs["messages"][1]["content"], "user text")
        self.assertEqual(completions.kwargs["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", completions.kwargs)
        self.assertNotIn("test-secret", repr(completions.kwargs))

    def test_malformed_response_error_does_not_echo_model_output(self):
        private_response = "API key: do-not-echo"
        completions = FakeCompletions(private_response)
        model = MimoModel(
            api_key="test-secret",
            base_url="https://api.xiaomimimo.com/v1",
            model="mimo-v2.6-flash",
            client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        )
        with self.assertRaisesRegex(ValueError, "valid JSON") as raised:
            model.generate_json("system", "user text", max_tokens=100)
        self.assertNotIn(private_response, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
