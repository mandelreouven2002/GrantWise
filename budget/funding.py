"""Interpreting LabSuit's free-text GRANT_ID labels."""
import re

from .models import FundingAlias, Grant, StandingOrder

STANDING_HINTS = ("הזמנה עומדת", "הזמנה עומת", "שמור/מכור", "שמור מכור", "standing")
OPEN_LABELS = {"", "general budget", "general", "תקציב כללי"}


def classify(label):
    """Best-guess meaning of a label, as an *unsaved* FundingAlias (no database writes)."""
    label = (label or "").strip()
    if label.lower() in OPEN_LABELS:
        return FundingAlias(label=label, kind=FundingAlias.Kind.OPEN)
    if re.fullmatch(r"\d{5,9}", label):
        g = Grant.objects.filter(number=label).first()
        return FundingAlias(label=label, kind=FundingAlias.Kind.GRANT, grant=g, needs_review=g is None)
    if any(h in label.lower() for h in STANDING_HINTS):
        return FundingAlias(label=label, kind=FundingAlias.Kind.STANDING)
    return FundingAlias(label=label, kind=FundingAlias.Kind.OPEN, needs_review=True)


def alias_for(label):
    """Read-only lookup used by Resolve: the saved alias if there is one, else a fresh classification."""
    label = (label or "").strip()
    alias = FundingAlias.objects.select_related("grant").filter(label=label).first()
    if alias and alias.kind == FundingAlias.Kind.GRANT and alias.grant_id is None:
        alias.grant = Grant.objects.filter(number=label).first()
    return alias or classify(label)


def ensure_alias(label):
    """Return the saved FundingAlias for a label, creating it (called by importers)."""
    label = (label or "").strip()
    alias = FundingAlias.objects.filter(label=label).first()
    if alias:
        if alias.kind == FundingAlias.Kind.GRANT and alias.grant_id is None:
            g = Grant.objects.filter(number=label).first()
            if g:
                alias.grant = g
                alias.needs_review = False
                alias.save(update_fields=["grant", "needs_review"])
        return alias
    alias = classify(label)
    if alias.kind == FundingAlias.Kind.STANDING:
        alias.standing_order, _ = StandingOrder.objects.get_or_create(name=label[:120])
    alias.save()
    return alias
