"""Data model for LabFund.

Principles
- Every file import is an ImportBatch with an as-of date; imports never delete history.
- Money is stored in its original currency (NUMERIC, never float). Conversions are
  frozen on the Allocation row that used them.
- SAP balances are already net of SAP commitments, so only orders SAP has not seen yet
  (no PO, or PO after the SAP snapshot) are candidates for resolve.
"""
from decimal import Decimal

from django.db import models
from django.core.validators import RegexValidator
from django.utils import timezone

MONEY = dict(max_digits=14, decimal_places=2)


def validate_currency(code):
    """Any 3-letter code that is ILS or one of the tracked currencies (Settings → exchange rates)."""
    from django.core.exceptions import ValidationError
    code = (code or "").upper()
    if code == "ILS":
        return
    if not TrackedCurrency.objects.filter(code=code).exists():
        raise ValidationError(f"המטבע {code} לא מוגדר. אפשר להוסיף אותו בהגדרות → שערי חליפין.")


class TrackedCurrency(models.Model):
    """A currency the lab works with. Its rate to ILS is refreshed automatically."""

    class Source(models.TextChoices):
        BOI = "boi", "בנק ישראל (שער יציג)"
        ERAPI = "erapi", "ExchangeRate-API"
        MANUAL = "manual", "ידני"

    code = models.CharField("קוד", max_length=3, primary_key=True)
    name_he = models.CharField("שם", max_length=60, blank=True)
    is_active = models.BooleanField("פעיל", default=True)
    source = models.CharField("מקור", max_length=10, choices=Source.choices, default=Source.BOI)
    last_fetched = models.DateTimeField("עודכן", null=True, blank=True)
    sort = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["sort", "code"]
        verbose_name = "מטבע"
        verbose_name_plural = "מטבעות"

    def __str__(self):
        return self.code


class ImportBatch(models.Model):
    class Source(models.TextChoices):
        LABSUIT_ORDERS = "labsuit_orders", "LabSuit — הזמנות"
        LABSUIT_INVENTORY = "labsuit_inventory", "LabSuit — מלאי"
        SAP_GRANTS = "sap_grants", "SAP/TRDF — יתרות מענקים"
        SAP_BUDGET = "sap_budget", "SAP — תקציב מול ביצוע"

    class Mode(models.TextChoices):
        MERGE = "merge", "Merge — עדכון והוספה"
        REPLACE = "replace", "Replace — החלפה מלאה"

    source = models.CharField(max_length=32, choices=Source.choices)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.MERGE)
    file_name = models.CharField(max_length=255)
    as_of = models.DateField(help_text="תאריך הנתונים בקובץ (לא תאריך ההעלאה)")
    uploaded_at = models.DateTimeField(default=timezone.now)
    uploaded_by = models.CharField(max_length=150, blank=True)
    stats = models.JSONField(default=dict, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-as_of", "-id"]
        verbose_name = "ייבוא"
        verbose_name_plural = "ייבואים"

    def __str__(self):
        return f"{self.get_source_display()} · {self.as_of:%d/%m/%Y} · {self.file_name}"

    @classmethod
    def latest(cls, source):
        return cls.objects.filter(source=source).order_by("-as_of", "-id").first()


class PendingImport(models.Model):
    """An uploaded file waiting for the user to confirm its preview. Kept in the DB (not on disk) so it
    survives across gunicorn workers; deleted on confirm/cancel, and stale ones are purged."""
    table_key = models.CharField(max_length=20)
    source = models.CharField(max_length=32, choices=ImportBatch.Source.choices)
    file_name = models.CharField(max_length=255)
    data = models.BinaryField()
    mode = models.CharField(max_length=10, choices=ImportBatch.Mode.choices)
    as_of = models.DateField()
    min_year = models.PositiveSmallIntegerField(null=True, blank=True)
    next_url = models.CharField(max_length=300, blank=True)
    preview = models.JSONField(default=dict, blank=True)
    uploaded_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def kwargs(self):
        return {"min_year": self.min_year} if self.source == ImportBatch.Source.LABSUIT_ORDERS and self.min_year else {}


class PendingFile(models.Model):
    """A PDF the robot scraped during an automatic update. It waits with its PendingImport and is attached
    to the order only when the user confirms the preview (nothing is saved before that)."""
    pending = models.ForeignKey(PendingImport, on_delete=models.CASCADE, related_name="files")
    natural_key = models.CharField(max_length=64)
    quote_id = models.CharField(max_length=60, blank=True)
    file_name = models.CharField(max_length=255)
    data = models.BinaryField()


def _fernet():
    """Symmetric key derived from SECRET_KEY: stored passwords are unreadable without it."""
    import base64
    import hashlib

    from cryptography.fernet import Fernet
    from django.conf import settings
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("labfund:" + settings.SECRET_KEY).encode()).digest()))


