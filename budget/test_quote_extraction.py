"""Standalone extraction tests; no database, scraper, or Claude calls."""
from decimal import Decimal
import unittest

from budget.quote_extraction import extract_pdf, extract_text_pages, validate_result


class QuoteExtractionTests(unittest.TestCase):
    def test_two_explicit_totals(self):
        result = extract_text_pages(['סה״כ לפני מע״מ 100.00 ₪\nסה״כ לתשלום 118.00 ₪'])
        self.assertEqual(result.extraction_status, 'success')
        self.assertEqual(result.quoted_total_excl_vat, Decimal('100.00'))
        self.assertEqual(result.quoted_total_incl_vat, Decimal('118.00'))
        self.assertEqual(result.quoted_currency, 'ILS')

    def test_partial_total_is_preserved_without_calculating_vat(self):
        result = extract_text_pages(['סה״כ לתשלום 118.00 ₪'])
        self.assertEqual(result.extraction_status, 'missing')
        self.assertIsNone(result.quoted_total_excl_vat)
        self.assertEqual(result.quoted_total_incl_vat, Decimal('118.00'))
        self.assertIn('quoted_total_excl_vat', result.missing_fields)

    def test_different_currencies_preserve_values_and_flag_discrepancy(self):
        result = extract_text_pages(['Total excluding VAT 100.00 USD\nסה״כ לתשלום 400.00 ₪'])
        self.assertEqual(result.extraction_status, 'ambiguous')
        self.assertIsNone(result.quoted_currency)
        self.assertIn('currency_discrepancy', result.issues)
        self.assertEqual(result.quoted_total_excl_vat, Decimal('100.00'))
        self.assertEqual(result.quoted_total_incl_vat, Decimal('400.00'))

    def test_subtotals_and_vat_amount_are_not_extracted(self):
        result = extract_text_pages(['Subtotal 100.00 USD\nVAT 18.00 USD'])
        self.assertIsNone(result.quoted_total_excl_vat)
        self.assertIsNone(result.quoted_total_incl_vat)

    def test_conflicting_payable_totals_require_review(self):
        result = extract_text_pages(['Total payable 118.00 USD\nTotal payable 120.00 USD'])
        self.assertEqual(result.extraction_status, 'ambiguous')
        self.assertIsNone(result.quoted_total_incl_vat)

    def test_discount_document_requires_fallback_or_review(self):
        result = extract_text_pages(['Discount 10.00 USD\nTotal excluding VAT 100.00 USD'])
        self.assertEqual(result.extraction_status, 'ambiguous')

    def test_fallback_amount_without_evidence_is_rejected(self):
        result = validate_result({'quoted_total_incl_vat': '118.00'})
        self.assertIsNone(result.quoted_total_incl_vat)
        self.assertEqual(result.extraction_status, 'ambiguous')

    def test_explicitly_labelled_subtotals_are_not_final_totals(self):
        for label in ('Subtotal', 'Sub total', 'Line total', 'Item total', 'Unit total'):
            with self.subTest(label=label):
                result = extract_text_pages([f'{label} excluding VAT 100.00 USD'])
                self.assertIsNone(result.quoted_total_excl_vat)

    def test_hebrew_ascii_quotes_and_direction_marks(self):
        result = extract_text_pages(['\u200fסה"כ לפני מע"מ 1,000.00 ש"ח\nסה"כ כולל מע"מ 1,180.00 ש"ח'])
        self.assertEqual(result.extraction_status, 'success')
        self.assertEqual(result.quoted_total_excl_vat, Decimal('1000.00'))

    def test_split_layout_is_not_guessed(self):
        result = extract_text_pages(['סה״כ לתשלום\n118.00\n₪'])
        self.assertEqual(result.extraction_status, 'ambiguous')
        self.assertIsNone(result.quoted_total_incl_vat)

    def test_reversed_hebrew_is_not_guessed(self):
        result = extract_text_pages(['םולשתל כ״הס 118.00 ₪'])
        self.assertIsNone(result.quoted_total_incl_vat)

    def test_real_pdf_text_extraction_without_network(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest('PyMuPDF is not installed')
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), 'Total excluding VAT 100.00 USD\nTotal payable 118.00 USD')
            data = document.tobytes()
        result = extract_pdf(data)
        self.assertEqual(result.extraction_status, 'success')
        self.assertEqual(result.quoted_total_incl_vat, Decimal('118.00'))
