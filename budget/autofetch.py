"""Automatic update from LabSuit: a headless browser does what a person would do.

    open the dashboard link → (log in: 1st field = username, 2nd = password, "Login")
    → click "Export" → the site's popup → "Export" again → wait for the download (up to a minute)
    → the file goes through the normal Merge preview; nothing is saved until the user confirms.

Orders only, in the same session and right after the export: for every request in the file, the robot
loads the whole list (it scrolls), clicks the request's name, and takes the one PDF listed under "Files"
in the side panel (LabSuit's supplier quote). The files wait with the preview and are attached to the
orders when the user confirms. A request that cannot be found, or whose file does not match its quote
number, is reported and skipped — never attached to the wrong order.

Elements are found by their text ("Login", "Export"), not by screen position, so a layout change
does not break the robot — a renamed button does. Any failure ends the job with a plain message
(and a screenshot of that moment) so the user can fall back to a manual file update.
"""
import logging
import re
import threading
import time
from datetime import date
from urllib.parse import urljoin, urlparse

from django.conf import settings
from django.db import close_old_connections, connection
from django.utils import timezone

from .models import AutoFetchJob, ImportBatch, OrderFile, PendingFile, PendingImport, SourceCredential

log = logging.getLogger(__name__)
EXPORT = re.compile(r"^\s*export\s*$", re.I)
LOGIN = re.compile(r"^\s*(log\s*in|sign\s*in)\s*$", re.I)
PDF_NAME = re.compile(r"\.pdf\s*$", re.I)


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



# --------------------------------------------------------------------------- attachments (PDF per request)

def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _load_whole_list(page, rounds=80):
    """LabSuit's list loads more requests as you scroll: scroll until nothing new appears."""
    last, stable = -1, 0
    for _ in range(rounds):
        height = page.evaluate("() => document.documentElement.scrollHeight")
        page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(600)
        stable = stable + 1 if height == last else 0
        if stable >= 3:
            break
        last = height
    page.evaluate("() => window.scrollTo(0, 0)")


_CARD_TEXT_JS = """(els) => {
  els.forEach(e => e.setAttribute('data-lf-cand', '1'));
  const out = els.map(e => {            // the request's card = the widest ancestor holding only this title
    let n = e;
    while (n.parentElement && n.parentElement.querySelectorAll('[data-lf-cand]').length === 1) n = n.parentElement;
    return n.innerText || '';
  });
  els.forEach(e => e.removeAttribute('data-lf-cand'));
  return out;
}"""


def _request_title(page, target):
    """The clickable name of one request on the (fully loaded) list, or (None, reason).
    Requests that share a name are told apart by the quote number, catalog number and requester in
    their card; when that is not enough the request is skipped rather than guessed."""
    title = target["item_name"]
    cands = page.get_by_text(title, exact=True).filter(visible=True)
    if not cands.count():
        cands = page.get_by_text(re.compile(r"^\s*" + re.escape(title) + r"\s*$", re.I)).filter(visible=True)
    n = cands.count()
    if n == 0:
        return None, "לא נמצאה ברשימה"
    if n == 1:
        return cands.first, ""
    cards = cands.evaluate_all(_CARD_TEXT_JS)
    scores = []
    for text in cards:
        t = _norm(text)
        s = 0
        if target["quote_id"] and target["quote_id"].lower() in t:
            s += 4
        if target["catalog_number"] and _norm(target["catalog_number"]) in t:
            s += 2
        if target["requested_by"] and _norm(target["requested_by"]) in t:
            s += 1
        scores.append(s)
    best = max(scores)
    if best >= 2 and scores.count(best) == 1:
        return cands.nth(scores.index(best)), ""
    return None, "כמה בקשות באותו שם ולא ניתן להבדיל ביניהן"


