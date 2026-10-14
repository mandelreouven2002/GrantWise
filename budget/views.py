import json
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import quote
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.deletion import ProtectedError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.views.decorators.http import require_POST

from . import ai_assist, autofetch
from .amounts import is_material, order_amount
from . import resolve as resolver
from .fx import MissingRate, rate_to_ils
from .importers import run_import
from .importers.preview import dry_run
from .models import (Allocation, AutoFetchJob, OrderFile, PendingImport, SourceCredential, BudgetCategory, FxRate, Grant, ImportBatch, InventoryItem, OrderRequest,
                     ResolveRun, TrackedCurrency)
from .tables import ADMIN_ONLY, LINE_CATS, STATUSES as STATUS_CHOICES, TABLES, cell, choices_for, create_row, filtered, last_import, set_value


# --------------------------------------------------------------------------- table helpers

def table_ctx(key, qs, request=None, extras=None, paginate=True, row_class=None,
              title=None, parent=None, hide=()):
    """Everything the _table.html include needs."""
    t = TABLES[key]
    page = None
    if paginate and request is not None:
        page = Paginator(qs, t.per_page).get_page(request.GET.get("page"))
        objs = list(page.object_list)
    else:
        objs = list(qs)
    cols = [c for c in t.cols if not c.hidden and c.field not in hide]
    col_choices = {c.field: choices_for(c) for c in cols}
    rows = []
    for o in objs:
        ex = (extras or {}).get(o.pk, {})
        cells = []
        for c in cols:
            d = cell(c, o, ex)
            ch = col_choices[c.field]
            if ch and d.get("value") not in ("", None) and d["value"] not in {v for v, _ in ch}:
                ch = [(d["value"], d["value"])] + list(ch)   # keep unexpected values visible
            d["choices"] = ch
            cells.append(d)
        rows.append(dict(obj=o, pk=o.pk, cells=cells, cls=row_class(o, ex) if row_class else ""))
    qd = request.GET.copy() if request else None
    if qd is not None:
        qd.pop("page", None)
    return dict(table=t, cols=cols, rows=rows, page=page, qs=qd.urlencode() if qd else "",
                title=title or t.title, parent=parent,
                add_cols=[dict(col=c, choices=choices_for(c)) for c in t.add_cols()],
                last=last_import(t))


def _cell_with_choices(col, obj):
    d = cell(col, obj)
    ch = choices_for(col)
    if ch and d.get("value") not in ("", None) and d["value"] not in {v for v, _ in ch}:
        ch = [(d["value"], d["value"])] + list(ch)
    d["choices"] = ch
    return d


# An order with one of these decisions is out of the orders page and the recommendations for good
# (recycling rejects the decision, which is the only way back).
DECIDED = [Allocation.Status.APPROVED, Allocation.Status.SUPERSEDED]


def _awaiting_decision():
    """Approved in LabSuit, not ordered yet, and no funding decision approved here."""
    return (OrderRequest.objects.filter(is_current=True, status__in=resolver.OPEN_STATUSES)
            .exclude(allocations__status__in=DECIDED).distinct())


def _ils(amount, ccy):
    try:
        r, _ = rate_to_ils(ccy)
        return (amount or 0) * r
    except MissingRate:
        return None


@login_required
def grants_view(request):
    today = timezone.localdate()
    show_all = request.GET.get("all") == "1"
    grants = Grant.objects.all() if show_all else Grant.objects.filter(is_active=True)
    pending = defaultdict(Decimal)     # approved here, or ordered after the SAP snapshot
    pending_cat = defaultdict(Decimal)  # same, per (grant, category)
    sap = ImportBatch.latest("sap_grants")
    for a in Allocation.objects.filter(status=Allocation.Status.APPROVED, grant__isnull=False).select_related("order"):
        if not resolver.covered_by_sap(a, sap.as_of if sap else None):
            pending[a.grant_id] += a.amount_grant_ccy or 0
            pending_cat[a.grant_id, a.category_id] += a.amount_grant_ccy or 0
    for num, cat, amount, _o in resolver.unseen_orders(sap.as_of if sap else None, today):
        pending[num] += amount
        pending_cat[num, cat] += amount

    extras, total_ils = {}, Decimal("0")
    for g in grants:
        lines = {l.category_id: l.balance for l in g.current_lines()}
        d = g.days_left(today)
        avail = (g.balance_total or 0) - pending[g.number]
        flags = []
        if d is not None:
            if d < 0:
                if g.balance_total > 0:
                    flags.append(("bad", "הסתיים עם יתרה"))
            elif d <= 45:
                flags.append(("bad", "יפוג בתוך 45 ימים"))
            elif d <= 90:
                flags.append(("warn", "יפוג בתוך 90 ימים"))
        ils = _ils(avail, g.currency)
        if ils and avail > 0 and (d is None or d >= 0):
            total_ils += ils
        extras[g.pk] = {
            **{f"line:{k}": v for k, v in lines.items()},
            **{f"after:{cat}": f"{lines.get(cat, Decimal('0')) - amt:,.0f}"
               for (num, cat), amt in pending_cat.items() if num == g.number and amt},
            # days left + status dots (hover for the text) instead of a separate alerts column
            "days": format_html("{}{}", "" if d is None else (format_html('<span class="muted">הסתיים</span>') if d < 0 else d),
                                format_html_join("", ' <span class="tag dot {}" title="{}"></span>', flags)),
            "pending": f"{pending[g.number]:,.0f}" if pending[g.number] else "",
            "available": format_html("<b>{}</b>", f"{avail:,.0f}"),
            "ils": f"{ils:,.0f}" if ils is not None else "",
            "_expired": d is not None and d < 0, "_end": g.end_date or date.max,
        }
    ordered = sorted(grants, key=lambda g: (extras[g.pk]["_expired"], extras[g.pk]["_end"]))
    # every budget category of the SAP file is a column — except those no grant on the page has a line for
    used = {k for ex in extras.values() for k in ex if k.startswith("line:")}
    hide = {f"line:{c}" for c, _ in LINE_CATS} - used
    t = table_ctx("grants", ordered, request, extras=extras, paginate=False,
                  row_class=lambda o, ex: "dim" if ex.get("_expired") else "", hide=hide)
    return render(request, "budget/grants.html", dict(
        t=t, total_ils=total_ils, show_all=show_all,
        open_orders=_awaiting_decision().filter(date_requested__gte=today - timedelta(days=180)).count()))


