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
from django.db import transaction

from .models import (BudgetCategory, BudgetReportLine, FxRate, Grant, GrantLine,
                     GrantRule, ImportBatch, InventoryItem, OrderRequest,
                     Reservation, TrackedCurrency)


@dataclass
class Col:
    field: str
    label: str
    kind: str = "text"      # text|suggest|num|money|int|date|bool|select|currency|category|grant|ro|calc|line
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


FK_KINDS = {"category", "grant"}   # stored as <field>_id, edited with a select


def _currency_choices():
    """ILS + every active tracked currency (Settings → exchange rates)."""
    return [("ILS", "ILS")] + [(c, c) for c in TrackedCurrency.objects.filter(is_active=True)
                                                           .values_list("code", flat=True)]


# every budget category of the SAP grants file, in its order (the table hides the ones nobody has)
LINE_CATS = [("SALARY", "שכר"), ("MATERIAL", "חומרים"), ("EQUIPMENT", "ציוד"), ("SERVICE", "שירותים"),
             ("TRAVEL_EXT", "נסיעות לחו״ל"), ("TRAVEL_INT", "נסיעות בארץ"), ("BOOKS", "ספרים"),
             ("CONFERENCE", "אירוח וכנסים"), ("OFFICE", "משרדיות"), ("PUBLICATION", "פרסומים"),
             ("REPORTING", "דיווח"), ("OVERHEAD", "תקורה"), ("SELF", "עצמי"), ("OTHER", "אחר"),
             ("GENERAL", "כללי (מאגר)")]
STATUSES = [(s, s) for s in ["requested", "approved", "ordered", "received", "canceled"]]

