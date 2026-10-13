from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def money(value, digits=0):
    """1234567.8 -> '1,234,568' (digits=0) or '1,234,567.80' (digits=2). Blank for None."""
    if value in (None, ""):
        return ""
    try:
        return f"{Decimal(value):,.{int(digits)}f}"
    except Exception:
        return value