MATERIAL_Q = Q(category_id="MATERIAL") | Q(category__isnull=True)       # a request without a category is a material


def _orders_page(request, kind):
    """The two lists of requests: "materials" (the LabSuit update lives here; the amount is the quote's two totals,
    with and without VAT) and "other" (every other category; one typed amount, no VAT)."""
    key = "orders" if kind == "materials" else "orders_other"
    g = request.GET
    years = [d.year for d in OrderRequest.objects.dates("date_requested", "year", order="DESC")]
    year = g.get("year", str(years[0]) if years else "")
    # Orders with a funding decision are not listed here: they wait in the recycle bin on the recommendations
    # page until marked "עודכן במערכות המעבדה" (then they disappear for good) or recycled back.
    qs = (OrderRequest.objects.filter(is_current=True).select_related("category")
          .exclude(allocations__status__in=DECIDED))
    qs = qs.filter(MATERIAL_Q) if kind == "materials" else qs.exclude(MATERIAL_Q)
    if year and year != "all":
        qs = qs.filter(date_requested__year=int(year))
    if g.get("status"):
        qs = qs.filter(status=g["status"])
    if g.get("open") == "1":
        qs = qs.filter(status__in=resolver.OPEN_STATUSES)
    if g.get("noamount") == "1":
        no_amount = Q(amount_est__isnull=True)
        if kind == "materials":
            no_amount &= Q(quoted_total_incl_vat__isnull=True, quoted_total_excl_vat__isnull=True)
        qs = qs.filter(no_amount)
    if g.get("pending") == "1":
        qs = qs.filter(pk__in=_awaiting_decision().values("pk"))
    qs = filtered(TABLES[key], qs, g).order_by("-date_requested", "-id")
    t = table_ctx(key, qs, request)
    stored = dict(OrderFile.objects.filter(order_id__in=[r["pk"] for r in t["rows"]])
                  .values_list("order_id", "file_name"))
    for r in t["rows"]:              # the calculated "quote PDF" column: a link to the stored file
        if r["pk"] in stored:
            for c in r["cells"]:
                if c["col"].field == "quote_pdf":
                    c["value"] = format_html('<a href="{}" target="_blank" rel="noopener" title="{}">📎 PDF</a>',
                                             reverse("order_file", args=[r["pk"]]), stored[r["pk"]])
    in_bin = Allocation.objects.filter(status=Allocation.Status.APPROVED, lab_updated_at__isnull=True,
                                       order__is_current=True)
    in_bin = in_bin.filter(order__in=OrderRequest.objects.filter(MATERIAL_Q if kind == "materials" else ~MATERIAL_Q))
    return render(request, "budget/orders.html", dict(
        t=t, total=qs.count(), years=years, year=year, kind=kind, status_choices=STATUS_CHOICES,
        page_title="הזמנות חומרים" if kind == "materials" else "אחר",
        grant_options=[(g.number, f"{g.number} · {(g.sponsor or g.description or '')[:40]}".rstrip(" ·"))
                       for g in Grant.objects.order_by("number")],
        in_bin=in_bin.values("order_id").distinct().count()))


@login_required
def orders_view(request):
    return _orders_page(request, "materials")


@login_required
def other_view(request):
    return _orders_page(request, "other")


