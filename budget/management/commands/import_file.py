import json
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand

from budget.importers import IMPORTERS, run_import


class Command(BaseCommand):
    help = "Import a LabSuit / SAP export. Example: import_file sap_grants grants.XLS --as-of 2026-10-12"

    def add_arguments(self, p):
        p.add_argument("source", choices=list(IMPORTERS))
        p.add_argument("path")
        p.add_argument("--mode", choices=["merge", "replace"], default="merge")
        p.add_argument("--as-of", default=None, help="YYYY-MM-DD (default: today)")
        p.add_argument("--min-year", type=int, default=None, help="orders only: skip older year sheets")
        p.add_argument("--preview", action="store_true", help="show what would change, write nothing")

    def handle(self, source, path, mode, as_of, min_year, preview=False, **_):
        as_of = date.fromisoformat(as_of) if as_of else date.today()
        kw = {"min_year": min_year} if source == "labsuit_orders" and min_year else {}
        if preview:
            from budget.importers.preview import dry_run
            v = dry_run(source, Path(path).read_bytes(), Path(path).name, as_of, mode, user="cli", **kw)
            self.stdout.write(json.dumps({k: v[k] for k in ("counts", "fields", "status_moves", "preserved", "warnings")},
                                         ensure_ascii=False, indent=1))
            for r in v["changed"][:20]:
                self.stdout.write(f"~ {r['label']}: " + "; ".join(f"{f}: {a} → {b}" for f, a, b in r["changes"]))
            self.stdout.write("(preview only — nothing was written)")
            return
        batch = run_import(source, path, Path(path).name, as_of, mode, user="cli", **kw)
        self.stdout.write(self.style.SUCCESS(f"{batch}"))
        self.stdout.write(json.dumps(batch.stats, ensure_ascii=False, indent=1))
