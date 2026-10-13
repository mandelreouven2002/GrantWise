from datetime import date
from decimal import Decimal

import unittest
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

    def test_one_grant_per_request(self):
        o1, o2 = self.order("1000"), self.order("5000")   # A (≈1,000 EUR) can't hold both
        r = resolve.compute(today=TODAY)
        for o in (o1, o2):
            self.assertEqual(len(self.plan_of(r, o).lines), 1)                  # one grant, in full
        self.assertEqual(self.grants_of(r, o1), {"A"})                         # small one → expiring grant
        self.assertEqual(self.grants_of(r, o2), {"L"})

    def test_too_big_for_any_single_grant_is_flagged_unfunded(self):
        GrantRule.objects.create(grant=self.reserve, category_id="MATERIAL", allowed=False)
        o = self.order("52000")       # L holds 50,000; A ≈ 3,300 ₪ — no single grant covers it
        plan = self.plan_of(resolve.compute(today=TODAY), o)
        self.assertEqual(plan.state, "unfunded")
        self.assertEqual([l.grant for l in plan.lines], [None])      # nothing is partly funded

    def test_expiring_grant_first_reserve_last(self):
        self.order("1000")
        self.order("5000")            # A cannot hold both → the second one goes to L
        r = resolve.compute(today=TODAY)
        lines = [l for p in r.plans for l in p.lines]
        self.assertTrue(all(l.grant for l in lines), "everything funded")
        used_a = sum(l.amount_grant for l in lines if l.grant.number == "A")
        self.assertAlmostEqual(float(used_a), 1000 * 1.03 / 3.4422, delta=0.1)   # the 1,000 ₪ request + FX margin
        self.assertTrue(any(l.grant.number == "L" for l in lines))
        self.assertFalse(any(l.grant.number == "R" for l in lines), "reserve should be the last resort")

    # ---- plain-language explanation

    def reason_of(self, result, order):
        return self.plan_of(result, order).lines[0].reason.split("\n")

    def test_explanation_for_an_expiring_grant(self):
        o = self.order("1000")
        lines = self.reason_of(resolve.compute(today=TODAY), o)
        self.assertIn("המענק מסתיים בעוד 80 ימים", lines[0])
        self.assertIn("כדאי לנצל אותו לפני שיפוג", lines[0])
        self.assertTrue(lines[1].startswith("יורדים ממנו") and "ונשארים" in lines[1])
        self.assertTrue(any(l.startswith("⚠ ההזמנה ב-ILS והמענק ב-EUR") and "3%" in l for l in lines))
        self.assertTrue(any(l.startswith("הבא בתור: ") for l in lines))

    def test_explanation_names_the_only_eligible_grant_and_has_no_alternative(self):
        GrantRule.objects.create(grant=self.soon, category_id="MATERIAL", allowed=False)
        GrantRule.objects.create(grant=self.reserve, category_id="MATERIAL", allowed=False)
        o = self.order("1000")
        lines = self.reason_of(resolve.compute(today=TODAY), o)
        self.assertEqual(lines[0], "זה המענק היחיד שמתאים להזמנה הזו.")
        self.assertFalse(any(l.startswith("הבא בתור") for l in lines))
        self.assertFalse(any(l.startswith("⚠") for l in lines))        # same currency, plenty left: nothing to warn

    def test_split_of_reason_into_main_text_and_comparison(self):
        o = self.order("1000")
        line = self.plan_of(resolve.compute(today=TODAY), o).lines[0]
        self.assertTrue(line.reason_compare.startswith("הבא בתור"))
        self.assertNotIn("הבא בתור", line.reason_main)

    def test_saved_reason_flags_a_manual_choice_and_names_the_next_in_line(self):
        o = self.order("2000")
        resolve.approve_plans({o.pk: "L"}, today=TODAY, edited={o.pk})
        reason = Allocation.objects.get(order=o).reason
        self.assertIn("⚠ בחרת מענק אחר מההמלצה.", reason)
        self.assertIn("הבא בתור: ", reason)

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
        decision = {o.pk: self.plan_of(r, o).lines[0].grant.number}
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

    def test_ineligible_grant_is_rejected_and_nothing_saved(self):
        o = self.order("1000")
        GrantRule.objects.create(grant=self.later, category_id="MATERIAL", allowed=False)
        with self.assertRaises(resolve.ApprovalError) as e:
            resolve.approve_plans({o.pk: "L"}, today=TODAY)
        self.assertIn("אינו כשיר", " ".join(e.exception.errors))
        with self.assertRaises(resolve.ApprovalError):
            resolve.approve_plans({o.pk: ""}, today=TODAY)                          # no grant chosen
        self.assertEqual(Allocation.objects.count(), 0)

    def test_overdraw_rejected_even_across_requests(self):
        o1, o2 = self.order("3000"), self.order("3000")      # each ≈ 898 EUR in A, together > 1000
        with self.assertRaises(resolve.ApprovalError) as e:
            resolve.approve_plans({o1.pk: "A", o2.pk: "A"}, today=TODAY)
        self.assertIn("חריגה", " ".join(e.exception.errors))
        self.assertEqual(Allocation.objects.count(), 0)
        # sequential approvals: the second one sees the first and is refused
        resolve.approve_plans({o1.pk: "A"}, today=TODAY)
        with self.assertRaises(resolve.ApprovalError):
            resolve.approve_plans({o2.pk: "A"}, today=TODAY)

    def test_po_without_date_stays_reserved(self):
        o = self.order("2000")
        resolve.approve_plans({o.pk: "L"}, today=TODAY)
        o.po_number = "5400000003"           # LabSuit shows a PO but no order date
        o.save()
        self.assertEqual(resolve.reconcile(TODAY), 0)
        self.assertEqual(resolve.grant_states(TODAY, TODAY)["L"].total, Decimal("48000"))

    def test_lab_updated_mark_dates_the_order_for_reconciliation(self):
        from datetime import datetime, timezone as tz
        o = self.order("2000")
        resolve.approve_plans({o.pk: "L"}, today=TODAY)
        Allocation.objects.filter(order=o).update(lab_updated_at=datetime(2026, 10, 13, 9, tzinfo=tz.utc))
        self.assertEqual(resolve.reconcile(TODAY), 0)                       # SAP report older than the mark
        self.assertEqual(resolve.grant_states(TODAY, TODAY)["L"].total, Decimal("48000"))
        self.assertEqual(resolve.reconcile(date(2026, 10, 20)), 1)          # next report covers it
        self.assertEqual(Allocation.objects.get(order=o).status, "superseded")

    def test_grant_written_in_labsuit_marks_decision(self):
        o = self.order("2000")
        resolve.approve_plans({o.pk: "L"}, today=TODAY)
        self.assertEqual(resolve.mark_written_in_labsuit(), 0)          # LabSuit still says General Budget
        o.grant_label = "A"                                              # wrong grant → not marked
        o.save()
        self.assertEqual(resolve.mark_written_in_labsuit(), 0)
        o.grant_label = "L"
        o.save()
        FundingAlias.objects.create(label="L", kind="grant", grant=self.later)
        self.assertEqual(resolve.mark_written_in_labsuit(), 1)
        self.assertFalse(OrderRequest.objects.filter(pk=o.pk).exists())    # same as pressing the button: deleted
        a = Allocation.objects.get()
        self.assertEqual((a.status, a.lab_updated_by), ("approved", "LabSuit (GRANT_ID)"))   # still reserved
        self.assertEqual((a.order_id, a.item_name), (None, o.item_name))

    def test_lab_updated_button_deletes_the_order_and_keeps_the_reservation(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        o = self.order("2000")
        resolve.approve_plans({o.pk: "L"}, today=TODAY)
        a = Allocation.objects.get(order=o)
        self.client.post(f"/resolve/lab-updated/{a.pk}/")
        self.assertFalse(OrderRequest.objects.filter(pk=o.pk).exists())    # really deleted
        a.refresh_from_db()
        self.assertEqual((a.status, a.lab_updated_by, a.order_id), ("approved", "u", None))   # …but still reserved
        self.assertEqual((a.item_name, a.order_key), (o.item_name, o.natural_key))
        # nothing of it is left in the recycle bin, the orders page or the recommendations
        page = self.client.get("/resolve/")
        self.assertEqual(page.context["bin"], [])
        self.assertNotIn(o.item_name, [r["order"].item_name for r in page.context["rows"]])
        self.assertNotIn(o.item_name, [r["obj"].item_name for r in self.client.get("/orders/?year=all").context["t"]["rows"]])
        # the money stays deducted from the grant, and the decision still shows in the grant's list
        self.assertEqual(resolve.grant_states(TODAY, TODAY)["L"].total, Decimal("48000"))
        self.assertContains(self.client.get("/grants/"), o.item_name)
        # pressing it again (old tab) changes nothing
        self.client.post(f"/resolve/lab-updated/{a.pk}/")
        self.assertEqual(Allocation.objects.count(), 1)

    def test_deleted_order_is_covered_by_sap_by_the_marked_date(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        o = self.order("2000")
        resolve.approve_plans({o.pk: "L"}, today=TODAY)
        self.client.post(f"/resolve/lab-updated/{Allocation.objects.get(order=o).pk}/")
        self.assertEqual(resolve.reconcile(TODAY), 0)                       # report older than the mark
        self.assertEqual(resolve.reconcile(date(2026, 10, 20)), 1)          # a later report covers it
        self.assertEqual(Allocation.objects.get().status, "superseded")
        self.assertEqual(resolve.grant_states(TODAY, date(2026, 10, 20))["L"].total, Decimal("50000"))

    def test_the_same_request_returning_is_a_new_request_and_is_not_subtracted_twice(self):
        o = self.order("2000", po_number="5400000077", date_ordered=date(2026, 10, 14))
        resolve.approve_plans({o.pk: "L"}, today=TODAY)
        Allocation.objects.filter(order=o).update(lab_updated_at=timezone_now())
        resolve.retire_orders([o.pk])
        self.assertEqual(OrderRequest.objects.count(), 0)
        # LabSuit lists it again: it is simply a new request…
        back = OrderRequest.objects.create(natural_key=o.natural_key, item_name=o.item_name, status="approved",
                                           amount_est=Decimal("2000"), amount_currency="ILS", category_id="MATERIAL",
                                           po_number="5400000077", date_ordered=date(2026, 10, 14),
                                           grant_label="L", date_requested=TODAY)
        FundingAlias.objects.create(label="L", kind="grant", grant=self.later)
        self.assertEqual(Allocation.objects.get().order_id, None)             # not re-linked
        # …but the money already reserved for it is not taken a second time as an "unseen" order
        self.assertEqual(resolve.unseen_orders(TODAY, TODAY), [])
        self.assertEqual(resolve.grant_states(TODAY, TODAY)["L"].total, Decimal("48000"))
        self.assertIn(back.pk, [p.cand.order.pk for p in resolve.compute(today=TODAY).plans])

    def test_deleting_an_order_by_hand_releases_an_unmarked_decision(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        o = self.order("2000", source="manual")           # LabSuit rows are only hidden by the delete button
        resolve.approve_plans({o.pk: "L"}, today=TODAY)
        self.client.post(f"/api/orders/{o.pk}/delete/")
        self.assertFalse(Allocation.objects.exists())
        self.assertEqual(resolve.grant_states(TODAY, TODAY)["L"].total, Decimal("50000"))

    def test_approval_locks_only_the_main_table(self):
        """PostgreSQL rejects SELECT … FOR UPDATE over a nullable join ("cannot be applied to the nullable side
        of an outer join"), which SQLite cannot show: the lock must be limited to the main table."""
        for qs in (resolve.locked_grants(["A", "L"]), resolve.locked_orders([1, 2])):
            self.assertTrue(qs.query.select_for_update)
            self.assertEqual(qs.query.select_for_update_of, ("self",))

    def test_double_approval_refused(self):
        o = self.order("100")
        resolve.approve_plans({o.pk: "L"}, today=TODAY)
        with self.assertRaises(resolve.ApprovalError):
            resolve.approve_plans({o.pk: "L"}, today=TODAY)


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
        for url in ["/", "/grants/", "/orders/", "/resolve/", "/settings/"]:
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
        r = self.client.post("/resolve/approve/", {"order": [o.pk], f"g_{o.pk}": ["A"]})
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
        self.client.post("/resolve/approve/", {"order": [o.pk], f"g_{o.pk}": ["A"]})
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


def timezone_now():
    from django.utils import timezone
    return timezone.now()


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
        resolve.approve_plans({o.pk: "A"})
        page = self.client.get("/grants/")
        self.assertContains(page, "אחרי החלטות: 2,500")
        self.assertContains(page, "Antibody X")
        self.assertContains(page, "ממתין ל-SAP")


@override_settings(FX_AUTO_REFRESH=False)
class NoEstimatedAmountsTests(TestCase):
    """An order's amount is LabSuit's own price or a typed-in amount — never filled in from an earlier order."""

    @staticmethod
    def xlsx(rows):
        import io
        import pandas as pd
        buf = io.BytesIO()
        pd.DataFrame({"ITEM_NAME": [r[0] for r in rows], "CATALOG_NUMBER": [r[1] for r in rows],
                      "TOTAL": [r[2] for r in rows], "REQUESTED_BY": ["a@lab.example"] * len(rows),
                      "STATUS": ["approved"] * len(rows), "QUANTITY": [1] * len(rows),
                      "DATE_REQUESTED": [f"{10 + i}-10-2026" for i in range(len(rows))],
                      "GRANT_ID": ["General Budget"] * len(rows)}).to_excel(buf, sheet_name="Year - 2026", index=False)
        return buf.getvalue()

    def import_rows(self, rows):
        from budget.importers import run_import
        return run_import("labsuit_orders", self.xlsx(rows), "o.xlsx", TODAY, "merge")

    def test_unpriced_order_gets_no_amount_even_if_same_catalog_number_was_priced(self):
        self.import_rows([("Antibody", "AB-1", "ILS 250"), ("Antibody again", "AB-1", None)])
        priced = OrderRequest.objects.get(item_name="Antibody")
        unpriced = OrderRequest.objects.get(item_name="Antibody again")
        self.assertEqual((priced.amount_est, priced.amount_source), (Decimal("250.00"), "labsuit"))
        self.assertEqual((unpriced.amount_est, unpriced.amount_source), (None, "none"))

    def test_no_price_means_no_recommendation_until_an_amount_is_typed(self):
        b = ImportBatch.objects.create(source="sap_grants", file_name="t", as_of=TODAY)
        g = Grant.objects.create(number="A", currency="ILS", end_date=date(2027, 6, 1), balance_total=5000,
                                 last_batch=b)
        GrantLine.objects.create(batch=b, grant=g, category_id="MATERIAL", balance=Decimal("5000"))
        FxRate.objects.create(date=TODAY, currency="USD", rate_to_ils=Decimal("3.078"))
        self.import_rows([("Unpriced", "X-1", None)])
        o = OrderRequest.objects.get(item_name="Unpriced")
        o.date_requested = TODAY
        o.category_id = "MATERIAL"
        o.save()
        r = resolve.compute(today=TODAY)
        self.assertEqual(r.needs_amount, [o])
        self.assertEqual(r.plans, [])

    def test_old_estimates_are_cleared_on_import_but_manual_amounts_survive(self):
        self.import_rows([("Old guess", "G-1", None), ("Typed in", "G-2", None), ("Real price", "G-3", "ILS 90")])
        OrderRequest.objects.filter(item_name="Old guess").update(amount_est=Decimal("40"), amount_source="history")
        OrderRequest.objects.filter(item_name="Typed in").update(amount_est=Decimal("70"), amount_source="manual")
        self.import_rows([("Old guess", "G-1", None), ("Typed in", "G-2", None), ("Real price", "G-3", None)])
        got = {o.item_name: (o.amount_est, o.amount_source) for o in OrderRequest.objects.all()}
        self.assertEqual(got["Old guess"], (None, "none"))                         # legacy estimate dropped
        self.assertEqual(got["Typed in"], (Decimal("70.00"), "manual"))            # lab's own amount kept
        self.assertEqual(got["Real price"], (Decimal("90.00"), "labsuit"))         # price LabSuit stopped sending is kept


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


# --------------------------------------------------------------------------- PDF scraping against a mock LabSuit

def _chromium_ready():
    try:
        import os
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:  # noqa: BLE001
        return False


MOCK_ROWS = [
    dict(name="Calfectin Transfection Reagent", by="Elian Far", cat="SL100478", quote="19363", pdf=True),
    dict(name="Pipette tips", by="Dana Lev", cat="PT-1", quote="20001", pdf=True),
    dict(name="Pipette tips", by="Noa Bar", cat="PT-1", quote="20002", pdf=True),      # same name, other quote
    dict(name="Gloves", by="Dana Lev", cat="GL-9", quote="20003", pdf=False),           # nothing attached
] + [dict(name=f"Filler item {i}", by="Dana Lev", cat=f"F-{i}", quote=str(30000 + i), pdf=True) for i in range(8)]


class MockLabSuit:
    """A small stand-in for LabSuit's orders page: login, list that loads more on scroll, Export popup,
    a side panel with the request's file, and PDFs that need the login cookie."""

    def __init__(self, viewer=False):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        site = self
        self.viewer = viewer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="text/html; charset=utf-8", headers=()):
                if isinstance(body, str):
                    body = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _auth(self):
                return "sid=ok" in (self.headers.get("Cookie") or "")

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(302)
                self.send_header("Set-Cookie", "sid=ok; Path=/")
                self.send_header("Location", "/home")
                self.end_headers()

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/home":
                    return self._send(200, "<html><body>Home</body></html>")
                if path.startswith("/dashboard"):
                    return self._send(200, site.page() if self._auth() else LOGIN_PAGE)
                if path == "/export.xlsx":
                    if not self._auth():
                        return self._send(403, "no")
                    return self._send(200, site.xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                      [("Content-Disposition", 'attachment; filename="orders.xlsx"')])
                if path.startswith("/files/"):
                    if not self._auth():
                        return self._send(403, "no")
                    q = path.rsplit("_", 1)[-1].replace(".pdf", "")
                    body = f"%PDF-1.4\n% quote {q}\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n".encode()
                    return self._send(200, body, "application/pdf")
                self._send(404, "not found")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/dashboard/?labId=1#orders/status[]=approved"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()

    def xlsx(self):
        import io
        import pandas as pd
        buf = io.BytesIO()
        n = len(MOCK_ROWS)
        pd.DataFrame({"ITEM_NAME": [r["name"] for r in MOCK_ROWS], "CATALOG_NUMBER": [r["cat"] for r in MOCK_ROWS],
                      "REQUESTED_BY": [r["by"] for r in MOCK_ROWS], "QUOTE_ID": [r["quote"] for r in MOCK_ROWS],
                      "STATUS": ["approved"] * n, "DATE_REQUESTED": ["10-10-2026"] * n,
                      "GRANT_ID": ["General Budget"] * n, "TOTAL": ["ILS 100"] * n}).to_excel(
            buf, sheet_name="Year - 2026", index=False)
        return buf.getvalue()

    def page(self):
        import json
        viewer = "true" if self.viewer else "false"
        return f"""<!doctype html><html><body style="margin:0">
<button id="exp">Export</button><div id="modal" role="dialog" style="display:none"><button id="exp2">Export</button></div>
<div id="list"></div><div id="panel" style="position:fixed;right:0;top:0;width:360px;background:#fff"></div>
<script>
const rows = {json.dumps(MOCK_ROWS)}; let shown = 0; const viewer = {viewer};
function more() {{ for (let i = 0; i < 4 && shown < rows.length; i++, shown++) {{
  const r = rows[shown], d = document.createElement('div'); d.className = 'card'; d.style.minHeight = '260px';
  d.innerHTML = '<a href="#" class="title">' + r.name + '</a> <span>By:</span> <b>' + r.by + '</b> <span>Catalog #</span> <b>'
    + r.cat + '</b> <span>Quote #</span> <a href="#">' + r.quote + '</a>';
  d.querySelector('.title').onclick = (e) => {{ e.preventDefault(); openPanel(r); }};
  document.getElementById('list').appendChild(d); }} }}
function openPanel(r) {{
  const p = document.getElementById('panel');
  if (!r.pdf) {{ p.innerHTML = '<h3>Files</h3><button>Attach File</button>'; return; }}
  const name = 'workorder_' + r.quote + '.pdf';
  p.innerHTML = '<h3>Files</h3><a id="f" href="' + (viewer ? '#' : '/files/' + name) + '">' + name + '</a><button>Attach File</button>';
  if (viewer) document.getElementById('f').onclick = (e) => {{ e.preventDefault();
    p.insertAdjacentHTML('beforeend', '<iframe src="/files/' + name + '" width="300" height="200"></iframe>'); }};
}}
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') document.getElementById('panel').innerHTML = ''; }});
window.addEventListener('scroll', () => {{ if (innerHeight + scrollY >= document.body.scrollHeight - 80) more(); }});
document.getElementById('exp').onclick = () => document.getElementById('modal').style.display = 'block';
document.getElementById('exp2').onclick = () => {{ location.href = '/export.xlsx'; }};
more();
</script></body></html>"""


LOGIN_PAGE = """<html><body><form method="post" action="/login">
<input type="text" name="u"><input type="password" name="p"><button type="submit">Login</button></form></body></html>"""


@unittest.skipUnless(_chromium_ready(), "needs Playwright's Chromium (python -m playwright install chromium)")
@override_settings(FX_AUTO_REFRESH=False, AUTOFETCH_SYNC=True, AUTOFETCH_PDF_PANEL_SECONDS=2)
class PdfScrapingTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")

    def targets_by_quote(self, site):
        from budget.importers.labsuit_orders import scrape_targets
        return {t["quote_id"]: t for t in scrape_targets(site.xlsx())}

    def test_robot_saves_the_right_pdf_for_every_request(self):
        from budget import autofetch
        site = MockLabSuit()
        self.addCleanup(site.stop)
        data, name, extra = autofetch.fetch_labsuit(site.url, "u", "p", download_seconds=20, attachments={})
        by_quote = self.targets_by_quote(site)
        got = {f["quote_id"]: f for f in extra["files"]}
        want = {r["quote"] for r in MOCK_ROWS if r["pdf"]}
        self.assertEqual(set(got), want)                               # incl. the rows that load on scroll
        for q, f in got.items():
            self.assertTrue(f["data"].startswith(b"%PDF"))
            self.assertIn(f"quote {q}".encode(), f["data"])            # each file is its own request's
            self.assertEqual(f["name"], f"workorder_{q}.pdf")
            self.assertEqual(f["key"], by_quote[q]["key"])             # …and keyed like the importer keys it
        rep = extra["report"]
        self.assertEqual((rep["saved"], len(rep["not_found"]), rep["mismatch"], rep["failed"]),
                         (len(want), 1, [], []))
        self.assertIn("Gloves", rep["not_found"][0])

    def test_files_already_stored_are_skipped(self):
        from budget import autofetch
        site = MockLabSuit()
        self.addCleanup(site.stop)
        by_quote = self.targets_by_quote(site)
        have = {by_quote["19363"]["key"]: "19363"}
        _, _, extra = autofetch.fetch_labsuit(site.url, "u", "p", download_seconds=20, attachments=have)
        self.assertEqual(extra["report"]["skipped"], 1)
        self.assertNotIn("19363", {f["quote_id"] for f in extra["files"]})

    def test_file_that_opens_in_a_viewer_is_captured_too(self):
        from budget import autofetch
        site = MockLabSuit(viewer=True)
        self.addCleanup(site.stop)
        _, _, extra = autofetch.fetch_labsuit(site.url, "u", "p", download_seconds=20, attachments={})
        self.assertEqual({f["quote_id"] for f in extra["files"]}, {r["quote"] for r in MOCK_ROWS if r["pdf"]})

    def test_job_preview_confirm_saves_files_and_serves_them(self):
        from budget import autofetch
        from budget.models import AutoFetchJob, OrderFile, PendingFile, SourceCredential
        site = MockLabSuit()
        self.addCleanup(site.stop)
        c = SourceCredential(source="labsuit", username="u")
        c.set_password("p")
        c.save()
        with override_settings(LABSUIT_URLS={"orders": site.url}):
            job = AutoFetchJob.objects.create(table_key="orders", source="labsuit_orders", created_by="u")
            autofetch.run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "done", job.error)
        pending = job.pending
        self.assertEqual(PendingFile.objects.filter(pending=pending).count(), 11)
        self.assertEqual(OrderFile.objects.count(), 0)                   # nothing attached before the user confirms
        self.assertEqual(pending.preview["attachments"]["saved"], 11)
        self.assertContains(self.client.get(f"/import/{pending.pk}/"), "קובצי PDF מ-LabSuit")
        self.client.post(f"/import/{pending.pk}/confirm/")
        self.assertEqual(OrderFile.objects.count(), 11)
        self.assertFalse(PendingFile.objects.exists())
        o = OrderRequest.objects.get(quote_id="19363")
        self.assertEqual(o.file.file_name, "workorder_19363.pdf")
        r = self.client.get(f"/orders/{o.pk}/file/")
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "application/pdf"))
        self.assertTrue(r.content.startswith(b"%PDF") and b"quote 19363" in r.content)
        self.assertContains(self.client.get("/orders/?year=all"), f"/orders/{o.pk}/file/")
        # second run: everything we hold is skipped, nothing is downloaded again
        with override_settings(LABSUIT_URLS={"orders": site.url}):
            job2 = AutoFetchJob.objects.create(table_key="orders", source="labsuit_orders", created_by="u")
            autofetch.run_job(job2.pk)
        job2.refresh_from_db()
        self.assertEqual(job2.pending.preview["attachments"]["skipped"], 11)
        self.assertEqual(job2.pending.preview["attachments"]["saved"], 0)