@login_required
def order_file(request, pk):
    """The PDF LabSuit has attached to this request (the supplier's quote), served inline."""
    f = get_object_or_404(OrderFile, order_id=pk)
    resp = HttpResponse(bytes(f.data), content_type="application/pdf")
    resp["Content-Disposition"] = "inline; filename*=UTF-8''" + quote(f.file_name or "quote.pdf")
    return resp


def _resolve_params(request):
    p = request.GET
    return dict(respect_labels=False,
                lookback_days=int(p.get("lookback") or 180))


@login_required
def resolve_view(request):
    """Computes the recommendation on every visit — in memory only; nothing is saved until approval."""
    params = _resolve_params(request)
    constraints = request.session.get("ai_constraints", [])
    skip = request.session.get("ai_skip", [])
    result, error = None, None
    try:
        result = resolver.compute(constraints=constraints, skip_orders=skip, **params)
    except (ValueError, MissingRate) as e:
        error = str(e)

    cat_names_he = dict(BudgetCategory.objects.values_list("code", "name_he"))
    rows, payload = [], {"margin": float(settings.RESOLVE_DEFAULTS["fx_margin"]), "grants": {}, "orders": {},
                         }
    if result:
        used_grants = set()
        for plan in result.plans:
            c, o = plan.cand, plan.cand.order
            opts = []
            for op in plan.options:
                g, st = op.state.grant, op.state
                used_grants.add(g.number)
                opts.append(dict(num=g.number, label=(g.sponsor or g.description)[:40], currency=g.currency,
                                 end=g.end_date, days=st.days_left, avail_cat=op.avail_cat, total=st.total,
                                 pooled=st.pooled, rank=op.rank, urgent=op.urgency >= 0.5,
                                 reserve=g.is_reserve, priority=g.pi_priority, blocked=op.blocked))
            first = next((l for l in plan.lines if l.grant), None)
            rem = rem_where = None
            if first:
                op = next((x for x in opts if x["num"] == first.grant.number), None)
                if op:
                    rem = (op["total"] if op["pooled"] else op["avail_cat"]) - first.amount_grant
                    rem_where = "מכלל המאגר" if op["pooled"] else "בסעיף " + cat_names_he.get(c.category, c.category)
            rows.append(dict(order=o, state=plan.state, lines=plan.lines, options=opts,
                             amt=c.amount, amt_cur=c.currency, is_material=c.material,
                             pay=first.amount_grant if first else None,
                             rem=rem, rem_where=rem_where, rem_cur=first.grant.currency if first else "",
                             why_not=sorted(plan.why_not.items())))
            payload["orders"][o.pk] = dict(amount=float(c.amount), cur=c.currency, rate=float(c.rate),
                                           cat=c.category, opts=[x["num"] for x in opts],
                                           mat=c.material, ratio=float(c.vat_ratio))
        for o in result.needs_amount:
            rows.append(dict(order=o, state="noamount", lines=[], options=[], why_not=[], amt=None, amt_cur="",
                             is_material=is_material(o)))
        # Everything still open on the orders page is listed here, marked with why it gets no recommendation
        # (orders already decided here are in the recycle bin, or gone once marked as updated).
        decided = set(Allocation.objects.filter(status__in=DECIDED).values_list("order_id", flat=True))
        for o, why in result.skipped:
            if o.pk not in decided:
                a = order_amount(o)
                rows.append(dict(order=o, state="skipped", lines=[], options=[], why_not=[], note=why,
                                 amt=a.gross if a else None, amt_cur=a.currency if a else "",
                                 is_material=is_material(o), user_skipped=o.pk in result.user_skipped))
        for o in result.standing:
            a = order_amount(o)
            rows.append(dict(order=o, state="standing", lines=[], options=[], why_not=[],
                             amt=a.gross if a else None, amt_cur=a.currency if a else "", is_material=is_material(o),
                             note="על הזמנה עומדת / שמור-מכור — כבר מחויבת ב-SAP, לא דורשת החלטה"))
        for num in used_grants:
            st = result.states[num]
            g = st.grant
            payload["grants"][num] = dict(
                cur=g.currency, rate=float(st.rate), total=float(st.total), pooled=st.pooled,
                cats={k: float(v) for k, v in st.cats.items()}, extra={k: float(v) for k, v in st.extra.items()},
                label=(g.sponsor or g.description)[:40], end=g.end_date.strftime("%d/%m/%Y") if g.end_date else "",
                days=st.days_left, vatReq=bool(g.vat_required), held=float(result.held.get(num, 0)))
        rank = {"unfunded": 0, "ok": 1, "noamount": 2, "skipped": 3, "standing": 3}
        rows.sort(key=lambda r: (rank[r["state"]], -(r["order"].date_requested or date.min).toordinal()))
    payload["catNames"] = dict(BudgetCategory.objects.values_list("code", "name_he"))

    count = lambda *states: sum(1 for r in rows if r["state"] in states)  # noqa: E731
    return render(request, "budget/resolve.html", dict(
        result=result, error=error, rows=rows, bin=_recycle_bin(), params=params, payload=payload,
        ai_ready=ai_assist.configured(), ai_chips=_ai_chips(constraints),
        ai_summary=request.session.get("ai_summary", ""), ai_max=ai_assist.MAX_INSTRUCTION_CHARS,
        n_user_skipped=len(result.user_skipped) if result else 0,
        n_ready=count("ok", "unfunded"), n_noamount=count("noamount"),
        n_unfunded=count("unfunded"), n_skipped=count("skipped"), n_standing=count("standing")))


