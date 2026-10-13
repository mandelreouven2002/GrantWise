"""LabSuit 'orders' export: one sheet per year ('Year - 2026', ...), 37 columns, no order ID.

- Natural key = (date requested, requester, catalog/item name) + occurrence index, because the
  export has no stable ID. Editing those fields in LabSuit creates a 'new' order on next import.
- PRICE is 0 for almost every row (real prices live in SAP), so the importer estimates an amount:
  LabSuit TOTAL/PRICE if > 1, else the latest priced order of the same catalog number,
  else the inventory price of that catalog number.
- Lab-owned fields (project, category override, manual amount, notes) survive re-imports.
"""
from collections import Counter
from decimal import Decimal

from django.db import transaction

from ..funding import ensure_alias
from ..models import CategoryMap, ImportBatch, InventoryItem, OrderRequest, Person
from .common import blank, dec, excel_frames, money, norm_catalog, parse_date, read_bytes, stable_key, text

ONE = Decimal("1")


def _unit_price(r):
    price = dec(r.get("PRICE"))
    if price and price > ONE:
        return price, text(r.get("CURRENCY")) or "ILS"
    total, cur = money(r.get("TOTAL"))
    qty = dec(r.get("QUANTITY"), ONE) or ONE
    if total and total > ONE:
        return (total / qty).quantize(Decimal("0.01")), text(r.get("CURRENCY")) or cur
    return None, None


