"""Resolve: recommend which grant (and budget category) funds each approved, not-yet-ordered request.

LabFund is a decision-support tool, not an ERP: the recommendation is computed in memory every time
the page is opened, and nothing is written until the user approves. The user may edit the
recommendation (choose another grant for an order); approval re-validates everything
inside a locked transaction.

Pipeline (compute)
1. availability – per grant: SAP balance (already net of SAP commitments)
                  − active reservations
                  − approved allocations SAP does not show yet
                  − orders placed after the SAP snapshot (by their LabSuit GRANT_ID).
2. candidates   – orders approved in LabSuit and not ordered yet, requested within `lookback_days`,
                  not on a standing order, not already approved here, with an amount.
3. eligibility  – hard rules (dates, cutoff, project links, category allowed, balance).
4. Optimisation (PuLP 3.x + bundled CBC) over all candidates together.
   One grant per request (the lab funds a request by writing a single GRANT_ID):
       min  Σ cost[o,g]·A_o·y[o,g] + Σ BIG·A_o·z_o        y, z ∈ {0,1}
       s.t. Σ_g y[o,g] + z_o = 1                          (one grant, or flagged unfunded)
            Σ_o∈c A_o·k[o,g]·y[o,g] ≤ avail[g,c]          (category balance, non-pooled grants)
            Σ_o   A_o·k[o,g]·y[o,g] ≤ avail_total[g]      (grant total balance)
   A_o is in ₪; k converts ₪ to grant currency incl. FX safety margin.
   cost = 1 − w_u·urgency(expiry) + w_r·reserve − w_p·priority + w_fx·(currency mismatch)

Reconciliation with SAP (approved → "כלול ביתרת SAP") runs when files are imported, never on page load.
SAP exports only aggregate balances, so "covered" is an inference from dates (see `covered_by_sap`).
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pulp
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .funding import alias_for
from .fx import rate_to_ils
from .models import (Allocation, BudgetCategory, FundingAlias, Grant, GrantRule, ImportBatch,
                     OrderRequest, ResolveRun)

# Approved in LabSuit but not ordered yet: the only point where the funding source is still open.
# 'ordered' rows already have a PO, so SAP has committed them (counting them again would double-count).
OPEN_STATUSES = {"requested", "pending", "approved"}
BIG = 1000.0
Q = Decimal("0.01")
R6 = Decimal("0.000001")
TOL = Decimal("0.01")


# --------------------------------------------------------------------------- data classes

@dataclass
class GrantState:
    grant: Grant
    rate: Decimal                      # ₪ per 1 unit of grant currency
    pooled: bool
    total: Decimal                     # available, grant currency
    cats: dict                         # category -> available, grant currency
    extra: dict                        # category -> extra allowance from rules
    banned: set                        # categories disallowed by rules
    days_left: int | None

    def cat_cap(self, cat):
        return self.cats.get(cat, Decimal("0")) + self.extra.get(cat, Decimal("0"))


@dataclass
class Candidate:
    order: OrderRequest
    amount: Decimal                    # order currency
    currency: str
    rate: Decimal                      # ₪ per unit of order currency
    amount_ils: Decimal
    category: str
    fixed_grant: str | None = None
    notes: list = field(default_factory=list)


@dataclass
class Line:
    grant: Grant | None                # None = the part nothing can fund
    amount_orig: Decimal               # order currency
    amount_ils: Decimal
    amount_grant: Decimal | None = None
    reason: str = ""


@dataclass
class Option:
    """One eligible grant for one order, with what the user needs to judge it."""
    state: GrantState
    cost: float
    urgency: float
    avail_cat: Decimal                 # before this plan, grant currency
    rank: int = 0


@dataclass
class Plan:
    cand: Candidate
    lines: list
    options: list                      # [Option], best first
    why_not: dict                      # grant number -> reason

    @property
    def state(self):
        funded = [l for l in self.lines if l.grant]
        if len(funded) < len(self.lines):
            return "unfunded"
        return "ok"


@dataclass
class Result:
    today: date
    sap_batch: ImportBatch
    sap_as_of: date
    params: dict
    states: dict
    plans: list
    needs_amount: list
    standing: list
    skipped: list                      # [(order, reason)]
    alerts: list
    solver: str
    stale_fx: list


# --------------------------------------------------------------------------- availability

def in_sap(order, sap_as_of):
    """For orders with no decision here: an order with a PO is treated as already committed in SAP
    (it is not a funding decision any more). Lenient on purpose — see `covered_by_sap` for decisions."""
    if not order.po_number:
        return False
    return order.date_ordered is None or sap_as_of is None or order.date_ordered <= sap_as_of


def submitted_on(alloc):
    """The date the order behind a decision went out: the LabSuit PO date, else the day the user
    marked it 'עודכן במערכות המעבדה'. None = unknown."""
    o = alloc.order
    if o.po_number and o.date_ordered:
        return o.date_ordered
    if alloc.lab_updated_at:
        return timezone.localdate(alloc.lab_updated_at)
    return None


def covered_by_sap(alloc, sap_as_of):
    """Conservative: an approved decision stops being reserved only when the SAP report is dated on or
    after the day its order went out. Without a known date it stays reserved (shown as an estimate)."""
    d = submitted_on(alloc)
    return d is not None and sap_as_of is not None and d <= sap_as_of


def mark_written_in_labsuit():
    """The lab funds a request by writing its grant in LabSuit's GRANT_ID. When an imported order
    already carries the grant we decided on, mark the decision 'עודכן במערכות המעבדה' automatically.
    Only single-grant decisions (one GRANT_ID can't express more). Returns the count."""
    n, now = 0, timezone.now()
    pending = (Allocation.objects.filter(status=Allocation.Status.APPROVED, lab_updated_at__isnull=True,
                                         grant__isnull=False).select_related("order"))
    by_order = defaultdict(set)
    orders = {}
    for a in pending:
        by_order[a.order_id].add(a.grant_id)
        orders[a.order_id] = a.order
    for oid, grants in by_order.items():
        if len(grants) != 1 or Allocation.objects.filter(order_id=oid, status=Allocation.Status.APPROVED) \
                .exclude(grant_id__in=grants).exists():
            continue
        alias = alias_for(orders[oid].grant_label)
        if alias.kind == FundingAlias.Kind.GRANT and alias.grant and alias.grant.number in grants:
            n += Allocation.objects.filter(order_id=oid, status=Allocation.Status.APPROVED).update(
                lab_updated_at=now, lab_updated_by="LabSuit (GRANT_ID)")
    return n


def reconcile(sap_as_of=None):
    """Approved decisions a SAP report now covers (see `covered_by_sap`) → 'כלול ביתרת SAP'.
    Called after SAP / LabSuit imports and when a decision is marked — never on page load."""
    if sap_as_of is None:
        b = ImportBatch.latest(ImportBatch.Source.SAP_GRANTS)
        sap_as_of = b.as_of if b else None
    if sap_as_of is None:
        return 0
    ids = [a.pk for a in Allocation.objects.filter(status=Allocation.Status.APPROVED).select_related("order")
           if covered_by_sap(a, sap_as_of)]
    return Allocation.objects.filter(pk__in=ids).update(status=Allocation.Status.SUPERSEDED)


def unseen_orders(sap_as_of, today=None):
    """Orders that went out *after* the SAP snapshot: committed in reality, missing from the balances.

    Returns [(grant_number, category, amount_in_grant_currency, order)] for orders whose LabSuit
    GRANT_ID maps to a grant and that have an amount. Standing orders are skipped (their blanket PO
    is already committed), and so are orders with an approved allocation here (already subtracted).
    """
    if sap_as_of is None:
        return []
    out = []
    qs = (OrderRequest.objects.filter(is_current=True, date_ordered__gt=sap_as_of)
          .exclude(po_number="").exclude(status="canceled").exclude(amount_est__isnull=True)
          .exclude(allocations__status=Allocation.Status.APPROVED).distinct())
    for o in qs:
        alias = alias_for(o.grant_label)
        if alias.kind != FundingAlias.Kind.GRANT or not alias.grant:
            continue
        g = alias.grant
        try:
            r_o, _ = rate_to_ils(o.amount_currency, today)
            r_g, _ = rate_to_ils(g.currency, today)
        except Exception:  # noqa: BLE001 — a missing rate just means we can't count it
            continue
        out.append((g.number, o.category_id or "MATERIAL", (o.amount_est * r_o / r_g).quantize(Q), o))
    return out


def grant_states(today, sap_as_of, grants=None):
    states = {}
    rules = defaultdict(list)
    for r in GrantRule.objects.all():
        rules[r.grant_id].append(r)
    unseen = defaultdict(list)
    for row in unseen_orders(sap_as_of, today):
        unseen[row[0]].append(row)
    grants = grants if grants is not None else Grant.objects.filter(is_active=True).select_related("last_batch")
    for g in grants:
        lines = {l.category_id: l.balance for l in g.current_lines()}
        pooled = lines.get("GENERAL", 0) > 0
        total = g.balance_total or Decimal("0")
        for res in g.reservations.all():
            if res.until is None or res.until >= today:
                total -= res.amount
                lines[res.category_id] = lines.get(res.category_id, Decimal("0")) - res.amount
        for a in Allocation.objects.filter(grant=g, status=Allocation.Status.APPROVED).select_related("order"):
            if not covered_by_sap(a, sap_as_of):
                total -= a.amount_grant_ccy or 0
                lines[a.category_id] = lines.get(a.category_id, Decimal("0")) - (a.amount_grant_ccy or 0)
        for _num, cat, amount, _o in unseen.get(g.number, []):
            total -= amount
            lines[cat] = lines.get(cat, Decimal("0")) - amount
        try:
            rate, _ = rate_to_ils(g.currency, today)
        except Exception:  # noqa: BLE001 — a grant without a usable rate can't fund anything today
            continue
        states[g.number] = GrantState(
            grant=g, rate=rate, pooled=pooled, total=total, cats=lines,
            extra={r.category_id: r.extra_allowance for r in rules.get(g.number, [])},
            banned={r.category_id for r in rules.get(g.number, []) if not r.allowed},
            days_left=g.days_left(today))
    return states


def eligible(c: Candidate, s: GrantState, today):
    g = s.grant
    if s.days_left is not None and s.days_left < g.order_cutoff_days:
        return False, ("הסתיים" if s.days_left < 0 else f"נותרו {s.days_left} ימים (סף {g.order_cutoff_days})")
    if g.start_date and (g.start_date - today).days > 30:
        return False, "טרם התחיל"
    if c.order.project_id:
        allowed = c.order.project.eligible_grant_numbers()
        if allowed and g.number not in allowed:
            return False, "לא משויך לפרויקט של ההזמנה"
    if c.category in s.banned:
        return False, "הסעיף אסור לפי כללי המענק"
    if s.total <= 0:
        return False, "אין יתרה כוללת"
    if not s.pooled:
        if c.category not in s.cats and c.category not in s.extra:
            return False, "אין במענק סעיף כזה"
        if s.cat_cap(c.category) <= 0:
            return False, "אין יתרה בסעיף"
    return True, ""


def make_candidate(o, today):
    rate, _ = rate_to_ils(o.amount_currency, today)
    c = Candidate(order=o, amount=o.amount_est, currency=o.amount_currency, rate=rate,
                  amount_ils=(o.amount_est * rate).quantize(Q), category=o.category_id or "MATERIAL")
    if o.amount_source != OrderRequest.AmountSource.LABSUIT:
        c.notes.append(f"סכום משוער ({o.get_amount_source_display()})")
    return c


def margin_for(c, s, p):
    return Decimal(str(p["fx_margin"])) if c.currency != s.grant.currency else Decimal("0")


def to_grant_ccy(amount_ils, c, s, p):
    return (amount_ils * (1 + margin_for(c, s, p)) / s.rate).quantize(Q)


def collect(today, sap_as_of, p):
    buckets = {"standing": [], "needs_amount": [], "candidates": [], "skipped": []}
    already = set(Allocation.objects.filter(status__in=[Allocation.Status.APPROVED, Allocation.Status.SUPERSEDED])
                  .values_list("order_id", flat=True))
    replay = p.get("replay_days")
    if replay:
        # Backtest: re-decide the last N days of real purchases as if they were new requests.
        # SAP balances already include them, so capacity is understated.
        qs = (OrderRequest.objects.filter(is_current=True, date_requested__gte=today - timedelta(days=replay))
              .exclude(status="canceled").select_related("project", "category"))
        already = set()
    else:
        qs = (OrderRequest.objects.filter(is_current=True, status__in=OPEN_STATUSES)
              .select_related("project", "category"))
    for o in qs:
        if not replay and o.date_requested and (today - o.date_requested).days > p["lookback_days"]:
            buckets["skipped"].append((o, f"הוגשה ב-{o.date_requested:%d/%m/%Y}, לפני יותר מ-{p['lookback_days']} ימים"))
            continue
        if not replay and in_sap(o, sap_as_of):
            when = f" מ-{o.date_ordered:%d/%m/%Y}" if o.date_ordered else ""
            buckets["skipped"].append((o, f"יש לה הזמנת רכש {o.po_number}{when} — כבר מחויבת ביתרות SAP "
                                          f"(עד {sap_as_of:%d/%m/%Y}). למחוק את מספר ההזמנה אם היא עוד לא הוזמנה"))
            continue
        alias = alias_for(o.grant_label)
        if alias.kind == FundingAlias.Kind.STANDING:
            buckets["standing"].append(o)
            continue
        if o.id in already:
            buckets["skipped"].append((o, "כבר אושרה כאן"))
            continue
        if not o.amount_est:
            buckets["needs_amount"].append(o)
            continue
        try:
            c = make_candidate(o, today)
        except Exception as e:  # noqa: BLE001 — e.g. no exchange rate for the order's currency
            buckets["skipped"].append((o, str(e)))
            continue
        if p["respect_labels"] and not replay and alias.kind == FundingAlias.Kind.GRANT and alias.grant:
            c.fixed_grant = alias.grant.number
        buckets["candidates"].append(c)
    return buckets


def cost(c: Candidate, s: GrantState, p):
    urgency = 0.5 ** (max(s.days_left if s.days_left is not None else 3650, 0) / p["urgency_halflife_days"])
    weight = 0
    if c.order.project_id:
        pg = c.order.project.projectgrant_set.filter(grant=s.grant).first()
        weight = pg.weight if pg else 0
    val = (1.0 - p["w_urgency"] * urgency + p["w_reserve"] * s.grant.is_reserve
           - p["w_priority"] * (s.grant.pi_priority + weight)
           + p["w_fx"] * (c.currency != s.grant.currency))
    return max(val, 0.05), urgency


def explain(c, s, avail_cat, urgency, amount_grant, cat_names=None):
    """Plain-language reason for one recommended line."""
    g = s.grant
    cat = (cat_names or {}).get(c.category, c.category)
    parts = []
    if g.end_date:
        parts.append(f"מסתיים {g.end_date:%d/%m/%Y} (בעוד {s.days_left} ימים)")
    base = s.total if s.pooled else avail_cat
    where = "מענק מאגר, כל סעיף" if s.pooled else f"סעיף {cat}"
    parts.append(f"{where}: זמין {base:,.2f} {g.currency}, אחרי ההקצאה {base - amount_grant:,.2f}")
    if urgency >= 0.5:
        parts.append("מועדף כי היתרה בסכנת אי-ניצול")
    if g.is_reserve:
        parts.append("מענק רזרבה — נבחר רק כי אין חלופה מתאימה יותר")
    if g.pi_priority:
        parts.append(f"עדיפות מרצה {g.pi_priority:+d}")
    if c.currency != g.currency:
        parts.append(f"המרה {c.currency}→{g.currency} כולל מרווח ביטחון")
    if c.fixed_grant == g.number:
        parts.append("נשמר המענק שסומן ב-LabSuit")
    return " · ".join(parts + c.notes)


# --------------------------------------------------------------------------- compute (read-only)

def compute(today=None, sap_as_of=None, **overrides):
    """Recommend funding for all open requests. Pure: performs no database writes."""
    p = {**settings.RESOLVE_DEFAULTS, **{k: v for k, v in overrides.items() if v is not None}}
    today = today or timezone.localdate()
    sap_batch = ImportBatch.latest(ImportBatch.Source.SAP_GRANTS)
    if sap_batch is None:
        raise ValueError("אין עדיין יתרות מענקים מ-SAP — יש לעדכן את דשבורד המענקים מקובץ.")
    sap_as_of = sap_as_of or sap_batch.as_of

    from .fx import stale_rates
    states = grant_states(today, sap_as_of)
    b = collect(today, sap_as_of, p)
    cands = b["candidates"]
    cat_names = dict(BudgetCategory.objects.values_list("code", "name_he"))

    # eligibility (a LabSuit label wins when the user asked to respect labels and it is eligible)
    options, why_not = {}, {}
    for i, c in enumerate(cands):
        opts, why = [], {}
        for num, s in states.items():
            ok, reason = eligible(c, s, today)
            if ok:
                cst, urg = cost(c, s, p)
                opts.append(Option(state=s, cost=cst, urgency=urg, avail_cat=s.cat_cap(c.category)))
            else:
                why[num] = reason
        opts.sort(key=lambda o: o.cost)
        for r, o in enumerate(opts, 1):
            o.rank = r
        if c.fixed_grant:
            fixed = [o for o in opts if o.state.grant.number == c.fixed_grant]
            if fixed:
                opts = fixed + [o for o in opts if o not in fixed]
            else:
                c.notes.append(f"המענק שסומן ב-LabSuit ({c.fixed_grant}) אינו כשיר: {why.get(c.fixed_grant, 'לא פעיל')}")
                c.fixed_grant = None
        options[i], why_not[i] = opts, why

    # Optimisation: each request is funded in full from exactly one grant (binary y, x = A·y);
    # a request that no single grant can cover is flagged unfunded (z = 1).
    prob = pulp.LpProblem("resolve", pulp.LpMinimize)
    x, k, costs = {}, {}, {}
    A = {i: float(c.amount_ils) for i, c in enumerate(cands)}
    z = {i: pulp.LpVariable(f"z_{i}", cat="Binary") for i in A}
    u = {i: A[i] * z[i] for i in A}
    for i, c in enumerate(cands):
        usable = options[i][:1] if c.fixed_grant else options[i]
        for o in usable:
            num = o.state.grant.number
            x[i, num] = A[i] * pulp.LpVariable(f"y_{i}_{num}", cat="Binary")
            k[i, num] = float((1 + margin_for(c, o.state, p)) / o.state.rate)
            costs[i, num] = o.cost
    prob += pulp.lpSum(costs[key] * x[key] for key in x) + pulp.lpSum(BIG * u[i] for i in u)
    for i, c in enumerate(cands):
        prob += pulp.lpSum(x[key] for key in x if key[0] == i) + u[i] == A[i]
    for num, s in states.items():
        keys = [key for key in x if key[1] == num]
        if not keys:
            continue
        prob += pulp.lpSum(x[key] * k[key] for key in keys) <= max(float(s.total), 0)
        if not s.pooled:
            for cat in {cands[i].category for i, _ in keys}:
                ck = [key for key in keys if cands[key[0]].category == cat]
                prob += pulp.lpSum(x[key] * k[key] for key in ck) <= max(float(s.cat_cap(cat)), 0)
    solver = "—"
    if cands:
        prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=30))
        solver = pulp.LpStatus[prob.status]

    plans, used = [], defaultdict(Decimal)
    for i, c in enumerate(cands):
        lines = []
        for o in options[i]:
            s = o.state
            num = s.grant.number
            v = (pulp.value(x[i, num]) or 0) if (i, num) in x else 0
            if v < 0.01:
                continue
            ils = Decimal(str(v)).quantize(Q)
            amt_g = to_grant_ccy(ils, c, s, p)
            lines.append(Line(grant=s.grant, amount_orig=(ils / c.rate).quantize(Q), amount_ils=ils,
                              amount_grant=amt_g,
                              reason=explain(c, s, o.avail_cat, o.urgency, amt_g, cat_names)))
            used[num] += amt_g
        short = Decimal(str(pulp.value(u[i]) or 0)).quantize(Q) if cands else Decimal("0")
        if short >= Decimal("0.01"):
            text = ("אין מענק כשיר להזמנה הזו." if not options[i] else
                    "אין מענק אחד שהיתרה שלו מכסה את כל הסכום (אחרי שאר ההמלצות).")
            lines.append(Line(grant=None, amount_orig=(short / c.rate).quantize(Q), amount_ils=short, reason=text))
        plans.append(Plan(cand=c, lines=lines, options=options[i], why_not=why_not[i]))

    alerts = []
    for num, s in states.items():
        left = s.total - used.get(num, 0)
        if s.days_left is not None and 0 <= s.days_left <= 120 and left > 0:
            alerts.append(f"{num} מסתיים בעוד {s.days_left} ימים ונותרו בו {left:,.0f} {s.grant.currency} "
                          f"אחרי ההמלצה")
        if s.days_left is not None and -90 <= s.days_left < 0 and s.total > 0:
            alerts.append(f"{num} הסתיים לפני {-s.days_left} ימים ונותרה בו יתרה של {s.total:,.0f} "
                          f"{s.grant.currency} — לבדוק העברה/הארכה")
        neg = [f"{cat_names.get(cat, cat)} {bal:,.0f}" for cat, bal in s.cats.items() if bal < 0]
        if neg and s.total > 0 and not s.pooled and (s.days_left is None or s.days_left >= 0):
            alerts.append(f"{num}: סעיפים בחריגה — " + ", ".join(neg))

    return Result(today=today, sap_batch=sap_batch, sap_as_of=sap_as_of, params=p, states=states,
                  plans=plans, needs_amount=b["needs_amount"], standing=b["standing"], skipped=b["skipped"],
                  alerts=alerts, solver=solver, stale_fx=stale_rates(today))


