import io
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from wkly_rp_qc_v2.openrouter import OpenRouterError, complete_json


API_KEY = "unit-test-credential"


def response(body):
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(body).encode()

    return FakeResponse()


def chat(content='{"commitments":[]} ', finish_reason="stop", message_extra=None):
    message = {"role": "assistant", "content": content}
    message.update(message_extra or {})
    return {"choices": [{"message": message, "finish_reason": finish_reason}], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}


class OpenRouterTests(unittest.TestCase):
    @patch("wkly_rp_qc_v2.openrouter.urllib.request.urlopen")
    def test_posts_strict_json_schema_and_returns_parsed_payload_and_usage(self, urlopen):
        urlopen.return_value = response(chat())
        result = complete_json([{"role": "user", "content": "extract"}], {"type": "object"}, api_key=API_KEY, model="openai/gpt-5-mini")
        request = urlopen.call_args.args[0]
        sent = json.loads(request.data)
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {API_KEY}")
        self.assertEqual(sent["provider"], {"require_parameters": True})
        self.assertTrue(sent["response_format"]["json_schema"]["strict"])
        self.assertEqual(result["payload"], {"commitments": []})
        self.assertEqual(result["usage"]["prompt_tokens"], 4)
        self.assertNotIn("unit-test-credential", json.dumps(result["raw_response"]))
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 120)

    def test_http_errors_and_credential_in_error_text_are_sanitized(self):
        for status in (401, 429, 500):
            body = json.dumps({"error": {"message": f"failed with {API_KEY}"}}).encode()
            error = HTTPError("https://openrouter.ai", status, "error", {}, io.BytesIO(body))
            with patch("wkly_rp_qc_v2.openrouter.urllib.request.urlopen", side_effect=error):
                with self.subTest(status=status), self.assertRaises(OpenRouterError) as caught:
                    complete_json([], {}, api_key=API_KEY, model="test")
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn(API_KEY, str(caught.exception))
                self.assertNotIn(API_KEY, json.dumps(caught.exception.raw_response))

    def test_network_timeout_is_actionable(self):
        with patch("wkly_rp_qc_v2.openrouter.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(OpenRouterError, "timed out"):
                complete_json([], {}, api_key=API_KEY, model="test")

    def test_refusal_missing_content_and_missing_choices_fail_with_diagnostics(self):
        cases = [
            (chat(None, message_extra={"refusal": "cannot help"}), "refusal"),
            ({"choices": [{"message": {}, "finish_reason": "stop"}]}, "content"),
            ({"choices": [], "usage": {}}, "choices"),
        ]
        for body, message in cases:
            with patch("wkly_rp_qc_v2.openrouter.urllib.request.urlopen", return_value=response(body)):
                with self.subTest(message=message), self.assertRaisesRegex(OpenRouterError, message):
                    complete_json([], {}, api_key=API_KEY, model="test")

    def test_truncation_and_invalid_json_keep_raw_response_and_usage(self):
        for body, message in ((chat('{"partial":', finish_reason="length"), "truncated"), (chat("not json"), "valid JSON")):
            with patch("wkly_rp_qc_v2.openrouter.urllib.request.urlopen", return_value=response(body)):
                with self.subTest(message=message), self.assertRaisesRegex(OpenRouterError, message) as caught:
                    complete_json([], {}, api_key=API_KEY, model="test")
                self.assertIsNotNone(caught.exception.raw_response)
                self.assertEqual(caught.exception.usage["prompt_tokens"], 4)


if __name__ == "__main__":
    unittest.main()