@override_settings(FX_AUTO_REFRESH=False)
class HomePageTests(TestCase):
    """The home page is a normal page of the site: navigation, logout, and the messages of whatever sent you."""

    def setUp(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")

    def test_home_has_the_site_navigation(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        for label in ("הזמנות ודרישות", "מענקים", "המלצות", "הגדרות", "יציאה"):
            self.assertContains(page, label)
        self.assertContains(page, 'href="/orders/"')
        self.assertContains(page, 'href="/grants/"')

    def test_messages_are_shown_after_redirecting_home(self):
        # e.g. cancelling an import sends you to "/" with a message
        from budget.models import PendingImport
        p = PendingImport.objects.create(table_key="orders", source="labsuit_orders", file_name="x.xlsx",
                                         data=b"", mode="merge", as_of=TODAY, preview={"counts": {}},
                                         uploaded_by="u")
        r = self.client.post(f"/import/{p.pk}/cancel/", follow=True)
        self.assertContains(r, "העדכון בוטל")
        self.assertContains(r, "הזמנות ודרישות")             # …on a page with the navigation

    def test_login_lands_on_a_page_with_navigation(self):
        self.client.logout()
        r = self.client.post("/login/", {"username": "u", "password": "p"}, follow=True)
        self.assertContains(r, 'href="/orders/"')