def _ai_chips(constraints):
    """The active instructions as (index, plain-Hebrew text) — shown under the AI input, each removable."""
    if not constraints:
        return []
    grant_names = {g.number: (g.sponsor or g.description or "")[:30] for g in Grant.objects.all()}
    ids = [c["order"] for c in constraints if c.get("order") is not None]
    order_names = {o.pk: o.item_name[:30] for o in OrderRequest.objects.filter(pk__in=ids)}
    category_names = dict(BudgetCategory.objects.values_list("code", "name_he"))
    return [(i, ai_assist.describe(c, grant_names, order_names, category_names)) for i, c in enumerate(constraints)]


@login_required
@require_POST
def resolve_ai(request):
    """AI assist: a typed instruction becomes constraints for the optimiser (kept in the session only).
    The model returns a closed set of constraints — it never picks amounts or writes anything."""
    session = request.session
    back = reverse("resolve") + ("?" + request.GET.urlencode() if request.GET else "")
    if request.POST.get("unskip"):
        session.pop("ai_skip", None)
        messages.info(request, "כל הבקשות חזרו לחישוב.")
        return redirect(back)
    if request.POST.get("selection") == "1":
        # The requests the user unticked on the page stay out of every calculation until ticked again.
        skip = []
        for v in request.POST.getlist("skip"):
            try:
                skip.append(int(v))
            except ValueError:
                pass
        session["ai_skip"] = skip
        if not skip:
            session.pop("ai_skip", None)
    if request.POST.get("recalc"):
        n = len(session.get("ai_skip", []))
        messages.success(request, "ההמלצה חושבה מחדש" + (f" ללא {n} בקשות שהוסרו מהבחירה." if n else " לכל הבקשות."))
        return redirect(back)
    if request.POST.get("clear"):
        session.pop("ai_constraints", None)
        session.pop("ai_summary", None)
        messages.info(request, "ההוראות בוטלו — ההמלצה חזרה לחישוב הרגיל.")
        return redirect(back)
    current = list(session.get("ai_constraints", []))
    if request.POST.get("remove") not in (None, ""):
        try:
            current.pop(int(request.POST["remove"]))
        except (ValueError, IndexError):
            pass
        session["ai_constraints"] = current
        if not current:
            session.pop("ai_summary", None)
        return redirect(back)

    text = (request.POST.get("instruction") or "").strip()[:ai_assist.MAX_INSTRUCTION_CHARS]
    if not text:
        messages.error(request, "לא נכתבה הוראה.")
        return redirect(back)
    if not ai_assist.configured():
        messages.error(request, "ה-AI לא מוגדר: חסרים AWS_BEARER_TOKEN_BEDROCK ו-AWS_REGION בסביבה.")
        return redirect(back)
    if not ai_assist.check_rate(session):
        messages.error(request, f"הגעת למגבלה של {ai_assist.CALLS_PER_HOUR} בקשות AI בשעה. אפשר לנסות שוב מאוחר יותר.")
        return redirect(back)
    try:
        result = resolver.compute(constraints=current, skip_orders=session.get("ai_skip", []),
                                  **_resolve_params(request))
        ctx = ai_assist.build_context(result, current)
        reply = ai_assist.ask(text, ctx.text)
    except (ValueError, MissingRate) as e:
        messages.error(request, str(e))
        return redirect(back)
    except ai_assist.AiError as e:
        messages.error(request, str(e))
        return redirect(back)

    new, dropped = ai_assist.validate(reply, ctx.grants, ctx.orders, ctx.categories, ctx.sponsors, ctx.grant_info)
    # Ignoring / bringing back requests is a change of the page's selection (the ticks), not a funding rule.
    ops = [c for c in new if c["type"] in ai_assist.SELECTION_TYPES]
    new = [c for c in new if c["type"] not in ai_assist.SELECTION_TYPES]
    if ops:
        skip = set(session.get("ai_skip", []))
        n_out = n_in = 0
        for c in ops:
            if c["type"] == "skip_order":
                n_out += c["order"] not in skip
                skip.add(c["order"])
            elif c["type"] == "include_order":
                n_in += c["order"] in skip
                skip.discard(c["order"])
            else:
                n_in += len(skip)
                skip.clear()
        if skip:
            session["ai_skip"] = sorted(skip)
        else:
            session.pop("ai_skip", None)
        said = []
        if n_out:
            said.append(f"{n_out} בקשות הוצאו מהחישוב (הסימון שלהן הוסר)")
        if n_in:
            said.append(f"{n_in} בקשות הוחזרו לחישוב (סומנו)")
        messages.success(request, "; ".join(said) + "." if said else "אין שינוי בבחירת הבקשות.")
    if new:
        session["ai_constraints"] = ai_assist.merge(current, new)
        summary = str(reply.get("summary_he") or "").strip()[:300]
        session["ai_summary"] = summary
        messages.success(request, "ההוראה הופעלה — ההמלצה חושבה מחדש (לא נשמר דבר במסד הנתונים)."
                                  + (f" {summary}" if summary else ""))
    elif not (ops or reply.get("clarify") or reply.get("unsupported")):
        messages.warning(request, "לא הצלחתי להפוך את ההוראה לשינוי בהמלצה. אפשר לנסח אותה מחדש, "
                                  "עם שם המענק או הבקשה.")
    if reply.get("clarify"):
        messages.info(request, str(reply["clarify"])[:300])
    if reply.get("unsupported"):
        messages.warning(request, str(reply["unsupported"])[:300])
    if dropped:
        messages.warning(request, f"חלק מההוראה לא הופעל ({len(dropped)}): " + "; ".join(sorted(set(dropped))))
    return redirect(back)


