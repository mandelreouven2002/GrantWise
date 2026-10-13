"""LabSuit inventory export: one sheet per item type, 23-35 columns that vary by type.

Common columns go to fields; everything type-specific goes to the JSON `attributes`.
Key = SERIAL_NUMBER (unique in practice; duplicates get a name/catalog suffix).
"""
from collections import Counter

from django.db import transaction

from ..models import ImportBatch, InventoryItem
from .common import blank, dec, excel_frames, money, parse_date, read_bytes, stable_key, text

COMMON = {
    "SERIAL_NUMBER", "NAME", "CATALOG_NUMBER", "MANUFACTURER", "SUPPLIER", "PACK_SIZE",
    "AMOUNT_IN_STOCK", "In stock? (y/n)", "LOCATION", "SUB_LOCATION", "PRICE", "SUPPLY_DATE",
    "OWNER", "COMMENTS", "Delete? (y/n)",
}


@transaction.atomic
def run(f, file_name, as_of, mode="merge", user=""):
    frames = excel_frames(read_bytes(f))
    batch = ImportBatch.objects.create(source=ImportBatch.Source.LABSUIT_INVENTORY, mode=mode,
                                       file_name=file_name, as_of=as_of, uploaded_by=user)
    existing = {i.key: i for i in InventoryItem.objects.all()}
    serial_count = Counter()
    to_create, to_update, untouched, seen = [], [], [], set()
    flagged_delete = 0

    for sheet, df in frames.items():
        if "NAME" not in df.columns:
            continue
        for _, r in df.iterrows():
            if blank(r.get("NAME")):
                continue
            serial = text(r.get("SERIAL_NUMBER"), 40)
            serial_count[serial] += 1
            key = serial if serial and serial_count[serial] == 1 else \
                f"{serial or sheet}#{stable_key(r.get('NAME'), r.get('CATALOG_NUMBER'), serial_count[serial])[:10]}"
            price, cur = money(r.get("PRICE"))
            delete = text(r.get("Delete? (y/n)")).lower() == "y"
            flagged_delete += delete
            values = dict(
                batch=batch, is_current=not delete, serial_number=serial, item_type=sheet[:60],
                name=text(r.get("NAME"), 300), catalog_number=text(r.get("CATALOG_NUMBER"), 120),
                manufacturer=text(r.get("MANUFACTURER"), 120), supplier=text(r.get("SUPPLIER"), 120),
                pack_size=text(r.get("PACK_SIZE"), 80), amount_in_stock=dec(r.get("AMOUNT_IN_STOCK")),
                in_stock=text(r.get("In stock? (y/n)")).lower() != "n",
                location=text(r.get("LOCATION"), 120), sub_location=text(r.get("SUB_LOCATION"), 120),
                price=price if price else None, currency=cur or "",
                supply_date=parse_date(r.get("SUPPLY_DATE")), owner=text(r.get("OWNER"), 150).lower(),
                comments=text(r.get("COMMENTS")),
                attributes={c: text(r.get(c)) for c in df.columns if c not in COMMON and not blank(r.get(c))},
            )
            seen.add(key)
            obj = existing.get(key)
            if obj is None:
                to_create.append(InventoryItem(key=key, **values))
            else:
                dirty = False
                for k, v in values.items():
                    if k != "batch" and getattr(obj, k) != v:
                        setattr(obj, k, v)
                        dirty = True
                obj.batch = batch
                (to_update if dirty else untouched).append(obj if dirty else obj.pk)

    InventoryItem.objects.bulk_create(to_create, batch_size=500)
    fields = [f.name for f in InventoryItem._meta.fields if f.name not in ("id", "key")]
    InventoryItem.objects.bulk_update(to_update, fields, batch_size=500)
    for i in range(0, len(untouched), 500):        # unchanged rows: just stamp the batch
        InventoryItem.objects.filter(pk__in=untouched[i:i + 500]).update(batch=batch)
    hidden = 0
    if mode == ImportBatch.Mode.REPLACE:
        hidden = InventoryItem.objects.filter(is_current=True).exclude(key__in=seen).update(is_current=False)
    batch.stats = dict(sheets=len(frames), items=len(seen), created=len(to_create),
                       updated=len(to_update), hidden=hidden, flagged_delete=flagged_delete)
    batch.save(update_fields=["stats"])
    return batch