@transaction.atomic
def run(f, file_name, as_of, mode="replace", user="", min_year=None):
    frames = excel_frames(read_bytes(f))
    sheets = [(n, df) for n, df in frames.items() if "ITEM_NAME" in df.columns]
    if min_year:
        sheets = [(n, df) for n, df in sheets
                  if not n.split()[-1].isdigit() or int(n.split()[-1]) >= int(min_year)]

    batch = ImportBatch.objects.create(source=ImportBatch.Source.LABSUIT_ORDERS, mode=mode,
                                       file_name=file_name, as_of=as_of, uploaded_by=user)

    rows = [r for _, df in sheets for _, r in df.iterrows() if not blank(r.get("ITEM_NAME"))]

    # price history from the export itself (latest wins) + inventory as fallback
    history = {}
    for r in rows:
        cat = norm_catalog(r.get("CATALOG_NUMBER"))
        p, cur = _unit_price(r)
        d = parse_date(r.get("DATE_REQUESTED"))
        if cat and p and (cat not in history or (d and history[cat][2] and d > history[cat][2])):
            history[cat] = (p, cur, d)
    # orders already in the DB (e.g. older years not in this file) also feed the history
    for o in (OrderRequest.objects.filter(labsuit_total__gt=1).exclude(catalog_number="")
              .only("catalog_number", "labsuit_total", "quantity", "labsuit_currency", "date_requested")):
        cat = norm_catalog(o.catalog_number)
        if cat not in history or (o.date_requested and history[cat][2] and o.date_requested > history[cat][2]):
            unit = (o.labsuit_total / (o.quantity or ONE)).quantize(Decimal("0.01"))
            history[cat] = (unit, o.labsuit_currency or "ILS", o.date_requested)
    inv_prices = {}
    for it in InventoryItem.objects.filter(price__gt=1).exclude(catalog_number=""):
        inv_prices.setdefault(norm_catalog(it.catalog_number), (it.price, it.currency or "ILS"))

    type_map = {m.value.lower(): m.category_id for m in CategoryMap.objects.filter(source="labsuit_item_type")}
    existing = {o.natural_key: o for o in OrderRequest.objects.filter(source="labsuit")}
    occurrences = Counter()
    to_create, to_update, untouched, changed_objs, seen = [], [], [], [], set()
    labels, people = set(), set()
    amount_stats = Counter()

    for r in rows:
        base = stable_key(r.get("DATE_REQUESTED"), r.get("REQUESTED_BY"),
                          r.get("CATALOG_NUMBER") or r.get("ITEM_NAME"), r.get("ITEM_NAME"))
        occurrences[base] += 1
        key = base if occurrences[base] == 1 else stable_key(base, occurrences[base])
        seen.add(key)

        qty = dec(r.get("QUANTITY"), ONE) or ONE
        total, total_cur = money(r.get("TOTAL"))
        label = text(r.get("GRANT_ID"), 120)
        labels.add(label)
        if text(r.get("REQUESTED_BY")):
            people.add(text(r.get("REQUESTED_BY")).lower())

        values = dict(
            item_name=text(r.get("ITEM_NAME"), 300),
            catalog_number=text(r.get("CATALOG_NUMBER"), 120),
            manufacturer=text(r.get("MANUFACTURER"), 120),
            supplier=text(r.get("SUPPLIER"), 120),
            item_type=text(r.get("ITEM_TYPE"), 60),
            requested_by=text(r.get("REQUESTED_BY"), 150).lower(),
            grant_label=label,
            status=text(r.get("STATUS"), 20).lower(),
            po_number=text(r.get("PURCHASE_ORDER_NUMBER"), 40),
            quote_id=text(r.get("QUOTE_ID"), 60),
            quantity=qty,
            pack_size=text(r.get("PACK_SIZE"), 80),
            labsuit_total=total,
            labsuit_currency=text(r.get("CURRENCY"), 3) or (total_cur or ""),
            date_requested=parse_date(r.get("DATE_REQUESTED")),
            date_approved=parse_date(r.get("DATE_APPROVED")),
            date_ordered=parse_date(r.get("DATE_ORDERED")),
            date_received=parse_date(r.get("DATE_RECEIVED")),
            date_cancelled=parse_date(r.get("DATE_CANCELLED")),
            comments=text(r.get("COMMENTS")),
        )

        # amount estimate
        p, cur = _unit_price(r)
        src = OrderRequest.AmountSource.LABSUIT
        if not p:
            cat = norm_catalog(r.get("CATALOG_NUMBER"))
            if cat in history:
                p, cur, _ = history[cat]
                src = OrderRequest.AmountSource.HISTORY
            elif cat in inv_prices:
                p, cur = inv_prices[cat]
                src = OrderRequest.AmountSource.INVENTORY
            else:
                src = OrderRequest.AmountSource.NONE
        est = (p * qty).quantize(Decimal("0.01")) if p else None

        obj = existing.get(key)
        if obj is None:
            obj = OrderRequest(natural_key=key, source="labsuit", **values)
            obj.category_id = type_map.get(values["item_type"].lower())
            obj.amount_est, obj.amount_currency, obj.amount_source = est, cur or "ILS", src
            obj.batch, obj.is_current = batch, True
            to_create.append(obj)
        else:
            dirty = not obj.is_current
            for k, v in values.items():
                if getattr(obj, k) != v:
                    setattr(obj, k, v)
                    dirty = True
            keep = obj.amount_source == OrderRequest.AmountSource.MANUAL or (est is None and obj.amount_est)
            if not keep and (obj.amount_est, obj.amount_currency, obj.amount_source) != (est, cur or "ILS", src):
                obj.amount_est, obj.amount_currency, obj.amount_source = est, cur or "ILS", src
                dirty = True
            if obj.category_id is None and type_map.get(values["item_type"].lower()):
                obj.category_id = type_map.get(values["item_type"].lower())
                dirty = True
            obj.batch, obj.is_current = batch, True
            # rewriting thousands of unchanged rows is what made imports slow — only changed ones
            (to_update if dirty else untouched).append(obj.pk)
            if dirty:
                changed_objs.append(obj)
        amount_stats[src] += 1

    OrderRequest.objects.bulk_create(to_create, batch_size=500)
    OrderRequest.objects.bulk_update(
        changed_objs,
        OrderRequest.LABSUIT_FIELDS + ["amount_est", "amount_currency", "amount_source",
                                       "category", "batch", "is_current"],
        batch_size=500)
    for i in range(0, len(untouched), 500):        # unchanged rows: just stamp the batch
        OrderRequest.objects.filter(pk__in=untouched[i:i + 500]).update(batch=batch)

    hidden = 0
    if mode == ImportBatch.Mode.REPLACE:
        scope = OrderRequest.objects.filter(source="labsuit", is_current=True)
        if min_year:  # a partial export only replaces the years it contains
            scope = scope.filter(date_requested__year__gte=int(min_year))
        hidden = scope.exclude(natural_key__in=seen).update(is_current=False)

    for label in labels:
        ensure_alias(label)
    for email in people:
        Person.objects.get_or_create(email=email)

    from ..resolve import mark_written_in_labsuit, reconcile
    lab_marked = mark_written_in_labsuit()   # GRANT_ID in LabSuit now shows the grant we decided on
    reconciled = reconcile()                 # an approved order may now carry a PO that SAP already has
    batch.stats = dict(reconciled=reconciled, lab_marked=lab_marked, sheets=[n for n, _ in sheets], rows=len(rows), created=len(to_create),
                       updated=len(to_update), hidden=hidden,
                       amount_sources=dict(amount_stats))
    batch.save(update_fields=["stats"])
    return batch
