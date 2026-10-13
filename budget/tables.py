"""Editable-table engine: one config per table, used by every dashboard.

A config says which model, which columns (and how each is edited), which fields the
"add row" form asks for, and which file source the "update from file" button imports.
Cells save themselves through `POST /api/<table>/<pk>/` (see views.api_update).
"""
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import (BudgetCategory, BudgetReportLine, FundingAlias, FxRate, Grant, GrantLine,
                     GrantRule, ImportBatch, InventoryItem, OrderRequest, Project, ProjectGrant,
                     Reservation, TrackedCurrency)


@dataclass
class Col:
    field: str
    label: str
    kind: str = "text"      # text|num|money|int|date|bool|select|currency|category|project|grant|ro|calc|line
    choices: list | None = None
    width: str = ""
    required: bool = False  # for the add-row form
    default: object = None  # initial value in the add-row form (checkboxes start checked unless False)
    hidden: bool = False    # editable, but shown in the row's ▸ panel instead of the main table


@dataclass
class Table:
    key: str
    model: type
    title: str
    cols: list
    search: list = field(default_factory=list)
    add: list = field(default_factory=list)       # field names asked by the add-row form
    source: str | None = None                     # ImportBatch.Source for the file button
    deletable: bool = False
    per_page: int = 100
    add_only: list = field(default_factory=list)  # Cols that exist only in the add-row form

    def col(self, name):
        return next((c for c in self.cols if c.field == name), None)

    def add_cols(self):
        extra = {c.field: c for c in self.add_only}
        return [self.col(f) or extra.get(f) or Col(f, f) for f in self.add]


def _cat_choices():
    return [(c.code, c.name_he) for c in BudgetCategory.objects.all()]


def _grant_choices():
    return [(g.number, f"{g.number} · {g.currency}") for g in Grant.objects.order_by("number")]


FK_KINDS = {"category", "grant", "project"}   # stored as <field>_id, edited with a select


def _currency_choices():
    """ILS + every active tracked currency (Settings → exchange rates)."""
    return [("ILS", "ILS")] + [(c, c) for c in TrackedCurrency.objects.filter(is_active=True)
                                                           .values_list("code", flat=True)]


def _project_choices():
    return [("", "-")] + [(str(p.pk), p.name) for p in Project.objects.filter(is_active=True).order_by("name")]


LINE_CATS = [("SALARY", "שכר"), ("MATERIAL", "חומרים"), ("EQUIPMENT", "ציוד"), ("SERVICE", "שירותים"),
             ("GENERAL", "כללי")]
STATUSES = [(s, s) for s in ["requested", "approved", "ordered", "received", "canceled"]]

