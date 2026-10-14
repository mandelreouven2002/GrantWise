"""Repeatable backfill of stored quotation PDFs; preview unless --save is set."""
from collections import Counter
import hashlib
import json
from django.core.management.base import BaseCommand, CommandError
from budget.models import OrderFile
from budget.quote_storage import extract_order_quote, extract_order_quote_bedrock


class Command(BaseCommand):
    help = "Extract existing OrderFile PDFs locally. Defaults to preview; --save persists quotation fields only."

    def add_arguments(self, parser):
        parser.add_argument("--order-id", type=int)
        parser.add_argument("--limit", type=int, default=100, help="Maximum eligible PDFs; unchanged/manual orders do not use the limit")
        parser.add_argument("--save", action="store_true")
        parser.add_argument("--force", action="store_true", help="Retry unchanged PDFs; manual corrections stay protected")
        parser.add_argument("--bedrock", action="store_true", help="Explicitly approved single-order fallback; requires --save")
        parser.add_argument("--approve-sha256")
        parser.add_argument("--max-api-calls", type=int, default=1)
        parser.add_argument("--max-output-tokens", type=int)
        parser.add_argument("--disable-thinking", action="store_true")
        parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])

    def handle(self, order_id=None, limit=100, save=False, force=False, **options):
        if limit < 1:
            raise CommandError("--limit must be positive")
        if options.get("bedrock"):
            if order_id is None or not save or not options.get("approve_sha256") or force:
                raise CommandError("--bedrock requires --order-id, --save, --approve-sha256 and forbids --force")
            try:
                outcome, result = extract_order_quote_bedrock(order_id,
                    approved_sha256=options["approve_sha256"], max_api_calls=options["max_api_calls"],
                    max_output_tokens=options["max_output_tokens"],
                    disable_thinking=options["disable_thinking"], effort=options["effort"])
            except ValueError as exc:
                # Configuration errors are fixed messages; never print raw provider errors.
                raise CommandError("Bedrock configuration/approval rejected; check settings, hash and token options") from exc
            self.stdout.write(f"order={order_id} {outcome}" + (
                f" status={result.extraction_status} excl={result.quoted_total_excl_vat} incl={result.quoted_total_incl_vat} currency={result.quoted_currency}"
                if result else ""))
            stored = OrderFile.objects.select_related("order").filter(order_id=order_id).first()
            if stored:
                digest = hashlib.sha256(bytes(stored.data)).hexdigest()
                attempt = stored.order.extraction_evidence.get("bedrock_attempts", {}).get(digest, {})
                self.stdout.write(json.dumps({key: attempt.get(key) for key in
                    ("state", "persistence_outcome", "diagnostic", "usage")}, ensure_ascii=False))
            return
        files = OrderFile.objects.order_by("order_id")
        if order_id is not None:
            files = files.filter(order_id=order_id)
            stored = files.first()
            if stored:
                self.stdout.write(f"pdf_sha256={hashlib.sha256(bytes(stored.data)).hexdigest()}")
        counts = Counter()
        processed = 0
        for pk in files.values_list("order_id", flat=True).iterator():
            outcome, result = extract_order_quote(pk, save=save, force=force)
            counts[outcome] += 1
            self.stdout.write(f"order={pk} {outcome}" + (
                f" status={result.extraction_status} excl={result.quoted_total_excl_vat} incl={result.quoted_total_incl_vat} currency={result.quoted_currency}"
                if result else ""))
            if result is not None:
                processed += 1
                if processed >= limit:
                    break
        self.stdout.write(str(dict(counts)))
