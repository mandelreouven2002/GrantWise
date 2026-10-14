import io
import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pymupdf
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings

from budget.autofetch import attach_pending_files
from budget.models import ImportBatch, OrderFile, OrderRequest, PendingFile, PendingImport
from budget.quote_extraction import ExtractionResult
from budget.quote_storage import extract_order_quote


def pdf(text):
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), text)
        return doc.tobytes()


@override_settings(FX_AUTO_REFRESH=False)
class QuoteStorageTests(TestCase):
    def setUp(self):
        self.order = OrderRequest.objects.create(natural_key="quote-test", item_name="Reagent",
            amount_est=Decimal("123.45"), amount_currency="USD", amount_source="manual")
        User.objects.create_user("quote-user", password="test")
        self.client.login(username="quote-user", password="test")

    def store(self, text="Total excluding VAT 100.00 ILS\nTotal payable 118.00 ILS"):
        data = pdf(text)
        return OrderFile.objects.update_or_create(order=self.order,
            defaults={"data": data, "file_name": "quote.pdf", "size": len(data)})[0]

    def test_persistence_evidence_idempotence_and_funding_unchanged(self):
        self.store()
        self.assertEqual(extract_order_quote(self.order.pk)[0], "saved")
        self.order.refresh_from_db()
        self.assertEqual(self.order.quoted_total_excl_vat, Decimal("100.00"))
        self.assertEqual(self.order.quoted_total_incl_vat, Decimal("118.00"))
        self.assertEqual(self.order.quoted_currency, "ILS")
        self.assertEqual(self.order.extraction_status, "success")
        self.assertEqual(self.order.extraction_evidence["values"]["quoted_total_incl_vat"]["page"], 1)
        self.assertEqual((self.order.amount_est, self.order.amount_currency, self.order.amount_source),
                         (Decimal("123.45"), "USD", "manual"))
        with patch("budget.quote_storage.extract_pdf") as extractor:
            self.assertEqual(extract_order_quote(self.order.pk)[0], "unchanged")
            extractor.assert_not_called()

    def test_partial_results_and_missing_display(self):
        self.store("Total payable 118.00 ILS")
        extract_order_quote(self.order.pk)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.quoted_total_excl_vat)
        self.assertEqual(self.order.extraction_status, "missing")
        response = self.client.get("/orders/?year=all")
        self.assertContains(response, 'placeholder="—"')
        self.assertContains(response, 'value="118"')
        self.assertContains(response, 'value="ILS" selected')           # the currency has its own column
        self.assertNotContains(response, '<span>ILS</span>')
        self.assertContains(response, "לא נמצא בחילוץ")                 # the missing "before VAT" total

    def test_two_columns_and_traceability_display(self):
        self.store()
        extract_order_quote(self.order.pk)
        response = self.client.get("/orders/?year=all")
        self.assertContains(response, "סה״כ לפני מע״מ")
        self.assertContains(response, "סה״כ כולל מע״מ")
        self.assertContains(response, "עמוד 1")
        self.assertContains(response, "Total payable 118.00 ILS")

    def test_manual_edit_protects_both_amounts_even_when_forced(self):
        self.store()
        extract_order_quote(self.order.pk)
        response = self.client.post(f"/api/orders/{self.order.pk}/",
            data=json.dumps({"field": "quoted_total_incl_vat", "value": "119.00"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(extract_order_quote(self.order.pk, force=True)[0], "manual")
        self.order.refresh_from_db()
        self.assertEqual(self.order.quoted_total_incl_vat, Decimal("119.00"))
        self.assertTrue(self.order.quoted_manually_corrected)

    def test_direct_manual_change_is_detected(self):
        self.store()
        extract_order_quote(self.order.pk)
        OrderRequest.objects.filter(pk=self.order.pk).update(quoted_total_excl_vat=Decimal("99.00"))
        self.assertEqual(extract_order_quote(self.order.pk, force=True)[0], "manual")

    def test_manual_clear_and_currency_correction_are_protected(self):
        self.store()
        extract_order_quote(self.order.pk)
        for field, value in (("quoted_total_excl_vat", ""), ("quoted_currency", "eur")):
            response = self.client.post(f"/api/orders/{self.order.pk}/",
                data=json.dumps({"field": field, "value": value}), content_type="application/json")
            self.assertTrue(response.json()["ok"])
        self.assertEqual(extract_order_quote(self.order.pk, force=True)[0], "manual")
        self.order.refresh_from_db()
        self.assertIsNone(self.order.quoted_total_excl_vat)
        self.assertEqual(self.order.quoted_currency, "EUR")

    def test_changed_pdf_clears_stale_automatic_values(self):
        self.store()
        extract_order_quote(self.order.pk)
        self.store("Total payable 200.00 ILS")
        extract_order_quote(self.order.pk)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.quoted_total_excl_vat)
        self.assertEqual(self.order.quoted_total_incl_vat, Decimal("200.00"))

    def test_corrupt_pdf_is_failed_and_batch_continues(self):
        OrderFile.objects.create(order=self.order, file_name="broken.pdf", data=b"not a PDF")
        call_command("extract_order_quotes", save=True, stdout=io.StringIO())
        self.order.refresh_from_db()
        self.assertEqual(self.order.extraction_status, "failed")
        self.assertIsNone(self.order.quoted_total_incl_vat)

    def test_dry_run_does_not_save(self):
        self.store()
        call_command("extract_order_quotes", order_id=self.order.pk, stdout=io.StringIO())
        self.order.refresh_from_db()
        self.assertEqual(self.order.extraction_evidence, {})
        self.assertEqual(self.order.extraction_status, "")

    def test_newly_attached_pdf_is_extracted_automatically(self):
        pending = PendingImport.objects.create(table_key="orders", source="labsuit_orders", file_name="test.xlsx",
            data=b"", mode="merge", as_of=date.today())
        PendingFile.objects.create(pending=pending, natural_key=self.order.natural_key,
            file_name="quote.pdf", data=pdf("Total payable 118.00 ILS"))
        self.assertEqual(attach_pending_files(pending), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.quoted_total_incl_vat, Decimal("118.00"))
        self.assertEqual(self.order.extraction_status, "missing")

    def test_import_preserves_quotation_fields(self):
        import pandas as pd
        from budget.importers.labsuit_orders import keyed_rows, run
        row = {"ITEM_NAME": "Reagent", "DATE_REQUESTED": "2026-10-14", "REQUESTED_BY": "test@example.com"}
        self.order.natural_key = keyed_rows([row])[0][0]
        self.order.save(update_fields=["natural_key"])
        self.store()
        extract_order_quote(self.order.pk)
        self.order.refresh_from_db()
        evidence = self.order.extraction_evidence
        data = io.BytesIO()
        pd.DataFrame([row]).to_excel(data, index=False)
        run(data.getvalue(), "orders.xlsx", date.today(), mode="merge")
        self.order.refresh_from_db()
        self.assertEqual(self.order.quoted_total_excl_vat, Decimal("100.00"))
        self.assertEqual(self.order.extraction_evidence, evidence)

    def test_forced_failure_preserves_valid_result(self):
        self.store()
        extract_order_quote(self.order.pk)
        with patch("budget.quote_storage.extract_pdf", return_value=ExtractionResult(extraction_status="failed")):
            self.assertEqual(extract_order_quote(self.order.pk, force=True)[0], "preserved")
        self.order.refresh_from_db()
        self.assertEqual(self.order.quoted_total_incl_vat, Decimal("118.00"))

    def test_forced_partial_retry_does_not_erase_success(self):
        self.store()
        extract_order_quote(self.order.pk)
        with patch("budget.quote_storage.extract_pdf", return_value=ExtractionResult(
                quoted_total_incl_vat=Decimal("118.00"), quoted_currency="ILS", extraction_status="missing")):
            self.assertEqual(extract_order_quote(self.order.pk, force=True)[0], "preserved")
        self.order.refresh_from_db()
        self.assertEqual(self.order.quoted_total_excl_vat, Decimal("100.00"))

    def test_pdf_changed_during_extraction_does_not_save_old_result(self):
        stored = self.store()
        def changed(data):
            OrderFile.objects.filter(pk=stored.pk).update(data=pdf("Total payable 200.00 ILS"))
            return ExtractionResult(quoted_total_incl_vat=Decimal("118.00"), quoted_currency="ILS", extraction_status="missing")
        with patch("budget.quote_storage.extract_pdf", side_effect=changed):
            self.assertEqual(extract_order_quote(self.order.pk)[0], "changed_during_extraction")
        self.order.refresh_from_db()
        self.assertIsNone(self.order.quoted_total_incl_vat)

    def test_backfill_limit_skips_already_processed_orders(self):
        self.store()
        extract_order_quote(self.order.pk)
        other = OrderRequest.objects.create(natural_key="other", item_name="Other")
        OrderFile.objects.create(order=other, file_name="other.pdf", data=pdf("Total payable 200.00 ILS"))
        call_command("extract_order_quotes", save=True, limit=1, stdout=io.StringIO())
        other.refresh_from_db()
        self.assertEqual(other.quoted_total_incl_vat, Decimal("200.00"))

    def test_different_currency_evidence_is_displayed_per_amount(self):
        self.store("Total excluding VAT 100.00 USD\nTotal payable 400.00 ILS")
        extract_order_quote(self.order.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.extraction_status, "ambiguous")
        self.assertEqual(self.order.quoted_currency, "")
        response = self.client.get("/orders/?year=all")
        # no currency text inside the number cells: a marker with a hover, and the evidence of each amount
        self.assertNotContains(response, '<span>USD</span>')
        self.assertContains(response, "דורש בדיקה")
        self.assertContains(response, "Total excluding VAT 100.00 USD")
        self.assertContains(response, "Total payable 400.00 ILS")
