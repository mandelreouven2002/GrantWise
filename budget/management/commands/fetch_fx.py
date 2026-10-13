import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand

from budget.fx import fetch_all
from budget.models import FxRate


class Command(BaseCommand):
    help = ("Fetch rates for all tracked currencies now (the site also refreshes them by itself), "
            "or set manually: fetch_fx --set USD=3.078 EUR=3.4422 --on 2026-10-12")

    def add_arguments(self, p):
        p.add_argument("--set", dest="pairs", nargs="*", default=None, help="CUR=RATE pairs (₪ per unit)")
        p.add_argument("--on", default=None, help="YYYY-MM-DD for manual rates (default today)")

    def handle(self, pairs=None, on=None, **_):
        if pairs:
            d = datetime.date.fromisoformat(on) if on else datetime.date.today()
            for pair in pairs:
                cur, rate = pair.split("=")
                FxRate.objects.update_or_create(date=d, currency=cur.upper(),
                                                defaults={"rate_to_ils": Decimal(rate), "source": "manual"})
                self.stdout.write(f"{cur.upper()} = {rate} ({d})")
            return
        for cur, rate, d, source in fetch_all():
            self.stdout.write(f"{cur} = {rate} ({d}, {source})")