TABLES = {t.key: t for t in [
    Table("grants", Grant, "מענקים", source="sap_grants",
          add=["number", "sponsor", "currency", "start_date", "end_date", "balance_total"],
          cols=[Col("number", "מענק", "ro", required=True), Col("sponsor", "גורם מממן", width="w-m"),
                Col("currency", "מטבע", "currency", required=True),
                Col("end_date", "סיום", "date", required=True), Col("days", "ימים לסיום", "calc"),
                *[Col(f"line:{c}", n, "line") for c, n in LINE_CATS],
                Col("balance_total", "יתרה כוללת", "money"), Col("pending", "ממתין", "calc"),
                Col("available", "זמין", "calc"), Col("ils", "≈ ₪", "calc"),
                Col("pi_priority", "עדיפות (‎-3…+3)", "int", hidden=True),
                Col("is_reserve", "מענק רזרבה", "bool", hidden=True),
                Col("order_cutoff_days", "ימי חסימה לפני סיום", "int", hidden=True),
                Col("is_active", "פעיל", "bool", hidden=True),
                Col("start_date", "התחלה", "date", hidden=True)]),
    Table("orders", OrderRequest, "הזמנות ודרישות", source="labsuit_orders",
          search=["item_name", "catalog_number", "supplier", "po_number", "grant_label", "requested_by"],
          add=["item_name", "requested_by", "supplier", "catalog_number", "quantity", "amount_est",
               "amount_currency", "category", "grant_label"],
          deletable=True,
          cols=[Col("date_requested", "תאריך", "date"), Col("item_name", "פריט", width="w-l", required=True),
                Col("amount_est", "סכום", "money"), Col("amount_currency", "מטבע", "currency"),
                Col("status", "סטטוס", "select", STATUSES), Col("grant_label", "GRANT_ID", width="w-m"),
                Col("funding", "מימון שאושר ב-LabFund", "calc"), Col("category", "סעיף", "category"),
                Col("project", "פרויקט", "project"), Col("requested_by", "מבקש/ת", width="w-m"),
                Col("supplier", "ספק", width="w-m"), Col("po_number", "הזמנת רכש"), Col("quantity", "כמות", "num"),
                Col("amount_source", "מקור סכום", "ro"), Col("lab_notes", "הערות", width="w-l")]),
    Table("aliases", FundingAlias, "תוויות מימון מ-LabSuit", per_page=500,
          cols=[Col("label", "תווית ב-LabSuit", "ro"),
                Col("kind", "משמעות", "select", list(FundingAlias.Kind.choices)),
                Col("grant", "מענק", "grant"), Col("needs_review", "לבדיקה", "bool")]),
    Table("fx", FxRate, "שערי חליפין", deletable=True, per_page=20,
          add=["date", "currency", "rate_to_ils"],
          cols=[Col("date", "תאריך", "date", required=True), Col("currency", "מטבע", "currency", required=True),
                Col("rate_to_ils", "₪ ליחידה", "num", required=True), Col("source", "מקור", "ro")]),
    Table("reservations", Reservation, "שריונים", deletable=True,
          add=["category", "amount", "until", "note"],
          cols=[Col("category", "סעיף", "category", required=True), Col("amount", "סכום (במטבע המענק)", "money", required=True),
                Col("until", "עד", "date"), Col("note", "הערה", width="w-l")]),
    Table("rules", GrantRule, "כללי מענק", deletable=True,
          add=["category", "allowed", "extra_allowance", "note"],
          cols=[Col("category", "סעיף", "category", required=True), Col("allowed", "מותר", "bool"),
                Col("extra_allowance", "חריגה מותרת", "money"), Col("note", "הערה", width="w-l")]),
    Table("projectgrants", ProjectGrant, "פרויקטים מורשים", deletable=True,
          add=["project", "weight"],
          cols=[Col("project", "פרויקט", "project", required=True), Col("weight", "העדפה", "int")]),
    # ---- settings
    Table("currencies", TrackedCurrency, "מטבעות", per_page=200,
          add=["code", "name_he"],
          cols=[Col("code", "קוד", "ro", required=True), Col("name_he", "שם"), Col("rate", "₪ ליחידה", "calc"),
                Col("rate_date", "תאריך השער", "calc"), Col("source", "מקור", "ro"),
                Col("is_active", "פעיל", "bool")]),
    Table("projects", Project, "פרויקטים", deletable=True, per_page=500,
          add=["name", "parent", "owner"],
          cols=[Col("name", "שם", width="w-l", required=True), Col("parent", "פרויקט-על", "project"),
                Col("owner", "אחראי/ת", width="w-m"), Col("is_active", "פעיל", "bool"),
                Col("orders", "הזמנות", "calc")]),
    Table("users", User, "משתמשים", per_page=200,
          add=["username", "password", "first_name", "email", "is_superuser"],
          add_only=[Col("password", "סיסמה", required=True)],
          cols=[Col("username", "שם משתמש", "ro", required=True), Col("first_name", "שם", width="w-m"),
                Col("email", "אימייל", width="w-l"), Col("is_superuser", "מנהל/ת", "bool", default=False),
                Col("is_active", "פעיל", "bool"), Col("last_login", "כניסה אחרונה", "calc")]),
]}

ADMIN_ONLY = {"users"}


# --------------------------------------------------------------------------- reading cells

def fmt_num(v):
    """41000 -> '41,000'; 1234.5 -> '1,234.50'; None -> ''. Commas are stripped again on save."""
    if v is None or v == "":
        return ""
    d = Decimal(v)
    return f"{d:,.0f}" if d == d.to_integral_value() else f"{d:,.2f}"


