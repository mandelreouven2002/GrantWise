import hashlib
import io
import math
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

CURRENCY_HINTS = [("USD", ("USD", "$")), ("EUR", ("EUR", "€")), ("GBP", ("GBP", "£")),
                  ("CHF", ("CHF",)), ("ILS", ("ILS", "₪", "NIS"))]


def read_bytes(f):
    """Accept a path, a Django UploadedFile or any file-like object."""
    if isinstance(f, bytes):
        return f
    if isinstance(f, (str, os.PathLike)):
        with open(f, "rb") as fh:
            return fh.read()
    if hasattr(f, "chunks"):
        return b"".join(f.chunks())
    data = f.read()
    return data if isinstance(data, bytes) else data.encode()


def blank(v):
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return str(v).strip() in ("", "nan", "NaN", "None", "NaT")


def text(v, maxlen=None):
    if blank(v):
        return ""
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].isdigit():   # 2032917.0 -> 2032917
        s = s[:-2]
    return s[:maxlen] if maxlen else s


def dec(v, default=None):
    if blank(v):
        return default
    s = str(v).replace(",", "").replace("‏", "").strip()
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return default


def money(v):
    """'ILS 629.24' | '0.00ILS' | '$ 12' | '1720.00USD' -> (Decimal, 'ILS') or (None, None)."""
    if blank(v):
        return None, None
    s = str(v)
    m = re.search(r"-?[\d,]+(?:\.\d+)?", s)
    if not m:
        return None, None
    amount = dec(m.group(0))
    cur = "ILS"
    up = s.upper()
    for code, hints in CURRENCY_HINTS:
        if any(h in up for h in hints):
            cur = code
            break
    return amount, cur


def parse_date(v):
    """dd-mm-yyyy, dd/mm/yyyy, ISO, Excel serial numbers, datetime objects."""
    if blank(v):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if re.fullmatch(r"\d{5}(\.0+)?", s):                       # Excel serial
        return date(1899, 12, 30) + timedelta(days=int(float(s)))
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    return None


def norm_catalog(v):
    return re.sub(r"\s+", "", text(v)).upper()


def stable_key(*parts):
    raw = "|".join(text(p).lower() for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def excel_frames(data, **kw):
    import pandas as pd
    return pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str, **kw)
