# Changelog

## Unreleased
- **The grants table is one line per grant.** The expandable row (grant settings, reservations, grant rules, decisions list) is gone, with the reservations and rules tables and their edit endpoints. Existing reservations, rules and the grant settings (priority, reserve flag, cutoff days, active) stay in the database and still apply in the recommendations; there is no screen to edit them for now.
- **"פרויקט" is a project text, and has no effect on funding.** The orders table (and the recommendations table) show one "פרויקט" column: free text the lab types (`OrderRequest.project_text`, lab-owned, kept across imports), defaulting to the request's GRANT_ID from LabSuit, with every grant of the grants table offered as a suggestion (it copies the grant number as text; any other text is fine). Removed: the project select column, the separate GRANT_ID column, the Settings → projects tab and the grant's "allowed projects" panel; the resolver no longer restricts or steers by project. The `Project`/`ProjectGrant` models and the old `project` field stay in the database, unused. Migration `0016`.
- **VAT is not divided for salary and services.** A grant that does not pay VAT is charged price ÷ 1.18, except for SALARY and SERVICE requests (`RESOLVE_DEFAULTS["vat_free_categories"]`): their price stays the price.
- **Grants table shows every budget category of the SAP file** (all 15 `*_BAL` columns), hiding only the categories no grant on the page has a line for. The per-category figures are what is left (budget minus expense and commitments), exactly what the recommendations use.
- **VAT per grant** (`Grant.vat_required`, default on; a "מע״מ" checkbox in the grants table; not touched by SAP imports). A grant that does not pay VAT is charged the price before VAT (price ÷ `RESOLVE_DEFAULTS["vat_rate"]` = 1.18): in the optimisation, in approvals (the stored `amount_grant_ccy` and `fx_rate`), in the explanation (a warning line), in the "unseen orders" deduction and in the page's live summary. Migration `0015`.
- **Recommendations page:** the requests table is read-only (only the approve checkbox and the grant choice remain); a visible "יישאר" column shows what will be left in the recommended grant (the category, or the pool), live when the grant is changed; the explanation, reason, comparison and eligible grants are only inside each row's expandable part; the summary and the approve button are below the requests table. A request without an amount links to the orders page to enter it.
- Home page renders inside the site layout again (navigation and messages), keeping the dashboard content.

## Unreleased
- **"עודכן במערכות המעבדה" now deletes the order for real.** The order row (and its stored quote PDF) is removed from the system; the funding decision stays on the grant as a reservation until a SAP report covers it. The decision keeps what it needs about the order (`order_label`, `order_key`, `order_po_date`; `Allocation.order` is now nullable, `SET_NULL`). A later import that lists the same request brings it back as a **new** request (no link to the old decision); its amount is not subtracted a second time as an "unseen" order. Orders marked automatically from LabSuit's GRANT_ID are deleted the same way, and the import preview lists them under "זוהו כרשומות" rather than "יוסתרו". Deleting a manual order by hand releases an unmarked decision. Migration `0014`.
- **PDF per request.** In the same session as the automatic export, the robot loads the whole orders list (it loads on scroll), clicks each request's name and takes the one PDF under "Files" in the side panel — LabSuit's supplier quote. Files wait with the preview (`PendingFile`) and are attached to the orders on confirm (`OrderFile`, shown as the "הצעת מחיר" column, served at `orders/<id>/file/`). The preview shows a report (saved / already stored / no file or not found / mismatch / failed). Requests that share a name are told apart by quote number, catalog number and requester; a file whose name does not contain the request's quote number is not saved. Stored files are skipped on later runs. Settings: `AUTOFETCH_PDFS`, `AUTOFETCH_PDF_SECONDS`, `AUTOFETCH_PDF_REFETCH`, `AUTOFETCH_PDF_PANEL_SECONDS`. Tested end to end against a mock LabSuit with a real Chromium (skipped when Chromium is not installed); not yet run against the real site.
- **No more estimated amounts.** The orders importer no longer fills a missing price from an earlier order of the same catalog number or from the inventory. An order's amount is LabSuit's own price (PRICE, else TOTAL ÷ QUANTITY, when > 1) or a typed-in amount; a request without one shows "חסר סכום" and gets no recommendation until an amount is entered. The "סכום משוער" warning on recommendations is gone. Migration `0012` clears amounts that were previously filled in this way (`history` / `inventory` sources); LabSuit and manual amounts are untouched, and the `HISTORY` / `INVENTORY` amount sources are removed.

