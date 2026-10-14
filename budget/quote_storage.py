"""Persist local PDF extraction independently of funding amounts."""
import hashlib

from django.db import transaction
from django.utils import timezone

from .models import OrderFile, OrderRequest
from .quote_extraction import ExtractionResult, extract_pdf

EXTRACTOR_VERSION = "local-v2"
QUOTE_FIELDS = ("quoted_total_excl_vat", "quoted_total_incl_vat", "quoted_currency")


def _snapshot(order):
    return {name: (format(getattr(order, name), ".2f") if name != "quoted_currency"
                   and getattr(order, name) is not None else getattr(order, name))
            for name in QUOTE_FIELDS}


def _manually_changed(order):
    previous = order.extraction_evidence.get("saved_values")
    return order.quoted_manually_corrected or (
        previous is not None and previous != _snapshot(order)) or (
        previous is None and (any(getattr(order, name) is not None for name in QUOTE_FIELDS[:2])
                              or bool(order.quoted_currency)))


def extract_order_quote(order_id, *, save=True, force=False):
    """Returns (outcome, result). Never calls a scraper or remote API.

    Idempotent per PDF hash/version, including partial and failed attempts.
    Manual edits always win, even with force. Changed PDFs replace old automatic
    values, so a missing result cannot leave a stale total from another PDF.
    """
    stored = OrderFile.objects.select_related("order").filter(order_id=order_id).first()
    if stored is None:
        return "no_pdf", None
    order = stored.order
    if _manually_changed(order):
        return "manual", None
    pdf_bytes = bytes(stored.data)
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    meta = order.extraction_evidence
    if not force and meta.get("pdf_sha256") == digest and meta.get("extractor_version") == EXTRACTOR_VERSION:
        return "unchanged", None
    try:
        result = extract_pdf(pdf_bytes)  # No fallback: local only.
        # Reject values that cannot be represented by our DecimalFields.
        for name in QUOTE_FIELDS[:2]:
            OrderRequest._meta.get_field(name).clean(getattr(result, name), order)
    except Exception as exc:
        result = ExtractionResult(extraction_status="failed", missing_fields=list(QUOTE_FIELDS[:2]),
                                  issues=[f"extraction_failed:{type(exc).__name__}"])
    if not save:
        return "preview", result
    with transaction.atomic():
        current = OrderRequest.objects.select_for_update().get(pk=order_id)
        current_file = OrderFile.objects.select_for_update().filter(order_id=order_id).first()
        if current_file is None or hashlib.sha256(bytes(current_file.data)).hexdigest() != digest:
            return "changed_during_extraction", None
        if _manually_changed(current):
            return "manual", None
        if not force and current.extraction_evidence.get("pdf_sha256") == digest and current.extraction_evidence.get("extractor_version") == EXTRACTOR_VERSION:
            return "unchanged", None
        # A forced failed retry on the same PDF must not erase valid values.
        if (current.extraction_evidence.get("pdf_sha256") == digest and (
                (result.extraction_status == "failed" and current.extraction_status in ("success", "missing"))
                or any(getattr(current, name) is not None and getattr(result, name) is None
                       for name in QUOTE_FIELDS[:2])
                or (current.extraction_status == "success" and result.extraction_status != "success"))):
            return "preserved", result
        current.quoted_total_excl_vat = result.quoted_total_excl_vat
        current.quoted_total_incl_vat = result.quoted_total_incl_vat
        current.quoted_currency = result.quoted_currency or ""
        current.extraction_status = result.extraction_status
        current.extraction_evidence = {
            "values": result.evidence, "issues": result.issues,
            "missing_fields": result.missing_fields, "pdf_sha256": digest,
            "extractor_version": EXTRACTOR_VERSION, "method": "pymupdf",
            "extracted_at": timezone.now().isoformat(), "saved_values": _snapshot(current),
        }
        current.save(update_fields=[*QUOTE_FIELDS, "extraction_status", "extraction_evidence"])
    return "saved", result
