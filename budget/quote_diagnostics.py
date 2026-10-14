"""Diagnostics contain static codes and allowlisted metadata, never raw errors."""


class FallbackDiagnosticError(ValueError):
    def __init__(self, stage, code, **details):
        self.diagnostic = {"stage": stage, "code": code, **details}
        super().__init__(code)


VALIDATION_CODES = {
    "Unexpected response fields": "response_fields_invalid",
    "Invalid status": "status_invalid",
    "Invalid common currency": "common_currency_invalid",
    "Invalid evidence or currencies shape": "evidence_shape_invalid",
    "Missing amount must have null evidence and currency": "null_amount_metadata_invalid",
    "Amount must be a nonnegative decimal string": "amount_format_invalid",
    "Invalid evidence": "evidence_invalid",
    "Empty evidence": "evidence_empty",
    "Invalid evidence page": "evidence_page_invalid",
    "Invalid amount currency": "amount_currency_invalid",
    "Evidence does not contain the amount": "amount_not_in_evidence",
    "Evidence is not present on the cited page": "evidence_not_on_page",
    "Evidence identifies a non-final amount or lacks a total label": "non_final_or_unlabelled_evidence",
}


def safe_exception(exc, stage):
    if isinstance(exc, FallbackDiagnosticError):
        return exc.diagnostic
    # Only known fixed validator messages are mapped, not printed.
    if stage == "validation" and type(exc) is ValueError:
        code = VALIDATION_CODES.get(str(exc), "validation_error")
    elif isinstance(exc, PermissionError):
        code = "approval_required"
    else:
        code = "unexpected_error"
    return {"stage": stage, "code": code}