def _read_pdf(page, ctx, link, seen, timeout_ms):
    """The PDF behind a file link in the side panel: (bytes, name). Tries the link's own URL first (same
    logged-in session), then clicks it and takes whatever arrives — a download, or a PDF the viewer loads."""
    name = (link.inner_text() or "").strip()
    href = link.get_attribute("href")
    if href and not href.lower().startswith(("javascript:", "#", "blob:", "data:")):
        try:
            resp = ctx.request.get(urljoin(page.url, href))
            body = resp.body()
            if resp.ok and body[:4] == b"%PDF":
                return body, name
        except Exception:  # noqa: BLE001 — fall through to clicking
            pass
    n_dl, n_pdf = len(seen["downloads"]), len(seen["pdfs"])
    link.click()
    waited = 0
    while waited < timeout_ms:
        for dl in seen["downloads"][n_dl:]:
            try:
                body = open(dl.path(), "rb").read()
            except Exception:  # noqa: BLE001
                continue
            if body[:4] == b"%PDF":
                return body, dl.suggested_filename or name
        for resp in seen["pdfs"][n_pdf:]:
            try:
                body = resp.body()
            except Exception:  # noqa: BLE001 — the body of a navigation that became a download
                continue
            if body[:4] == b"%PDF":
                return body, name
        src = page.evaluate("() => { const e = document.querySelector('iframe[src], embed[src], object[data]');"
                            " return e ? (e.src || e.data || '') : '' }")
        if src.startswith(("http://", "https://")):
            try:
                resp = ctx.request.get(src)
                body = resp.body()
                if resp.ok and body[:4] == b"%PDF":
                    return body, name
            except Exception:  # noqa: BLE001
                pass
        page.wait_for_timeout(300)
        waited += 300
    raise AutoFetchError("הקובץ לא הגיע")


def _close_panel(page):
    """Leave the side panel closed, so the next request cannot be read from the previous one's file."""
    links = page.locator("a").filter(has_text=PDF_NAME).filter(visible=True)
    for _ in range(3):
        if not links.count():
            return True
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        if links.count():
            closer = page.get_by_role("button", name=re.compile(r"^\s*(close|×|✕|x|סגור)\s*$", re.I)).filter(visible=True)
            if closer.count():
                closer.first.click()
                page.wait_for_timeout(300)
    return not links.count()


