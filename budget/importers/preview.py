"""Import preview: run an import inside a transaction, diff the data before/after, roll back.

The result is a JSON-safe dict the preview page renders before the user confirms. Nothing is written:
the importer, the reconciliation it triggers and the ImportBatch it creates are all rolled back.
"""
from collections import Counter
from datetime import date
from decimal import Decimal

from django.db import transaction

from ..models import (Allocation, BudgetReportLine, FundingAlias, Grant, GrantRule, ImportBatch, InventoryItem,
                      OrderRequest, ProjectGrant, Reservation)

LIST_CAP = 200          # rows listed per section; counts are always complete

# Hebrew labels for fields that have no verbose_name
LABELS = {
    "manufacturer": "יצרן", "pack_size": "גודל אריזה", "quote_id": "הצעת מחיר", "labsuit_total": "סכום ב-LabSuit",
    "labsuit_currency": "מטבע ב-LabSuit", "date_requested": "תאריך בקשה", "date_approved": "תאריך אישור",
    "date_ordered": "תאריך הזמנה", "date_received": "תאריך קבלה", "date_cancelled": "תאריך ביטול",
    "comments": "הערות", "in_stock": "במלאי?", "sub_location": "תת-מיקום", "price": "מחיר", "currency": "מטבע",
    "supply_date": "תאריך אספקה", "owner": "אחראי/ת", "budget": "תקציב", "is_active": "פעיל",
    "is_total": "שורת סיכום",
}

ORDER_FIELDS = OrderRequest.LABSUIT_FIELDS
INVENTORY_FIELDS = ["name", "catalog_number", "manufacturer", "supplier", "pack_size", "amount_in_stock", "in_stock",
                    "location", "sub_location", "price", "currency", "supply_date", "owner", "comments"]
GRANT_FIELDS = ["sponsor", "currency", "start_date", "end_date", "budget_total", "expense_total", "balance_total",
                "grant_type", "status"]
REPORT_FIELDS = ["budget", "committed", "actual", "balance"]


def label(model, name):
    if name.startswith("line:"):
        return f"יתרה · {name[5:]}"
    try:
        v = str(model._meta.get_field(name).verbose_name)
    except Exception:  # noqa: BLE001
        v = name
    return LABELS.get(name, v) if v.replace(" ", "_") == name else v


def show(v):
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "כן" if v else "לא"
    if isinstance(v, Decimal):
        return f"{v:,.2f}"
    if isinstance(v, date):
        return f"{v:%d/%m/%Y}"
    s = str(v)
    return s if len(s) <= 80 else s[:79] + "…"


# --------------------------------------------------------------------------- snapshots
# {key: (is_current, display label, {field: value})}

def snap_orders(_batch=None):
    rows = OrderRequest.objects.filter(source="labsuit").values("natural_key", "is_current", *ORDER_FIELDS)
    return {r["natural_key"]: (r["is_current"],
                               f'{r["item_name"][:60]} · {show(r["date_requested"])} · {r["requested_by"] or "—"}',
                               {f: r[f] for f in ORDER_FIELDS}) for r in rows}


def snap_inventory(_batch=None):
    rows = InventoryItem.objects.values("key", "is_current", "item_type", *INVENTORY_FIELDS)
    return {r["key"]: (r["is_current"], f'{r["name"][:60]} · {r["item_type"] or "—"}',
                       {f: r[f] for f in INVENTORY_FIELDS}) for r in rows}


def snap_grants(_batch=None):
    out = {}
    for g in Grant.objects.all():
        v = {f: getattr(g, f) for f in GRANT_FIELDS}
        for line in g.current_lines().select_related("category"):
            v[f"line:{line.category.name_he}"] = line.balance
        out[g.number] = (g.is_active, f"{g.number} · {g.sponsor[:50] or '—'}", v)
    return out


def snap_report(batch=None):
    batch = batch or ImportBatch.latest(ImportBatch.Source.SAP_BUDGET)
    out, seen = {}, Counter()
    if batch is None:
        return out
    for line in BudgetReportLine.objects.filter(batch=batch):
        base = f"{line.block}|{line.cost_element or line.label}|{int(line.is_total)}"
        seen[base] += 1
        key = base if seen[base] == 1 else f"{base}#{seen[base]}"
        name = f"{line.block} · {line.cost_element} {line.label}".strip() if not line.is_total else f"{line.block} · סיכום"
        out[key] = (True, name, {f: getattr(line, f) for f in REPORT_FIELDS})
    return out


