from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from budget.evaluate_quotes import main
from budget.quote_bedrock import BedrockFallback, BedrockHTTPError
from budget.quote_hybrid import evaluate_pdf
from budget.test_quote_hybrid import pdf, payload


ENV = {"AWS_BEARER_TOKEN_BEDROCK": "fake-bedrock-token", "AWS_REGION": "us-east-1",
       "BEDROCK_MODEL_ID": "us.anthropic.claude-example-v1:0"}


@patch.dict("os.environ", ENV)
class BedrockTests(unittest.TestCase):
    def setUp(self):
        self.data = pdf("Total payable 118.00 ILS")
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.fallback = BedrockFallback([self.digest])
        self.response = Mock(status_code=200)
        self.response.json.return_value = {"stopReason": "end_turn", "output": {"message": {
            "role": "assistant", "content": [{"text": json.dumps(payload(excl=None))}]}}}

    def test_bearer_auth_and_converse_payload(self):
        with patch("budget.quote_bedrock.requests.post", return_value=self.response) as post:
            result = self.fallback(self.data, "Extract totals")
        self.assertEqual(result, payload(excl=None))
        url = post.call_args.args[0]
        self.assertEqual(url, "https://bedrock-runtime.us-east-1.amazonaws.com/model/us.anthropic.claude-example-v1%3A0/converse")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fake-bedrock-token")
        self.assertNotIn("x-api-key", kwargs["headers"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("output_config", kwargs["json"])
        document = kwargs["json"]["messages"][0]["content"][0]["document"]
        self.assertEqual(document["format"], "pdf")
        self.assertTrue(document["citations"]["enabled"])
        self.assertIn("bytes", document["source"])

    def test_cited_json_content_is_supported(self):
        self.response.json.return_value["output"]["message"]["content"] = [
            {"citationsContent": {"content": [{"text": json.dumps(payload(excl=None))}], "citations": []}}]
        with patch("budget.quote_bedrock.requests.post", return_value=self.response):
            self.assertEqual(self.fallback(self.data, "test"), payload(excl=None))

    def test_bedrock_response_passes_existing_validator(self):
        with patch("budget.quote_bedrock.requests.post", return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.final.extraction_status, "missing")
        self.assertEqual(result.fallback_outcome, "evaluated")

    def test_no_approval_means_no_request(self):
        with patch("budget.quote_bedrock.requests.post") as post:
            for approved in ([], [hashlib.sha256(b"different").hexdigest()]):
                with self.assertRaises(PermissionError):
                    BedrockFallback(approved)(self.data, "test")
            post.assert_not_called()

    def test_only_bedrock_environment_credentials_are_used(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "wrong", "AWS_ACCESS_KEY_ID": "wrong"}, clear=True), patch("budget.quote_bedrock.requests.post") as post:
            with self.assertRaises(ValueError):
                self.fallback(self.data, "test")
            post.assert_not_called()

    def test_unsafe_region_is_rejected_without_network(self):
        with patch.dict("os.environ", {"AWS_REGION": "us-east-1.attacker.example"}), patch("budget.quote_bedrock.requests.post") as post:
            with self.assertRaises(ValueError):
                self.fallback(self.data, "test")
            post.assert_not_called()

    def test_inference_profile_arn_is_url_encoded(self):
        arn = 'arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/example'
        with patch.dict('os.environ', {'BEDROCK_MODEL_ID': arn}), patch('budget.quote_bedrock.requests.post', return_value=self.response) as post:
            self.fallback(self.data, 'test')
        self.assertIn('arn%3Aaws%3Abedrock%3A', post.call_args.args[0])
        self.assertIn('application-inference-profile%2Fexample', post.call_args.args[0])

    def test_api_errors_do_not_leak_token_or_server_body(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                response = Mock(status_code=status, text="secret server body")
                with patch("budget.quote_bedrock.requests.post", return_value=response) as post:
                    result = evaluate_pdf(self.data, self.fallback)
                self.assertEqual(post.call_count, 1)
                self.assertIn(f"bedrock_http_{status}", result.final.issues)
                self.assertNotIn("fake-bedrock-token", str(asdict(result)))
                self.assertNotIn("secret server body", str(asdict(result)))
                self.assertEqual(result.final.quoted_total_incl_vat, result.rules.quoted_total_incl_vat)

    def test_incomplete_response_is_rejected(self):
        for reason in ("max_tokens", "guardrail_intervened", "content_filtered", "tool_use"):
            self.response.json.return_value["stopReason"] = reason
            with patch("budget.quote_bedrock.requests.post", return_value=self.response), self.assertRaises(ValueError):
                self.fallback(self.data, "test")

    def test_bad_json_is_rejected(self):
        self.response.json.return_value["output"]["message"]["content"] = [{"text": "Not JSON"}]
        with patch("budget.quote_bedrock.requests.post", return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.fallback_outcome, "rejected")

    def test_dry_run_does_not_call_bedrock(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'quote.pdf').write_bytes(self.data)
            with patch('budget.quote_bedrock.requests.post') as post, patch('sys.stdout', new_callable=io.StringIO):
                main([folder, '--provider', 'bedrock'])
                post.assert_not_called()

    def test_one_selected_approved_document_and_call_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'selected.pdf').write_bytes(self.data)
            Path(folder, 'other.pdf').write_bytes(self.data)
            with patch('budget.quote_bedrock.requests.post', return_value=self.response) as post, patch('sys.stdout', new_callable=io.StringIO) as output:
                main([folder, '--provider', 'bedrock', '--filename', 'selected.pdf', '--pretty', '--claude',
                      '--approve-sha256', self.digest, '--max-api-calls', '1'])
                self.assertEqual(post.call_count, 1)
                report = json.loads(output.getvalue())
                self.assertEqual(report['filename'], 'selected.pdf')
                self.assertEqual(report['provider'], 'bedrock')
