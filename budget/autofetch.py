"""Automatic update from LabSuit: a headless browser does what a person would do.

    open the dashboard link → (log in: 1st field = username, 2nd = password, "Login")
    → click "Export" → the site's popup → "Export" again → wait for the download (up to a minute)
    → the file goes through the normal Merge preview; nothing is saved until the user confirms.

Elements are found by their text ("Login", "Export"), not by screen position, so a layout change
does not break the robot — a renamed button does. Any failure ends the job with a plain message
(and a screenshot of that moment) so the user can fall back to a manual file update.
"""
import logging
import re
import threading
from datetime import date
from urllib.parse import urlparse

from django.conf import settings
from django.db import close_old_connections, connection
from django.utils import timezone

from .models import AutoFetchJob, PendingImport, SourceCredential

log = logging.getLogger(__name__)
EXPORT = re.compile(r"^\s*export\s*$", re.I)
LOGIN = re.compile(r"^\s*(log\s*in|sign\s*in)\s*$", re.I)


class AutoFetchError(Exception):
    def __init__(self, message, detail="", screenshot=None):
        super().__init__(message)
        self.detail, self.screenshot = detail, screenshot


# --------------------------------------------------------------------------- the robot

def _first_visible(scope, pattern, timeout):
    """Visible buttons / links / <input type=submit> whose label matches — or, failing that, any element
    whose whole text matches (sites often make a styled <div> or <span> clickable)."""
    loc = (scope.get_by_role("button", name=pattern)
           .or_(scope.get_by_role("link", name=pattern))
           .or_(scope.get_by_text(pattern))).filter(visible=True)
    loc.first.wait_for(state="visible", timeout=timeout)
    return loc


def fetch_labsuit(url, username, password, step=lambda s: None, download_seconds=60, headless=True):
    """Returns (file bytes, suggested file name). Raises AutoFetchError with a Hebrew message."""
    from playwright.sync_api import Error as PwError
    from playwright.sync_api import TimeoutError as PwTimeout
    from playwright.sync_api import sync_playwright

    def scrub(text):
        return str(text).replace(password, "•••") if password else str(text)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
        except PwError as e:
            if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                raise AutoFetchError(
                    "הדפדפן האוטומטי עוד לא מותקן במחשב שמריץ את LabFund. מתקינים אותו פעם אחת בפקודה: "
                    "python -m playwright install chromium (בשרת Railway הוא כבר מותקן דרך ה-Dockerfile).",
                    str(e).split("╔")[0].strip()) from None
            raise AutoFetchError("לא הצלחנו להפעיל את הדפדפן האוטומטי.", scrub(e)) from None
        ctx = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 900}, locale="en-US")
        page = ctx.new_page()
        page.set_default_timeout(30_000)
        downloads = []
        ctx.on("page", lambda pg: pg.on("download", lambda d: downloads.append(d)))
        page.on("download", lambda d: downloads.append(d))

        def fail(message, exc=None):
            shot = None
            try:
                shot = page.screenshot(full_page=False)
            except Exception:  # noqa: BLE001
                pass
            raise AutoFetchError(message, scrub(exc) if exc else f"url: {page.url}", shot)

        try:
            step("נכנס ל-LabSuit")
            page.goto(url, wait_until="domcontentloaded")
            pw = page.locator("input[type=password]").filter(visible=True)
            try:
                pw.first.wait_for(state="visible", timeout=8_000)
                need_login = True
            except PwTimeout:
                need_login = False                      # already inside (unlikely for a fresh browser)

            if need_login:
                step("מתחבר עם המשתמש השמור")
                fields = page.locator("input:not([type=hidden]):not([type=checkbox]):not([type=radio])"
                                      ":not([type=submit]):not([type=button])").filter(visible=True)
                fields.nth(0).fill(username)            # 1st field: username
                fields.nth(1).fill(password)            # 2nd field: password
                try:
                    _first_visible(page, LOGIN, 5_000).first.click()
                except PwTimeout:
                    fail("לא נמצא כפתור Login בדף ההתחברות של LabSuit.")
                # logged in = the password field is gone. A visible "invalid / incorrect …" message
                # means wrong credentials — fail right away instead of waiting out the timeout.
                bad = page.get_by_text(re.compile(r"invalid|incorrect|wrong|failed|שגוי|לא נכון", re.I)).filter(visible=True)
                for _ in range(60):
                    if not page.locator("input[type=password]").filter(visible=True).count():
                        break
                    if bad.count():
                        fail("ההתחברות ל-LabSuit נכשלה — כנראה שם משתמש או סיסמה לא נכונים.")
                    page.wait_for_timeout(500)
                else:
                    fail("ההתחברות ל-LabSuit נכשלה — כנראה שם משתמש או סיסמה לא נכונים.")
            # LabSuit sends you to its home page after the login, so the link is opened a second time and
            # reloaded — a hash route (#orders) alone doesn't make the single-page app render that view.
            step("פותח את דף ההזמנות")
            page.goto(url, wait_until="domcontentloaded")
            page.reload(wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except PwTimeout:
                pass                                    # long-polling sites never go idle; carry on

            step("לוחץ Export")
            try:
                first = _first_visible(page, EXPORT, 30_000).first
            except PwTimeout:
                fail("לא נמצא כפתור Export בדף של LabSuit.")
            first.click()

            step("מאשר את חלון הייצוא")
            if not downloads:
                popup = page.locator(".modal, [role=dialog], dialog[open], .ui-dialog, .popup").filter(visible=True)
                confirm = None
                try:
                    confirm = _first_visible(popup, EXPORT, 10_000).last
                except PwTimeout:
                    # no recognisable popup: take the last visible "Export" that isn't the one we clicked
                    every = _first_visible(page, EXPORT, 1_000) if not downloads else None
                    if every is not None and every.count() > 1:
                        confirm = every.last
                if confirm is not None and not downloads:
                    confirm.click()

            step("ממתין לקובץ מ-LabSuit")
            deadline = download_seconds * 1000
            waited = 0
            while not downloads and waited < deadline:
                page.wait_for_timeout(500)
                waited += 500
            if not downloads:
                fail(f"LabSuit לא הוריד קובץ תוך {download_seconds} שניות.")
            dl = downloads[0]
            if dl.failure():
                fail(f"ההורדה מ-LabSuit נכשלה: {dl.failure()}")
            data = open(dl.path(), "rb").read()
            return data, dl.suggested_filename or "labsuit-export.xlsx"
        except AutoFetchError:
            raise
        except PwError as e:
            fail("הדפדפן האוטומטי נתקל בשגיאה מול LabSuit.", e)
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- jobs

def credentials():
    c = SourceCredential.objects.filter(source="labsuit").first()
    return (c.username, c.get_password()) if c else (None, None)


KIND_LABELS = {"labsuit_orders": "הזמנות", "labsuit_inventory": "מלאי"}


def describe_file(data):
    """(kind, summary): which LabSuit export this looks like — orders sheets have ITEM_NAME,
    inventory sheets have NAME — and a short list of its sheets and columns for the error details."""
    import io

    import pandas as pd
    try:
        frames = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str, nrows=5)
    except Exception as e:  # noqa: BLE001
        head = data[:200].decode("utf-8", "replace")
        return None, f"לא קובץ Excel ({e.__class__.__name__}). תחילת הקובץ: {head!r}"
    kind, lines = None, []
    for name, df in frames.items():
        cols = [str(c) for c in df.columns]
        if "ITEM_NAME" in cols:
            kind = kind or "labsuit_orders"
        elif "NAME" in cols:
            kind = kind or "labsuit_inventory"
        lines.append(f"{name}: {', '.join(cols[:12])}{' …' if len(cols) > 12 else ''}")
    return kind, "גיליונות ועמודות בקובץ:\n" + "\n".join(lines[:10]) + (f"\n… ועוד {len(lines) - 10}" if len(lines) > 10 else "")