SNAPSHOTS = {
    ImportBatch.Source.LABSUIT_ORDERS: (snap_orders, OrderRequest),
    ImportBatch.Source.LABSUIT_INVENTORY: (snap_inventory, InventoryItem),
    ImportBatch.Source.SAP_GRANTS: (snap_grants, Grant),
    ImportBatch.Source.SAP_BUDGET: (snap_report, BudgetReportLine),
}


# --------------------------------------------------------------------------- lab-owned data that survives

def preserved_orders(keys):
    qs = OrderRequest.objects.filter(source="labsuit", natural_key__in=keys)
    out = [
        ("הזמנות עם פרויקט שהוגדר ב-LabFund", qs.exclude(project__isnull=True).count()),
        ("הזמנות עם סכום שהוזן ידנית", qs.filter(amount_source=OrderRequest.AmountSource.MANUAL).count()),
        ("הזמנות עם הערות מעבדה", qs.exclude(lab_notes="").count()),
        ("הזמנות עם החלטת מימון שאושרה", qs.filter(allocations__status=Allocation.Status.APPROVED)
         .distinct().count()),
    ]
    return [x for x in out if x[1]]


def preserved_grants(keys):
    default_cutoff = Grant._meta.get_field("order_cutoff_days").default
    qs = Grant.objects.filter(number__in=keys)
    out = [
        ("מענקים עם עדיפות שהוגדרה", qs.exclude(pi_priority=0).count()),
        ("מענקים עם ימי חסימה שהוגדרו", qs.exclude(order_cutoff_days=default_cutoff).count()),
        ("מענקים עם הערות", qs.exclude(notes="").count()),
        ("שריונים", Reservation.objects.filter(grant__in=qs).count()),
        ("כללי סעיף", GrantRule.objects.filter(grant__in=qs).count()),
        ("קישורי פרויקט", ProjectGrant.objects.filter(grant__in=qs).count()),
    ]
    return [x for x in out if x[1]]


# --------------------------------------------------------------------------- dry run

