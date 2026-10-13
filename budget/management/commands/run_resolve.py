from collections import defaultdict
from datetime import date

from django.core.management.base import BaseCommand

from budget import resolve


class Command(BaseCommand):
    help = "Compute the Resolve recommendation and print it (read-only — nothing is saved)"

    def add_arguments(self, p):
        p.add_argument("--today", default=None)
        p.add_argument("--sap-as-of", default=None,
                       help="Treat SAP as known only up to this date")
        p.add_argument("--lookback-days", type=int, default=None)
        p.add_argument("--respect-labels", action="store_true", help="Keep grants already labelled in LabSuit")
        p.add_argument("--replay-days", type=int, default=None,
                       help="Backtest: re-decide all purchases of the last N days")
        p.add_argument("--brief", action="store_true", help="One line per order, no reasons")
        p.add_argument("--split", action="store_true", help="Allow splitting a request across grants")

    def handle(self, today, sap_as_of, lookback_days, respect_labels, replay_days, brief, split=False, **_):
        r = resolve.compute(
            today=date.fromisoformat(today) if today else None,
            sap_as_of=date.fromisoformat(sap_as_of) if sap_as_of else None,
            lookback_days=lookback_days, respect_labels=True if respect_labels else None,
            replay_days=replay_days, allow_split=split or None)
        self.stdout.write(self.style.SUCCESS(
            f"solver={r.solver} · plans={len(r.plans)} · needs amount={len(r.needs_amount)} "
            f"· standing={len(r.standing)} · skipped={len(r.skipped)} · SAP as of {r.sap_as_of}"))
        per_grant = defaultdict(lambda: [0, 0.0])
        for plan in r.plans:
            for l in plan.lines:
                g = l.grant.number if l.grant else "UNFUNDED"
                per_grant[g][0] += 1
                per_grant[g][1] += float(l.amount_ils)
                line = (f"  [{plan.cand.order.id}] {plan.cand.order.item_name[:38]:38} {l.amount_orig:>10,.2f} "
                        f"{plan.cand.currency} → {g:>8} {plan.cand.category:9} {l.amount_grant or 0:>10,.2f}")
                self.stdout.write(line if brief else f"{line}\n        {l.reason}")
        self.stdout.write("  — per grant (₪): " + ", ".join(
            f"{g}: {n} lines / {v:,.0f}" for g, (n, v) in sorted(per_grant.items(), key=lambda kv: -kv[1][1])))
        for o in r.needs_amount:
            self.stdout.write(f"  needs amount: [{o.id}] {o.item_name}")
        for o, why in r.skipped:
            self.stdout.write(f"  skipped: [{o.id}] {o.item_name[:40]} — {why}")
        for al in r.alerts:
            self.stdout.write(self.style.WARNING(f"  ! {al}"))
