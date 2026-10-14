"""Standalone extraction tests; no database, scraper, or Claude calls."""
from decimal import Decimal
from pathlib import Path
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

    def test_reversed_hebrew_payable_label(self):
        result = extract_text_pages(['םולשתל כ״הס 118.00 ₪'])
        self.assertEqual(result.quoted_total_incl_vat, Decimal('118.00'))
        self.assertEqual(result.extraction_status, 'missing')

    def test_fragmented_summary_with_discount_and_usd_items(self):
        result = extract_text_pages(['''מחיר לאחר הנחה
1,270.88 ₪ 416.00 USD 520.00 USD
1,270.88 ₪ כ" סה איש מכירות 052-3452060
ההנחה מותנית ברכישה
228.76 ₪ 18 % מ"מע
1,499.64 ₪ לתשלום כ" סה'''])
        self.assertEqual(result.extraction_status, 'success')
        self.assertEqual(result.quoted_total_excl_vat, Decimal('1270.88'))
        self.assertEqual(result.quoted_total_incl_vat, Decimal('1499.64'))
        self.assertEqual(result.quoted_currency, 'ILS')
        self.assertIn('228.76', result.evidence['quoted_total_excl_vat']['text'])

    def test_generic_total_without_vat_summary_is_missing(self):
        result = extract_text_pages(['Total 100.00 USD\nTotal payable 118.00 USD'])
        self.assertIsNone(result.quoted_total_excl_vat)
        self.assertEqual(result.extraction_status, 'missing')

    def test_discount_does_not_invalidate_explicit_payable_total(self):
        result = extract_text_pages(['Discount 10.00 USD\nTotal payable 118.00 USD'])
        self.assertEqual(result.extraction_status, 'missing')
        self.assertEqual(result.quoted_total_incl_vat, Decimal('118.00'))

    def test_explicit_after_discount_total_needs_no_vat_calculation(self):
        result = extract_text_pages(['Discount 10.00 USD\nTotal excluding VAT after discounts 90.00 USD\nTotal payable 94.50 USD'])
        self.assertEqual(result.extraction_status, 'success')
        self.assertEqual(result.quoted_total_excl_vat, Decimal('90.00'))

    def test_vat_percentage_does_not_generate_missing_total(self):
        result = extract_text_pages(['VAT 18%\nTotal payable 118.00 USD'])
        self.assertIsNone(result.quoted_total_excl_vat)

    def test_summary_currency_discrepancy_is_flagged(self):
        result = extract_text_pages(['Total 100.00 USD\nVAT 18.00 USD\nTotal payable 400.00 ILS'])
        self.assertEqual(result.extraction_status, 'ambiguous')
        self.assertIn('currency_discrepancy', result.issues)

    def test_monetary_discount_after_total_prevents_using_it(self):
        result = extract_text_pages(['Total 100.00 USD\nDiscount 10.00 USD\nVAT 16.20 USD\nTotal payable 106.20 USD'])
        self.assertIsNone(result.quoted_total_excl_vat)
        self.assertEqual(result.extraction_status, 'ambiguous')

    def test_summary_after_discount_uses_printed_totals_not_fixed_rate(self):
        result = extract_text_pages(['Discount 10.00 USD\nTotal 90.00 USD\nVAT 4.50 USD\nTotal payable 94.50 USD'])
        self.assertEqual(result.extraction_status, 'success')
        self.assertEqual(result.quoted_total_excl_vat, Decimal('90.00'))
        self.assertEqual(result.quoted_total_incl_vat, Decimal('94.50'))

    def test_real_supplier_quotation_when_available(self):
        path = Path(__file__).resolve().parent.parent / 'test-quotes' / '248534.pdf'
        if not path.is_file():
            self.skipTest('Private real quotation fixture is not present')
        result = extract_pdf(path.read_bytes())
        self.assertEqual(result.extraction_status, 'success')
        self.assertEqual(result.quoted_total_excl_vat, Decimal('1270.88'))
        self.assertEqual(result.quoted_total_incl_vat, Decimal('1499.64'))
        self.assertEqual(result.quoted_currency, 'ILS')

    def test_hebrew_long_labels_and_reversed_currency(self):
        for totals in (
            'סך הכל לפני מע״מ 1,091.56 ש״ח\nמס ערך מוסף (%) 18.00 196.48 ש״ח\nסך הכול 1,288.04 ש״ח',
            "ח''ש 1,091.56 מ''מע לפני הכל סך\nח''ש 196.48 18.00 (%) מוסף ערך מס\nח''ש 1,288.04 הכול סך",
        ):
            with self.subTest(totals=totals):
                result = extract_text_pages(['אחוז הנחה\n397 USD -10\n' + totals])
                self.assertEqual(result.extraction_status, 'success')
                self.assertEqual(result.quoted_total_excl_vat, Decimal('1091.56'))
                self.assertEqual(result.quoted_total_incl_vat, Decimal('1288.04'))
                self.assertEqual(result.quoted_currency, 'ILS')

    def test_before_discount_is_not_final_even_with_zero_discount(self):
        result = extract_text_pages(['סה״כ לפני הנחה 780.00 ₪\nהנחה 0.00 ₪\nמע״מ 140.40 ₪\nסה״כ לתשלום 920.40 ₪'])
        self.assertIsNone(result.quoted_total_excl_vat)
        self.assertEqual(result.quoted_total_incl_vat, Decimal('920.40'))
        self.assertEqual(result.extraction_status, 'missing')

    def test_generic_grand_total_without_vat_context_is_not_payable(self):
        result = extract_text_pages(['סך הכול 1,288.04 ש״ח'])
        self.assertIsNone(result.quoted_total_incl_vat)

    def test_summary_with_additional_charge_requires_review(self):
        result = extract_text_pages(['סך הכל לפני מע״מ 100.00 ₪\nמשלוח 20.00 ₪\nמע״מ 21.60 ₪\nסך הכול 141.60 ₪'])
        self.assertNotEqual(result.extraction_status, 'success')

    def test_long_label_summary_does_not_mix_currencies(self):
        result = extract_text_pages(['סך הכל לפני מע״מ 100.00 USD\nמע״מ 18.00 ₪\nסך הכול 400.00 ₪'])
        self.assertEqual(result.extraction_status, 'ambiguous')
        self.assertIsNone(result.quoted_currency)

    def test_vat_row_currency_conflict_requires_review(self):
        result = extract_text_pages(['סך הכל לפני מע״מ 100.00 ₪\nמע״מ 18.00 USD\nסך הכול 118.00 ₪'])
        self.assertEqual(result.extraction_status, 'ambiguous')

    def test_other_real_quotations_when_available(self):
        folder = Path(__file__).resolve().parent.parent / 'test-quotes'
        for name, excl, incl, status in (
            ('215990.pdf', Decimal('1091.56'), Decimal('1288.04'), 'success'),
            ('workorder_19363.pdf', None, Decimal('920.40'), 'missing'),
        ):
            with self.subTest(filename=name):
                path = folder / name
                if not path.is_file():
                    continue
                result = extract_pdf(path.read_bytes())
                self.assertEqual(result.quoted_total_excl_vat, excl)
                self.assertEqual(result.quoted_total_incl_vat, incl)
                self.assertEqual(result.quoted_currency, 'ILS')
                self.assertEqual(result.extraction_status, status)

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