def _recycle_bin():
    """Approved decisions not yet marked "עודכן במערכות המעבדה", one entry per order, with the recommendation's
    reason. These orders are hidden from the orders page; recycling one sends it back there, marking it
    removes it from the system's lists while its amount stays deducted from the grant (until SAP covers it)."""
    groups = {}
    lines = (Allocation.objects.filter(status=Allocation.Status.APPROVED, lab_updated_at__isnull=True)
             .select_related("order", "grant", "category").order_by("-decided_at", "id"))
    for a in lines:
        g = groups.setdefault(a.order_id, dict(order=a.order, lines=[], pk=a.pk, decided_by=a.decided_by,
                                               decided_at=a.decided_at, lab_updated_at=a.lab_updated_at,
                                               then=Decimal("0")))
        g["lines"].append(a)
        g["then"] += a.amount_ils or 0
    for g in groups.values():
        o = g["order"]
        a = order_amount(o)
        g["amt"], g["amt_cur"] = (a.gross, a.currency) if a else (None, "")
        g["now"] = _ils(a.gross, a.currency) if a else None
        # the exchange rate (or the amount, if LabSuit changed it) moved since the approval
        g["drift"] = bool(g["now"] is not None and g["then"] and abs(g["now"] - g["then"]) / g["then"] > Decimal("0.005"))
    return list(groups.values())


@login_required
@require_POST
def resolve_approve(request):
    decisions, edited = {}, set()
    for oid in request.POST.getlist("order"):
        oid = int(oid)
        decisions[oid] = request.POST.get(f"g_{oid}", "")      # one grant funds the whole order
        if request.POST.get(f"edited_{oid}") == "1":
            edited.add(oid)
    nxt = request.POST.get("next") or "resolve"
    if not decisions:
        messages.error(request, "לא סומנו הזמנות לאישור")
        return redirect(nxt)
    try:
        n = resolver.approve_plans(decisions, user=request.user.get_username(), edited=edited)
        messages.success(request, f"אושרו {n} הזמנות והועברו לסל המחזור (למטה) — הן כבר לא מופיעות בדפי ההזמנות (חומרים / אחר). "
                                  "היתרות בכל הדשבורדים עודכנו (\"ממתין\"). "
                                  "נשאר לעדכן ידנית את ה-GRANT_ID ב-LabSuit / את ההזמנה ב-SAP — "
                                  "האישור כאן אינו הזמנת רכש.")
    except resolver.ApprovalError as e:
        messages.error(request, "שום דבר לא אושר — יש לתקן ולנסות שוב:")
        for err in e.errors:
            messages.error(request, err)
    return redirect(nxt)


@login_required
@require_POST
def allocation_recycle(request, pk):
    """Recycle: the order was not really approved after all — drop the decision (its frozen amounts and
    rates) and send the order back to the orders page, where the next recommendation prices it afresh."""
    a = get_object_or_404(Allocation, pk=pk, status=Allocation.Status.APPROVED, order__isnull=False)
    Allocation.objects.filter(order=a.order, status=Allocation.Status.APPROVED).update(
        status=Allocation.Status.REJECTED, decided_by=request.user.get_username(), decided_at=timezone.now())
    messages.success(request, f"הוחזר לדף ההזמנות (חומרים / אחר): {a.order.item_name}. "
                              "ההמלצה תחושב מחדש לפי השערים והיתרות של היום.")
    return redirect("resolve")