## 0.7.0 — 2026-10-13
- **Recycle bin (סל מחזור):** approving recommendations moves those orders out of "הזמנות ודרישות" and into a recycle bin at the bottom of the recommendations page. Each entry shows the funding that was approved, the full reason for the recommendation, the ₪ value at approval (and today's value when the rate or amount has moved), and who/when. "♻ מחזור" drops the decision and sends the order back to the orders page as an open request; the next recommendation prices it at today's exchange rate and balances.
- **"עודכן במערכות המעבדה" removes the order from the bin and from the system's lists** (orders page, recommendations) for good — also after SAP later covers it. Its amount stays deducted from the grant (never returned) until SAP covers it, and is still listed in the grant's decisions. A confirmation guards the click. Orders whose LabSuit GRANT_ID already shows the decided grant are marked automatically at import, so they leave the bin too.
- Orders with a decision (approved or covered by SAP) are never recommended again.
- The orders page shows a banner with the number of orders waiting in the bin.
- **Every open request on the orders page is listed in the recommendations table.** Requests that get no recommendation (too old, already have a PO, on a standing order) are rows marked with the reason instead of sitting in collapsed lists below.
- **Splitting is gone.** Every request is funded in full from exactly one grant (the lab writes one GRANT_ID anyway): no "לאפשר פיצול" option, no "+ פיצול / מענק נוסף", no manual amounts, no `--split` in `run_resolve`, no LP-relaxation mode (`allow_split` is removed from `RESOLVE_DEFAULTS`). `approve_plans` now takes `{order_id: grant_number}`. A request no single grant covers is flagged "אין מקור מספיק" and gets no recommendation.
- **The funding-labels table is removed from the orders dashboard** (with its "needs review" badge), and with it the table's editing endpoint. GRANT_ID values are still classified automatically (grant number / standing order / open). The now-empty "מימון שאושר ב-LabFund" column is removed too: decided orders are not listed on the orders page.
- `resolve/cancel/<pk>/` (allocation_cancel) is replaced by `resolve/recycle/<pk>/` (allocation_recycle).

## 0.6.0 — 2026-10-13
- Scope narrowed to orders, grants and recommendations: the inventory and budget-vs-actual dashboards are removed (their models and CLI importers stay; old inventory prices still fill missing order amounts).
- The Merge preview warns about orders still "approved" in LabFund that are missing from the file (with an approved-only export: their status probably changed in LabSuit).
- "Resolve" is now "המלצות" everywhere in the UI (code names unchanged).
- Automatic update is orders-only, from `…/dashboard/?labId=3861#orders/status[]=approved` (approved orders only): the robot opens the link, logs in, opens the link again and reloads it (LabSuit lands on its home page after login), then Export → popup Export → download.
- Online updates are always Merge: nothing is hidden or deleted — neither rows missing from the export nor rows added by hand — and lab-edited fields are kept. Stated in the dialog and covered by a test.

## 0.5.0 — 2026-10-13
- **Automatic update from LabSuit** (inventory and orders): the file button becomes "עדכון ממקור אחר" → file / automatic. A headless Chromium (Playwright) opens the dashboard link, fills the 1st/2nd fields with the saved username/password, clicks "Login", "Export", the popup's "Export", waits up to a minute for the download, and opens the usual Merge preview. Elements are found by their text, not position. Credentials are stored once for both dashboards, password encrypted (Fernet keyed from SECRET_KEY) and never sent back to the browser. Runs as a background job with live progress; failures show a plain message, a manual-file fallback and a screenshot of the moment it stopped. Empty / wrong-type files are rejected before the preview.
- **Resolve: one grant per request by default** (binary assignment, i.e. a generalized assignment problem with category capacities) — the lab funds a request by writing a single GRANT_ID. "לאפשר פיצול" re-solves it as the LP relaxation. Requests no single grant can cover are flagged with a hint to split by hand. CLI: `run_resolve --split`.
- Decisions whose LabSuit GRANT_ID already shows the decided grant are marked "עודכן במערכות המעבדה" automatically on orders import (shown in the preview).
- Tabs reordered: inventory, orders, grants, analysis, Resolve.
- A rejected automatic download now says which export it actually is (orders vs inventory — e.g. swapped dashboard URLs), lists its sheets and columns, and can be downloaded from the error dialog.
- LabSuit dashboard links: inventory `…#search-substance/my-lab/simple`, orders `…#orders` (the robot returns to the link after the login redirect).
- Opening a preview that was already confirmed / cancelled (refresh, back, an old tab, double click) redirects with a message instead of a 404; preview buttons lock after the first click.
- A missing local Chromium now shows the one-line install command instead of a raw Playwright error.
- Deployment: Dockerfile on `mcr.microsoft.com/playwright/python:v1.56.0-noble`; `railway.json` builds from it.

## 0.4.0 — 2026-10-13
- **Import preview:** every file update runs as a dry run first (inside a rolled-back transaction) and opens a preview — new / changed (field-by-field, before → after) / hidden / reactivated / unchanged counts, LabSuit status moves, lab data that is preserved, funding decisions the file would reconcile, and warnings (unknown GRANT_ID labels, unmapped SAP columns or cost elements, decided orders missing from the file). Confirm, cancel, or switch Merge ↔ Replace without re-uploading. Uploads wait in `PendingImport` (DB, so it works across workers). CLI: `import_file … --preview`.
- **"עודכן במערכות המעבדה"** button on approved decisions in Resolve: records who updated LabSuit / SAP and when; toggleable. It does not release the reservation.
- **Conservative SAP reconciliation:** a decision is treated as covered by a SAP report only when the order's date (LabSuit PO date, else the lab-updated mark) is on or before the report date. A PO without a date no longer counts as covered. Status renamed "נקלט ב-SAP" → "כלול ביתרת SAP".
- Imports rewrite only rows that actually changed (orders preview/import ~21 s → ~3 s on the real export).

## 0.3.0 — 2026-10-13
- **Resolve:** splitting across grants is the default; every recommendation is editable (grant, amount, extra split lines); live allocation summary with per-category checks; "↻ refresh recommendation" resets to the optimum.
- Approval is all-or-nothing, revalidated in a locked transaction, and is reflected in every dashboard (pending, available, per-category "after decisions", orders column, analysis).
- Grant ▸ panel lists the funding decisions taken from the grant, by category.
- Design pass: info tooltips instead of explanatory text, narrower tables (settings moved into ▸), cells that read as text, reduced palette with status dots, slim stats bar.
- All 14 Bank of Israel currencies tracked from the first migrate; FX precision raised to 10 decimal places (LBP).
- SAP "budget vs actual": cross-currency total rows (always 0 in the export) are skipped.
- Version shown in the footer; README rewritten with screenshots.

## 0.2.0
- Custom editable dashboards (inventory, grants, orders, analysis) replace Django admin entirely; file update with Merge / Replace on each dashboard, add-row where relevant.
- Resolve computed automatically on entry for approved, not-yet-ordered requests (read-only until approval).
- Automatic exchange rates on page load (Bank of Israel, ExchangeRate-API fallback); add a currency by 3-letter code.
- Settings tab: exchange rates, projects, users. Default admin user created on migrate with a reminder to change the password.
- SAP reconciliation on import; orders placed after the SAP snapshot reduce availability.

## 0.1.0
- Data model, importers for LabSuit (orders, inventory) and SAP (TRDF balances, budget vs actual), LP-based Resolve from the command line with backtest.