class SourceCredential(models.Model):
    """Login for an external system the robot reads from (one per system: both LabSuit dashboards share it).
    The password is encrypted at rest and never sent back to the browser."""
    source = models.CharField(max_length=20, primary_key=True)        # "labsuit"
    username = models.CharField(max_length=150)
    password_enc = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=150, blank=True)

    def set_password(self, raw):
        self.password_enc = _fernet().encrypt(raw.encode()).decode()

    def get_password(self):
        from cryptography.fernet import InvalidToken
        try:
            return _fernet().decrypt(self.password_enc.encode()).decode()
        except InvalidToken:
            return None          # SECRET_KEY changed since it was saved — ask for it again


class AutoFetchJob(models.Model):
    """One automatic update run: robot → downloaded file → PendingImport (preview)."""
    class Status(models.TextChoices):
        QUEUED = "queued", "בתור"
        RUNNING = "running", "רץ"
        DONE = "done", "הסתיים"
        FAILED = "failed", "נכשל"

    table_key = models.CharField(max_length=20)
    source = models.CharField(max_length=32, choices=ImportBatch.Source.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    step = models.CharField(max_length=120, blank=True)
    error = models.TextField(blank=True)
    detail = models.TextField(blank=True)
    screenshot = models.BinaryField(null=True, blank=True)
    file_data = models.BinaryField(null=True, blank=True)          # what LabSuit sent, kept for diagnosis
    file_name = models.CharField(max_length=255, blank=True)
    pending = models.ForeignKey(PendingImport, null=True, blank=True, on_delete=models.SET_NULL)
    next_url = models.CharField(max_length=300, blank=True)
    created_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)


# --------------------------------------------------------------------------- budget taxonomy

class BudgetCategory(models.Model):
    code = models.CharField(max_length=20, primary_key=True)
    name_he = models.CharField(max_length=60)
    sort = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["sort"]
        verbose_name = "סעיף תקציבי"
        verbose_name_plural = "סעיפים תקציביים"

    def __str__(self):
        return self.name_he


class CategoryMap(models.Model):
    """Maps an external value (SAP cost element, TRDF column, LabSuit ITEM_TYPE) to a category."""

    class Source(models.TextChoices):
        SAP_COST_ELEMENT = "sap_cost_element", "SAP cost element"
        TRDF_COLUMN = "trdf_column", "TRDF balance column"
        LABSUIT_ITEM_TYPE = "labsuit_item_type", "LabSuit ITEM_TYPE"

    source = models.CharField(max_length=32, choices=Source.choices)
    value = models.CharField(max_length=80)
    category = models.ForeignKey(BudgetCategory, on_delete=models.PROTECT)

    class Meta:
        unique_together = [("source", "value")]
        verbose_name = "מיפוי לסעיף"
        verbose_name_plural = "מיפויים לסעיפים"

    def __str__(self):
        return f"{self.value} → {self.category_id}"

    @classmethod
    def lookup(cls, source, value, default=None):
        if value is None:
            return default
        key = str(value).strip()
        m = cls.objects.filter(source=source, value__iexact=key).first()
        return m.category_id if m else default


# --------------------------------------------------------------------------- grants