@login_required
@require_POST
def allocation_lab_updated(request, pk):
    """'עודכן במערכות המעבדה': the order is deleted from the system. Its funding stays on the grant
    (reserved) until a SAP report covers it. There is no way back — a later export brings the request again
    as a new one."""
    a = get_object_or_404(Allocation, pk=pk, status=Allocation.Status.APPROVED)
    nxt = request.POST.get("next") or "resolve"
    if a.order_id is None or a.lab_updated_at:
        messages.info(request, "ההזמנה כבר סומנה כמעודכנת והוסרה מהמערכת.")
        return redirect(nxt)
    name = a.item_name
    Allocation.objects.filter(order=a.order, status=Allocation.Status.APPROVED).update(
        lab_updated_at=timezone.now(), lab_updated_by=request.user.get_username())
    resolver.retire_orders([a.order_id])
    n = resolver.reconcile()
    tail = " ההחלטה כבר כלולה ביתרת SAP האחרונה." if n else " הסכום נשאר שמור עד שדוח SAP יכסה את ההזמנה."
    messages.success(request, f"סומן כעודכן במערכות המעבדה והוסר מהמערכת: {name}. "
                              f"הסכום נשאר מנוכה מהמענק.{tail}")
    return redirect(nxt)


# --------------------------------------------------------------------------- generic API

def _table(key):
    if key not in TABLES:
        raise Http404
    return TABLES[key]


@login_required
@require_POST
def api_update(request, key, pk):
    t = _table(key)
    if key in ADMIN_ONLY and not request.user.is_superuser:
        return JsonResponse({"ok": False, "error": "רק מנהל/ת יכול/ה לערוך משתמשים"}, status=403)
    obj = get_object_or_404(t.model, pk=pk)
    try:
        data = json.loads(request.body)
        if key == "users" and obj == request.user and data.get("field") in ("is_active", "is_superuser") \
                and data.get("value") in (False, "false", "0", ""):
            raise ValidationError("אי אפשר לבטל את ההרשאות של המשתמש שמחובר עכשיו")
        set_value(t, obj, data["field"], data.get("value"))
        if key == "users" and data["field"] == "is_superuser":
            obj.is_staff = obj.is_superuser
            obj.save(update_fields=["is_staff"])
    except ValidationError as e:
        msgs = e.messages if hasattr(e, "messages") else [str(e)]
        return JsonResponse({"ok": False, "error": " ".join(msgs)}, status=400)
    except (KeyError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "בקשה לא תקינה"}, status=400)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def api_create(request, key):
    t = _table(key)
    if key in ADMIN_ONLY and not request.user.is_superuser:
        messages.error(request, "רק מנהל/ת יכול/ה להוסיף משתמשים")
        return redirect(request.POST.get("next") or "/")
    parent = None
    if request.POST.get("parent"):
        parent = get_object_or_404(Grant, pk=request.POST["parent"])
    try:
        create_row(t, request.POST, parent=parent)
        messages.success(request, f"נוספה שורה ל{t.title}")
    except ValidationError as e:
        messages.error(request, "לא נוספה שורה: " + " ".join(e.messages))
    return redirect(request.POST.get("next") or "/")


@login_required
@require_POST
def api_delete(request, key, pk):
    t = _table(key)
    if not t.deletable:
        raise Http404
    obj = get_object_or_404(t.model, pk=pk)
    if key in ("orders", "orders_other"):
        # Deleting is deleting (a LabSuit request comes back as a new one if a later export lists it again).
        # A decision that is not marked "updated" goes with its order: its money is released.
        Allocation.objects.filter(order=obj, lab_updated_at__isnull=True).delete()
    try:
        obj.delete()
    except ProtectedError:
        if key != "grants":
            raise
        messages.error(request, "לא ניתן למחוק את המענק כי הוא מקושר להחלטות מימון.")
    else:
        if key == "grants":
            messages.success(request, "נתוני המענק נמחקו.")
    return redirect(request.POST.get("next") or "/")


@login_required
@require_POST
def api_import(request, key):
    """Step 1: upload → dry run → preview page. Nothing is written until the user confirms."""
    t = _table(key)
    f = request.FILES.get("file")
    nxt = request.POST.get("next") or "/"
    if not f or not t.source:
        messages.error(request, "לא נבחר קובץ")
        return redirect(nxt)
    try:
        as_of = date.fromisoformat(request.POST.get("as_of") or date.today().isoformat())
        latest = ImportBatch.latest(t.source)
        if t.source in ("sap_grants", "sap_budget") and latest and as_of < latest.as_of \
                and request.POST.get("confirm_old") != "1":
            messages.error(request, f"הקובץ מתאריך {as_of:%d/%m/%Y}, ישן מהנתונים הפעילים ({latest.as_of:%d/%m/%Y}). "
                                    "לא עודכן. כדי לייבא בכל זאת, יש לאשר בחלון העדכון.")
            return redirect(nxt)
        PendingImport.objects.filter(created_at__lt=timezone.now() - timedelta(days=1)).delete()
        min_year = request.POST.get("min_year")
        p = PendingImport.objects.create(
            table_key=key, source=t.source, file_name=f.name, data=b"".join(f.chunks()),
            mode=request.POST.get("mode", "merge"), as_of=as_of,
            min_year=int(min_year) if min_year and min_year.isdigit() else None,
            next_url=nxt, uploaded_by=request.user.get_username())
        _build_preview(p)
    except Exception as e:  # show parsing problems instead of a 500
        PendingImport.objects.filter(table_key=key, uploaded_by=request.user.get_username(), preview={}).delete()
        messages.error(request, f"קריאת הקובץ נכשלה: {e}")
        return redirect(nxt)
    return redirect("import_preview", pk=p.pk)


