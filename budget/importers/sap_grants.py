"""TRDF 'GRANTS-CLASS-BALANCES' export.

Despite the .XLS extension this is UTF-16 tab-separated text (pd.read_excel fails on it).
One row per grant: totals + one *_BAL column per budget category, in the grant's currency.
Grant Expense already includes SAP commitments (open POs), so Grant Balance is net of them.
"""
import io

import pandas as pd
from django.db import transaction

from ..models import BudgetCategory, CategoryMap, Grant, GrantLine, ImportBatch
from .common import dec, parse_date, read_bytes, text


def _frame(data):
    for enc in ("utf-16", "utf-8-sig", "cp1255"):
        try:
            df = pd.read_csv(io.StringIO(data.decode(enc)), sep="\t", dtype=str)
            if "GRANT" in [c.strip() for c in df.columns]:
                break
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
    else:  # a genuine Excel file
        df = pd.read_excel(io.BytesIO(data), dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    return df


@transaction.atomic
def run(f, file_name, as_of, mode="merge", user=""):
    df = _frame(read_bytes(f))
    batch = ImportBatch.objects.create(source=ImportBatch.Source.SAP_GRANTS, mode=mode,
                                       file_name=file_name, as_of=as_of, uploaded_by=user)
    col_map = {m.value: m.category_id for m in CategoryMap.objects.filter(source="trdf_column")}
    known_cats = set(BudgetCategory.objects.values_list("code", flat=True))
    created = updated = lines = 0
    seen = set()
    unmapped = sorted(c for c in df.columns if c.endswith("_BAL") and c not in col_map)

    for _, r in df.iterrows():
        number = text(r.get("GRANT"))
        if not number:
            continue
        seen.add(number)
        fields = dict(
            sponsor=text(r.get("sponsor_name"), 200),
            currency=text(r.get("grant_currency")) or "ILS",
            start_date=parse_date(r.get("fund_start_dt")),
            end_date=parse_date(r.get("fund_end_dt")),
            budget_total=dec(r.get("grant_budjet")),
            expense_total=dec(r.get("Grant Expense")),
            balance_total=dec(r.get("Grant Balance"), default=0),
            grant_type=text(r.get("Granttype"), 60),
            subject=text(r.get("grant_subject"), 300),
            description=text(r.get("DESCRIPTION"), 300),
            coordinator=text(r.get("COORD_NAME"), 100),
            status=text(r.get("status"), 30),
            last_batch=batch,
            is_active=True,
        )
        grant, was_created = Grant.objects.update_or_create(number=number, defaults=fields)
        if was_created:
            grant.is_reserve = fields["grant_type"] in Grant.RESERVE_TYPES
            grant.save(update_fields=["is_reserve"])
            created += 1
        else:
            updated += 1
        for col, cat in col_map.items():
            if col in df.columns and cat in known_cats:
                bal = dec(r.get(col))
                if bal is not None:
                    GrantLine.objects.create(batch=batch, grant=grant, category_id=cat, balance=bal)
                    lines += 1

    deactivated = 0
    if mode == ImportBatch.Mode.REPLACE:
        deactivated = Grant.objects.exclude(number__in=seen).update(is_active=False)

    from ..resolve import reconcile          # approved decisions the new report covers → "כלול ביתרת SAP"
    reconciled = reconcile(as_of)
    batch.stats = dict(grants=len(seen), created=created, updated=updated, lines=lines,
                       deactivated=deactivated, unmapped_columns=unmapped, reconciled=reconciled)
    batch.save(update_fields=["stats"])
    return batch
