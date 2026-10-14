from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pymupdf

from budget.evaluate_quotes import main
from budget.quote_extraction import ExtractionResult
from budget.quote_hybrid import ClaudeFallback, evaluate_pdf, validate_claude


def pdf(text="Total excluding VAT after discounts 100.00 ILS\nTotal payable 118.00 ILS"):
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), text)
        return doc.tobytes()


def payload(excl="100.00", incl="118.00", currency="ILS"):
    values = {"quoted_total_excl_vat": excl, "quoted_total_incl_vat": incl}
    return {**values, "quoted_currency": currency,
            "extraction_status": "success" if excl and incl else "missing",
            "evidence": {name: {"text": (f"Total excluding VAT after discounts {amount} {currency}"
                         if name.endswith("excl_vat") else f"Total payable {amount} {currency}"), "page": 1}
                         if amount is not None else None for name, amount in values.items()},
            "amount_currencies": {name: currency if amount is not None else None for name, amount in values.items()}}


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.data = pdf()
        self.response = payload()

    def test_valid_response_is_grounded(self):
        result = validate_claude(self.response, self.data)
        self.assertEqual(result.extraction_status, "success")
        self.assertEqual(result.quoted_total_excl_vat, Decimal("100.00"))

    def test_partial_response_never_calculates_missing_value(self):
        result = validate_claude(payload(excl=None), self.data)
        self.assertIsNone(result.quoted_total_excl_vat)
        self.assertEqual(result.extraction_status, "missing")

    def test_rejects_invalid_types_and_amounts(self):
        for value in (100.0, True, "NaN", "-100", "100.001", "1e2"):
            response = deepcopy(self.response)
            response["quoted_total_excl_vat"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_claude(response, self.data)

    def test_rejects_bad_page_and_missing_evidence(self):
        for page in (True, 0, 2, "1"):
            response = deepcopy(self.response)
            response["evidence"]["quoted_total_excl_vat"]["page"] = page
            with self.subTest(page=page), self.assertRaises(ValueError):
                validate_claude(response, self.data)
        response = deepcopy(self.response)
        response["evidence"]["quoted_total_excl_vat"] = None
        with self.assertRaises(ValueError):
            validate_claude(response, self.data)

    def test_rejects_hallucinated_amount_or_quote(self):
        for response in (payload(excl="99.00"), payload()):
            response["evidence"]["quoted_total_excl_vat"]["text"] = "Imaginary total 99.00 ILS"
            with self.assertRaises(ValueError):
                validate_claude(response, self.data)

    def test_currency_discrepancy_remains_ambiguous(self):
        response = payload()
        response["amount_currencies"]["quoted_total_excl_vat"] = "USD"
        response["quoted_currency"] = None
        response["extraction_status"] = "ambiguous"
        response["evidence"]["quoted_total_excl_vat"]["text"] = "Total excluding VAT after discounts 100.00 USD"
        result = validate_claude(response, pdf("Total excluding VAT after discounts 100.00 USD\nTotal payable 118.00 ILS"))
        self.assertIsNone(result.quoted_currency)
        self.assertEqual(result.extraction_status, "ambiguous")

    def test_scanned_evidence_requires_visual_review(self):
        with pymupdf.open() as doc:
            doc.new_page()
            data = doc.tobytes()
        result = validate_claude(self.response, data)
        self.assertEqual(result.extraction_status, "ambiguous")
        self.assertIn("visual_review_required:quoted_total_excl_vat", result.issues)

    def test_before_discount_cannot_be_accepted_as_after_discount(self):
        response = payload()
        response["evidence"]["quoted_total_excl_vat"]["text"] = "Total before discount 100.00 ILS"
        with self.assertRaises(ValueError):
            validate_claude(response, pdf("Total before discount 100.00 ILS\nDiscount 0.00 ILS\nTotal payable 118.00 ILS"))

    def test_unit_price_evidence_is_rejected(self):
        response = payload()
        response["evidence"]["quoted_total_excl_vat"]["text"] = "Unit price 100.00 ILS"
        with self.assertRaises(ValueError):
            validate_claude(response, pdf("Unit price 100.00 ILS\nTotal payable 118.00 ILS"))

    def test_generic_total_requires_semantic_review(self):
        response = payload()
        response["evidence"]["quoted_total_excl_vat"]["text"] = "Total 100.00 ILS"
        result = validate_claude(response, pdf("Total 100.00 ILS\nTotal payable 118.00 ILS"))
        self.assertEqual(result.extraction_status, "ambiguous")

    def test_success_never_calls_fallback(self):
        fallback = Mock()
        result = evaluate_pdf(self.data, fallback)
        fallback.assert_not_called()
        self.assertEqual(result.fallback_outcome, "not_needed")

    def test_dry_run_does_not_call_network(self):
        with patch("budget.quote_hybrid.requests.post") as post:
            result = evaluate_pdf(pdf("Total payable 118.00 ILS"))
            post.assert_not_called()
        self.assertEqual(result.fallback_outcome, "dry_run_candidate")

    def test_missing_first_stage_uses_mock_fallback(self):
        rules = ExtractionResult(extraction_status="missing")
        with patch("budget.quote_hybrid.extract_pdf", return_value=rules):
            result = evaluate_pdf(self.data, Mock(return_value=self.response))
        self.assertEqual(result.final.extraction_status, "success")

    def test_supported_rule_partial_is_preserved(self):
        rules = ExtractionResult(quoted_total_incl_vat=Decimal("118.00"), quoted_currency="ILS",
            extraction_status="missing", evidence={"quoted_total_incl_vat": {"text": "Total payable 118.00 ILS", "page": 1, "currency": "ILS"}})
        with patch("budget.quote_hybrid.extract_pdf", return_value=rules):
            result = evaluate_pdf(self.data, Mock(return_value=payload(incl=None)))
        self.assertEqual(result.final.quoted_total_incl_vat, Decimal("118.00"))
        self.assertEqual(result.final.extraction_status, "success")

    def test_conflict_preserves_rule_value_and_flags_review(self):
        rules = ExtractionResult(quoted_total_incl_vat=Decimal("119.00"), quoted_currency="ILS",
            extraction_status="missing", evidence={"quoted_total_incl_vat": {"text": "Total payable 119.00 ILS", "page": 1, "currency": "ILS"}})
        with patch("budget.quote_hybrid.extract_pdf", return_value=rules):
            result = evaluate_pdf(self.data, Mock(return_value=self.response))
        self.assertEqual(result.final.quoted_total_incl_vat, Decimal("119.00"))
        self.assertEqual(result.final.extraction_status, "ambiguous")

    def test_api_failure_preserves_partial_without_leaking_errors(self):
        data = pdf("Total payable 118.00 ILS")
        result = evaluate_pdf(data, Mock(side_effect=RuntimeError("SECRET")))
        self.assertEqual(result.final.quoted_total_incl_vat, Decimal("118.00"))
        self.assertNotIn("SECRET", str(asdict(result)))

    def test_approval_is_required_and_bound_to_bytes(self):
        with patch("budget.quote_hybrid.requests.post") as post:
            for fallback in (ClaudeFallback(), ClaudeFallback([hashlib.sha256(b"other").hexdigest()])):
                with self.assertRaises(PermissionError):
                    fallback(self.data, "test")
            post.assert_not_called()

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "mock-key", "ANTHROPIC_MODEL": "mock-model"})
    def test_mocked_http_request_uses_pdf_and_schema(self):
        response = Mock(status_code=200)
        response.json.return_value = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(self.response)}]}
        with patch("budget.quote_hybrid.requests.post", return_value=response) as post:
            output = ClaudeFallback([hashlib.sha256(self.data).hexdigest()])(self.data, "test")
        self.assertEqual(output, self.response)
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"]["output_config"]["format"]["type"], "json_schema")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["x-api-key"], "mock-key")

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "mock-key", "ANTHROPIC_MODEL": "mock-model"})
    def test_truncated_response_is_rejected(self):
        response = Mock(status_code=200)
        response.json.return_value = {"stop_reason": "max_tokens"}
        with patch("budget.quote_hybrid.requests.post", return_value=response), self.assertRaises(ValueError):
            ClaudeFallback([hashlib.sha256(self.data).hexdigest()])(self.data, "test")

    def test_cli_requires_approval_and_is_dry_by_default(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "quote.pdf").write_bytes(pdf("Total payable 118.00 ILS"))
            with patch("budget.quote_hybrid.requests.post") as post, patch("sys.stdout", new_callable=io.StringIO):
                main([folder])
                post.assert_not_called()
            with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                main([folder, "--claude"])

    def test_cli_call_budget_and_unapproved_documents(self):
        with tempfile.TemporaryDirectory() as folder:
            data = pdf("Total payable 118.00 ILS")
            for name in ('a.pdf', 'b.pdf'):
                Path(folder, name).write_bytes(data)
            Path(folder, 'unapproved.pdf').write_bytes(pdf('Total payable 120.00 ILS'))
            approval = hashlib.sha256(data).hexdigest()
            response = payload(excl=None)
            with patch('budget.evaluate_quotes.ClaudeFallback') as factory, patch('sys.stdout', new_callable=io.StringIO):
                factory.return_value.return_value = response
                main([folder, '--claude', '--approve-sha256', approval, '--max-api-calls', '1'])
                self.assertEqual(factory.return_value.call_count, 1)

    def test_unknown_currency_is_not_inherited_from_other_total(self):
        response = payload()
        response['amount_currencies']['quoted_total_excl_vat'] = None
        response['quoted_currency'] = None
        with patch('budget.quote_hybrid.extract_pdf', return_value=ExtractionResult(extraction_status='missing')):
            result = evaluate_pdf(self.data, Mock(return_value=response))
        self.assertEqual(result.final.extraction_status, 'ambiguous')
        self.assertIsNone(result.final.quoted_currency)

    def test_extra_vat_amount_field_is_rejected(self):
        response = deepcopy(self.response)
        response['vat_amount'] = '18.00'
        with self.assertRaises(ValueError):
            validate_claude(response, self.data)