def dry_run(source, data, file_name, as_of, mode, user="", **kw):
    from . import run_import

    snap, model = SNAPSHOTS[source]
    with transaction.atomic():
        before = snap()
        approved_before = set(Allocation.objects.filter(status=Allocation.Status.APPROVED).values_list("pk", flat=True))
        unmarked_before = set(Allocation.objects.filter(status=Allocation.Status.APPROVED, lab_updated_at__isnull=True)
                              .values_list("pk", flat=True))
        aliases_before = set(FundingAlias.objects.values_list("label", flat=True))

        batch = run_import(source, data, file_name, as_of, mode, user=user, **kw)
        after = snap(batch)

        new, changed, hidden, reactivated, unchanged = [], [], [], [], 0
        field_counts, status_moves = Counter(), Counter()
        for key, (cur, name, vals) in after.items():
            old = before.get(key)
            if old is None:
                if cur:
                    new.append(name)
                continue
            was_cur, _, old_vals = old
            if cur and not was_cur:
                reactivated.append(name)
            elif was_cur and not cur:
                hidden.append(name)
                continue
            if not cur:
                continue
            diffs = [(f, old_vals.get(f), v) for f, v in vals.items() if old_vals.get(f) != v]
            diffs += [(f, v, None) for f, v in old_vals.items() if f not in vals and v is not None]
            if diffs:
                changed.append({"label": name, "changes": [[label(model, f), show(a), show(b)] for f, a, b in diffs]})
                field_counts.update(label(model, f) for f, _, _ in diffs)
                if source == ImportBatch.Source.LABSUIT_ORDERS:
                    st = next(((a, b) for f, a, b in diffs if f == "status"), None)
                    if st:
                        status_moves[(st[0] or "—", st[1] or "—")] += 1
            else:
                unchanged += 1
        # rows that vanished from a per-batch source (the SAP report) or were hidden by Replace
        for key, (was_cur, name, _) in before.items():
            if key not in after and was_cur:
                hidden.append(name)

        reconciled = []
        for a in (Allocation.objects.filter(pk__in=approved_before, status=Allocation.Status.SUPERSEDED)
                  .select_related("order", "grant")):
            reconciled.append({"item": a.order.item_name[:60], "grant": a.grant_id,
                               "amount": f"{a.amount_grant_ccy or 0:,.2f} {a.grant.currency if a.grant else ''}"})

        lab_marked = sorted({a.order.item_name[:60] for a in Allocation.objects.filter(
            pk__in=unmarked_before, lab_updated_at__isnull=False).select_related("order")})

        in_file = [k for k, v in after.items() if v[0]]
        file_rows = {ImportBatch.Source.LABSUIT_ORDERS: OrderRequest.objects.filter(batch=batch),
                     ImportBatch.Source.LABSUIT_INVENTORY: InventoryItem.objects.filter(batch=batch),
                     ImportBatch.Source.SAP_GRANTS: Grant.objects.filter(last_batch=batch),
                     ImportBatch.Source.SAP_BUDGET: BudgetReportLine.objects.filter(batch=batch)}[source].count()
        warnings, preserved = [], []
        stats = batch.stats or {}
        if source == ImportBatch.Source.LABSUIT_ORDERS:
            preserved = preserved_orders([k for k in in_file if k in before])
            fresh = sorted(FundingAlias.objects.filter(needs_review=True).exclude(label__in=aliases_before)
                           .values_list("label", flat=True))
            if fresh:
                warnings.append("ערכי GRANT_ID חדשים שלא זוהו — לבדוק בטבלת תוויות המימון: " + ", ".join(fresh[:15])
                                + (f" ועוד {len(fresh) - 15}" if len(fresh) > 15 else ""))
            gone = OrderRequest.objects.filter(source="labsuit", is_current=False,
                                               allocations__status=Allocation.Status.APPROVED).distinct().count()
            if gone:
                warnings.append(f"{gone} הזמנות עם החלטת מימון מאושרת לא מופיעות בקובץ. הן יוסתרו מהטבלה, "
                                "וההחלטות נשארות (אפשר לבטל אותן בהמלצות).")
            if mode == ImportBatch.Mode.MERGE:
                stale = (OrderRequest.objects.filter(source="labsuit", is_current=True, status="approved")
                         .exclude(batch=batch).count())
                if stale:
                    warnings.append(f"{stale} הזמנות שמסומנות approved ב-LabFund לא מופיעות בקובץ. אם הקובץ כולל רק "
                                    "הזמנות מאושרות, כנראה שהסטטוס שלהן השתנה ב-LabSuit (למשל הוזמנו). ב-Merge הן "
                                    "נשארות approved, ולכן ימשיכו להופיע בהמלצות עד עדכון מלא מקובץ.")
            if kw.get("min_year"):
                warnings.append(f"עדכון חלקי: Replace חל רק על הזמנות מ-{kw['min_year']} והלאה.")
        elif source == ImportBatch.Source.SAP_GRANTS:
            preserved = preserved_grants([k for k in in_file if k in before])
            if stats.get("unmapped_columns"):
                warnings.append("עמודות יתרה שלא ממופות לסעיף: " + ", ".join(stats["unmapped_columns"]))
        elif source == ImportBatch.Source.SAP_BUDGET:
            if stats.get("unmapped_cost_elements"):
                warnings.append("סיווגים שלא ממופים לסעיף: " + ", ".join(stats["unmapped_cost_elements"]))
        elif source == ImportBatch.Source.LABSUIT_INVENTORY:
            if stats.get("flagged_delete"):
                warnings.append(f"{stats['flagged_delete']} פריטים מסומנים למחיקה בקובץ (Delete? = y) ויוסתרו.")

        transaction.set_rollback(True)

    return {
        "counts": {"in_file": file_rows, "new": len(new), "changed": len(changed), "hidden": len(hidden),
                   "reactivated": len(reactivated), "unchanged": unchanged, "reconciled": len(reconciled)},
        "new": new[:LIST_CAP], "changed": changed[:LIST_CAP], "hidden": hidden[:LIST_CAP],
        "reactivated": reactivated[:LIST_CAP], "reconciled": reconciled[:LIST_CAP], "lab_marked": lab_marked,
        "fields": field_counts.most_common(12),
        "status_moves": [[a, b, n] for (a, b), n in status_moves.most_common(12)],
        "preserved": preserved, "warnings": warnings,
        "per_batch": source == ImportBatch.Source.SAP_BUDGET,
    }
