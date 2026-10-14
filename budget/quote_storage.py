"""Persist local PDF extraction independently of funding amounts."""
import hashlib
from copy import deepcopy
from uuid import uuid4

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .models import OrderFile, OrderRequest
from .quote_extraction import ExtractionResult, extract_pdf

EXTRACTOR_VERSION = "local-v3"
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
            "bedrock_attempts": deepcopy(current.extraction_evidence.get("bedrock_attempts", {})),
            "values": result.evidence, "issues": result.issues,
            "missing_fields": result.missing_fields, "pdf_sha256": digest,
            "extractor_version": EXTRACTOR_VERSION, "method": "pymupdf",
            "extracted_at": timezone.now().isoformat(), "saved_values": _snapshot(current),
        }
        current.save(update_fields=[*QUOTE_FIELDS, "extraction_status", "extraction_evidence"])
    return "saved", result


def extract_order_quote_bedrock(order_id, *, approved_sha256, max_api_calls=1,
                               max_output_tokens=None, disable_thinking=False, effort=None):
    """One durable, explicitly approved attempt per order/PDF; never retries.

    The existing JSON evidence holds the ledger. A committed compare-and-swap
    claim also protects SQLite, where select_for_update does not lock rows.
    Interrupted attempts remain claimed and require operator investigation.
    """
    from .quote_bedrock import BedrockFallback
    from .quote_hybrid import evaluate_pdf
    from .quote_diagnostics import safe_exception

    if not getattr(settings, "QUOTE_BEDROCK_ENABLED", False):
        raise ValueError("Bedrock is disabled; set QUOTE_BEDROCK_ENABLED=1 explicitly")
    if max_api_calls != 1:
        raise ValueError("Bedrock requires max_api_calls=1")
    if connection.in_atomic_block or not connection.get_autocommit():
        raise ValueError("Bedrock must run outside a database transaction")
    stored = OrderFile.objects.select_related("order").filter(order_id=order_id).first()
    if stored is None:
        return "no_pdf", None
    data = bytes(stored.data)
    digest = hashlib.sha256(data).hexdigest()
    if approved_sha256 != digest:
        raise ValueError("SHA-256 approval does not match the stored PDF")
    if _manually_changed(stored.order):
        return "manual", None
    if digest in stored.order.extraction_evidence.get("bedrock_attempts", {}):
        return "already_attempted", None
    adapter = BedrockFallback(approved_sha256=[digest], max_output_tokens=max_output_tokens,
                             disable_thinking=disable_thinking, effort=effort)
    config = adapter.preflight()  # Local validation only; no network.
    extract_order_quote(order_id, save=True)
    token = uuid4().hex
    with transaction.atomic():
        current = OrderRequest.objects.select_for_update().get(pk=order_id)
        current_file = OrderFile.objects.select_for_update().filter(order_id=order_id).first()
        if current_file is None or hashlib.sha256(bytes(current_file.data)).hexdigest() != digest:
            return "changed_during_extraction", None
        if _manually_changed(current):
            return "manual", None
        old_meta = current.extraction_evidence
        if digest in old_meta.get("bedrock_attempts", {}):
            return "already_attempted", None
        if old_meta.get("pdf_sha256") != digest:
            return "changed_during_extraction", None
        rules = ExtractionResult(**{name: getattr(current, name) for name in QUOTE_FIELDS},
            extraction_status=current.extraction_status, evidence=deepcopy(old_meta.get("values", {})),
            missing_fields=old_meta.get("missing_fields", []), issues=old_meta.get("issues", []))
        if rules.extraction_status not in ("missing", "ambiguous"):
            return "not_needed", rules
        meta = deepcopy(old_meta)
        meta.setdefault("bedrock_attempts", {})[digest] = {
            "token": token, "state": "claimed", "claimed_at": timezone.now().isoformat(),
            "configuration": config,
        }
        claimed = OrderRequest.objects.filter(pk=order_id, extraction_evidence=old_meta,
            quoted_manually_corrected=False).update(extraction_evidence=meta)
        if not claimed:
            return "claim_conflict", None
    # The durable claim has committed. No database transaction spans this call.
    calls = 0

    def once(pdf_bytes, prompt):
        nonlocal calls
        if calls >= max_api_calls:
            raise PermissionError("API call limit reached")
        # Recheck corrections and file replacement immediately before sending.
        latest = OrderFile.objects.select_related("order").filter(order_id=order_id).first()
        if (latest is None or _manually_changed(latest.order)
                or hashlib.sha256(bytes(latest.data)).hexdigest() != digest):
            raise PermissionError("Order or PDF changed after approval")
        calls += 1
        return adapter(pdf_bytes, prompt)

    try:
        evaluation = evaluate_pdf(data, fallback=once, rules=rules)
        result = evaluation.final
        for name in QUOTE_FIELDS[:2]:
            OrderRequest._meta.get_field(name).clean(getattr(result, name), current)
        outcome = evaluation.fallback_outcome
        diagnostic = evaluation.diagnostic
    except Exception as exc:
        result, outcome, diagnostic = rules, "rejected", safe_exception(exc, "persistence_validation")
    with transaction.atomic():
        current = OrderRequest.objects.select_for_update().get(pk=order_id)
        meta = deepcopy(current.extraction_evidence)
        attempt = meta.get("bedrock_attempts", {}).get(digest)
        if attempt is None or attempt.get("token") != token:
            return "claim_lost", None
        state = "uncertain" if diagnostic and diagnostic.get("stage") in ("transport", "provider") else outcome
        attempt.update(state=state, finished_at=timezone.now().isoformat(),
                       diagnostic=diagnostic, usage=adapter.usage)
        current_file = OrderFile.objects.select_for_update().filter(order_id=order_id).first()
        if _manually_changed(current):
            outcome = "manual"
        elif current_file is None or hashlib.sha256(bytes(current_file.data)).hexdigest() != digest:
            outcome = "changed_during_extraction"
        elif meta.get("saved_values") != old_meta.get("saved_values"):
            outcome = "preserved"
        elif outcome == "evaluated":
            for name in QUOTE_FIELDS:
                setattr(current, name, getattr(result, name) if name != "quoted_currency" else result.quoted_currency or "")
            current.extraction_status = result.extraction_status
            meta.update(values=result.evidence, issues=result.issues, missing_fields=result.missing_fields,
                        method="pymupdf+bedrock", saved_values=_snapshot(current),
                        extracted_at=timezone.now().isoformat())
        attempt["persistence_outcome"] = outcome
        current.extraction_evidence = meta
        current.save(update_fields=[*QUOTE_FIELDS, "extraction_status", "extraction_evidence"])
    return outcome, result
