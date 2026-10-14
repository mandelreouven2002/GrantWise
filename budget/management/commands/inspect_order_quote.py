"""Read one stored quotation and print diagnostics, without persistence."""
import json
from dataclasses import asdict
from pathlib import Path

from django.db import connections, DatabaseError
from django.core.management.base import BaseCommand, CommandError

from budget.models import OrderFile
from budget.quote_extraction import extract_text_pages


class Command(BaseCommand):
    help = "Inspect ONE existing OrderFile PDF locally; no writes, downloads, OCR, or Claude."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--order-id", type=int, help="OrderRequest primary key")
        parser.add_argument("--database", default="default", help="Configured Django database alias")

    def handle(self, order_id=None, database="default", **options):
        connection = connections[database]
        # Opening a missing SQLite database normally creates it: prevent that.
        if connection.vendor == "sqlite":
            name = str(connection.settings_dict["NAME"])
            if name != ":memory:" and not name.startswith("file:") and not Path(name).is_file():
                raise CommandError("SQLite database does not exist. Use your existing database; do not run migrations for this inspection.")
        try:
            files = OrderFile.objects.using(database).only("id", "order_id", "file_name", "data")
            stored = (files.filter(order_id=order_id).first() if order_id is not None
                      else files.order_by("pk").first())
        except DatabaseError as exc:
            raise CommandError(f"Cannot read OrderFile ({type(exc).__name__}). Check your existing database configuration.") from None
        if stored is None:
            raise CommandError("No stored PDF found for this order." if order_id is not None
                               else "No stored OrderFile PDFs found in this database.")
        try:
            import pymupdf
        except ImportError:
            raise CommandError("PyMuPDF is missing. Install it in your local environment: python -m pip install PyMuPDF") from None
        self.stdout.write(f"OrderRequest id: {stored.order_id}; OrderFile id: {stored.pk}; file: {stored.file_name}")
        try:
            with pymupdf.open(stream=bytes(stored.data), filetype="pdf") as document:
                if document.needs_pass:
                    raise CommandError("PDF is password protected; no extraction attempted.")
                pages = [page.get_text("text", sort=True) for page in document]
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f"PDF cannot be read ({type(exc).__name__}).") from None
        for index, text in enumerate(pages, 1):
            self.stdout.write(f"\n--- Extracted text: page {index} ---")
            self.stdout.write(text or "[No extractable text: scanned/empty page; OCR and Claude are disabled.]")
        result = extract_text_pages(pages)
        self.stdout.write("\n--- Extraction result (not saved) ---")
        self.stdout.write(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
        self.stdout.write("\nCompare the evidence with the PDF visually. This command never saves results.")
