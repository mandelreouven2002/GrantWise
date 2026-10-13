"""SAP 'תקציב מול ביצוע' report (old BIFF .xls, read with xlrd).

Layout: column B holds '<cost element>  <label>' for detail rows and '*   EUR  יורו' for the
block total that closes the rows above it. Columns C..F: budget, commitments, actual, balance.
The report aggregates the selected grants by currency, so a block may cover several grants.
"""
import io
import re

import pandas as pd
from django.db import transaction

from ..models import BudgetReportLine, CategoryMap, ImportBatch
from .common import dec, read_bytes, text

DETAIL = re.compile(r"^\s*([0-9A-Z]{3,})\s+(.*)$")
TOTAL = re.compile(r"^\s*\*+\s*(.*)$")


@transaction.atomic
def run(f, file_name, as_of, mode="replace", user=""):
    data = read_bytes(f)
    try:
        df = pd.read_excel(io.BytesIO(data), header=None, dtype=object)
    except Exception:
        df = pd.read_excel(io.BytesIO(data), header=None, dtype=object, engine="xlrd")

    batch = ImportBatch.objects.create(source=ImportBatch.Source.SAP_BUDGET, mode=mode,
                                       file_name=file_name, as_of=as_of, uploaded_by=user)
    cmap = {m.value: m.category_id for m in CategoryMap.objects.filter(source="sap_cost_element")}

    # find the label column: the first column containing a header like 'סיווג'
    label_col = next((c for c in df.columns if df[c].astype(str).str.contains("סיווג").any()), 1)
    nums = [c for c in df.columns if c > label_col][:4]

    pending, rows, unmapped = [], 0, set()
    for i, r in df.iterrows():
        raw = text(r[label_col])
        if not raw or "סיווג" in raw:
            continue
        vals = dict(zip(["budget", "committed", "actual", "balance"], [dec(r[c], 0) for c in nums]))
        t = TOTAL.match(raw)
        if t:
            rest = t.group(1).strip()
            code = rest.split()[0] if rest else ""
            block = code if re.fullmatch(r"[A-Z]{3}", code) else ("OVERHEAD" if "תקורה" in rest else "TOTAL")
            for line in pending:
                line.block = block
            BudgetReportLine.objects.bulk_create(pending)
            rows += len(pending)
            pending = []
            if block == "TOTAL":
                # '**' / '*** סה"כ' are cross-currency totals: SAP can't add EUR+ILS+USD, so the
                # file leaves them at 0. They carry no information — skip them.
                continue
            BudgetReportLine.objects.create(batch=batch, row=i, block=block, label=rest[:120],
                                            is_total=True, **vals)
            rows += 1
            continue
        d = DETAIL.match(raw)
        if d:
            ce, label = d.group(1), d.group(2).strip()
            cat = cmap.get(ce)
            if not cat:
                unmapped.add(ce)
            pending.append(BudgetReportLine(batch=batch, row=i, block="?", cost_element=ce,
                                            label=label[:120], category_id=cat, **vals))
    if pending:
        BudgetReportLine.objects.bulk_create(pending)
        rows += len(pending)

    batch.stats = dict(rows=rows, unmapped_cost_elements=sorted(unmapped))
    batch.save(update_fields=["stats"])
    return batch