class Grant(models.Model):
    RESERVE_TYPES = {"Cathedras", "Labs & research Centers", "Internal Grant"}

    number = models.CharField("מספר מענק", max_length=20, primary_key=True)
    sponsor = models.CharField("גורם מממן", max_length=200, blank=True)
    currency = models.CharField("מטבע", max_length=3, validators=[validate_currency])
    start_date = models.DateField("התחלה", null=True, blank=True)
    end_date = models.DateField("סיום", null=True, blank=True)
    budget_total = models.DecimalField("תקציב", **MONEY, null=True, blank=True)
    expense_total = models.DecimalField("הוצאה+התחייבות", **MONEY, null=True, blank=True)
    balance_total = models.DecimalField("יתרה כוללת", **MONEY, default=Decimal("0"))
    grant_type = models.CharField("סוג", max_length=60, blank=True)
    subject = models.CharField("נושא", max_length=300, blank=True)
    description = models.CharField("תיאור", max_length=300, blank=True)
    coordinator = models.CharField("רכז/ת", max_length=100, blank=True)
    status = models.CharField(max_length=30, blank=True)

    # lab-side settings (never overwritten by imports)
    pi_priority = models.SmallIntegerField(
        "עדיפות", default=0, help_text="-3 … +3. חיובי = להעדיף מענק זה")
    is_reserve = models.BooleanField(
        "מענק רזרבה", default=False,
        help_text="מענק גמיש/ארוך שעדיף לשמור (קתדרות, תקציב מעבדה). נקבע אוטומטית לפי סוג בייבוא הראשון.")
    vat_required = models.BooleanField(
        "מצריך תשלום מע״מ", default=True,
        help_text="כבוי = המענק לא משלם מע״מ: מהמענק יורד המחיר לפני מע״מ (מחולק ב-1.18)")
    order_cutoff_days = models.PositiveSmallIntegerField(
        "ימי חסימה לפני סיום", default=30,
        help_text="אין להקצות הזמנה חדשה למענק שנשארו לו פחות ימים מזה")
    is_active = models.BooleanField("פעיל", default=True)
    notes = models.TextField("הערות", blank=True)

    last_batch = models.ForeignKey(ImportBatch, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["end_date"]
        verbose_name = "מענק"
        verbose_name_plural = "מענקים"

    def __str__(self):
        return f"{self.number} · {self.sponsor or self.description}"[:80]

    def current_lines(self):
        if not self.last_batch_id:
            return self.lines.none()
        return self.lines.filter(batch_id=self.last_batch_id)

    @property
    def is_pooled(self):
        """Grants with a GENERAL budget line can be spent on any category up to the total balance."""
        return self.current_lines().filter(category_id="GENERAL", balance__gt=0).exists()

    def days_left(self, today=None):
        if not self.end_date:
            return None
        return (self.end_date - (today or timezone.localdate())).days


class GrantLine(models.Model):
    """Balance of one category in one grant, as of one import batch (grant currency)."""

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="grant_lines")
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="lines")
    category = models.ForeignKey(BudgetCategory, on_delete=models.PROTECT)
    balance = models.DecimalField("יתרה", **MONEY)

    class Meta:
        unique_together = [("batch", "grant", "category")]
        verbose_name = "יתרת סעיף"
        verbose_name_plural = "יתרות סעיפים"


class GrantRule(models.Model):
    """Hard spending rules per grant+category. Not in any SAP export — entered once by the PI."""

    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="rules")
    category = models.ForeignKey(BudgetCategory, on_delete=models.PROTECT)
    allowed = models.BooleanField("מותר", default=True)
    extra_allowance = models.DecimalField(
        "חריגה מותרת", **MONEY, default=Decimal("0"),
        help_text="סכום (במטבע המענק) שמותר לחרוג מעבר ליתרת הסעיף — העברה בין סעיפים")
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = [("grant", "category")]
        verbose_name = "כלל מענק"
        verbose_name_plural = "כללי מענקים"