def _wrong_file(job, data, reason=""):
    from django.conf import settings as s
    kind, summary = describe_file(data)
    want = KIND_LABELS.get(job.source, job.source)
    if kind and kind != job.source:
        return AutoFetchError(
            f"הקובץ שהתקבל מ-LabSuit הוא ייצוא {KIND_LABELS[kind]}, לא {want}. כנראה שהדף שנפתח ב-LabSuit "
            f"הוא לא דף ה{want} — כדאי לבדוק את הקישור (LABSUIT_ORDERS_URL).",
            f"URL: {s.LABSUIT_URLS.get(job.table_key, '')}\n\n{summary}")
    return AutoFetchError(f"הקובץ שהתקבל מ-LabSuit ריק, או שהוא לא ייצוא של {want}.",
                          (reason + "\n" if reason else "") + summary)


def run_job(job_id, fetch=None):
    """Background worker: robot → PendingImport with a Merge preview. Never raises."""
    from .importers.preview import dry_run

    close_old_connections()
    job = AutoFetchJob.objects.get(pk=job_id)

    def step(text):
        # called from inside Playwright, which runs its own event loop in this thread — Django refuses
        # ORM calls there, so the progress update is written from a short-lived side thread
        def write():
            AutoFetchJob.objects.filter(pk=job.pk).update(step=text)
            connection.close()
        t = threading.Thread(target=write)
        t.start()
        t.join()
        job.step = text

    try:
        job.status = AutoFetchJob.Status.RUNNING
        job.save(update_fields=["status"])
        user, pw = credentials()
        if not user or not pw:
            raise AutoFetchError("חסרים שם משתמש וסיסמה ל-LabSuit.")
        url = settings.LABSUIT_URLS[job.table_key]
        data, name = (fetch or fetch_labsuit)(url, user, pw, step=step,
                                              download_seconds=settings.AUTOFETCH_DOWNLOAD_SECONDS)
        job.file_data, job.file_name = data, name
        step("מכין תצוגה מקדימה")
        pending = PendingImport.objects.create(
            table_key=job.table_key, source=job.source, file_name=f"LabSuit (אוטומטי) · {name}", data=data,
            mode="merge", as_of=date.today(), next_url=job.next_url, uploaded_by=job.created_by)
        try:
            pending.preview = dry_run(job.source, data, name, pending.as_of, "merge", user=job.created_by)
        except Exception as e:  # noqa: BLE001
            pending.delete()
            raise _wrong_file(job, data, str(e)) from None
        if not pending.preview["counts"]["in_file"]:
            pending.delete()
            raise _wrong_file(job, data)
        pending.save(update_fields=["preview"])
        job.pending, job.status = pending, AutoFetchJob.Status.DONE
    except AutoFetchError as e:
        job.status, job.error, job.detail = AutoFetchJob.Status.FAILED, str(e), e.detail[:4000]
        job.screenshot = e.screenshot
    except Exception as e:  # noqa: BLE001 — e.g. Chromium missing on the server
        log.exception("auto-fetch failed")
        job.status, job.error, job.detail = AutoFetchJob.Status.FAILED, "שגיאה לא צפויה בעדכון האוטומטי.", str(e)[:4000]
    job.finished_at = timezone.now()
    job.save()
    if not settings.AUTOFETCH_SYNC:
        connection.close()


def start_job(job):
    if settings.AUTOFETCH_SYNC:
        run_job(job.pk)
    else:
        threading.Thread(target=run_job, args=(job.pk,), daemon=True, name=f"autofetch-{job.pk}").start()