def cell(col, obj, extra=None):
    """Dict the template needs to render one cell."""
    extra = extra or {}
    if col.kind == "calc":
        return dict(col=col, value=extra.get(col.field, ""), ro=True)
    if col.kind == "line":
        v = extra.get(col.field)
        return dict(col=col, value=fmt_num(v), neg=v is not None and v < 0,
                    after=extra.get("after:" + col.field.split(":", 1)[1]))
    if col.kind in FK_KINDS:
        v = getattr(obj, f"{col.field}_id")
        return dict(col=col, value="" if v is None else str(v))
    v = getattr(obj, col.field)
    if col.kind == "ro":
        disp = obj._get_FIELD_display(obj._meta.get_field(col.field)) if getattr(
            obj._meta.get_field(col.field), "choices", None) else v
        return dict(col=col, value=disp if disp is not None else "", ro=True)
    if col.kind == "bool":
        return dict(col=col, value=bool(v))
    if col.kind == "date":
        return dict(col=col, value=v.isoformat() if v else "")
    if col.kind in ("money", "num", "int"):
        return dict(col=col, value=fmt_num(v), neg=v is not None and v < 0)
    return dict(col=col, value="" if v is None else v)


def choices_for(col):
    if col.kind == "category":
        return [("", "—")] + _cat_choices()
    if col.kind == "grant":
        return [("", "—")] + _grant_choices()
    if col.kind == "project":
        return _project_choices()
    if col.kind == "currency":
        return _currency_choices()
    if col.choices:
        return col.choices
    return None


# --------------------------------------------------------------------------- writing cells

def _to_python(obj, name, raw):
    f = obj._meta.get_field(name)
    if isinstance(raw, str):
        raw = raw.strip().replace(",", "") if f.get_internal_type() in ("DecimalField", "IntegerField",
                                                                       "SmallIntegerField", "PositiveSmallIntegerField",
                                                                       "PositiveIntegerField", "FloatField") else raw.strip()
    if raw in ("", None) and f.null:
        return None
    if raw in ("", None) and f.get_internal_type() == "DecimalField" and f.has_default():
        return f.get_default()
    return f.clean(raw, obj)


def set_value(table, obj, name, raw):
    """Apply one edit. Returns the saved object. Raises ValidationError with a Hebrew message."""
    col = table.col(name)
    if col is None or col.kind in ("ro", "calc"):
        raise ValidationError("לא ניתן לערוך את השדה הזה")
    try:
        if col.kind == "line":
            return _set_grant_line(obj, name.split(":", 1)[1], raw)
        if col.kind == "bool":
            setattr(obj, name, raw in (True, "true", "1", "on", 1))
        elif col.kind in FK_KINDS:
            setattr(obj, f"{name}_id", _fk_value(col, raw))
        elif col.kind == "currency":
            setattr(obj, name, _to_python(obj, name, (raw or "").upper()))
        else:
            setattr(obj, name, _to_python(obj, name, raw))
    except (InvalidOperation, ValueError):
        raise ValidationError("ערך לא תקין")

    # side effects
    if isinstance(obj, OrderRequest) and name == "amount_est":
        obj.amount_source = OrderRequest.AmountSource.MANUAL if obj.amount_est else OrderRequest.AmountSource.NONE
    if isinstance(obj, FundingAlias) and name == "grant" and obj.grant_id:
        obj.kind, obj.needs_review = FundingAlias.Kind.GRANT, False
    if isinstance(obj, FundingAlias) and name == "kind":
        obj.needs_review = False
    if isinstance(obj, Project) and name == "parent" and obj.parent_id == obj.pk:
        raise ValidationError("פרויקט לא יכול להיות פרויקט-העל של עצמו")
    obj.full_clean(validate_unique=False, exclude=[f.name for f in obj._meta.fields if f.name != name])
    obj.save()
    return obj


