"""Exchange rates, refreshed automatically.

Sources (no API keys needed):
1. Bank of Israel — the official representative rate (שער יציג) for ~14 major currencies.
   GET https://boi.org.il/PublicApi/GetExchangeRates
   -> {"exchangeRates":[{"key":"USD","currentExchangeRate":3.078,"unit":1,"lastUpdate":"2026-10-12T12:23:04Z"}]}
   (`unit`: JPY is quoted per 100.)
2. ExchangeRate-API open endpoint for every other ISO code (~165 currencies), updated daily.
   GET https://open.er-api.com/v6/latest/ILS -> {"result":"success","rates":{"USD":0.3253,...}}  (units per 1 ₪)

`refresh_if_stale()` runs from middleware on page loads: at most once per FX_REFRESH_MINUTES, and
after a network failure it backs off, so the site never waits on an unreachable API.
"""
import logging
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from .models import FxRate, TrackedCurrency

log = logging.getLogger(__name__)
BOI_URL = "https://boi.org.il/PublicApi/GetExchangeRates"
ERAPI_URL = "https://open.er-api.com/v6/latest/ILS"
TIMEOUT = 6


class MissingRate(Exception):
    pass


# --------------------------------------------------------------------------- sources

def _get(url):
    import requests
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def boi_rates():
    """{code: (₪ per unit, date)} from Bank of Israel."""
    out = {}
    for r in _get(BOI_URL).get("exchangeRates", []):
        rate = Decimal(str(r["currentExchangeRate"])) / Decimal(str(r.get("unit") or 1))
        d = datetime.fromisoformat(r["lastUpdate"].replace("Z", "+00:00")).date()
        out[r["key"].upper()] = (rate, d)
    return out


def erapi_rates():
    """{code: (₪ per unit, date)} from ExchangeRate-API (inverted: the API gives units per ₪)."""
    j = _get(ERAPI_URL)
    if j.get("result") != "success":
        raise RuntimeError(j.get("error-type", "ExchangeRate-API error"))
    ts = j.get("time_last_update_unix")
    d = datetime.fromtimestamp(ts, tz=dt_timezone.utc).date() if ts else date.today()
    return {code.upper(): ((Decimal("1") / Decimal(str(v))).quantize(Decimal("0.0000000001")), d)
            for code, v in j.get("rates", {}).items() if v}


# --------------------------------------------------------------------------- refresh

def fetch_all(codes=None):
    """Fetch rates for the given (default: all active tracked) currencies. Returns [(code, rate, date, source)]."""
    tracked = list(TrackedCurrency.objects.filter(is_active=True)) if codes is None else \
        list(TrackedCurrency.objects.filter(code__in=codes))
    if not tracked:
        return []
    saved, missing = [], []
    try:
        boi = boi_rates()
    except Exception as e:  # noqa: BLE001 — any network/parse failure falls through to the second source
        log.warning("BoI rates failed: %s", e)
        boi = {}
    for c in tracked:
        if c.code in boi:
            saved.append(_store(c, *boi[c.code], TrackedCurrency.Source.BOI))
        else:
            missing.append(c)
    if missing:
        er = erapi_rates()
        for c in missing:
            if c.code in er:
                saved.append(_store(c, *er[c.code], TrackedCurrency.Source.ERAPI))
    if not saved:
        raise RuntimeError("לא התקבלו שערים מאף מקור")
    return saved


def _store(cur, rate, d, source):
    # a rate entered by hand for that day wins over the automatic one
    if not FxRate.objects.filter(date=d, currency=cur.code, source="manual").exists():
        FxRate.objects.update_or_create(date=d, currency=cur.code, defaults={"rate_to_ils": rate, "source": source})
    cur.source, cur.last_fetched = source, timezone.now()
    cur.save(update_fields=["source", "last_fetched"])
    return cur.code, rate, d, source


def refresh_if_stale():
    """Called on page loads. Cheap when fresh; never raises."""
    if not getattr(settings, "FX_AUTO_REFRESH", True):
        return
    if cache.get("fx:backoff"):
        return
    minutes = getattr(settings, "FX_REFRESH_MINUTES", 60)
    cutoff = timezone.now() - timezone.timedelta(minutes=minutes)
    stale = (TrackedCurrency.objects.filter(is_active=True)
             .filter(Q(last_fetched__isnull=True) | Q(last_fetched__lt=cutoff)).exists())
    if not stale:
        return
    if not cache.add("fx:lock", 1, 30):      # another request is already refreshing
        return
    try:
        fetch_all()
    except Exception as e:  # noqa: BLE001
        log.warning("FX refresh failed: %s", e)
        cache.set("fx:backoff", 1, 600)       # don't retry for 10 minutes
    finally:
        cache.delete("fx:lock")


def add_currency(code, name=""):
    """Track a new currency: validate the code against the sources and fetch its rate right away."""
    code = (code or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValidationError("קוד מטבע הוא 3 אותיות לועזיות, למשל CAD")
    if code == "ILS":
        raise ValidationError("₪ הוא מטבע הבסיס — אין צורך להוסיף אותו")
    cur, created = TrackedCurrency.objects.get_or_create(code=code, defaults={"name_he": name.strip()})
    if not created:
        cur.is_active = True
        if name.strip():
            cur.name_he = name.strip()
        cur.save()
    try:
        got = fetch_all([code])
    except Exception:  # noqa: BLE001
        got = []
    if not got:
        if created:
            cur.delete()
            raise ValidationError(f"לא נמצא שער ל-{code} (קוד לא מוכר, או שאין חיבור לאינטרנט). "
                                  "אפשר להזין שער ידנית בהיסטוריית השערים.")
    return cur


# --------------------------------------------------------------------------- lookup

def rate_to_ils(currency, on=None):
    """₪ per 1 unit of `currency` on the given date, using the latest rate on or *before* it.

    Never uses a rate from after `on` (a past decision must not borrow a future rate).
    Without `on`, the latest known rate is used.
    """
    currency = (currency or "ILS").upper()
    if currency == "ILS":
        return Decimal("1"), on or date.today()
    qs = FxRate.objects.filter(currency=currency).order_by("-date")
    r = qs.filter(date__lte=on).first() if on else qs.first()
    if not r:
        when = f" לתאריך {on:%d/%m/%Y} או לפניו" if on else ""
        raise MissingRate(f"אין שער חליפין ל-{currency}{when}. אפשר להוסיף אותו בהגדרות → שערי חליפין.")
    return r.rate_to_ils, r.date


def stale_rates(today=None, max_age_days=7):
    """Active currencies whose newest rate is missing or older than `max_age_days`: [(code, date|None)]."""
    today = today or date.today()
    out = []
    for c in TrackedCurrency.objects.filter(is_active=True):
        r = FxRate.objects.filter(currency=c.code, date__lte=today).order_by("-date").first()
        if r is None or (today - r.date).days > max_age_days:
            out.append((c.code, r.date if r else None))
    return out


def convert(amount, from_ccy, to_ccy, on=None):
    if from_ccy == to_ccy:
        return amount, Decimal("1")
    a, _ = rate_to_ils(from_ccy, on)
    b, _ = rate_to_ils(to_ccy, on)
    rate = a / b
    return (amount * rate).quantize(Decimal("0.01")), rate