# --------------------------------------------------------------------------- approve (writes)

class ApprovalError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def approve_plans(decisions, user="", today=None, edited=None):
    """Approve the user's decisions: {order_id: grant_number} — each order is funded in full from one grant.

    Everything is re-validated against fresh balances inside one transaction with the grants locked
    (SELECT … FOR UPDATE on Postgres), so concurrent approvals cannot overdraw a grant. All or nothing.
    Returns the number of approved orders; raises ApprovalError with readable messages.
    """
    p = dict(settings.RESOLVE_DEFAULTS)
    today = today or timezone.localdate()
    edited = edited or set()
    cat_names = dict(BudgetCategory.objects.values_list("code", "name_he"))
    nums = {n for n in decisions.values() if n}
    with transaction.atomic():
        grants = list(Grant.objects.select_for_update().filter(number__in=nums).select_related("last_batch"))
        sap = ImportBatch.latest(ImportBatch.Source.SAP_GRANTS)
        if sap is None:
            raise ApprovalError(["אין יתרות SAP"])
        states = grant_states(today, sap.as_of, grants=[g for g in grants if g.is_active])
        errors, new_allocs = [], []
        used_total, used_cat = defaultdict(Decimal), defaultdict(Decimal)
        orders = {o.pk: o for o in OrderRequest.objects.select_for_update()
                  .filter(pk__in=decisions.keys()).select_related("project")}
        approved_already = set(Allocation.objects.filter(order_id__in=decisions.keys(),
                                                         status=Allocation.Status.APPROVED)
                               .values_list("order_id", flat=True))
        for oid, num in decisions.items():
            o = orders.get(oid)
            if o is None:
                errors.append(f"הזמנה {oid} לא נמצאה")
                continue
            name = o.item_name[:40]
            if oid in approved_already:
                errors.append(f"{name}: כבר אושרה")
                continue
            if not o.amount_est:
                errors.append(f"{name}: אין סכום")
                continue
            if not num:
                errors.append(f"{name}: לא נבחר מענק")
                continue
            try:
                c = make_candidate(o, today)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {e}")
                continue
            s = states.get(num)
            if s is None:
                errors.append(f"{name}: המענק {num} לא פעיל או חסר לו שער חליפין")
                continue
            ok, reason = eligible(c, s, today)
            if not ok:
                errors.append(f"{name}: המענק {num} אינו כשיר — {reason}")
                continue
            ils = (o.amount_est * c.rate).quantize(Q)
            amt_g = to_grant_ccy(ils, c, s, p)
            if s.total - used_total[num] - amt_g < -TOL:
                errors.append(f"{name}: חריגה מהיתרה של {num} — זמין {s.total - used_total[num]:,.2f}, "
                              f"מבוקש {amt_g:,.2f} {s.grant.currency}")
                continue
            if not s.pooled and s.cat_cap(c.category) - used_cat[num, c.category] - amt_g < -TOL:
                errors.append(f"{name}: חריגה בסעיף {cat_names.get(c.category, c.category)} של {num} — "
                              f"זמין {s.cat_cap(c.category) - used_cat[num, c.category]:,.2f}, "
                              f"מבוקש {amt_g:,.2f} {s.grant.currency}")
                continue
            urg = cost(c, s, p)[1]
            reason_txt = explain(c, s, s.cat_cap(c.category) - used_cat[num, c.category], urg, amt_g, cat_names)
            used_total[num] += amt_g
            used_cat[num, c.category] += amt_g
            new_allocs.append(Allocation(
                order=o, grant=s.grant, category_id=c.category, amount_orig=o.amount_est.quantize(Q),
                currency_orig=c.currency, amount_ils=ils, amount_grant_ccy=amt_g,
                fx_rate=(c.rate * (1 + margin_for(c, s, p)) / s.rate).quantize(R6), fx_date=today,
                status=Allocation.Status.APPROVED, decided_by=user, decided_at=timezone.now(),
                reason=("נבחר ידנית · " if oid in edited else "") + reason_txt))
        if errors:
            raise ApprovalError(errors)
        run = ResolveRun.objects.create(run_date=today, created_by=user, sap_batch=sap,
                                        params={**p, "sap_as_of": sap.as_of.isoformat()},
                                        summary={"approved_orders": len(decisions), "lines": len(new_allocs)})
        for a in new_allocs:
            a.run = run
        Allocation.objects.bulk_create(new_allocs)
    return len(decisions)