def _fk_value(col, raw):
    raw = "" if raw is None else str(raw).strip()
    if raw in ("", "-"):
        return None
    if col.kind == "project":
        if not Project.objects.filter(pk=raw).exists():
            raise ValidationError("פרויקט לא קיים — אפשר להוסיף אותו בהגדרות → פרויקטים")
        return int(raw)
    return raw


def _manual_batch(source):
    b = ImportBatch.latest(source)
    return b or ImportBatch.objects.create(source=source, file_name="(ידני)", as_of=date.today(), mode="merge")


def _set_grant_line(grant, cat, raw):
    raw = (raw or "").strip().replace(",", "")
    batch = grant.last_batch or _manual_batch(ImportBatch.Source.SAP_GRANTS)
    if grant.last_batch_id is None:
        grant.last_batch = batch
        grant.save(update_fields=["last_batch"])
    if raw == "":
        GrantLine.objects.filter(batch=batch, grant=grant, category_id=cat).delete()
        return grant
    GrantLine.objects.update_or_create(batch=batch, grant=grant, category_id=cat,
                                       defaults={"balance": Decimal(raw)})
    return grant


# --------------------------------------------------------------------------- adding rows

def create_row(table, data, parent=None):
    if table.key == "currencies":
        from .fx import add_currency
        return add_currency(data.get("code", ""), data.get("name_he", ""))
    if table.key == "users":
        username = (data.get("username") or "").strip()
        if not username or not data.get("password"):
            raise ValidationError("חסרים שם משתמש או סיסמה")
        if User.objects.filter(username=username).exists():
            raise ValidationError(f"המשתמש {username} כבר קיים")
        u = User.objects.create_user(username, email=data.get("email", ""), password=data["password"],
                                     first_name=data.get("first_name", ""))
        u.is_superuser = u.is_staff = data.get("is_superuser") in ("on", "true", "1")
        u.save()
        return u
    obj = table.model()
    if table.key == "orders":
        obj.natural_key, obj.source = f"manual-{uuid.uuid4().hex[:16]}", "manual"
        obj.date_requested = date.today()
        obj.status = "approved"
    if table.key == "grants":
        number = (data.get("number") or "").strip()
        if not number:
            raise ValidationError("חסר מספר מענק")
        if Grant.objects.filter(number=number).exists():
            raise ValidationError(f"מענק {number} כבר קיים")
        obj.number = number
        obj.last_batch = _manual_batch(ImportBatch.Source.SAP_GRANTS)
    if table.key == "fx":
        obj.source = "manual"
    if parent is not None:          # reservations / rules / project links belong to a grant
        obj.grant = parent
    for col in table.add_cols():
        if col.field == "number":
            continue
        raw = data.get(col.field, "")
        if col.kind == "bool":
            setattr(obj, col.field, raw in ("on", "true", "1"))
        elif col.kind in FK_KINDS:
            setattr(obj, f"{col.field}_id", _fk_value(col, raw))
            if col.required and not raw:
                raise ValidationError(f"חסר: {col.label}")
        elif col.kind == "currency" and raw:
            setattr(obj, col.field, raw.upper())
        elif raw != "":
            setattr(obj, col.field, _to_python(obj, col.field, raw))
        elif col.required:
            raise ValidationError(f"חסר: {col.label}")
    if table.key == "orders" and obj.amount_est:
        obj.amount_source = OrderRequest.AmountSource.MANUAL
    if table.key == "orders" and obj.category_id is None:
        obj.category_id = "MATERIAL"
    obj.full_clean(validate_unique=table.key != "fx")
    if table.key == "fx":
        FxRate.objects.update_or_create(date=obj.date, currency=obj.currency,
                                        defaults={"rate_to_ils": obj.rate_to_ils, "source": "manual"})
        return obj
    obj.save()
    return obj


# --------------------------------------------------------------------------- listing

def filtered(table, qs, params):
    q = (params.get("q") or "").strip()
    if q and table.search:
        cond = Q()
        for f in table.search:
            cond |= Q(**{f"{f}__icontains": q})
        qs = qs.filter(cond)
    return qs


def last_import(table):
    return ImportBatch.latest(table.source) if table.source else None