TABLES = {t.key: t for t in [
    Table("grants", Grant, "מענקים", source="sap_grants", deletable=True,
          add=["number", "sponsor", "currency", "start_date", "end_date", "balance_total"],
          cols=[Col("number", "מענק", "ro", required=True), Col("sponsor", "גורם מממן", width="w-m"),
                Col("currency", "מטבע", "currency", required=True),
                Col("vat_required", "מע״מ", "bool"),
                Col("end_date", "סיום", "date", required=True), Col("days", "ימים לסיום", "calc"),
                *[Col(f"line:{c}", n, "line") for c, n in LINE_CATS],
                Col("balance_total", "יתרה כוללת", "money"), Col("pending", "ממתין", "calc"),
                Col("available", "זמין", "calc"), Col("ils", "≈ ₪", "calc"),
                Col("pi_priority", "עדיפות (‎-3…+3)", "int", hidden=True),
                Col("is_reserve", "מענק רזרבה", "bool", hidden=True),
                Col("order_cutoff_days", "ימי חסימה לפני סיום", "int", hidden=True),
                Col("is_active", "פעיל", "bool", hidden=True),
                Col("start_date", "התחלה", "date", hidden=True)]),
    # Materials: the two quoted totals (with / without VAT) are the amount; the LabSuit update lives here.
    Table("orders", OrderRequest, "הזמנות חומרים", source="labsuit_orders",
          search=["item_name", "catalog_number", "supplier", "po_number", "grant_label", "project_text", "requested_by"],
          add=["item_name", "requested_by", "supplier", "catalog_number", "quantity", "quoted_total_incl_vat",
               "quoted_total_excl_vat", "quoted_currency", "project_text"],
          deletable=True,
          cols=[Col("date_requested", "תאריך", "date"), Col("item_name", "פריט", width="w-l", required=True),
                Col("quoted_total_incl_vat", "סה״כ כולל מע״מ", "quote_money"),
                Col("quoted_total_excl_vat", "סה״כ לפני מע״מ", "quote_money"),
                Col("quoted_currency", "מטבע", "currency"),
                Col("status", "סטטוס", "select", STATUSES), Col("project_text", "פרויקט", "suggest", width="w-m"),
                Col("category", "סעיף", "category"),
                Col("requested_by", "מבקש/ת", width="w-m"),
                Col("supplier", "ספק", width="w-m"), Col("po_number", "הזמנת רכש"), Col("quantity", "כמות", "num"),
                Col("quote_pdf", "הצעת מחיר", "calc"),
                Col("extraction_status", "מצב חילוץ", "ro", hidden=True),
                Col("lab_notes", "הערות", width="w-l")]),
    # Everything that is not a material: one typed amount, VAT plays no role.
    Table("orders_other", OrderRequest, "אחר",
          search=["item_name", "catalog_number", "supplier", "po_number", "grant_label", "project_text", "requested_by"],
          add=["item_name", "requested_by", "supplier", "catalog_number", "quantity", "amount_est",
               "amount_currency", "category", "project_text"],
          deletable=True,
          cols=[Col("date_requested", "תאריך", "date"), Col("item_name", "פריט", width="w-l", required=True),
                Col("amount_est", "סכום", "money"), Col("amount_currency", "מטבע", "currency"),
                Col("status", "סטטוס", "select", STATUSES), Col("project_text", "פרויקט", "suggest", width="w-m"),
                Col("category", "סעיף", "category"),
                Col("requested_by", "מבקש/ת", width="w-m"),
                Col("supplier", "ספק", width="w-m"), Col("po_number", "הזמנת רכש"), Col("quantity", "כמות", "num"),
                Col("amount_source", "מקור סכום", "ro"), Col("quote_pdf", "הצעת מחיר", "calc"),
                Col("lab_notes", "הערות", width="w-l")]),
    Table("fx", FxRate, "שערי חליפין", deletable=True, per_page=20,
          add=["date", "currency", "rate_to_ils"],
          cols=[Col("date", "תאריך", "date", required=True), Col("currency", "מטבע", "currency", required=True),
                Col("rate_to_ils", "₪ ליחידה", "num", required=True), Col("source", "מקור", "ro")]),
    Table("currencies", TrackedCurrency, "מטבעות", per_page=200,
          add=["code", "name_he"],
          cols=[Col("code", "קוד", "ro", required=True), Col("name_he", "שם"), Col("rate", "₪ ליחידה", "calc"),
                Col("rate_date", "תאריך השער", "calc"), Col("source", "מקור", "ro"),
                Col("is_active", "פעיל", "bool")]),
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
    if col.kind == "suggest":      # free text typed by the lab, else the request's GRANT_ID from LabSuit
        v = getattr(obj, col.field) or getattr(obj, "grant_label", "")
        return dict(col=col, value=v or "")
    if col.kind == "line":
        v = extra.get(col.field)
        return dict(col=col, value=fmt_num(v), neg=v is not None and v < 0,
                    after=extra.get("after:" + col.field.split(":", 1)[1]))
    if col.kind in FK_KINDS:
        v = getattr(obj, f"{col.field}_id")
        return dict(col=col, value="" if v is None else str(v))
    v = getattr(obj, col.field)
    if col.field == "quoted_currency":      # shows the currency the model really uses for this request
        from .amounts import quote_currency
        return dict(col=col, value=quote_currency(obj) or obj.amount_currency or "ILS")
    if col.kind == "quote_money":
        evidence = obj.extraction_evidence.get("values", {}).get(col.field, {})
        status = obj.extraction_status
        # a small marker (its text on hover) instead of an expandable part: only when the number needs a word
        if obj.quoted_manually_corrected:
            mark, text = "manual", "נכתב ידנית"
        elif not status:
            mark, text = "none", "טרם חולץ"
        elif v is None:
            mark, text = "miss", "לא נמצא בחילוץ"
        elif status == "ambiguous":
            mark, text = "miss", "דורש בדיקה"
        elif status == "failed":
            mark, text = "miss", "החילוץ נכשל"
        else:
            mark, text = "", ""
        tip = " · ".join(x for x in (text, f"עמוד {evidence['page']}" if evidence.get("page") else "",
                                     evidence.get("text", "")) if x)
        return dict(col=col, value=fmt_num(v), mark=mark, mark_text=text, tip=tip,
                    mark_glyph={"manual": "✎", "none": "·", "miss": "!"}.get(mark, ""))
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
    if isinstance(obj, OrderRequest) and name in (
            "quoted_total_excl_vat", "quoted_total_incl_vat", "quoted_currency"):
        with transaction.atomic():
            obj = OrderRequest.objects.select_for_update().get(pk=obj.pk)
            return _set_value(table, obj, name, raw)
    return _set_value(table, obj, name, raw)


def _set_value(table, obj, name, raw):
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
        elif isinstance(obj, OrderRequest) and name == "quoted_currency":
            setattr(obj, name, _to_python(obj, name, (raw or "").upper()))
        else:
            setattr(obj, name, _to_python(obj, name, raw))
    except (InvalidOperation, ValueError):
        raise ValidationError("ערך לא תקין")

    # side effects
    if isinstance(obj, OrderRequest) and name == "amount_est":
        obj.amount_source = OrderRequest.AmountSource.MANUAL if obj.amount_est else OrderRequest.AmountSource.NONE
    if isinstance(obj, OrderRequest) and name in (
            "quoted_total_excl_vat", "quoted_total_incl_vat", "quoted_currency"):
        if name == "quoted_currency":
            obj.quoted_currency = obj.quoted_currency.upper()
        if name != "quoted_currency" and getattr(obj, name) is not None and getattr(obj, name) < 0:
            raise ValidationError("סכום הצעה אינו יכול להיות שלילי")
        obj.quoted_manually_corrected = True
    obj.full_clean(validate_unique=False, exclude=[f.name for f in obj._meta.fields if f.name != name])
    if isinstance(obj, OrderRequest) and name in (
            "quoted_total_excl_vat", "quoted_total_incl_vat", "quoted_currency"):
        obj.save(update_fields=[name, "quoted_manually_corrected"])
    else:
        obj.save()
    return obj


def _fk_value(col, raw):
    raw = "" if raw is None else str(raw).strip()
    if raw in ("", "-"):
        return None
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
    if table.key in ("orders", "orders_other"):
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
    if parent is not None:          # reservations / rules belong to a grant
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
    if table.key in ("orders", "orders_other") and obj.amount_est:
        obj.amount_source = OrderRequest.AmountSource.MANUAL
    if table.key == "orders":
        if obj.category_id is None:
            obj.category_id = "MATERIAL"
        if obj.quoted_total_incl_vat is not None or obj.quoted_total_excl_vat is not None:
            obj.quoted_manually_corrected = True        # typed by the lab: a PDF extraction must not replace it
    if table.key == "orders_other" and obj.category_id in (None, "MATERIAL"):
        obj.category_id = "OTHER"
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
