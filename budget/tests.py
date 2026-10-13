from datetime import date
from decimal import Decimal

from unittest import mock

from django.test import TestCase, override_settings

from budget import resolve
from budget.importers.common import money, parse_date
from budget.models import (Allocation, FundingAlias, FxRate, Grant, GrantLine, GrantRule, ImportBatch,
                           OrderRequest, Project, ProjectGrant, ResolveRun, TrackedCurrency)

TODAY = date(2026, 10, 12)


class ParsingTests(TestCase):
    def test_money_formats_seen_in_labsuit(self):
        self.assertEqual(money("ILS 629.24"), (Decimal("629.24"), "ILS"))
        self.assertEqual(money("0.00ILS"), (Decimal("0.00"), "ILS"))
        self.assertEqual(money("1720.00USD"), (Decimal("1720.00"), "USD"))
        self.assertEqual(money("$ 12"), (Decimal("12.00"), "USD"))
        self.assertEqual(money(None), (None, None))

    def test_dates(self):
        self.assertEqual(parse_date("08-10-2026"), date(2026, 10, 8))
        self.assertEqual(parse_date("31/12/2026"), date(2026, 12, 31))
        self.assertIsNone(parse_date("nan"))


class ResolveTests(TestCase):
    """Synthetic lab: one grant about to expire, one long-lived reserve, one in USD."""

    def setUp(self):
        FxRate.objects.create(date=TODAY, currency="USD", rate_to_ils=Decimal("3.078"))
        FxRate.objects.create(date=TODAY, currency="EUR", rate_to_ils=Decimal("3.4422"))
        self.batch = ImportBatch.objects.create(source="sap_grants", file_name="t", as_of=TODAY)

        def grant(num, ccy, end, total, lines, **kw):
            g = Grant.objects.create(number=num, currency=ccy, end_date=end, balance_total=Decimal(total),
                                     last_batch=self.batch, **kw)
            for cat, bal in lines.items():
                GrantLine.objects.create(batch=self.batch, grant=g, category_id=cat, balance=Decimal(bal))
            return g

        self.soon = grant("A", "EUR", date(2026, 12, 31), "1000", {"MATERIAL": "1000", "SALARY": "-500"})
        self.reserve = grant("R", "USD", date(2029, 12, 31), "100000", {"GENERAL": "100000"}, is_reserve=True)
        self.later = grant("L", "ILS", date(2027, 9, 30), "50000", {"MATERIAL": "50000"})
        self.n = 0

    def order(self, amount, ccy="ILS", label="General Budget", category="MATERIAL", **kw):
        self.n += 1
        return OrderRequest.objects.create(
            natural_key=f"k{self.n}", item_name=f"item {self.n}", status="approved", grant_label=label,
            amount_est=Decimal(amount), amount_currency=ccy, category_id=category,
            amount_source="manual", date_requested=TODAY, **kw)

    def plan_of(self, result, order):
        return next(p for p in result.plans if p.cand.order.pk == order.pk)

    def grants_of(self, result, order):
        return {l.grant.number for l in self.plan_of(result, order).lines if l.grant}

    def test_one_grant_per_request_by_default(self):
        o1, o2 = self.order("1000"), self.order("5000")   # A (≈1,000 EUR) can't hold both
        r = resolve.compute(today=TODAY)
        for o in (o1, o2):
            self.assertEqual(len(self.plan_of(r, o).lines), 1)                  # never split
        self.assertEqual(self.grants_of(r, o1), {"A"})                         # small one → expiring grant
        self.assertEqual(self.grants_of(r, o2), {"L"})

    def test_too_big_for_any_single_grant_is_flagged_unless_split_allowed(self):
        GrantRule.objects.create(grant=self.reserve, category_id="MATERIAL", allowed=False)
        o = self.order("52000")       # L holds 50,000; A ≈ 3,300 ₪ — only a split covers it
        self.assertEqual(self.plan_of(resolve.compute(today=TODAY), o).state, "unfunded")
        plan = self.plan_of(resolve.compute(today=TODAY, allow_split=True), o)
        self.assertEqual((plan.state, self.grants_of(resolve.compute(today=TODAY, allow_split=True), o)),
                         ("split", {"A", "L"}))

    def test_expiring_grant_first_then_spill_over(self):
        self.order("1000")
        self.order("5000")            # A cannot hold everything → split / spill to L
        r = resolve.compute(today=TODAY, allow_split=True)
        lines = [l for p in r.plans for l in p.lines]
        self.assertTrue(all(l.grant for l in lines), "everything funded")
        used_a = sum(l.amount_grant for l in lines if l.grant.number == "A")
        self.assertAlmostEqual(float(used_a), 1000, delta=1)          # expiring grant used to the end
        self.assertTrue(any(l.grant.number == "L" for l in lines))
        self.assertFalse(any(l.grant.number == "R" for l in lines), "reserve should be the last resort")

    def test_compute_writes_nothing(self):
        self.order("1000")
        counts = lambda: (Allocation.objects.count(), ResolveRun.objects.count())
        before = counts()
        resolve.compute(today=TODAY)
        resolve.compute(today=TODAY)
        self.assertEqual(counts(), before)

    def test_cutoff_excludes_grant_ending_soon(self):
        self.soon.order_cutoff_days = 120
        self.soon.save()
        o = self.order("100")
        r = resolve.compute(today=TODAY)
        self.assertEqual(self.grants_of(r, o), {"L"})
        self.assertIn("A", self.plan_of(r, o).why_not)

    def test_category_rule_and_project_link(self):
        GrantRule.objects.create(grant=self.later, category_id="EQUIPMENT", allowed=False)
        p = Project.objects.create(name="P1")
        ProjectGrant.objects.create(project=p, grant=self.reserve)
        o = self.order("2000", category="EQUIPMENT", project=p)
        r = resolve.compute(today=TODAY)
        self.assertEqual(self.grants_of(r, o), {"R"})   # pooled grant, linked to project

    def test_unfunded_when_nothing_fits(self):
        o = self.order("10000000")
        r = resolve.compute(today=TODAY)
        self.assertEqual(self.plan_of(r, o).state, "unfunded")

    def test_in_sap_orders_are_not_double_counted(self):
        o = self.order("100", po_number="5400000001", date_ordered=date(2026, 10, 1))
        r = resolve.compute(today=TODAY)
        self.assertEqual(r.plans, [])
        self.assertEqual([x[0] for x in r.skipped], [o])

    # ---- approval

    def test_approve_recommendation_then_reconcile_on_import(self):
        o = self.order("2000")
        r = resolve.compute(today=TODAY)
        decision = {o.pk: [(l.grant.number, l.amount_orig) for l in self.plan_of(r, o).lines]}
        resolve.approve_plans(decision, user="u", today=TODAY)
        used = sum(a.amount_grant_ccy for a in Allocation.objects.filter(status="approved", grant_id="A"))
        self.assertEqual(resolve.grant_states(TODAY, TODAY)["A"].total, Decimal("1000") - used)
        self.assertEqual(resolve.compute(today=TODAY).plans, [])          # no longer awaiting a decision
        # opening the page again changes nothing
        resolve.compute(today=TODAY)
        self.assertTrue(Allocation.objects.filter(status="approved").exists())
        # SAP now shows the order → reconciled at import time, not on page load
        o.po_number, o.date_ordered = "5400000002", TODAY
        o.save()
        self.assertEqual(resolve.reconcile(TODAY), Allocation.objects.filter(order=o).count())
        self.assertFalse(Allocation.objects.filter(status="approved").exists())

    def test_manual_split_is_approved(self):
        o = self.order("1000")
        resolve.approve_plans({o.pk: [("L", Decimal("600")), ("A", Decimal("400"))]}, today=TODAY)
        got = {a.grant_id: a.amount_orig for a in Allocation.objects.filter(order=o)}
        self.assertEqual(got, {"L": Decimal("600"), "A": Decimal("400")})

    def test_ineligible_or_wrong_total_is_rejected_and_nothing_saved(self):
        o = self.order("1000")
        GrantRule.objects.create(grant=self.later, category_id="MATERIAL", allowed=False)
        with self.assertRaises(resolve.ApprovalError) as e:
            resolve.approve_plans({o.pk: [("L", Decimal("1000"))]}, today=TODAY)
        self.assertIn("אינו כשיר", " ".join(e.exception.errors))
        with self.assertRaises(resolve.ApprovalError):
            resolve.approve_plans({o.pk: [("A", Decimal("10"))]}, today=TODAY)     # sum ≠ order amount
        self.assertEqual(Allocation.objects.count(), 0)

    def test_overdraw_rejected_even_across_requests(self):
        o1, o2 = self.order("3000"), self.order("3000")      # each ≈ 898 EUR in A, together > 1000
        with self.assertRaises(resolve.ApprovalError) as e:
            resolve.approve_plans({o1.pk: [("A", Decimal("3000"))], o2.pk: [("A", Decimal("3000"))]}, today=TODAY)
        self.assertIn("חריגה", " ".join(e.exception.errors))
        self.assertEqual(Allocation.objects.count(), 0)
        # sequential approvals: the second one sees the first and is refused
        resolve.approve_plans({o1.pk: [("A", Decimal("3000"))]}, today=TODAY)
        with self.assertRaises(resolve.ApprovalError):
            resolve.approve_plans({o2.pk: [("A", Decimal("3000"))]}, today=TODAY)

    def test_po_without_date_stays_reserved(self):
        o = self.order("2000")
        resolve.approve_plans({o.pk: [("L", Decimal("2000"))]}, today=TODAY)
        o.po_number = "5400000003"           # LabSuit shows a PO but no order date
        o.save()
        self.assertEqual(resolve.reconcile(TODAY), 0)
        self.assertEqual(resolve.grant_states(TODAY, TODAY)["L"].total, Decimal("48000"))

    def test_lab_updated_mark_dates_the_order_for_reconciliation(self):
        from datetime import datetime, timezone as tz
        o = self.order("2000")
        resolve.approve_plans({o.pk: [("L", Decimal("2000"))]}, today=TODAY)
        Allocation.objects.filter(order=o).update(lab_updated_at=datetime(2026, 10, 13, 9, tzinfo=tz.utc))
        self.assertEqual(resolve.reconcile(TODAY), 0)                       # SAP report older than the mark
        self.assertEqual(resolve.grant_states(TODAY, TODAY)["L"].total, Decimal("48000"))
        self.assertEqual(resolve.reconcile(date(2026, 10, 20)), 1)          # next report covers it
        self.assertEqual(Allocation.objects.get(order=o).status, "superseded")

    def test_grant_written_in_labsuit_marks_decision(self):
        o = self.order("2000")
        resolve.approve_plans({o.pk: [("L", Decimal("2000"))]}, today=TODAY)
        self.assertEqual(resolve.mark_written_in_labsuit(), 0)          # LabSuit still says General Budget
        o.grant_label = "A"                                              # wrong grant → not marked
        o.save()
        self.assertEqual(resolve.mark_written_in_labsuit(), 0)
        o.grant_label = "L"
        o.save()
        FundingAlias.objects.create(label="L", kind="grant", grant=self.later)
        self.assertEqual(resolve.mark_written_in_labsuit(), 1)
        a = Allocation.objects.get(order=o)
        self.assertEqual((a.status, a.lab_updated_by), ("approved", "LabSuit (GRANT_ID)"))   # still reserved

    def test_lab_updated_button_toggles_and_keeps_reservation(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        o = self.order("2000")
        resolve.approve_plans({o.pk: [("L", Decimal("1500")), ("A", Decimal("500"))]}, today=TODAY)
        a = Allocation.objects.filter(order=o).first()
        self.client.post(f"/resolve/lab-updated/{a.pk}/")
        lines = Allocation.objects.filter(order=o)
        self.assertTrue(all(x.lab_updated_at and x.lab_updated_by == "u" for x in lines))   # whole order
        self.assertTrue(all(x.status == "approved" for x in lines))       # marked today > SAP 12/10: still reserved
        self.assertContains(self.client.get("/resolve/"), "עודכן ·")
        self.client.post(f"/resolve/lab-updated/{a.pk}/")
        self.assertFalse(Allocation.objects.filter(order=o, lab_updated_at__isnull=False).exists())

    def test_double_approval_refused(self):
        o = self.order("100")
        resolve.approve_plans({o.pk: [("L", Decimal("100"))]}, today=TODAY)
        with self.assertRaises(resolve.ApprovalError):
            resolve.approve_plans({o.pk: [("L", Decimal("100"))]}, today=TODAY)


@override_settings(FX_AUTO_REFRESH=False)
class DashboardTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        FxRate.objects.create(date=TODAY, currency="EUR", rate_to_ils=Decimal("3.4422"))
        self.batch = ImportBatch.objects.create(source="sap_grants", file_name="t", as_of=TODAY)
        self.g = Grant.objects.create(number="A", currency="EUR", end_date=date(2027, 1, 1),
                                      balance_total=Decimal("1000"), last_batch=self.batch)

    def post_json(self, url, payload):
        import json
        return self.client.post(url, json.dumps(payload), content_type="application/json")

    def test_pages_render_without_admin(self):
        for url in ["/", "/orders/", "/resolve/", "/settings/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        for url in ["/admin/", "/inventory/", "/analysis/"]:                  # removed dashboards
            self.assertEqual(self.client.get(url).status_code, 404, url)
        nav = self.client.get("/orders/").content.decode()
        self.assertIn(">המלצות</a>", nav)
        self.assertLess(nav.index(">הזמנות ודרישות</a>"), nav.index(">מענקים</a>"))

    def test_inline_edit_cell_and_grant_line(self):
        r = self.post_json("/api/grants/A/", {"field": "pi_priority", "value": "2"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Grant.objects.get(pk="A").pi_priority, 2)
        self.post_json("/api/grants/A/", {"field": "line:MATERIAL", "value": "1,500"})
        self.assertEqual(GrantLine.objects.get(grant_id="A", category_id="MATERIAL").balance, Decimal("1500"))
        bad = self.post_json("/api/grants/A/", {"field": "number", "value": "B"})
        self.assertEqual(bad.status_code, 400)

    def test_add_row_and_manual_amount_feeds_resolve(self):
        self.post_json("/api/grants/A/", {"field": "line:MATERIAL", "value": "1000"})
        self.client.post("/api/orders/new/", {"item_name": "x", "amount_est": "500", "amount_currency": "ILS",
                                              "category": "MATERIAL", "grant_label": "General Budget"})
        o = OrderRequest.objects.get(item_name="x")
        self.assertEqual(o.amount_source, "manual")
        page = self.client.get("/resolve/")
        self.assertContains(page, "x")
        self.assertEqual(page.context["rows"][0]["lines"][0].grant.number, "A")
        # approving through the page posts the (possibly edited) lines
        r = self.client.post("/resolve/approve/", {"order": [o.pk], f"g_{o.pk}": ["A"], f"a_{o.pk}": ["500.00"]})
        self.assertEqual(Allocation.objects.filter(order=o, status="approved").count(), 1)
        # the approved order leaves the orders page and waits in the recycle bin on the recommendations page
        orders_page = self.client.get("/orders/?year=all")
        self.assertNotIn(o, [r["obj"] for r in orders_page.context["t"]["rows"]])
        self.assertContains(orders_page, "סל המחזור")
        bin_ = self.client.get("/resolve/").context["bin"]
        self.assertEqual([g["order"] for g in bin_], [o])
        self.assertTrue(bin_[0]["lines"][0].reason)                    # the recommendation's reason is kept

    def _approved_order(self, amount="500", currency="ILS"):
        self.post_json("/api/grants/A/", {"field": "line:MATERIAL", "value": "1000"})
        o = OrderRequest.objects.create(natural_key="bin-1", item_name="bin item", status="approved",
                                        amount_est=Decimal(amount), amount_currency=currency, category_id="MATERIAL",
                                        date_requested=timezone_today())
        self.client.post("/resolve/approve/", {"order": [o.pk], f"g_{o.pk}": ["A"], f"a_{o.pk}": [amount]})
        self.assertEqual(Allocation.objects.filter(order=o, status="approved").count(), 1)
        return o

    def test_recycle_returns_order_to_orders_page_and_recommendations(self):
        o = self._approved_order()
        a = Allocation.objects.get(order=o)
        self.assertNotIn(o.pk, [r["pk"] for r in self.client.get("/orders/?year=all").context["t"]["rows"]])
        self.assertNotIn(o.pk, [r["order"].pk for r in self.client.get("/resolve/").context["rows"]])
        r = self.client.post(f"/resolve/recycle/{a.pk}/")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Allocation.objects.filter(order=o, status="approved").exists())
        self.assertIn(o.pk, [r["pk"] for r in self.client.get("/orders/?year=all").context["t"]["rows"]])
        page = self.client.get("/resolve/")
        self.assertEqual(page.context["bin"], [])
        row = next(r for r in page.context["rows"] if r["order"].pk == o.pk)
        self.assertEqual(row["lines"][0].grant.number, "A")            # recommended again, from today's balances

    def test_recycled_order_is_priced_at_todays_rate(self):
        o = self._approved_order(amount="100", currency="EUR")
        a = Allocation.objects.get(order=o)
        self.assertEqual(a.amount_ils, Decimal("344.22"))
        FxRate.objects.update_or_create(date=TODAY, currency="EUR", defaults={"rate_to_ils": Decimal("4.0000")})
        g = self.client.get("/resolve/").context["bin"][0]
        self.assertEqual((g["then"], g["now"], g["drift"]), (Decimal("344.22"), Decimal("400.0000"), True))
        self.client.post(f"/resolve/recycle/{a.pk}/")
        page = self.client.get("/resolve/")
        row = next(r for r in page.context["rows"] if r["order"].pk == o.pk)
        self.assertEqual(row["lines"][0].amount_ils, Decimal("400.00"))   # frozen rate dropped, repriced
        self.assertEqual(Allocation.objects.get(pk=a.pk).status, "rejected")

    def test_every_open_order_is_listed_with_a_marking(self):
        self.post_json("/api/grants/A/", {"field": "line:MATERIAL", "value": "1000"})
        kw = dict(status="approved", amount_est=Decimal("10"), amount_currency="ILS", category_id="MATERIAL")
        ok = OrderRequest.objects.create(natural_key="k-ok", item_name="ok", date_requested=timezone_today(), **kw)
        old = OrderRequest.objects.create(natural_key="k-old", item_name="old", date_requested=date(2020, 1, 1), **kw)
        po = OrderRequest.objects.create(natural_key="k-po", item_name="has po", po_number="5400000001",
                                         date_ordered=date(2026, 10, 1), date_requested=timezone_today(), **kw)
        st = OrderRequest.objects.create(natural_key="k-st", item_name="standing", grant_label="Standing X",
                                         date_requested=timezone_today(), **kw)
        FundingAlias.objects.create(label="Standing X", kind="standing")
        page = self.client.get("/resolve/")
        states = {r["order"].pk: r["state"] for r in page.context["rows"]}
        self.assertEqual(states[ok.pk], "ok")
        self.assertEqual((states[old.pk], states[po.pk], states[st.pk]), ("skipped", "skipped", "standing"))
        self.assertContains(page, "לא בחישוב")
        # nothing from the orders page is missing from the recommendations
        on_orders_page = {r["pk"] for r in self.client.get("/orders/?year=all").context["t"]["rows"]}
        self.assertTrue(on_orders_page <= set(states))


    def test_project_select_and_dash_for_empty(self):
        p = Project.objects.create(name="Melanoma")
        self.client.post("/api/orders/new/", {"item_name": "y", "amount_est": "10", "amount_currency": "ILS"})
        o = OrderRequest.objects.get(item_name="y")
        self.assertEqual(self.post_json(f"/api/orders/{o.pk}/", {"field": "project", "value": str(p.pk)}).status_code, 200)
        self.assertEqual(OrderRequest.objects.get(pk=o.pk).project, p)
        self.post_json(f"/api/orders/{o.pk}/", {"field": "project", "value": ""})
        self.assertIsNone(OrderRequest.objects.get(pk=o.pk).project)
        page = self.client.get("/orders/")
        self.assertContains(page, '<option value="" selected>-</option>', html=False)

    def test_unknown_currency_rejected_until_tracked(self):
        o = OrderRequest.objects.create(natural_key="k", item_name="z")
        bad = self.post_json(f"/api/orders/{o.pk}/", {"field": "amount_currency", "value": "SGD"})
        self.assertEqual(bad.status_code, 400)
        TrackedCurrency.objects.create(code="SGD")
        ok = self.post_json(f"/api/orders/{o.pk}/", {"field": "amount_currency", "value": "sgd"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(OrderRequest.objects.get(pk=o.pk).amount_currency, "SGD")

    def test_users_admin_only_and_no_self_lockout(self):
        from django.contrib.auth.models import User
        me = User.objects.get(username="u")
        r = self.client.post("/api/users/new/", {"username": "v", "password": "pw12"})
        self.assertFalse(User.objects.filter(username="v").exists())      # not an admin
        me.is_superuser = me.is_staff = True
        me.save()
        self.client.post("/api/users/new/", {"username": "v", "password": "pw12", "first_name": "Vi"})
        v = User.objects.get(username="v")
        self.assertFalse(v.is_superuser)
        self.assertTrue(v.check_password("pw12"))
        r = self.post_json(f"/api/users/{me.pk}/", {"field": "is_superuser", "value": False})
        self.assertEqual(r.status_code, 400)


BOI = {"exchangeRates": [{"key": "USD", "currentExchangeRate": 3.078, "unit": 1, "lastUpdate": "2026-10-12T12:00:00Z"},
                         {"key": "JPY", "currentExchangeRate": 1.9407, "unit": 100, "lastUpdate": "2026-10-12T12:00:00Z"}]}
ERAPI = {"result": "success", "time_last_update_unix": 1791763200, "rates": {"ILS": 1, "SGD": 0.417678, "USD": 0.3253}}


def fake_get(url):
    return BOI if "boi.org.il" in url else ERAPI


@override_settings(FX_AUTO_REFRESH=True, FX_REFRESH_MINUTES=60)
class FxTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        TrackedCurrency.objects.exclude(code__in=["USD", "JPY"]).delete()

    @mock.patch("budget.fx._get", side_effect=fake_get)
    def test_boi_first_then_fallback_and_unit(self, _):
        from budget import fx
        fx.add_currency("sgd", "דולר סינגפורי")
        fx.fetch_all()
        self.assertEqual(fx.rate_to_ils("USD")[0], Decimal("3.078"))
        self.assertEqual(fx.rate_to_ils("JPY")[0], Decimal("0.019407"))          # quoted per 100
        self.assertAlmostEqual(float(fx.rate_to_ils("SGD")[0]), 1 / 0.417678, places=4)
        self.assertEqual(TrackedCurrency.objects.get(code="SGD").source, "erapi")
        self.assertEqual(TrackedCurrency.objects.get(code="USD").source, "boi")

    @mock.patch("budget.fx._get", side_effect=fake_get)
    def test_unknown_code_is_rejected(self, _):
        from django.core.exceptions import ValidationError
        from budget import fx
        with self.assertRaises(ValidationError):
            fx.add_currency("XXQ")
        self.assertFalse(TrackedCurrency.objects.filter(code="XXQ").exists())

    def test_page_load_refreshes_and_backs_off_on_failure(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        with mock.patch("budget.fx._get", side_effect=fake_get) as g:
            self.client.get("/settings/")
            self.assertTrue(g.called)
            self.assertTrue(FxRate.objects.filter(currency="USD").exists())
            g.reset_mock()
            self.client.get("/settings/")                       # fresh → no second call
            self.assertFalse(g.called)
        TrackedCurrency.objects.update(last_fetched=None)
        with mock.patch("budget.fx._get", side_effect=OSError("offline")) as g:
            self.assertEqual(self.client.get("/settings/").status_code, 200)   # page still loads
            n = g.call_count
            self.client.get("/settings/")
            self.assertEqual(g.call_count, n)                    # backed off, no retry storm

    @mock.patch("budget.fx._get", side_effect=fake_get)
    def test_manual_rate_wins_for_that_day(self, _):
        from budget import fx
        FxRate.objects.create(date=date(2026, 10, 12), currency="USD", rate_to_ils=Decimal("3.5"), source="manual")
        fx.fetch_all()
        self.assertEqual(FxRate.objects.get(date=date(2026, 10, 12), currency="USD").rate_to_ils, Decimal("3.5"))


@override_settings(FX_AUTO_REFRESH=False)
class DefaultAdminTests(TestCase):
    def test_default_admin_exists_after_migrate_and_reminds_to_change(self):
        from django.contrib.auth.models import User
        admin = User.objects.get(username="admin")
        self.assertTrue(admin.is_superuser and admin.check_password("admin"))
        self.client.post("/login/", {"username": "admin", "password": "admin"})
        self.assertContains(self.client.get("/settings/?tab=users"), "סיסמת ברירת המחדל")
        self.client.post("/settings/password/", {"user": admin.pk, "password": "new-pass"})
        self.assertNotContains(self.client.get("/settings/?tab=users"), "סיסמת ברירת המחדל")

    def test_not_recreated_when_users_exist(self):
        from django.contrib.auth.models import User
        from budget.apps import create_default_admin
        User.objects.filter(username="admin").delete()
        User.objects.create_user("someone", password="x")
        create_default_admin(sender=None)
        self.assertFalse(User.objects.filter(username="admin").exists())


@override_settings(FX_AUTO_REFRESH=False)
class SkippedReasonTests(TestCase):
    def test_approved_with_old_po_is_listed_with_reason(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        b = ImportBatch.objects.create(source="sap_grants", file_name="t", as_of=TODAY)
        Grant.objects.create(number="A", currency="ILS", end_date=date(2027, 1, 1), balance_total=1000, last_batch=b)
        OrderRequest.objects.create(natural_key="k1", item_name="has PO", status="approved", amount_est=10,
                                    po_number="5400089734", date_ordered=date(2026, 10, 5),
                                    date_requested=timezone_today())
        OrderRequest.objects.create(natural_key="k2", item_name="too old", status="approved", amount_est=10,
                                    date_requested=date(2020, 1, 1))
        page = self.client.get("/resolve/")
        reasons = {o.item_name: why for o, why in page.context["result"].skipped}
        self.assertIn("5400089734", reasons["has PO"])
        self.assertIn("180", reasons["too old"])


def timezone_today():
    from django.utils import timezone
    return timezone.localdate()


@override_settings(FX_AUTO_REFRESH=False)
class UnseenOrdersTests(TestCase):
    def test_order_placed_after_sap_snapshot_reduces_availability(self):
        from budget import resolve
        b = ImportBatch.objects.create(source="sap_grants", file_name="t", as_of=date(2026, 10, 1))
        g = Grant.objects.create(number="2032917", currency="ILS", end_date=date(2027, 1, 1),
                                 balance_total=Decimal("1000"), last_batch=b)
        GrantLine.objects.create(batch=b, grant=g, category_id="MATERIAL", balance=Decimal("1000"))
        common = dict(status="ordered", grant_label="2032917", category_id="MATERIAL", amount_est=Decimal("300"))
        OrderRequest.objects.create(natural_key="before", item_name="a", po_number="1", date_ordered=date(2026, 9, 30), **common)
        OrderRequest.objects.create(natural_key="after", item_name="b", po_number="2", date_ordered=date(2026, 10, 5), **common)
        s = resolve.grant_states(TODAY, date(2026, 10, 1))["2032917"]
        self.assertEqual(s.total, Decimal("700"))          # only the order after the snapshot
        self.assertEqual(s.cats["MATERIAL"], Decimal("700"))


@override_settings(FX_AUTO_REFRESH=False)
class GuardTests(TestCase):
    def test_no_future_rate_for_past_date(self):
        from budget.fx import MissingRate, rate_to_ils
        FxRate.objects.create(date=date(2026, 10, 12), currency="USD", rate_to_ils=Decimal("3.078"))
        with self.assertRaises(MissingRate):
            rate_to_ils("USD", date(2026, 10, 1))
        FxRate.objects.create(date=date(2026, 9, 30), currency="USD", rate_to_ils=Decimal("3.70"))
        self.assertEqual(rate_to_ils("USD", date(2026, 10, 1))[0], Decimal("3.70"))

    def test_older_sap_file_needs_confirmation(self):
        import io
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        ImportBatch.objects.create(source="sap_grants", file_name="new", as_of=date(2026, 10, 12))
        tsv = "GRANT\tgrant_currency\tGrant Balance\tfund_end_dt\n123\tILS\t100\t31/12/2027\n".encode("utf-16")
        f = io.BytesIO(tsv); f.name = "old.XLS"
        self.client.post("/api/grants/import/", {"file": f, "as_of": "2026-09-01", "mode": "merge"})
        self.assertFalse(Grant.objects.filter(number="123").exists())
        f = io.BytesIO(tsv); f.name = "old.XLS"
        r = self.client.post("/api/grants/import/", {"file": f, "as_of": "2026-09-01", "mode": "merge", "confirm_old": "1"})
        self.assertFalse(Grant.objects.filter(number="123").exists())        # preview first
        self.client.post(r.url + "confirm/")
        self.assertTrue(Grant.objects.filter(number="123").exists())


@override_settings(FX_AUTO_REFRESH=False)
class ImportPreviewTests(TestCase):
    TSV = ("GRANT\tgrant_currency\tGrant Balance\tfund_end_dt\tsponsor_name\n"
           "A1\tILS\t100\t31/12/2027\tFund A\nB2\tILS\t200\t31/12/2027\tFund B\n")

    def setUp(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")

    def upload(self, text, mode="merge"):
        import io
        f = io.BytesIO(text.encode("utf-16")); f.name = "grants.XLS"
        return self.client.post("/api/grants/import/", {"file": f, "as_of": "2026-10-12", "mode": mode, "next": "/"})

    def test_preview_writes_nothing_then_confirm_applies(self):
        from budget.models import PendingImport
        r = self.upload(self.TSV)
        self.assertEqual(Grant.objects.count(), 0)
        self.assertEqual(ImportBatch.objects.count(), 0)
        page = self.client.get(r.url)
        self.assertContains(page, "תצוגה מקדימה")
        self.assertEqual(PendingImport.objects.get().preview["counts"]["new"], 2)
        self.client.post(r.url + "confirm/")
        self.assertEqual(Grant.objects.count(), 2)
        self.assertFalse(PendingImport.objects.exists())

    def test_changes_hidden_and_preserved_are_reported(self):
        from budget.models import PendingImport
        self.client.post(self.upload(self.TSV).url + "confirm/")
        Grant.objects.filter(number="A1").update(pi_priority=2)
        changed = self.TSV.replace("A1\tILS\t100", "A1\tILS\t150").replace(
            "B2\tILS\t200\t31/12/2027\tFund B\n", "C3\tILS\t50\t31/12/2027\tFund C\n")
        r = self.upload(changed, mode="replace")
        v = PendingImport.objects.get().preview
        self.assertEqual((v["counts"]["new"], v["counts"]["changed"], v["counts"]["hidden"]), (1, 1, 1))
        self.assertIn(["מענקים עם עדיפות שהוגדרה", 1], [list(x) for x in v["preserved"]])
        self.assertTrue(Grant.objects.get(number="B2").is_active)              # still nothing written
        self.client.post(r.url + "mode/")                                       # same file as Merge
        self.assertEqual(PendingImport.objects.get().preview["counts"]["hidden"], 0)
        self.client.post(r.url + "cancel/")
        self.assertFalse(PendingImport.objects.exists())
        self.assertEqual(Grant.objects.get(number="A1").balance_total, Decimal("100"))


@override_settings(FX_AUTO_REFRESH=False)
class GrantCategoryViewTests(TestCase):
    def test_grants_page_shows_per_category_after_decisions_and_decision_list(self):
        from django.contrib.auth.models import User
        from budget import resolve
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        b = ImportBatch.objects.create(source="sap_grants", file_name="t", as_of=TODAY)
        g = Grant.objects.create(number="A", currency="ILS", end_date=date(2027, 6, 1), balance_total=5000, last_batch=b)
        GrantLine.objects.create(batch=b, grant=g, category_id="MATERIAL", balance=Decimal("4000"))
        o = OrderRequest.objects.create(natural_key="k", item_name="Antibody X", status="approved", amount_est=1500,
                                        category_id="MATERIAL", date_requested=timezone_today())
        resolve.approve_plans({o.pk: [("A", Decimal("1500"))]})
        page = self.client.get("/")
        self.assertContains(page, "אחרי החלטות: 2,500")
        self.assertContains(page, "Antibody X")
        self.assertContains(page, "ממתין ל-SAP")


@override_settings(FX_AUTO_REFRESH=False, AUTOFETCH_SYNC=True)
class AutoFetchTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")

    @staticmethod
    def orders_xlsx(rows=None):
        import io
        import pandas as pd
        rows = rows or [("Pipette 10ml", "student1@lab.example", "approved"), ("DMEM", "student2@lab.example", "ordered")]
        buf = io.BytesIO()
        pd.DataFrame({"ITEM_NAME": [r[0] for r in rows], "REQUESTED_BY": [r[1] for r in rows],
                      "STATUS": [r[2] for r in rows], "DATE_REQUESTED": ["10-10-2026"] * len(rows),
                      "GRANT_ID": ["General Budget"] * len(rows), "TOTAL": ["ILS 100"] * len(rows)}).to_excel(
            buf, sheet_name="Year - 2026", index=False)
        return buf.getvalue()

    def test_credentials_are_encrypted_and_required(self):
        from budget.models import SourceCredential
        r = self.client.post("/api/orders/autofetch/", {})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json()["need_credentials"])
        with mock.patch("budget.autofetch.fetch_labsuit", return_value=(self.orders_xlsx(), "orders.xlsx")):
            self.client.post("/api/orders/autofetch/", {"username": "lab@x.org", "password": "s3cret!"})
        c = SourceCredential.objects.get(source="labsuit")
        self.assertNotIn("s3cret!", c.password_enc)
        self.assertEqual(c.get_password(), "s3cret!")
        self.assertContains(self.client.get("/orders/"), "lab@x.org")      # shown in the dialog…
        self.assertNotContains(self.client.get("/orders/"), "s3cret!")     # …the password never is

    def test_robot_file_opens_merge_preview_and_nothing_is_written(self):
        from budget.models import AutoFetchJob, PendingImport
        seen = {}

        def fake(url, user, pw, step, download_seconds):
            seen.update(url=url, user=user, pw=pw)
            step("לוחץ Export")
            return self.orders_xlsx(), "orders.xlsx"
        with mock.patch("budget.autofetch.fetch_labsuit", side_effect=fake):
            j = self.client.post("/api/orders/autofetch/",
                                 {"username": "lab@x.org", "password": "pw", "next": "/orders/"}).json()
        self.assertEqual((seen["user"], seen["pw"]), ("lab@x.org", "pw"))
        self.assertEqual(seen["url"], "https://www.app.labsuit.com/dashboard/?labId=3861#orders/status[]=approved")
        s = self.client.get(f"/autofetch/{j['job']}/").json()
        self.assertEqual(s["status"], "done")
        p = PendingImport.objects.get()
        self.assertEqual((p.mode, p.preview["counts"]["new"]), ("merge", 2))
        self.assertEqual(OrderRequest.objects.count(), 0)                     # preview only
        self.client.post(s["preview_url"] + "confirm/")
        self.assertEqual(OrderRequest.objects.count(), 2)
        self.assertEqual(AutoFetchJob.objects.get().created_by, "u")

    def test_failure_is_reported_with_manual_fallback(self):
        from budget.autofetch import AutoFetchError
        from budget.models import PendingImport
        boom = AutoFetchError("לא נמצא כפתור Export בדף של LabSuit.", "url: x", b"\x89PNG")
        with mock.patch("budget.autofetch.fetch_labsuit", side_effect=boom):
            j = self.client.post("/api/orders/autofetch/", {"username": "a", "password": "b"}).json()
        s = self.client.get(f"/autofetch/{j['job']}/").json()
        self.assertEqual((s["status"], s["error"]), ("failed", "לא נמצא כפתור Export בדף של LabSuit."))
        self.assertEqual(self.client.get(s["screenshot_url"])["Content-Type"], "image/png")
        self.assertFalse(PendingImport.objects.exists())
        self.assertContains(self.client.get("/orders/"), "מומלץ להביא אותם ידנית")

    def test_wrong_kind_of_file_fails_cleanly(self):
        with mock.patch("budget.autofetch.fetch_labsuit", return_value=(b"not a spreadsheet", "x.xlsx")):
            j = self.client.post("/api/orders/autofetch/", {"username": "a", "password": "b"}).json()
        s = self.client.get(f"/autofetch/{j['job']}/").json()
        self.assertEqual(s["status"], "failed")
        self.assertIn("לא ייצוא של הזמנות", s["error"])

    def test_export_of_another_page_is_named_and_kept_for_download(self):
        import io
        import pandas as pd
        buf = io.BytesIO()
        pd.DataFrame({"NAME": ["Pipette"], "SERIAL_NUMBER": ["S1"]}).to_excel(buf, sheet_name="Consumable", index=False)
        with mock.patch("budget.autofetch.fetch_labsuit", return_value=(buf.getvalue(), "export.xlsx")):
            j = self.client.post("/api/orders/autofetch/", {"username": "a", "password": "b"}).json()
        s = self.client.get(f"/autofetch/{j['job']}/").json()
        self.assertIn("ייצוא מלאי, לא הזמנות", s["error"])
        self.assertIn("NAME", s["detail"])
        self.assertEqual(self.client.get(s["file_url"]).content, buf.getvalue())

    def test_online_update_merges_and_keeps_manual_rows(self):
        from budget.models import PendingImport
        self.client.post("/api/orders/new/", {"item_name": "Added by hand", "amount_est": "50",
                                              "amount_currency": "ILS", "grant_label": "General Budget"})
        with mock.patch("budget.autofetch.fetch_labsuit", return_value=(self.orders_xlsx(), "o.xlsx")):
            j = self.client.post("/api/orders/autofetch/", {"username": "a", "password": "b"}).json()
        url = self.client.get(f"/autofetch/{j['job']}/").json()["preview_url"]
        self.assertEqual(PendingImport.objects.get().mode, "merge")
        self.client.post(url + "confirm/")
        # a later export without one LabSuit row: Merge keeps it, and the hand-made row is never touched
        fewer = self.orders_xlsx([("DMEM", "student2@lab.example", "received")])
        with mock.patch("budget.autofetch.fetch_labsuit", return_value=(fewer, "o.xlsx")):
            j = self.client.post("/api/orders/autofetch/", {"username": "a", "password": "b"}).json()
        url = self.client.get(f"/autofetch/{j['job']}/").json()["preview_url"]
        self.assertEqual(PendingImport.objects.get().preview["counts"]["hidden"], 0)
        self.assertIn("לא מופיעות בקובץ", " ".join(PendingImport.objects.get().preview["warnings"]))   # approved row gone
        self.client.post(url + "confirm/")
        current = set(OrderRequest.objects.filter(is_current=True).values_list("item_name", flat=True))
        self.assertEqual(current, {"Added by hand", "Pipette 10ml", "DMEM"})
        self.assertEqual(OrderRequest.objects.get(item_name="DMEM").status, "received")
        self.assertEqual(OrderRequest.objects.get(item_name="Added by hand").source, "manual")

    def test_missing_browser_gives_install_instructions(self):
        import os
        import tempfile
        from budget.autofetch import AutoFetchError, fetch_labsuit
        with tempfile.TemporaryDirectory() as empty, mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": empty}):
            with self.assertRaises(AutoFetchError) as e:
                fetch_labsuit("http://127.0.0.1:9/", "u", "p")
        self.assertIn("python -m playwright install chromium", str(e.exception))

    def test_closed_preview_redirects_with_a_message(self):
        from budget.models import PendingImport
        with mock.patch("budget.autofetch.fetch_labsuit", return_value=(self.orders_xlsx(), "orders.xlsx")):
            j = self.client.post("/api/orders/autofetch/", {"username": "a", "password": "b", "next": "/orders/"}).json()
        url = self.client.get(f"/autofetch/{j['job']}/").json()["preview_url"]
        self.assertEqual(self.client.post(url + "confirm/").status_code, 302)
        for r in (self.client.get(url), self.client.post(url + "confirm/")):      # refresh / double click
            self.assertEqual(r.status_code, 302)
        page = self.client.get("/", follow=True)
        self.assertContains(page, "כבר לא פעילה")
        self.assertFalse(PendingImport.objects.exists())

