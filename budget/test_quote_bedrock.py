from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from budget.evaluate_quotes import main
from budget.quote_bedrock import BedrockFallback, BedrockHTTPError, _post_once, BEDROCK_PROMPT
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
        with patch("budget.quote_bedrock._post_once", return_value=self.response) as post:
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
        with patch("budget.quote_bedrock._post_once", return_value=self.response):
            self.assertEqual(self.fallback(self.data, "test"), payload(excl=None))

    def test_bedrock_response_passes_existing_validator(self):
        with patch("budget.quote_bedrock._post_once", return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.final.extraction_status, "missing")
        self.assertEqual(result.fallback_outcome, "evaluated")

    def test_no_approval_means_no_request(self):
        with patch("budget.quote_bedrock._post_once") as post:
            for approved in ([], [hashlib.sha256(b"different").hexdigest()]):
                with self.assertRaises(PermissionError):
                    BedrockFallback(approved)(self.data, "test")
            post.assert_not_called()

    def test_only_bedrock_environment_credentials_are_used(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "wrong", "AWS_ACCESS_KEY_ID": "wrong"}, clear=True), patch("budget.quote_bedrock._post_once") as post:
            with self.assertRaises(ValueError):
                self.fallback(self.data, "test")
            post.assert_not_called()

    def test_unsafe_region_is_rejected_without_network(self):
        with patch.dict("os.environ", {"AWS_REGION": "us-east-1.attacker.example"}), patch("budget.quote_bedrock._post_once") as post:
            with self.assertRaises(ValueError):
                self.fallback(self.data, "test")
            post.assert_not_called()

    def test_inference_profile_arn_is_url_encoded(self):
        arn = 'arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/example'
        with patch.dict('os.environ', {'BEDROCK_MODEL_ID': arn}), patch('budget.quote_bedrock._post_once', return_value=self.response) as post:
            self.fallback(self.data, 'test')
        self.assertIn('arn%3Aaws%3Abedrock%3A', post.call_args.args[0])
        self.assertIn('application-inference-profile%2Fexample', post.call_args.args[0])

    def test_api_errors_do_not_leak_token_or_server_body(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                response = Mock(status_code=status, text="secret server body")
                with patch("budget.quote_bedrock._post_once", return_value=response) as post:
                    result = evaluate_pdf(self.data, self.fallback)
                self.assertEqual(post.call_count, 1)
                self.assertIn(f"bedrock_http_{status}", result.final.issues)
                self.assertNotIn("fake-bedrock-token", str(asdict(result)))
                self.assertNotIn("secret server body", str(asdict(result)))
                self.assertEqual(result.final.quoted_total_incl_vat, result.rules.quoted_total_incl_vat)

    def test_incomplete_response_is_rejected(self):
        for reason in ("max_tokens", "guardrail_intervened", "content_filtered", "tool_use"):
            self.response.json.return_value["stopReason"] = reason
            with patch("budget.quote_bedrock._post_once", return_value=self.response), self.assertRaises(ValueError):
                self.fallback(self.data, "test")

    def test_bad_json_is_rejected(self):
        self.response.json.return_value["output"]["message"]["content"] = [{"text": "Not JSON"}]
        with patch("budget.quote_bedrock._post_once", return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.fallback_outcome, "rejected")

    def test_dry_run_does_not_call_bedrock(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'quote.pdf').write_bytes(self.data)
            with patch('budget.quote_bedrock._post_once') as post, patch('sys.stdout', new_callable=io.StringIO):
                main([folder, '--provider', 'bedrock'])
                post.assert_not_called()

    def test_one_selected_approved_document_and_call_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'selected.pdf').write_bytes(self.data)
            Path(folder, 'other.pdf').write_bytes(self.data)
            with patch('budget.quote_bedrock._post_once', return_value=self.response) as post, patch('sys.stdout', new_callable=io.StringIO) as output:
                main([folder, '--provider', 'bedrock', '--filename', 'selected.pdf', '--pretty', '--claude',
                      '--approve-sha256', self.digest, '--max-api-calls', '1'])
                self.assertEqual(post.call_count, 1)
                report = json.loads(output.getvalue())
                self.assertEqual(report['filename'], 'selected.pdf')
                self.assertEqual(report['provider'], 'bedrock')

    def test_configuration_diagnostic_never_exposes_environment_values(self):
        with patch.dict('os.environ', {'AWS_REGION': 'secret-invalid-host'}), patch('budget.quote_bedrock._post_once') as post:
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.diagnostic, {'stage': 'configuration', 'code': 'region_format_invalid'})
        self.assertNotIn('secret-invalid-host', str(asdict(result)))
        post.assert_not_called()

    def test_http_error_reports_allowlisted_aws_code_only(self):
        response = Mock(status_code=400)
        response.json.return_value = {'__type': 'ValidationException', 'message': 'secret PDF contents and fake-bedrock-token'}
        with patch('budget.quote_bedrock._post_once', return_value=response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.diagnostic, {'stage': 'http', 'code': 'bedrock_http_400',
            'http_status': 400, 'aws_error': 'ValidationException'})
        self.assertNotIn('secret PDF', str(asdict(result)))
        self.assertNotIn('fake-bedrock-token', str(asdict(result)))

    def test_markdown_json_is_identified_without_relaxing_parser(self):
        self.response.json.return_value['output']['message']['content'] = [
            {'text': '```json\n' + json.dumps(payload(excl=None)) + '\n```'}]
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.fallback_outcome, 'rejected')
        self.assertEqual(result.diagnostic['stage'], 'response_json')
        self.assertEqual(result.diagnostic['code'], 'model_output_not_json')
        self.assertTrue(result.diagnostic['markdown_fence'])

    def test_stop_reason_is_reported_safely(self):
        self.response.json.return_value['stopReason'] = 'max_tokens'
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.diagnostic, {'stage': 'response_envelope', 'code': 'response_not_complete', 'stop_reason': 'max_tokens'})

    def test_evidence_mismatch_identifies_validation_stage_and_field(self):
        response = payload(excl=None)
        response['evidence']['quoted_total_incl_vat']['text'] = 'Total payable 118.00 ILS fabricated detail'
        self.response.json.return_value['output']['message']['content'] = [{'text': json.dumps(response)}]
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.diagnostic, {'stage': 'validation', 'code': 'evidence_not_on_page', 'field': 'quoted_total_incl_vat'})
        self.assertNotIn('fabricated detail', str(result.diagnostic))

    def test_before_discount_rejection_is_identified(self):
        data = pdf('Total before discount 780.00 ILS\nDiscount 0.00 ILS\nTotal payable 920.40 ILS')
        response = payload(excl='780.00', incl='920.40')
        response['evidence']['quoted_total_excl_vat']['text'] = 'Total before discount 780.00 ILS'
        self.response.json.return_value['output']['message']['content'] = [{'text': json.dumps(response)}]
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(data, BedrockFallback([hashlib.sha256(data).hexdigest()]))
        self.assertEqual(result.diagnostic, {'stage': 'validation', 'code': 'non_final_or_unlabelled_evidence', 'field': 'quoted_total_excl_vat'})
        self.assertIsNone(result.final.quoted_total_excl_vat)

    def test_transport_exception_does_not_expose_request(self):
        import requests
        with patch('budget.quote_bedrock._post_once', side_effect=requests.Timeout('secret-key request body')):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.diagnostic, {'stage': 'transport', 'code': 'request_timeout'})
        self.assertNotIn('secret-key', str(asdict(result)))

    def test_preflight_cli_performs_no_network_and_prints_no_values(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('budget.quote_bedrock._post_once') as post, patch('sys.stdout', new_callable=io.StringIO) as output:
                main([folder, '--provider', 'bedrock', '--check-config'])
                report = json.loads(output.getvalue())
                self.assertFalse(report['diagnostic']['network_called'])
                self.assertFalse(report['diagnostic']['model_access_verified'])
                self.assertNotIn('fake-bedrock-token', output.getvalue())
                post.assert_not_called()

    def test_malformed_content_is_diagnosed_without_dumping_it(self):
        self.response.json.return_value['output']['message']['content'] = [{'citationsContent': None}]
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.diagnostic['stage'], 'response_content')
        self.assertEqual(result.diagnostic['code'], 'cited_content_shape_invalid')

    def test_explicit_environment_output_limit_512_is_respected(self):
        with patch.dict('os.environ', {'BEDROCK_MAX_OUTPUT_TOKENS': '512'}), patch('budget.quote_bedrock._post_once', return_value=self.response) as post:
            BedrockFallback([self.digest])(self.data, 'unused verbose prompt')
        self.assertEqual(post.call_args.kwargs['json']['inferenceConfig']['maxTokens'], 512)
        self.assertEqual(post.call_count, 1)
        instructions = post.call_args.kwargs['json']['messages'][0]['content'][1]['text']
        self.assertEqual(instructions, BEDROCK_PROMPT)
        self.assertNotIn('additionalProperties', instructions)

    def test_output_limit_env_and_explicit_override(self):
        with patch.dict('os.environ', {'BEDROCK_MAX_OUTPUT_TOKENS': '256'}):
            self.assertEqual(BedrockFallback().max_output_tokens, 256)
            self.assertEqual(BedrockFallback(max_output_tokens=384).max_output_tokens, 384)
        for value in ('bad', 0, -1, True, 4097, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BedrockFallback(max_output_tokens=value)

    def test_usage_contains_only_nonnegative_integer_counts(self):
        self.response.json.return_value['usage'] = {'inputTokens': 1800, 'outputTokens': 210, 'totalTokens': 2010,
            'cacheReadInputTokens': 0, 'cacheWriteInputTokens': True, 'secret': 'private', 'other': {'text': 'private'}}
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            self.fallback(self.data, 'test')
        self.assertEqual(self.fallback.usage, {'inputTokens': 1800, 'outputTokens': 210,
                                             'totalTokens': 2010, 'cacheReadInputTokens': 0})

    def test_truncated_response_reports_usage_and_is_not_retried(self):
        self.response.json.return_value['usage'] = {'inputTokens': 1000, 'outputTokens': 512, 'totalTokens': 1512}
        self.response.json.return_value['stopReason'] = 'max_tokens'
        with patch('budget.quote_bedrock._post_once', return_value=self.response) as post:
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.fallback_outcome, 'rejected')
        self.assertEqual(self.fallback.usage['outputTokens'], 512)
        self.assertEqual(post.call_count, 1)

    def test_http_adapter_has_zero_retries_and_forbids_redirects(self):
        with patch('budget.quote_bedrock.requests.Session') as factory:
            session = factory.return_value.__enter__.return_value
            _post_once('https://example.invalid', allow_redirects=True)
        adapter = session.mount.call_args.args[1]
        self.assertEqual(adapter.max_retries.total, 0)
        self.assertEqual(session.post.call_count, 1)
        self.assertFalse(session.post.call_args.kwargs['allow_redirects'])

    def test_cli_reports_usage_and_configured_limit(self):
        self.response.json.return_value['usage'] = {'inputTokens': 1000, 'outputTokens': 150, 'totalTokens': 1150}
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'quote.pdf').write_bytes(self.data)
            with patch('budget.quote_bedrock._post_once', return_value=self.response) as post, patch('sys.stdout', new_callable=io.StringIO) as output:
                main([folder, '--provider', 'bedrock', '--claude', '--approve-sha256', self.digest,
                      '--max-api-calls', '1', '--max-output-tokens', '384'])
            report = json.loads(output.getvalue())
            self.assertEqual(report['api_usage']['inputTokens'], 1000)
            self.assertEqual(report['max_output_tokens'], 384)
            self.assertEqual(report['sent_pdf_pages'], 1)
            self.assertEqual(post.call_count, 1)

    def test_cli_limit_one_prevents_second_attempt_after_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ('a.pdf', 'b.pdf'):
                Path(folder, name).write_bytes(self.data)
            response = Mock(status_code=429)
            with patch('budget.quote_bedrock._post_once', return_value=response) as post, patch('sys.stdout', new_callable=io.StringIO):
                main([folder, '--provider', 'bedrock', '--claude', '--approve-sha256', self.digest, '--max-api-calls', '1'])
            self.assertEqual(post.call_count, 1)

    def test_bedrock_cli_refuses_more_than_one_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('budget.quote_bedrock._post_once') as post, patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
                main([folder, '--provider', 'bedrock', '--max-api-calls', '2'])
            post.assert_not_called()

    def test_default_output_limit_is_1024(self):
        with patch.dict('os.environ', ENV, clear=True):
            self.assertEqual(BedrockFallback().max_output_tokens, 1024)

    def test_reasoning_and_cited_answer_are_supported_without_exposing_reasoning(self):
        self.response.json.return_value['output']['message']['content'] = [
            {'reasoningContent': {'reasoningText': {'text': 'PRIVATE REASONING', 'signature': 'PRIVATE SIGNATURE'}}},
            {'citationsContent': {'content': [{'text': json.dumps(payload(excl=None))}], 'citations': [{'sourceContent': [{'text': 'PRIVATE SOURCE'}]}]}}]
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.fallback_outcome, 'evaluated')
        self.assertEqual(self.fallback.response_blocks, {'reasoningContent': 1, 'citationsContent': 1})
        self.assertNotIn('PRIVATE', str(asdict(result)))
        self.assertNotIn('PRIVATE', str(self.fallback.response_blocks))

    def test_redacted_reasoning_and_plain_answer_are_supported(self):
        self.response.json.return_value['output']['message']['content'].insert(0,
            {'reasoningContent': {'redactedContent': 'cHJpdmF0ZQ=='}})
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.fallback_outcome, 'evaluated')
        self.assertEqual(self.fallback.response_blocks['reasoningContent'], 1)

    def test_split_plain_and_cited_json_is_combined_in_original_order(self):
        answer = json.dumps(payload(excl=None))
        self.response.json.return_value['output']['message']['content'] = [
            {'text': answer[:35]},
            {'citationsContent': {'content': [{'text': answer[35:80]}, {'text': answer[80:]}], 'citations': []}}]
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            self.assertEqual(self.fallback(self.data, 'test'), payload(excl=None))

    def test_cache_metadata_is_explicitly_counted(self):
        self.response.json.return_value['output']['message']['content'].insert(0,
            {'cachePoint': {'type': 'default', 'ttl': '5m'}})
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            self.fallback(self.data, 'test')
        self.assertEqual(self.fallback.response_blocks, {'cachePoint': 1, 'text': 1})

    def test_reasoning_alone_cannot_be_used_as_extraction_answer(self):
        self.response.json.return_value['output']['message']['content'] = [
            {'reasoningContent': {'reasoningText': {'text': json.dumps(payload(excl=None))}}}]
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.fallback_outcome, 'rejected')
        self.assertEqual(result.diagnostic['code'], 'response_text_empty')

    def test_known_but_unexpected_block_types_are_named_and_rejected(self):
        for kind in ('toolUse', 'toolResult', 'image', 'audio', 'video', 'document', 'searchResult', 'guardContent'):
            with self.subTest(kind=kind):
                self.response.json.return_value['output']['message']['content'] = [{kind: {'private': 'SECRET'}}]
                with patch('budget.quote_bedrock._post_once', return_value=self.response):
                    result = evaluate_pdf(self.data, self.fallback)
                self.assertEqual(result.diagnostic['code'], 'content_block_unsupported')
                self.assertEqual(result.diagnostic['block_types'], [kind])
                self.assertEqual(result.diagnostic['block_index'], 0)
                self.assertNotIn('SECRET', str(asdict(result)))

    def test_unknown_key_is_not_logged_or_silently_ignored(self):
        self.response.json.return_value['output']['message']['content'].append({'PRIVATE KEY': 'PRIVATE RESPONSE'})
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.fallback_outcome, 'rejected')
        self.assertEqual(result.diagnostic['block_index'], 1)
        self.assertEqual(result.diagnostic['block_types'], [])
        self.assertEqual(result.diagnostic['unknown_member_count'], 1)
        self.assertNotIn('PRIVATE', str(asdict(result)))

    def test_invalid_union_rejects_even_when_answer_is_present(self):
        self.response.json.return_value['output']['message']['content'] = [
            {'text': json.dumps(payload(excl=None)), 'reasoningContent': {'reasoningText': {'text': 'private'}}}]
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.diagnostic['code'], 'content_union_invalid')
        self.assertEqual(result.diagnostic['member_count'], 2)

    def test_malformed_reasoning_is_rejected(self):
        for reasoning in ({'reasoningText': {'signature': 'private'}}, {'redactedContent': 'not base64!'},
                          {'reasoningText': {'text': 'private'}, 'redactedContent': 'cHJpdmF0ZQ=='}):
            with self.subTest(reasoning=reasoning):
                self.response.json.return_value['output']['message']['content'].insert(0, {'reasoningContent': reasoning})
                with patch('budget.quote_bedrock._post_once', return_value=self.response):
                    result = evaluate_pdf(self.data, self.fallback)
                self.assertEqual(result.diagnostic['code'], 'reasoning_content_shape_invalid')
                self.response.json.return_value['output']['message']['content'].pop(0)

    def test_valid_wrapper_does_not_bypass_extraction_validation(self):
        answer = payload(excl=None)
        answer['evidence']['quoted_total_incl_vat']['text'] = 'Total payable 118.00 ILS invented words'
        self.response.json.return_value['output']['message']['content'] = [
            {'reasoningContent': {'reasoningText': {'text': 'private'}}},
            {'citationsContent': {'content': [{'text': json.dumps(answer)}]}}]
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.diagnostic['stage'], 'validation')
        self.assertEqual(result.diagnostic['code'], 'evidence_not_on_page')

    def test_truncated_reasoning_is_counted_but_never_parsed_or_exposed(self):
        self.response.json.return_value['stopReason'] = 'max_tokens'
        self.response.json.return_value['usage'] = {'inputTokens': 3243, 'outputTokens': 1024, 'totalTokens': 4267}
        self.response.json.return_value['output']['message']['content'] = [
            {'reasoningContent': {'reasoningText': {'text': 'PRIVATE REASONING'}}},
            {'text': '{"quoted_total_incl_vat":'}]
        with patch('budget.quote_bedrock._post_once', return_value=self.response) as post:
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.diagnostic['code'], 'response_not_complete')
        self.assertEqual(self.fallback.response_blocks, {'reasoningContent': 1, 'text': 1})
        self.assertNotIn('PRIVATE', str(asdict(result)))
        self.assertEqual(post.call_count, 1)

    def test_truncated_cited_json_is_rejected_even_if_it_looks_complete(self):
        self.response.json.return_value['stopReason'] = 'max_tokens'
        self.response.json.return_value['output']['message']['content'] = [
            {'citationsContent': {'content': [{'text': json.dumps(payload(excl=None))}]}}]
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        self.assertEqual(result.fallback_outcome, 'rejected')
        self.assertEqual(result.diagnostic['stop_reason'], 'max_tokens')
        self.assertEqual(self.fallback.response_blocks, {'citationsContent': 1})

    def test_request_does_not_enable_thinking_implicitly_in_code(self):
        with patch('budget.quote_bedrock._post_once', return_value=self.response) as post:
            self.fallback(self.data, 'test')
        body = post.call_args.kwargs['json']
        self.assertNotIn('additionalModelRequestFields', body)
        self.assertNotIn('thinking', body)
        self.assertNotIn('output_config', body)

    def test_explicit_disable_is_sent_only_for_verified_haiku_models(self):
        for model in ('anthropic.claude-haiku-4-5-20251001-v1:0', 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
                      'global.anthropic.claude-haiku-5-5'):
            with self.subTest(model=model), patch.dict('os.environ', {'BEDROCK_MODEL_ID': model}), patch('budget.quote_bedrock._post_once', return_value=self.response) as post:
                BedrockFallback([self.digest], max_output_tokens=1024, disable_thinking=True)(self.data, 'test')
            fields = post.call_args.kwargs['json']['additionalModelRequestFields']
            self.assertEqual(fields, {'thinking': {'type': 'disabled'}})
            self.assertEqual(post.call_args.kwargs['json']['inferenceConfig']['maxTokens'], 1024)

    def test_disable_refuses_unknown_model_or_opaque_profile_without_network(self):
        for model in ('anthropic.claude-haiku-future', 'arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/example'):
            with self.subTest(model=model), patch.dict('os.environ', {'BEDROCK_MODEL_ID': model}), patch('budget.quote_bedrock._post_once') as post:
                result = evaluate_pdf(self.data, BedrockFallback([self.digest], disable_thinking=True))
            self.assertEqual(result.diagnostic['code'], 'thinking_disable_model_unverified')
            post.assert_not_called()

    def test_exact_haiku_55_cli_reports_disabled_policy_and_keeps_1024(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'quote.pdf').write_bytes(self.data)
            with patch.dict('os.environ', {'BEDROCK_MODEL_ID': 'global.anthropic.claude-haiku-5-5'}), patch('budget.quote_bedrock._post_once', return_value=self.response) as post, patch('sys.stdout', new_callable=io.StringIO) as output:
                main([folder, '--provider', 'bedrock', '--claude', '--approve-sha256', self.digest,
                      '--disable-thinking', '--max-api-calls', '1', '--max-output-tokens', '1024'])
            report = json.loads(output.getvalue())
            self.assertEqual(report['thinking_policy'], 'disabled')
            self.assertEqual(report['max_output_tokens'], 1024)
            self.assertEqual(post.call_count, 1)

    def test_haiku_55_low_effort_with_disabled_thinking(self):
        with patch.dict('os.environ', {'BEDROCK_MODEL_ID': 'global.anthropic.claude-haiku-5-5'}), patch('budget.quote_bedrock._post_once', return_value=self.response) as post:
            BedrockFallback([self.digest], disable_thinking=True, effort='low', max_output_tokens=1024)(self.data, 'test')
        body = post.call_args.kwargs['json']
        self.assertEqual(body['additionalModelRequestFields'], {'thinking': {'type': 'disabled'}, 'output_config': {'effort': 'low'}})
        self.assertEqual(body['inferenceConfig']['maxTokens'], 1024)
        self.assertEqual(post.call_count, 1)

    def test_effort_is_not_placed_inside_thinking(self):
        with patch.dict('os.environ', {'BEDROCK_MODEL_ID': 'anthropic.claude-haiku-5-5'}):
            fields = BedrockFallback(disable_thinking=True, effort='low').model_request_fields('anthropic.claude-haiku-5-5')
        self.assertNotIn('effort', fields['thinking'])
        self.assertEqual(fields['output_config'], {'effort': 'low'})

    def test_invalid_effort_and_incompatible_disabled_combination_are_rejected(self):
        with self.assertRaises(ValueError):
            BedrockFallback(effort='unknown')
        for effort in ('xhigh', 'max'):
            with self.subTest(effort=effort), patch.dict('os.environ', {'BEDROCK_MODEL_ID': 'global.anthropic.claude-haiku-5-5'}), patch('budget.quote_bedrock._post_once') as post:
                result = evaluate_pdf(self.data, BedrockFallback([self.digest], disable_thinking=True, effort=effort))
            self.assertEqual(result.diagnostic['code'], 'effort_incompatible_with_disabled_thinking')
            post.assert_not_called()

    def test_effort_is_refused_for_older_haiku_and_unknown_profiles(self):
        for model in ('us.anthropic.claude-haiku-4-5-20251001-v1:0', 'arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/example'):
            with self.subTest(model=model), patch.dict('os.environ', {'BEDROCK_MODEL_ID': model}), patch('budget.quote_bedrock._post_once') as post:
                result = evaluate_pdf(self.data, BedrockFallback([self.digest], effort='low'))
            self.assertEqual(result.diagnostic['code'], 'effort_model_unverified')
            post.assert_not_called()

    def test_environment_effort_and_explicit_override(self):
        with patch.dict('os.environ', {'BEDROCK_EFFORT': 'medium'}):
            self.assertEqual(BedrockFallback().effort, 'medium')
            self.assertEqual(BedrockFallback(effort='low').effort, 'low')

    def test_truncated_response_reports_lengths_without_content(self):
        self.response.json.return_value['stopReason'] = 'max_tokens'
        self.response.json.return_value['output']['message']['content'] = [
            {'reasoningContent': {'reasoningText': {'text': 'SECRET', 'signature': 'DO NOT COUNT SIGNATURE'}}},
            {'text': 'שלום'},
            {'text': 'private'},
            {'citationsContent': {'content': [{'text': 'ab'}, {'text': 'c'}]}},
            {'reasoningContent': {'redactedContent': 'YWJj'}}]
        with patch('budget.quote_bedrock._post_once', return_value=self.response):
            result = evaluate_pdf(self.data, self.fallback)
        lengths = self.fallback.response_lengths
        self.assertEqual(lengths['reasoning_text'], {'blocks': 1, 'lengths': [6], 'total_length': 6})
        self.assertEqual(lengths['text'], {'blocks': 2, 'lengths': [4, 7], 'total_length': 11})
        self.assertEqual(lengths['cited_text']['lengths'], [3])
        self.assertEqual(lengths['redacted_reasoning_base64']['lengths'], [4])
        self.assertNotIn('SECRET', str(lengths))
        self.assertNotIn('DO NOT COUNT', str(lengths))
        self.assertEqual(result.fallback_outcome, 'rejected')

    def test_cli_effort_check_is_free_and_reports_requested_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict('os.environ', {'BEDROCK_MODEL_ID': 'global.anthropic.claude-haiku-5-5'}), patch('budget.quote_bedrock._post_once') as post, patch('sys.stdout', new_callable=io.StringIO) as output:
                main([folder, '--provider', 'bedrock', '--check-config', '--disable-thinking', '--effort', 'low', '--max-output-tokens', '1024'])
            diagnostic = json.loads(output.getvalue())['diagnostic']
            self.assertEqual(diagnostic['requested_effort'], 'low')
            self.assertEqual(diagnostic['thinking_policy'], 'disabled')
            self.assertEqual(diagnostic['max_output_tokens'], 1024)
            self.assertFalse(diagnostic['network_called'])
            post.assert_not_called()

    def test_cli_evaluation_reports_effort_and_response_lengths(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'quote.pdf').write_bytes(self.data)
            with patch.dict('os.environ', {'BEDROCK_MODEL_ID': 'global.anthropic.claude-haiku-5-5'}), patch('budget.quote_bedrock._post_once', return_value=self.response) as post, patch('sys.stdout', new_callable=io.StringIO) as output:
                main([folder, '--provider', 'bedrock', '--claude', '--approve-sha256', self.digest,
                      '--disable-thinking', '--effort', 'low', '--max-api-calls', '1', '--max-output-tokens', '1024'])
            report = json.loads(output.getvalue())
            self.assertEqual(report['requested_effort'], 'low')
            self.assertEqual(report['response_lengths']['text']['blocks'], 1)
            self.assertEqual(report['response_lengths']['reasoning_text']['blocks'], 0)
            self.assertEqual(post.call_count, 1)