def _build_preview(p):
    attachments = (p.preview or {}).get("attachments")        # what the robot scraped; not part of the dry run
    p.preview = dry_run(p.source, bytes(p.data), p.file_name, p.as_of, p.mode, user=p.uploaded_by, **p.kwargs())
    if attachments:
        p.preview["attachments"] = attachments
    p.save(update_fields=["preview", "mode"])


class PreviewGone(Exception):
    pass


def _pending(request, pk):
    p = PendingImport.objects.filter(pk=pk).first()
    if p is None or (p.uploaded_by != request.user.get_username() and not request.user.is_superuser):
        raise PreviewGone
    return p


def _gone(request):
    """A preview that was already confirmed / cancelled (back button, refresh, double click, an old tab)."""
    messages.info(request, "התצוגה המקדימה הזו כבר לא פעילה: העדכון כבר אושר או בוטל. "
                           "כדי לעדכן שוב, בחרו שוב \"עדכון מקובץ\" או \"עדכון אוטומטי\".")
    ref = request.META.get("HTTP_REFERER", "")
    return redirect(ref if ref and "/import/" not in ref else "/")


def preview_view(fn):
    def wrapper(request, pk):
        try:
            return fn(request, pk)
        except PreviewGone:
            return _gone(request)
    wrapper.__name__ = fn.__name__
    return wrapper


@login_required
@preview_view
def import_preview(request, pk):
    p = _pending(request, pk)
    return render(request, "budget/import_preview.html",
                  dict(p=p, v=p.preview, title=TABLES[p.table_key].title,
                       source_label=ImportBatch.Source(p.source).label))


@login_required
@require_POST
@preview_view
def import_mode(request, pk):
    p = _pending(request, pk)
    p.mode = "replace" if p.mode == "merge" else "merge"
    try:
        _build_preview(p)
    except Exception as e:  # noqa: BLE001
        messages.error(request, f"קריאת הקובץ נכשלה: {e}")
    return redirect("import_preview", pk=p.pk)


@login_required
@require_POST
@preview_view
def import_confirm(request, pk):
    """Step 2: the real import, from the stored bytes."""
    p = _pending(request, pk)
    nxt = p.next_url or "/"
    try:
        batch = run_import(p.source, bytes(p.data), p.file_name, p.as_of, p.mode,
                           user=request.user.get_username(), **p.kwargs())
        c = p.preview.get("counts", {})
        parts = [f"{c.get('new', 0)} חדשות", f"{c.get('changed', 0)} השתנו"]
        if c.get("hidden"):
            parts.append(f"{c['hidden']} הוסתרו")
        if batch.stats.get("reconciled"):
            parts.append(f"{batch.stats['reconciled']} החלטות נכללו ביתרת SAP")
        if batch.stats.get("lab_marked"):
            parts.append(f"{batch.stats['lab_marked']} החלטות זוהו כרשומות ב-LabSuit")
        saved_files = autofetch.attach_pending_files(p)
        if saved_files:
            parts.append(f"{saved_files} קבצי PDF נשמרו")
        messages.success(request, f"עודכן מהקובץ {p.file_name} ({'Replace' if p.mode == 'replace' else 'Merge'}) — "
                                  + ", ".join(parts))
        p.delete()
    except Exception as e:  # noqa: BLE001
        messages.error(request, f"העדכון מהקובץ נכשל: {e}")
    return redirect(nxt)


@login_required
@require_POST
@preview_view
def import_cancel(request, pk):
    p = _pending(request, pk)
    nxt = p.next_url or "/"
    p.delete()
    messages.info(request, "העדכון בוטל. שום דבר לא השתנה.")
    return redirect(nxt)


# --------------------------------------------------------------------------- automatic update (LabSuit robot)