class Reservation(models.Model):
    """Money to hold back from resolve (e.g. salaries planned until the grant ends)."""

    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="reservations")
    category = models.ForeignKey(BudgetCategory, on_delete=models.PROTECT)
    amount = models.DecimalField("סכום (במטבע המענק)", **MONEY)
    until = models.DateField("בתוקף עד", null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = "שריון"
        verbose_name_plural = "שריונים"


# --------------------------------------------------------------------------- projects & people

class Project(models.Model):
    name = models.CharField("שם", max_length=120, unique=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="children", verbose_name="פרויקט-על")
    owner = models.CharField("אחראי/ת", max_length=120, blank=True)
    is_active = models.BooleanField(default=True)
    grants = models.ManyToManyField(Grant, through="ProjectGrant", blank=True)

    class Meta:
        verbose_name = "פרויקט"
        verbose_name_plural = "פרויקטים"

    def __str__(self):
        return self.name

    def eligible_grant_numbers(self):
        """Grants linked to this project or any ancestor. Empty set = no restriction."""
        nums, node, seen = set(), self, set()
        while node and node.pk not in seen:
            seen.add(node.pk)
            nums |= set(node.projectgrant_set.values_list("grant_id", flat=True))
            node = node.parent
        return nums


class ProjectGrant(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE)
    weight = models.SmallIntegerField("העדפה", default=0, help_text="-3 … +3")

    class Meta:
        unique_together = [("project", "grant")]


class Person(models.Model):
    email = models.EmailField(primary_key=True)
    name = models.CharField(max_length=120, blank=True)
    default_project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        verbose_name = "חבר/ת צוות"
        verbose_name_plural = "צוות"

    def __str__(self):
        return self.name or self.email


# --------------------------------------------------------------------------- funding labels

class StandingOrder(models.Model):
    """A blanket PO / consignment arrangement ("הזמנה עומדת", "שמור/מכור") — already committed in SAP."""

    name = models.CharField(max_length=120, unique=True)
    vendor = models.CharField(max_length=120, blank=True)
    grant = models.ForeignKey(Grant, null=True, blank=True, on_delete=models.SET_NULL)
    po_number = models.CharField(max_length=30, blank=True)
    cap = models.DecimalField(**MONEY, null=True, blank=True)
    currency = models.CharField(max_length=3, default="ILS", validators=[validate_currency])
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "הזמנה עומדת"
        verbose_name_plural = "הזמנות עומדות"

    def __str__(self):
        return self.name


class FundingAlias(models.Model):
    """What a free-text GRANT_ID label from LabSuit actually means."""

    class Kind(models.TextChoices):
        GRANT = "grant", "מענק ספציפי"
        STANDING = "standing", "הזמנה עומדת / שמור-מכור (כבר מחויב ב-SAP)"
        OPEN = "open", "פתוח — resolve יחליט"

    label = models.CharField(max_length=120, unique=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.OPEN)
    grant = models.ForeignKey(Grant, null=True, blank=True, on_delete=models.SET_NULL)
    standing_order = models.ForeignKey(StandingOrder, null=True, blank=True, on_delete=models.SET_NULL)
    needs_review = models.BooleanField(default=False)

    class Meta:
        verbose_name = "כינוי מימון"
        verbose_name_plural = "כינויי מימון"

    def __str__(self):
        return f"{self.label} ({self.get_kind_display()})"


# --------------------------------------------------------------------------- orders & inventory

class OrderRequest(models.Model):
    class AmountSource(models.TextChoices):
        LABSUIT = "labsuit", "LabSuit"
        MANUAL = "manual", "ידני"
        NONE = "none", "אין"

    # identity
    natural_key = models.CharField(max_length=64, unique=True)
    source = models.CharField(max_length=12, default="labsuit")  # labsuit | manual
    batch = models.ForeignKey(ImportBatch, null=True, blank=True, on_delete=models.SET_NULL)
    is_current = models.BooleanField("בייצוא האחרון", default=True)

    # LabSuit-owned fields (overwritten on every import)
    item_name = models.CharField("פריט", max_length=300)
    catalog_number = models.CharField("מק״ט", max_length=120, blank=True)
    manufacturer = models.CharField(max_length=120, blank=True)
    supplier = models.CharField("ספק", max_length=120, blank=True)
    item_type = models.CharField("סוג", max_length=60, blank=True)
    requested_by = models.CharField("מבקש/ת", max_length=150, blank=True)
    grant_label = models.CharField("GRANT_ID ב-LabSuit", max_length=120, blank=True)
    status = models.CharField("סטטוס", max_length=20, blank=True)
    po_number = models.CharField("הזמנת רכש", max_length=40, blank=True)
    quote_id = models.CharField(max_length=60, blank=True)
    quantity = models.DecimalField("כמות", max_digits=10, decimal_places=2, default=1)
    pack_size = models.CharField(max_length=80, blank=True)
    labsuit_total = models.DecimalField(**MONEY, null=True, blank=True)
    labsuit_currency = models.CharField(max_length=3, blank=True)
    date_requested = models.DateField(null=True, blank=True)
    date_approved = models.DateField(null=True, blank=True)
    date_ordered = models.DateField(null=True, blank=True)
    date_received = models.DateField(null=True, blank=True)
    date_cancelled = models.DateField(null=True, blank=True)
    comments = models.TextField(blank=True)

    # lab-owned fields (preserved across imports)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="פרויקט (ישן)")  # legacy: no longer set or read
    category = models.ForeignKey(BudgetCategory, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="סעיף")
    amount_est = models.DecimalField("סכום", **MONEY, null=True, blank=True)
    amount_currency = models.CharField("מטבע", max_length=3, default="ILS", validators=[validate_currency])
    amount_source = models.CharField(max_length=10, choices=AmountSource.choices, default=AmountSource.NONE)
    lab_notes = models.TextField("הערות מעבדה", blank=True)
    # What the "פרויקט" column shows: free text typed by the lab (suggestions come from the grants table).
    # Empty = the request's GRANT_ID from LabSuit is shown. Display only: nothing in the funding logic reads it.
    project_text = models.CharField("פרויקט", max_length=200, blank=True)

    # Quotation facts are independent of funding inputs and LabSuit imports.
    quoted_total_excl_vat = models.DecimalField("סה״כ לפני מע״מ", **MONEY, null=True, blank=True)
    quoted_total_incl_vat = models.DecimalField("סה״כ כולל מע״מ", **MONEY, null=True, blank=True)
    quoted_currency = models.CharField(max_length=3, blank=True, default="",
        validators=[RegexValidator(r"^[A-Z]{3}$", "יש להזין קוד מטבע בן שלוש אותיות")])
    extraction_status = models.CharField(max_length=10, blank=True, default="", choices=[
        ("success", "הצלחה"), ("ambiguous", "דורש בדיקה"), ("missing", "חלקי / חסר"), ("failed", "נכשל")])
    extraction_evidence = models.JSONField(default=dict, blank=True)
    # Lock the whole amount/currency pair when a user corrects either total.
    quoted_manually_corrected = models.BooleanField(default=False)

    LABSUIT_FIELDS = [
        "item_name", "catalog_number", "manufacturer", "supplier", "item_type", "requested_by",
        "grant_label", "status", "po_number", "quote_id", "quantity", "pack_size",
        "labsuit_total", "labsuit_currency", "date_requested", "date_approved", "date_ordered",
        "date_received", "date_cancelled", "comments",
    ]

    class Meta:
        ordering = ["-date_requested", "-id"]
        verbose_name = "הזמנה/דרישה"
        verbose_name_plural = "הזמנות ודרישות"

    def __str__(self):
        return f"{self.item_name} ×{self.quantity:g}"


class OrderFile(models.Model):
    """The PDF attached to a request in LabSuit (the supplier's quote). Kept apart from OrderRequest so
    listing orders never loads the file bytes."""
    order = models.OneToOneField(OrderRequest, on_delete=models.CASCADE, related_name="file")
    quote_id = models.CharField(max_length=60, blank=True)
    file_name = models.CharField("קובץ", max_length=255)
    data = models.BinaryField()
    size = models.PositiveIntegerField(default=0)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "קובץ הצעת מחיר"
        verbose_name_plural = "קבצי הצעות מחיר"


class InventoryItem(models.Model):
    key = models.CharField(max_length=80, unique=True)
    batch = models.ForeignKey(ImportBatch, null=True, blank=True, on_delete=models.SET_NULL)
    is_current = models.BooleanField(default=True)
    serial_number = models.CharField("מספר סידורי", max_length=40, blank=True)
    item_type = models.CharField("סוג", max_length=60, blank=True)
    name = models.CharField("שם", max_length=300)
    catalog_number = models.CharField("מק״ט", max_length=120, blank=True)
    manufacturer = models.CharField(max_length=120, blank=True)
    supplier = models.CharField("ספק", max_length=120, blank=True)
    pack_size = models.CharField(max_length=80, blank=True)
    amount_in_stock = models.DecimalField("במלאי", max_digits=10, decimal_places=2, null=True, blank=True)
    in_stock = models.BooleanField(default=True)
    location = models.CharField("מיקום", max_length=120, blank=True)
    sub_location = models.CharField(max_length=120, blank=True)
    price = models.DecimalField(**MONEY, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    supply_date = models.DateField(null=True, blank=True)
    owner = models.CharField(max_length=150, blank=True)
    comments = models.TextField(blank=True)
    attributes = models.JSONField(default=dict, blank=True, help_text="עמודות ייחודיות לסוג הפריט")

    class Meta:
        ordering = ["item_type", "name"]
        verbose_name = "פריט מלאי"
        verbose_name_plural = "מלאי"

    def __str__(self):
        return self.name


# --------------------------------------------------------------------------- SAP budget-vs-actual

class BudgetReportLine(models.Model):
    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="report_lines")
    row = models.PositiveIntegerField()
    block = models.CharField("בלוק", max_length=20, help_text="מטבע הבלוק או OVERHEAD")
    cost_element = models.CharField("סיווג", max_length=30, blank=True)
    label = models.CharField(max_length=120, blank=True)
    category = models.ForeignKey(BudgetCategory, null=True, blank=True, on_delete=models.SET_NULL)
    is_total = models.BooleanField(default=False)
    budget = models.DecimalField(**MONEY, default=0)
    committed = models.DecimalField("התחייבויות", **MONEY, default=0)
    actual = models.DecimalField("ביצוע", **MONEY, default=0)
    balance = models.DecimalField("יתרה", **MONEY, default=0)

    class Meta:
        ordering = ["batch", "row"]


# --------------------------------------------------------------------------- FX & resolve

class FxRate(models.Model):
    date = models.DateField()
    currency = models.CharField(max_length=3)
    rate_to_ils = models.DecimalField(max_digits=20, decimal_places=10, help_text="₪ ליחידה אחת")
    source = models.CharField(max_length=30, default="boi")

    class Meta:
        unique_together = [("date", "currency")]
        ordering = ["-date"]
        verbose_name = "שער חליפין"
        verbose_name_plural = "שערי חליפין"

    def __str__(self):
        return f"{self.currency} {self.rate_to_ils} ({self.date:%d/%m/%Y})"


class ResolveRun(models.Model):
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.CharField(max_length=150, blank=True)
    run_date = models.DateField()
    sap_batch = models.ForeignKey(ImportBatch, null=True, blank=True, on_delete=models.SET_NULL)
    params = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)
    log = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class Allocation(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "proposed", "הצעה"
        APPROVED = "approved", "מאושר"
        REJECTED = "rejected", "נדחה"
        SUPERSEDED = "superseded", "כלול ביתרת SAP"

    # SET_NULL: once the lab has updated LabSuit/SAP the order row itself is deleted, but the funding stays
    # reserved on the grant until SAP covers it — so what the decision needs about its order is kept here.
    order = models.ForeignKey(OrderRequest, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="allocations")
    order_label = models.CharField(max_length=300, blank=True, help_text="שם הפריט, אחרי שההזמנה נמחקה")
    order_key = models.CharField(max_length=64, blank=True, db_index=True,
                                 help_text="המפתח הטבעי של ההזמנה שנמחקה")
    order_po_date = models.DateField(null=True, blank=True, help_text="תאריך הזמנת הרכש, אם היה ידוע")
    run = models.ForeignKey(ResolveRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="allocations")
    grant = models.ForeignKey(Grant, null=True, blank=True, on_delete=models.PROTECT,
                              help_text="ריק = לא נמצא מימון לחלק הזה")
    category = models.ForeignKey(BudgetCategory, on_delete=models.PROTECT)
    amount_orig = models.DecimalField(**MONEY)
    currency_orig = models.CharField(max_length=3)
    amount_ils = models.DecimalField(**MONEY)
    amount_grant_ccy = models.DecimalField(**MONEY, null=True, blank=True)
    fx_rate = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True,
                                  help_text="מטבע ההזמנה → מטבע המענק, כולל מרווח ביטחון")
    fx_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PROPOSED)
    score = models.FloatField(null=True, blank=True)
    reason = models.TextField(blank=True)
    decided_by = models.CharField(max_length=150, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    # "עודכן במערכות המעבדה": the user updated LabSuit / SAP by hand after approving. Bookkeeping only —
    # the amount stays reserved until a SAP report dated on/after the order (or this mark) covers it.
    lab_updated_at = models.DateTimeField("עודכן במערכות המעבדה", null=True, blank=True)
    lab_updated_by = models.CharField(max_length=150, blank=True)

    @property
    def item_name(self):
        return self.order.item_name if self.order_id else self.order_label

    class Meta:
        ordering = ["-id"]
        verbose_name = "הקצאה"
        verbose_name_plural = "הקצאות"