def scrape_attachments(page, ctx, data, have, step, max_seconds, seen, panel_seconds=6, refetch=False):
    """One PDF per request in the export, from the side panel of the orders list.
    `have` = {natural_key: quote_id} of files we already store (skipped unless refetch).
    Returns {"files": [dict(key, quote_id, name, data)], "report": {...}}; never raises."""
    from playwright.sync_api import Error as PwError
    from playwright.sync_api import TimeoutError as PwTimeout

    from .importers.labsuit_orders import scrape_targets

    report = dict(saved=0, skipped=0, not_found=[], mismatch=[], failed=[], stopped=0, error="")
    files = []
    try:
        targets = scrape_targets(data)
    except Exception as e:  # noqa: BLE001
        report["error"] = f"קריאת הבקשות מהקובץ נכשלה: {e}"
        return dict(files=files, report=report)

    todo = []
    for t in targets:
        if not refetch and t["key"] in have and (not t["quote_id"] or have[t["key"]] == t["quote_id"]):
            report["skipped"] += 1
        else:
            todo.append(t)
    if not todo:
        return dict(files=files, report=report)

    started = time.monotonic()
    try:
        step("טוען את כל רשימת הבקשות")
        _load_whole_list(page)
    except (PwError, PwTimeout) as e:
        report["error"] = f"טעינת הרשימה נכשלה: {str(e)[:200]}"
        return dict(files=files, report=report)

    for i, t in enumerate(todo, 1):
        if time.monotonic() - started > max_seconds:
            report["stopped"] = len(todo) - i + 1
            break
        step(f"שומר קובץ PDF {i}/{len(todo)}")
        label = t["item_name"][:60]
        try:
            title, why = _request_title(page, t)
            if title is None:
                report["not_found"].append(f"{label} — {why}")
                continue
            title.click()
            link = page.locator("a").filter(has_text=PDF_NAME).filter(visible=True)
            try:
                link.first.wait_for(state="visible", timeout=panel_seconds * 1000)
            except PwTimeout:
                report["not_found"].append(f"{label} — אין קובץ מצורף")
                continue
            body, name = _read_pdf(page, ctx, link.first, seen, 20_000)
            if t["quote_id"] and t["quote_id"] not in name:
                report["mismatch"].append(f"{label} — הקובץ {name} לא תואם להצעה {t['quote_id']}")
                continue
            files.append(dict(key=t["key"], quote_id=t["quote_id"], name=name or "quote.pdf", data=body))
            report["saved"] += 1
        except AutoFetchError as e:
            report["failed"].append(f"{label} — {e}")
        except (PwError, PwTimeout) as e:
            report["failed"].append(f"{label} — {str(e).splitlines()[0][:120]}")
        finally:
            try:
                if not _close_panel(page):
                    page.reload(wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
                    _load_whole_list(page)
            except (PwError, PwTimeout):
                pass
    return dict(files=files, report=report)


def fetch_labsuit(url, username, password, step=lambda s: None, download_seconds=60, headless=True,
                  attachments=None):
    """Returns (file bytes, suggested file name), or — with `attachments` ({natural_key: quote_id} of the
    PDFs already stored) — (file bytes, file name, {"files": [...], "report": {...}}), the PDFs scraped
    right after the export. Raises AutoFetchError with a Hebrew message."""
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
        downloads, pdfs = [], []
        ctx.on("page", lambda pg: pg.on("download", lambda d: downloads.append(d)))
        page.on("download", lambda d: downloads.append(d))

        def on_response(resp):
            try:
                ct = (resp.headers.get("content-type") or "").lower()
            except Exception:  # noqa: BLE001
                return
            if "application/pdf" in ct:
                pdfs.append(resp)
        ctx.on("response", on_response)

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
            fname = dl.suggested_filename or "labsuit-export.xlsx"
            if attachments is None:
                return data, fname
            # same session, still on the orders list: the PDF behind every request
            extra = scrape_attachments(page, ctx, data, attachments, step, settings.AUTOFETCH_PDF_SECONDS,
                                       dict(downloads=downloads, pdfs=pdfs),
                                       panel_seconds=settings.AUTOFETCH_PDF_PANEL_SECONDS,
                                       refetch=settings.AUTOFETCH_PDF_REFETCH)
            return data, fname, extra
        except AutoFetchError:
            raise
        except PwError as e:
            fail("הדפדפן האוטומטי נתקל בשגיאה מול LabSuit.", e)
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


_REAL_FETCH = fetch_labsuit       # the real robot (a stand-in passed in or patched must not get PDF arguments)


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
        robot = fetch or fetch_labsuit
        kwargs = {}
        if robot is _REAL_FETCH and settings.AUTOFETCH_PDFS and job.source == ImportBatch.Source.LABSUIT_ORDERS:
            kwargs["attachments"] = stored_attachments()          # the PDFs we already have
        res = robot(url, user, pw, step=step, download_seconds=settings.AUTOFETCH_DOWNLOAD_SECONDS, **kwargs)
        data, name = res[0], res[1]
        extra = res[2] if len(res) > 2 else {}
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
        files = extra.get("files") or []
        if files:
            PendingFile.objects.bulk_create([PendingFile(pending=pending, natural_key=f["key"], quote_id=f["quote_id"],
                                                         file_name=f["name"][:255], data=f["data"]) for f in files])
        if extra.get("report"):
            pending.preview["attachments"] = extra["report"]
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


def stored_attachments():
    """{natural_key: quote_id} for the orders that already have a stored PDF (no need to fetch them again)."""
    return {k: q for k, q in OrderFile.objects.values_list("order__natural_key", "quote_id")}


def attach_pending_files(pending):
    """On confirm: the scraped PDFs become the file of their orders. Returns how many were attached."""
    from .models import OrderRequest
    n = 0
    orders = {o.natural_key: o for o in OrderRequest.objects.filter(
        natural_key__in=list(pending.files.values_list("natural_key", flat=True)))}
    for pf in pending.files.all():
        o = orders.get(pf.natural_key)
        if o is None:
            continue
        data = bytes(pf.data)
        OrderFile.objects.update_or_create(order=o, defaults=dict(
            quote_id=pf.quote_id, file_name=pf.file_name, data=data, size=len(data), fetched_at=timezone.now()))
        n += 1
    return n


def start_job(job):
    if settings.AUTOFETCH_SYNC:
        run_job(job.pk)
    else:
        threading.Thread(target=run_job, args=(job.pk,), daemon=True, name=f"autofetch-{job.pk}").start()