@login_required
@require_POST
def api_autofetch(request, key):
    """Start the robot for a LabSuit dashboard. Optional username/password replace the saved ones."""
    if key not in settings.LABSUIT_URLS:
        raise Http404
    t = _table(key)
    user, pw = request.POST.get("username", "").strip(), request.POST.get("password", "")
    if user or pw:
        if not (user and pw):
            return JsonResponse({"ok": False, "error": "צריך גם שם משתמש וגם סיסמה."}, status=400)
        cred = SourceCredential.objects.filter(source="labsuit").first() or SourceCredential(source="labsuit")
        cred.username, cred.updated_by = user, request.user.get_username()
        cred.set_password(pw)
        cred.save()
    saved_user, saved_pw = autofetch.credentials()
    if not saved_user or not saved_pw:
        return JsonResponse({"ok": False, "need_credentials": True,
                             "error": "חסרים שם משתמש וסיסמה ל-LabSuit."}, status=400)
    recent = timezone.now() - timedelta(minutes=30)      # the PDFs can make a run last many minutes
    job = AutoFetchJob.objects.filter(table_key=key, created_at__gte=recent,
                                      status__in=[AutoFetchJob.Status.QUEUED, AutoFetchJob.Status.RUNNING]).first()
    if job is None:
        job = AutoFetchJob.objects.create(table_key=key, source=t.source, next_url=request.POST.get("next") or "/",
                                          created_by=request.user.get_username(), step="מתחיל")
        autofetch.start_job(job)
    return JsonResponse({"ok": True, "job": job.pk})


@login_required
def api_autofetch_status(request, pk):
    job = get_object_or_404(AutoFetchJob, pk=pk)
    if job.status in (AutoFetchJob.Status.QUEUED, AutoFetchJob.Status.RUNNING) and \
            job.created_at < timezone.now() - timedelta(minutes=30):
        # the worker thread died (e.g. a server restart) — don't leave the user waiting forever
        job.status, job.error, job.finished_at = AutoFetchJob.Status.FAILED, "העדכון האוטומטי לא הסתיים בזמן.", timezone.now()
        job.save(update_fields=["status", "error", "finished_at"])
    data = {"status": job.status, "step": job.step, "error": job.error, "detail": job.detail}
    if job.status == AutoFetchJob.Status.DONE and job.pending_id:
        data["preview_url"] = reverse("import_preview", args=[job.pending_id])
    if job.screenshot:
        data["screenshot_url"] = reverse("autofetch_shot", args=[job.pk])
    if job.file_data and job.status == AutoFetchJob.Status.FAILED:
        data["file_url"] = reverse("autofetch_file", args=[job.pk])
    return JsonResponse(data)


@login_required
def autofetch_file(request, pk):
    """The file LabSuit sent, so a rejected download can be inspected or uploaded by hand."""
    job = get_object_or_404(AutoFetchJob, pk=pk)
    if not job.file_data:
        raise Http404
    r = HttpResponse(bytes(job.file_data), content_type="application/octet-stream")
    r["Content-Disposition"] = f'attachment; filename="{job.file_name or "labsuit-export.xlsx"}"'
    return r


@login_required
def autofetch_shot(request, pk):
    job = get_object_or_404(AutoFetchJob, pk=pk)
    if not job.screenshot:
        raise Http404
    return HttpResponse(bytes(job.screenshot), content_type="image/png")


# --------------------------------------------------------------------------- settings

SETTINGS_TABS = [("fx", "שערי חליפין"), ("users", "משתמשים")]


@login_required
def settings_view(request):
    tab = request.GET.get("tab", "fx")
    ctx = dict(tab=tab, tabs=SETTINGS_TABS)
    if tab == "fx":
        extras = {}
        for c in TrackedCurrency.objects.all():
            r = FxRate.objects.filter(currency=c.code).order_by("-date").first()
            extras[c.pk] = dict(rate=(f"{r.rate_to_ils:.4f}" if r.rate_to_ils >= Decimal("0.01")
                                      else f"{r.rate_to_ils:.8f}".rstrip("0")) if r else "—",
                                rate_date=r.date.strftime("%d/%m/%Y") if r else "")
        ctx.update(currencies=table_ctx("currencies", TrackedCurrency.objects.all(), paginate=False, extras=extras,
                                        row_class=lambda o, ex: "" if o.is_active else "dim"),
                   history=table_ctx("fx", FxRate.objects.order_by("-date", "currency")[:60], paginate=False))
    elif tab == "users":
        extras = {u.pk: {"last_login": timezone.localtime(u.last_login).strftime("%d/%m/%Y %H:%M")
                         if u.last_login else "—"}
                  for u in User.objects.all()}
        ctx.update(users=table_ctx("users", User.objects.order_by("username"), paginate=False, extras=extras,
                                   row_class=lambda o, ex: "" if o.is_active else "dim"),
                   all_users=User.objects.order_by("username"))
    else:
        raise Http404
    return render(request, "budget/settings.html", ctx)


@login_required
@require_POST
def set_password(request):
    target = get_object_or_404(User, pk=request.POST.get("user"))
    if target != request.user and not request.user.is_superuser:
        messages.error(request, "רק מנהל/ת יכול/ה לשנות סיסמה של משתמש אחר")
    elif len(request.POST.get("password", "")) < 4:
        messages.error(request, "הסיסמה קצרה מדי (לפחות 4 תווים)")
    else:
        target.set_password(request.POST["password"])
        target.save()
        if target == request.user:
            update_session_auth_hash(request, target)
            request.session["default_password"] = False
        messages.success(request, f"הסיסמה של {target.username} עודכנה")
    return redirect("/settings/?tab=users")
