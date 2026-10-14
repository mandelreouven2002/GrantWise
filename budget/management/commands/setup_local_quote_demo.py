"""Repeatable real-PDF demo, restricted to the project's exact local SQLite DB."""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from budget.models import OrderFile, OrderRequest
from budget.quote_storage import extract_order_quote


LOCAL_DATABASE = Path("/home/user/technion/GrantWise/db.sqlite3")
PDF_NAMES = ("215990.pdf", "248534.pdf", "workorder_19363.pdf")


class Command(BaseCommand):
    help = "Create/reuse three local quotation demo orders. Refuses every DB except the exact local SQLite file."
    requires_system_checks = []

    def handle(self, **options):
        # Check configured backend before opening a connection: never contact PostgreSQL.
        if (connection.vendor != "sqlite"
                or Path(str(connection.settings_dict["NAME"])).resolve() != LOCAL_DATABASE
                or not LOCAL_DATABASE.is_file()):
            raise CommandError("STOP: expected existing /home/user/technion/GrantWise/db.sqlite3 only.")
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA database_list")
            actual = next((row[2] for row in cursor.fetchall() if row[1] == "main"), "")
            if Path(actual).resolve() != LOCAL_DATABASE:
                raise CommandError("STOP: actual database connection differs.")
            for model in (OrderRequest, OrderFile):
                columns = {column.name for column in connection.introspection.get_table_description(cursor, model._meta.db_table)}
                if any(field.column not in columns for field in model._meta.local_fields):
                    raise CommandError("STOP: local schema is incomplete; this command does not run migrations.")
        pdfs = []
        for name in PDF_NAMES:
            path = LOCAL_DATABASE.parent / "test-quotes" / name
            if not path.is_file():
                raise CommandError(f"Missing local PDF: {name}")
            pdfs.append((name, path.read_bytes()))
        self.stdout.write(f"Verified local SQLite: {LOCAL_DATABASE}")
        with transaction.atomic():
            for name, data in pdfs:
                order, created = OrderRequest.objects.get_or_create(
                    natural_key=f"local-quote-demo:{name}",
                    defaults={"item_name": f"בדיקת הצעת מחיר — {name}", "source": "manual",
                              "status": "approved", "date_requested": timezone.localdate(),
                              "is_current": True, "lab_notes": "נתוני בדיקה מקומיים בלבד"})
                stored, attached = OrderFile.objects.get_or_create(order=order, defaults={
                    "file_name": name, "data": data, "size": len(data)})
                if bytes(stored.data) != data or stored.file_name != name:
                    raise CommandError(f"Existing demo PDF differs for order {order.pk}; refusing to overwrite it.")
                outcome, result = extract_order_quote(order.pk)
                order.refresh_from_db()
                self.stdout.write(
                    f"id={order.pk} file={name} created={created} outcome={outcome} "
                    f"excl={order.quoted_total_excl_vat} incl={order.quoted_total_incl_vat} "
                    f"currency={order.quoted_currency} status={order.extraction_status}")
