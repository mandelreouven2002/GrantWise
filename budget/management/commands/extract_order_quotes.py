"""Repeatable backfill of stored quotation PDFs; preview unless --save is set."""
from collections import Counter
from django.core.management.base import BaseCommand, CommandError
from budget.models import OrderFile
from budget.quote_storage import extract_order_quote


class Command(BaseCommand):
    help = "Extract existing OrderFile PDFs locally. Defaults to preview; --save persists quotation fields only."

    def add_arguments(self, parser):
        parser.add_argument("--order-id", type=int)
        parser.add_argument("--limit", type=int, default=100, help="Maximum eligible PDFs; unchanged/manual orders do not use the limit")
        parser.add_argument("--save", action="store_true")
        parser.add_argument("--force", action="store_true", help="Retry unchanged PDFs; manual corrections stay protected")

    def handle(self, order_id=None, limit=100, save=False, force=False, **options):
        if limit < 1:
            raise CommandError("--limit must be positive")
        files = OrderFile.objects.order_by("order_id")
        if order_id is not None:
            files = files.filter(order_id=order_id)
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
