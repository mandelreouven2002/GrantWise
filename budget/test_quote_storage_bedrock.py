"""Database integration tests: every external response is mocked."""
import hashlib
import io
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings

from .models import OrderFile, OrderRequest
from .quote_extraction import ExtractionResult
from .quote_storage import extract_order_quote, extract_order_quote_bedrock
from .test_quote_hybrid import pdf, payload


@override_settings(QUOTE_BEDROCK_ENABLED=True, FX_AUTO_REFRESH=False)
class BedrockStorageTests(TransactionTestCase):
    def setUp(self):
        self.order = OrderRequest.objects.create(natural_key="hybrid-test", item_name="Test")
        self.data = pdf()
        self.digest = hashlib.sha256(self.data).hexdigest()
        OrderFile.objects.create(order=self.order, data=self.data, size=len(self.data), file_name="test.pdf")
        self.preflight = patch("budget.quote_bedrock.BedrockFallback.preflight", return_value={"network_called": False})
        self.preflight.start()
        self.addCleanup(self.preflight.stop)
        # A conservative local partial result; the real hybrid validator is used.
        self.local = patch("budget.quote_storage.extract_pdf", return_value=ExtractionResult(
            quoted_total_incl_vat=Decimal("118.00"), quoted_currency="ILS", extraction_status="missing",
            evidence={"quoted_total_incl_vat": {"text": "Total payable 118.00 ILS", "page": 1, "currency": "ILS"}},
            missing_fields=["quoted_total_excl_vat"]))
        self.local.start()
        self.addCleanup(self.local.stop)

    def run_extraction(self, **kwargs):
        return extract_order_quote_bedrock(self.order.pk, approved_sha256=self.digest, **kwargs)

    def test_success_durable_claim_no_transaction_and_duplicate(self):
        def response(*args):
            self.assertFalse(connection.in_atomic_block)
            saved = OrderRequest.objects.get(pk=self.order.pk)
            self.assertEqual(saved.extraction_evidence["bedrock_attempts"][self.digest]["state"], "claimed")
            self.assertEqual(self.run_extraction()[0], "already_attempted")
            return payload()
        with patch("budget.quote_bedrock.BedrockFallback.__call__", side_effect=response) as api:
            self.assertEqual(self.run_extraction()[0], "evaluated")
            self.assertEqual(self.run_extraction()[0], "already_attempted")
            self.assertEqual(api.call_count, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.quoted_total_excl_vat, Decimal("100.00"))
        self.assertEqual(self.order.extraction_status, "success")

    def test_failure_preserves_partial_and_blocks_retry(self):
        with patch("budget.quote_bedrock.BedrockFallback.__call__", side_effect=TimeoutError("sensitive")) as api:
            self.assertEqual(self.run_extraction()[0], "rejected")
            self.assertEqual(self.run_extraction()[0], "already_attempted")
            self.assertEqual(api.call_count, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.quoted_total_incl_vat, Decimal("118.00"))
        self.assertNotIn("sensitive", str(self.order.extraction_evidence))
        self.assertEqual(self.order.extraction_evidence["bedrock_attempts"][self.digest]["state"], "uncertain")

    def test_partial_response_keeps_local_amount(self):
        with patch("budget.quote_bedrock.BedrockFallback.__call__", return_value=payload(excl=None)):
            self.assertEqual(self.run_extraction()[0], "evaluated")
        self.order.refresh_from_db()
        self.assertIsNone(self.order.quoted_total_excl_vat)
        self.assertEqual(self.order.quoted_total_incl_vat, Decimal("118.00"))
        self.assertEqual(self.order.extraction_status, "missing")

    def test_manual_changes_during_call_win(self):
        def response(*args):
            OrderRequest.objects.filter(pk=self.order.pk).update(quoted_total_incl_vat=Decimal("130.00"), quoted_manually_corrected=True)
            return payload()
        with patch("budget.quote_bedrock.BedrockFallback.__call__", side_effect=response):
            self.assertEqual(self.run_extraction()[0], "manual")
        self.order.refresh_from_db()
        self.assertEqual(self.order.quoted_total_incl_vat, Decimal("130.00"))
        self.assertIsNone(self.order.quoted_total_excl_vat)

    def test_manual_before_call_and_untracked_correction(self):
        self.order.quoted_total_excl_vat = Decimal("9.00")
        self.order.save(update_fields=["quoted_total_excl_vat"])
        with patch("budget.quote_bedrock.BedrockFallback.__call__") as api:
            self.assertEqual(self.run_extraction()[0], "manual")
            api.assert_not_called()

    def test_disabled_wrong_hash_limit_and_transaction_block_calls(self):
        with patch("budget.quote_bedrock.BedrockFallback.__call__") as api:
            with override_settings(QUOTE_BEDROCK_ENABLED=False), self.assertRaises(ValueError):
                self.run_extraction()
            with self.assertRaises(ValueError):
                extract_order_quote_bedrock(self.order.pk, approved_sha256="0" * 64)
            with self.assertRaises(ValueError):
                self.run_extraction(max_api_calls=2)
            with transaction.atomic(), self.assertRaises(ValueError):
                self.run_extraction()
            api.assert_not_called()

    def test_interruption_remains_claimed_and_local_force_preserves_ledger(self):
        with patch("budget.quote_bedrock.BedrockFallback.__call__", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_extraction()
        extract_order_quote(self.order.pk, force=True)
        self.assertEqual(self.run_extraction()[0], "already_attempted")
        self.order.refresh_from_db()
        self.assertEqual(self.order.extraction_evidence["bedrock_attempts"][self.digest]["state"], "claimed")

    def test_invalid_evidence_does_not_persist_candidate(self):
        bad = payload()
        bad["evidence"]["quoted_total_excl_vat"]["page"] = 2
        with patch("budget.quote_bedrock.BedrockFallback.__call__", return_value=bad):
            self.assertEqual(self.run_extraction()[0], "rejected")
        self.order.refresh_from_db()
        self.assertIsNone(self.order.quoted_total_excl_vat)

    def test_pdf_changed_during_call_preserves_values_and_attempt(self):
        def response(*args):
            OrderFile.objects.filter(order_id=self.order.pk).update(data=pdf("Replacement"))
            return payload()
        with patch("budget.quote_bedrock.BedrockFallback.__call__", side_effect=response):
            self.assertEqual(self.run_extraction()[0], "changed_during_extraction")
        self.order.refresh_from_db()
        self.assertIsNone(self.order.quoted_total_excl_vat)
        self.assertIn(self.digest, self.order.extraction_evidence["bedrock_attempts"])

    def test_complete_local_result_needs_no_api(self):
        self.local.stop()
        with patch("budget.quote_bedrock.BedrockFallback.__call__") as api:
            self.assertEqual(self.run_extraction()[0], "not_needed")
            api.assert_not_called()

    def test_compare_and_swap_loser_does_not_call_api(self):
        extract_order_quote(self.order.pk)
        with patch("django.db.models.query.QuerySet.update", return_value=0), patch(
                "budget.quote_bedrock.BedrockFallback.__call__") as api:
            self.assertEqual(self.run_extraction()[0], "claim_conflict")
            api.assert_not_called()

    def test_command_requires_explicit_single_saved_order(self):
        with self.assertRaises(CommandError):
            call_command("extract_order_quotes", bedrock=True, stdout=io.StringIO())
        with patch("budget.quote_bedrock.BedrockFallback.__call__", return_value=payload()) as api:
            call_command("extract_order_quotes", order_id=self.order.pk, save=True, bedrock=True,
                         approve_sha256=self.digest, max_api_calls=1, stdout=io.StringIO())
            self.assertEqual(api.call_count, 1)

    def test_command_processes_cached_missing_order_and_blocks_second_attempt(self):
        self.assertEqual(extract_order_quote(self.order.pk)[0], "saved")
        self.assertEqual(extract_order_quote(self.order.pk)[0], "unchanged")
        self.order.refresh_from_db()
        self.assertEqual(self.order.extraction_status, "missing")
        output = io.StringIO()
        with patch("budget.quote_storage.extract_pdf") as local, patch(
                "budget.quote_bedrock.BedrockFallback.__call__", return_value=payload()) as api:
            call_command("extract_order_quotes", order_id=self.order.pk, save=True,
                         bedrock=True, approve_sha256=self.digest, max_api_calls=1, stdout=output)
            self.assertIn(f"order={self.order.pk} evaluated", output.getvalue())
            persisted = OrderRequest.objects.get(pk=self.order.pk)
            self.assertEqual(persisted.quoted_total_excl_vat, Decimal("100.00"))
            self.assertEqual(persisted.quoted_total_incl_vat, Decimal("118.00"))
            self.assertEqual(persisted.quoted_currency, "ILS")
            self.assertEqual(persisted.extraction_status, "success")
            self.assertEqual(persisted.extraction_evidence["method"], "pymupdf+bedrock")
            self.assertEqual(persisted.extraction_evidence["values"]["quoted_total_excl_vat"]["page"], 1)
            second = io.StringIO()
            call_command("extract_order_quotes", order_id=self.order.pk, save=True,
                         bedrock=True, approve_sha256=self.digest, max_api_calls=1, stdout=second)
            self.assertIn("already_attempted", second.getvalue())
            self.assertEqual(api.call_count, 1)
            local.assert_not_called()
