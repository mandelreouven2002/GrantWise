"""What a request costs, and what comes off a grant that pays no VAT.

Materials carry two quoted totals, with and without VAT (from the supplier's quote; the lab can edit both).
From a grant that pays VAT the total with VAT comes off; from a grant that does not, the total before VAT.
Every other category has one amount, typed by the lab, and VAT plays no role in it at all.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings

MATERIAL = "MATERIAL"
Q = Decimal("0.01")


@dataclass
class OrderAmount:
    gross: Decimal        # the price of the request (with VAT, for materials)
    currency: str
    ratio: Decimal        # price before VAT / gross: what a grant that pays no VAT is charged (1 = unchanged)
    material: bool


def is_material(order):
    return (order.category_id or MATERIAL) == MATERIAL


def _vat_rate():
    return Decimal(str(settings.RESOLVE_DEFAULTS["vat_rate"]))


def quote_currency(o):
    """The currency of a material's quote: typed, else what the extraction read, else "" (then the order's)."""
    if o.quoted_currency:
        return o.quoted_currency
    if o.quoted_manually_corrected:
        return ""
    values = (o.extraction_evidence or {}).get("values", {})
    for name in ("quoted_total_incl_vat", "quoted_total_excl_vat"):
        cur = (values.get(name) or {}).get("currency", "")
        if cur:
            return cur
    return ""


def order_amount(o):
    """The request's OrderAmount, or None while it has no amount to fund."""
    if not is_material(o):
        return OrderAmount(o.amount_est, o.amount_currency or "ILS", Decimal("1"), False) if o.amount_est else None
    vat = _vat_rate()
    incl, excl = o.quoted_total_incl_vat, o.quoted_total_excl_vat
    cur = quote_currency(o) or o.amount_currency or "ILS"
    if incl:
        ratio = min(excl / incl, Decimal("1")) if excl else Decimal("1") / vat
        return OrderAmount(incl, cur, ratio, True)
    if excl:                                   # only the price before VAT is known
        return OrderAmount((excl * vat).quantize(Q), cur, Decimal("1") / vat, True)
    if o.amount_est:                           # an older amount, taken as the price with VAT
        return OrderAmount(o.amount_est, o.amount_currency or "ILS", Decimal("1") / vat, True)
    return None


def vat_factor(amount, grant):
    """What the grant is charged, as a share of the price: 1, or the before-VAT share for materials
    taken from a grant that pays no VAT."""
    return amount.ratio if (amount.material and not grant.vat_required) else Decimal("1")
