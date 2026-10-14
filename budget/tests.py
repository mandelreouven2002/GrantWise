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

    def test_category_rule(self):
        GrantRule.objects.create(grant=self.later, category_id="EQUIPMENT", allowed=False)
        o = self.order("2000", category="EQUIPMENT")
        r = resolve.compute(today=TODAY)
        self.assertEqual(self.grants_of(r, o), {"R"})   # the only grant that can take equipment (pooled)

    def test_project_has_no_effect_on_the_recommendation(self):
        """The project (a free text now; the old project links) is display only: it never restricts or steers."""
        plain = self.order("1000")
        p = Project.objects.create(name="P1")
        ProjectGrant.objects.create(project=p, grant=self.later, weight=3)     # would have steered to L
        linked = self.order("1000", project=p, project_text="whatever I like")
        r = resolve.compute(today=TODAY)
        self.assertEqual(self.grants_of(r, plain), self.grants_of(r, linked))

    # ---- salary and services carry no VAT: the price stays the price, even for a grant that pays none

    def test_only_materials_are_affected_by_the_vat_of_the_grant(self):
        self.later.vat_required = False
        self.later.save()
        GrantLine.objects.create(batch=self.batch, grant=self.later, category_id="SALARY", balance=Decimal("50000"))
        for cat in ("SALARY", "SERVICE", "EQUIPMENT", "OTHER"):
            o = self.order("2000", category=cat)
            r = resolve.compute(today=TODAY)
            c = self.plan_of(r, o).cand
            self.assertEqual(resolve.charge_factor(c, self.later), Decimal("1"), cat)     # price stays the price
            o.delete()
        o = self.order("2000", category="SALARY")
        resolve.approve_plans({o.pk: "L"}, today=TODAY)
        self.assertEqual(Allocation.objects.get(order=o).amount_grant_ccy, Decimal("2000.00"))   # not 1694.92

    def test_materials_use_the_quoted_totals_by_the_grant_vat(self):
        """A grant that pays VAT is charged the total with VAT, one that does not the total before VAT —
        the two numbers of the quote, not a fixed 1.18."""
        o = self.order("1", quoted_total_incl_vat=Decimal("1000"), quoted_total_excl_vat=Decimal("900"),
                       quoted_currency="ILS")
        OrderRequest.objects.filter(pk=o.pk).update(amount_est=None)
        o.refresh_from_db()
        resolve.approve_plans({o.pk: "L"}, today=TODAY)
        self.assertEqual(Allocation.objects.get(order=o).amount_grant_ccy, Decimal("1000.00"))   # L pays VAT
        Allocation.objects.all().delete()
        self.later.vat_required = False
        self.later.save()
        resolve.approve_plans({o.pk: "L"}, today=TODAY)
        self.assertEqual(Allocation.objects.get(order=o).amount_grant_ccy, Decimal("900.00"))    # the quote's own total

    def test_materials_with_one_total_derive_the_other_by_the_vat_rate(self):
        from budget.amounts import order_amount
        only_excl = self.order("1", quoted_total_excl_vat=Decimal("1000"), quoted_currency="ILS")
        only_incl = self.order("1", quoted_total_incl_vat=Decimal("1180"), quoted_currency="ILS")
        for o in (only_excl, only_incl):
            OrderRequest.objects.filter(pk=o.pk).update(amount_est=None)
            o.refresh_from_db()
        a, b = order_amount(only_excl), order_amount(only_incl)
        self.assertEqual((a.gross, a.ratio.quantize(Decimal("0.0001"))), (Decimal("1180.00"), Decimal("0.8475")))
        self.assertEqual((b.gross, b.ratio.quantize(Decimal("0.0001"))), (Decimal("1180"), Decimal("0.8475")))

    def test_other_categories_use_the_typed_amount_and_ignore_vat(self):
        from budget.amounts import order_amount
        o = self.order("750", category="TRAVEL_EXT")
        self.assertEqual(order_amount(o).gross, Decimal("750"))
        self.assertEqual(order_amount(o).ratio, Decimal("1"))
        o2 = self.order("1", category="OTHER", quoted_total_incl_vat=Decimal("999"))   # a stray quote total is ignored
        self.assertEqual(order_amount(o2).gross, Decimal("1"))

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
        # the money stays deducted from the grant
        self.assertEqual(resolve.grant_states(TODAY, TODAY)["L"].total, Decimal("48000"))
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

    # ---- VAT: a grant that does not pay VAT is charged the price before VAT

    def test_grant_without_vat_is_charged_price_divided_by_118(self):
        self.later.vat_required = False
        self.later.save()
        o = self.order("2000")
        resolve.approve_plans({o.pk: "L"}, today=TODAY)
        a = Allocation.objects.get(order=o)
        self.assertEqual(a.amount_ils, Decimal("2000.00"))                     # the order's price is unchanged
        self.assertEqual(a.amount_grant_ccy, Decimal("1694.92"))               # 2000 / 1.18 comes off the grant
        self.assertEqual(resolve.grant_states(TODAY, TODAY)["L"].total, Decimal("50000") - Decimal("1694.92"))
        self.assertEqual((a.amount_orig * 1 / Decimal("1.18")).quantize(Decimal("0.01")), a.amount_grant_ccy)

    def test_vat_changes_what_fits(self):
        GrantRule.objects.create(grant=self.soon, category_id="MATERIAL", allowed=False)
        GrantRule.objects.create(grant=self.reserve, category_id="MATERIAL", allowed=False)
        o = self.order("55000")                    # L holds 50,000: only fits once the VAT is not charged
        self.assertEqual(self.plan_of(resolve.compute(today=TODAY), o).state, "unfunded")
        self.later.vat_required = False
        self.later.save()
        r = resolve.compute(today=TODAY)
        self.assertEqual(self.grants_of(r, o), {"L"})
        self.assertAlmostEqual(float(self.plan_of(r, o).lines[0].amount_grant), 55000 / 1.18, delta=0.01)

    def test_explanation_mentions_that_the_grant_pays_no_vat(self):
        self.later.vat_required = False
        self.later.save()
        GrantRule.objects.create(grant=self.soon, category_id="MATERIAL", allowed=False)
        GrantRule.objects.create(grant=self.reserve, category_id="MATERIAL", allowed=False)
        o = self.order("1000")
        lines = self.reason_of(resolve.compute(today=TODAY), o)
        self.assertTrue(any("לא משלם מע״מ" in l and "לפני מע״מ" in l for l in lines))

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
        for url in ["/", "/grants/", "/orders/", "/other/", "/resolve/", "/settings/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        for url in ["/admin/", "/inventory/", "/analysis/"]:                  # removed dashboards
            self.assertEqual(self.client.get(url).status_code, 404, url)
        import re
        nav = re.sub(r"\s+", " ", self.client.get("/orders/").content.decode())     # the nav is multi-line HTML
        self.assertIn("> המלצות </a>", nav)
        self.assertLess(nav.index("> הזמנות חומרים </a>"), nav.index("> אחר </a>"))
        self.assertLess(nav.index("> מענקים </a>"), nav.index("> הזמנות חומרים </a>"))     # nav order since the redesign

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
        self.post_json("/api/grants/A/", {"field": "line:OTHER", "value": "1000"})
        self.client.post("/api/orders_other/new/", {"item_name": "x", "amount_est": "500", "amount_currency": "ILS",
                                                   "category": "OTHER"})
        o = OrderRequest.objects.get(item_name="x")
        self.assertEqual((o.amount_source, o.category_id), ("manual", "OTHER"))
        page = self.client.get("/resolve/")
        self.assertContains(page, "x")
        self.assertEqual(page.context["rows"][0]["lines"][0].grant.number, "A")
        # approving through the page posts the (possibly edited) lines
        r = self.client.post("/resolve/approve/", {"order": [o.pk], f"g_{o.pk}": ["A"]})
        self.assertEqual(Allocation.objects.filter(order=o, status="approved").count(), 1)
        # the approved order leaves the orders page and waits in the recycle bin on the recommendations page
        orders_page = self.client.get("/other/?year=all")
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
        on_orders_page = {r["pk"] for url in ("/orders/?year=all", "/other/?year=all")
                          for r in self.client.get(url).context["t"]["rows"]}
        self.assertTrue(on_orders_page <= set(states))


    def test_project_column_is_free_text_defaulting_to_the_grant_id_with_grant_suggestions(self):
        o = OrderRequest.objects.create(natural_key="y1", item_name="y", status="approved", grant_label="2032917",
                                        category_id="MATERIAL", date_requested=timezone_today())
        page = self.client.get("/orders/")
        cell = next(c for c in page.context["t"]["rows"][0]["cells"] if c["col"].field == "project_text")
        self.assertEqual(cell["value"], "2032917")                      # shows the GRANT_ID until the lab types
        self.assertContains(page, '<datalist id="grant-numbers">')      # suggestions: every grant of the table
        self.assertContains(page, '<option value="A">')
        # any text may be typed; it is only stored for display and no longer a link to anything
        r = self.post_json(f"/api/orders/{o.pk}/", {"field": "project_text", "value": "my own words"})
        self.assertEqual(r.status_code, 200)
        o.refresh_from_db()
        self.assertEqual((o.project_text, o.grant_label), ("my own words", "2032917"))
        # the LabSuit label is not touched, so an import keeps the typed text
        self.assertNotIn("project_text", OrderRequest.LABSUIT_FIELDS)
        self.assertEqual(self.client.get("/settings/?tab=projects").status_code, 404)   # the projects table is gone

    def test_unknown_currency_rejected_until_tracked(self):
        o = OrderRequest.objects.create(natural_key="k", item_name="z", category_id="OTHER")
        bad = self.post_json(f"/api/orders_other/{o.pk}/", {"field": "amount_currency", "value": "SGD"})
        self.assertEqual(bad.status_code, 400)
        TrackedCurrency.objects.create(code="SGD")
        ok = self.post_json(f"/api/orders_other/{o.pk}/", {"field": "amount_currency", "value": "sgd"})
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
    def test_grants_page_shows_per_category_after_decisions_and_no_row_expansion(self):
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
        self.assertNotContains(page, 'class="toggle"')                  # one line per grant, nothing to expand
        self.assertNotContains(page, "כללי מענק")
        self.assertNotContains(page, "פרויקטים מורשים")


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
        for label in ("הזמנות חומרים", "אחר", "מענקים", "המלצות", "הגדרות", "יציאה"):
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
        self.assertContains(r, "הזמנות חומרים")              # …on a page with the navigation

    def test_login_lands_on_a_page_with_navigation(self):
        self.client.logout()
        r = self.client.post("/login/", {"username": "u", "password": "p"}, follow=True)
        self.assertContains(r, 'href="/orders/"')


@override_settings(FX_AUTO_REFRESH=False)
class GrantsTableAndRecommendationPageTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        FxRate.objects.create(date=TODAY, currency="EUR", rate_to_ils=Decimal("3.4422"))
        self.batch = ImportBatch.objects.create(source="sap_grants", file_name="t", as_of=TODAY)
        self.g = Grant.objects.create(number="A", currency="EUR", end_date=date(2027, 6, 1), sponsor="Horizon",
                                      balance_total=Decimal("1000"), last_batch=self.batch)
        for cat, v in {"MATERIAL": "700", "TRAVEL_EXT": "300", "OVERHEAD": "150"}.items():
            GrantLine.objects.create(batch=self.batch, grant=self.g, category_id=cat, balance=Decimal(v))

    def test_grants_table_shows_every_category_the_file_has_and_only_those(self):
        page = self.client.get("/grants/")
        fields = [c.field for c in page.context["t"]["cols"]]
        for code in ("MATERIAL", "TRAVEL_EXT", "OVERHEAD"):
            self.assertIn(f"line:{code}", fields)
        for code in ("BOOKS", "SELF", "SALARY"):                      # nobody on the page has a line for these
            self.assertNotIn(f"line:{code}", fields)
        self.assertContains(page, "נסיעות לחו״ל")
        self.assertContains(page, "תקורה")

    def test_vat_flag_defaults_to_required_and_is_editable_in_the_table(self):
        import json
        self.assertTrue(Grant.objects.get(pk="A").vat_required)
        self.assertIn("vat_required", [c.field for c in self.client.get("/grants/").context["t"]["cols"]])
        r = self.client.post("/api/grants/A/", json.dumps({"field": "vat_required", "value": False}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Grant.objects.get(pk="A").vat_required)

    def test_import_does_not_reset_the_vat_flag(self):
        Grant.objects.filter(pk="A").update(vat_required=False)
        from budget.importers import run_import
        import io
        txt = "GRANT\tgrant_currency\tsponsor_name\tgrant_budjet\tGrant Expense\tGrant Balance\tfund_end_dt\tMATERIAL_BAL\n" \
              "A\tEUR\tX\t2000\t1000\t1000\t01/06/2027\t700\n"
        run_import("sap_grants", io.BytesIO(txt.encode("utf-16")), "t.xls", TODAY, "merge")
        self.assertFalse(Grant.objects.get(pk="A").vat_required)

    def test_recommendation_page_layout(self):
        OrderRequest.objects.create(natural_key="k", item_name="Thing", status="approved", amount_est=Decimal("100"),
                                    amount_currency="EUR", category_id="MATERIAL", date_requested=TODAY)
        page = self.client.get("/resolve/")
        html = page.content.decode()
        self.assertLess(html.index('id="plans"'), html.index('id="summary"'))      # the summary is below the requests
        self.assertLess(html.index('id="summary"'), html.index('id="approve-btn"'))
        plans = html[html.index('id="plans"'):html.index('id="summary"')]
        self.assertNotIn('class="cell', plans)                                     # nothing to edit in the table
        self.assertNotIn("data-f=", plans)
        row = page.context["rows"][0]
        self.assertEqual(row["rem_cur"], "EUR")                                   # "how much will remain" is on the row
        self.assertEqual(row["pay"], row["lines"][0].amount_grant)                # …and how much is paid from the grant
        self.assertIn("משלמים", html)
        self.assertNotIn("lconv", html)                                           # no side text for it any more
        self.assertIn("A · Horizon · EUR", html[html.index("<select"):html.index("</select>")])   # name sits in the option
        self.assertEqual(row["rem"], Decimal("700") - row["lines"][0].amount_grant)
        self.assertIn("יישאר", plans)
        # the reason lives inside the expandable part only
        why = plans[plans.index('<details class="why"'):]
        self.assertIn('class="reason lreason"', why)
        self.assertNotIn('class="reason lreason"', plans[:plans.index('<details class="why"')])


@override_settings(FX_AUTO_REFRESH=False)
class MaterialsAndOtherPagesTests(TestCase):
    """Two lists of requests: materials (two quoted totals, the LabSuit update) and everything else."""

    def setUp(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        FxRate.objects.create(date=TODAY, currency="EUR", rate_to_ils=Decimal("3.4422"))
        batch = ImportBatch.objects.create(source="sap_grants", file_name="t", as_of=TODAY)
        g = Grant.objects.create(number="A", currency="ILS", end_date=date(2027, 6, 1),
                                 balance_total=Decimal("100000"), last_batch=batch)
        for cat in ("MATERIAL", "OTHER", "TRAVEL_EXT"):
            GrantLine.objects.create(batch=batch, grant=g, category_id=cat, balance=Decimal("30000"))
        mk = lambda key, name, cat, **kw: OrderRequest.objects.create(
            natural_key=key, item_name=name, status="approved", category_id=cat, date_requested=timezone_today(),
            amount_currency="ILS", **kw)
        self.mat = mk("m", "Antibody", "MATERIAL", quoted_total_incl_vat=Decimal("1180"),
                      quoted_total_excl_vat=Decimal("1000"), quoted_currency="ILS")
        self.nocat = mk("n", "Gloves", None, quoted_total_incl_vat=Decimal("590"), quoted_total_excl_vat=Decimal("500"),
                        quoted_currency="ILS")
        self.other = mk("o", "Cleaning service", "OTHER", amount_est=Decimal("800"))
        self.travel = mk("t", "Flight", "TRAVEL_EXT", amount_est=Decimal("2500"))

    def rows(self, url):
        return {r["obj"].pk for r in self.client.get(url + "?year=all").context["t"]["rows"]}

    def test_requests_are_split_by_category(self):
        self.assertEqual(self.rows("/orders/"), {self.mat.pk, self.nocat.pk})        # no category = a material
        self.assertEqual(self.rows("/other/"), {self.other.pk, self.travel.pk})

    def test_each_page_has_its_own_amount_columns_and_only_materials_have_the_update(self):
        mat = [c.field for c in self.client.get("/orders/").context["t"]["cols"]]
        oth = [c.field for c in self.client.get("/other/").context["t"]["cols"]]
        self.assertIn("quoted_total_incl_vat", mat)
        self.assertIn("quoted_total_excl_vat", mat)
        self.assertNotIn("amount_est", mat)                                         # replaced by the two totals
        self.assertIn("amount_est", oth)
        self.assertNotIn("quoted_total_incl_vat", oth)                              # VAT plays no role there
        self.assertContains(self.client.get("/orders/"), "עדכון ממקור אחר")
        self.assertNotContains(self.client.get("/other/"), "עדכון ממקור אחר")
        # both pages can add a row, each to its own table
        mat, oth = self.client.get("/orders/"), self.client.get("/other/")
        self.assertContains(mat, 'data-open="add-orders"')
        self.assertContains(oth, 'data-open="add-orders_other"')
        self.assertContains(oth, 'action="/api/orders_other/new/"')
        self.assertContains(oth, 'name="amount_est"')                                # the typed amount, no VAT totals
        self.assertNotContains(oth, 'name="quoted_total_incl_vat"')

    def test_changing_the_category_moves_the_request_to_the_other_page(self):
        import json
        r = self.client.post(f"/api/orders/{self.mat.pk}/", json.dumps({"field": "category", "value": "OTHER"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.mat.pk, self.rows("/orders/"))
        self.assertIn(self.mat.pk, self.rows("/other/"))

    def test_recommendations_take_the_requests_of_both_pages(self):
        page = self.client.get("/resolve/")
        by_pk = {r["order"].pk: r for r in page.context["rows"]}
        self.assertEqual(set(by_pk), {self.mat.pk, self.nocat.pk, self.other.pk, self.travel.pk})
        self.assertEqual(by_pk[self.mat.pk]["amt"], Decimal("1180"))                # a material: the total with VAT
        self.assertEqual(by_pk[self.other.pk]["amt"], Decimal("800"))               # other: the typed amount
        self.assertTrue(all(r["state"] == "ok" for r in by_pk.values()))

    def test_recycling_returns_each_request_to_its_own_page(self):
        from budget import resolve
        resolve.approve_plans({self.mat.pk: "A", self.travel.pk: "A"}, today=TODAY)
        for pk in (self.mat.pk, self.travel.pk):
            self.assertNotIn(pk, self.rows("/orders/") | self.rows("/other/"))      # waiting in the recycle bin
        for pk in (self.mat.pk, self.travel.pk):
            a = Allocation.objects.get(order_id=pk)
            self.client.post(f"/resolve/recycle/{a.pk}/")
        self.assertIn(self.mat.pk, self.rows("/orders/"))
        self.assertIn(self.travel.pk, self.rows("/other/"))

    def test_delete_is_a_real_delete_even_for_a_labsuit_request(self):
        from budget import resolve
        self.assertEqual(self.mat.source, "labsuit")
        resolve.approve_plans({self.mat.pk: "A"}, today=TODAY)
        self.client.post(f"/api/orders/{self.mat.pk}/delete/")
        self.assertFalse(OrderRequest.objects.filter(pk=self.mat.pk).exists())
        self.assertFalse(Allocation.objects.exists())                               # its reservation was released
        self.client.post(f"/api/orders_other/{self.other.pk}/delete/")
        self.assertFalse(OrderRequest.objects.filter(pk=self.other.pk).exists())

    def test_new_rows_land_on_the_right_page(self):
        self.client.post("/api/orders/new/", {"item_name": "New antibody", "quoted_total_incl_vat": "118",
                                              "quoted_total_excl_vat": "100", "quoted_currency": "ILS"})
        self.client.post("/api/orders_other/new/", {"item_name": "New trip", "amount_est": "900",
                                                    "amount_currency": "ILS", "category": "TRAVEL_EXT"})
        m = OrderRequest.objects.get(item_name="New antibody")
        o = OrderRequest.objects.get(item_name="New trip")
        self.assertEqual((m.category_id, m.quoted_manually_corrected), ("MATERIAL", True))
        self.assertEqual((o.category_id, o.amount_est), ("TRAVEL_EXT", Decimal("900")))
        self.assertIn(m.pk, self.rows("/orders/"))
        self.assertIn(o.pk, self.rows("/other/"))

    # ---- the quote cells: just the number, a marker with a hover, and the currency in its own column

    def quote_cells(self, order):
        page = self.client.get("/orders/?year=all")
        row = next(r for r in page.context["t"]["rows"] if r["pk"] == order.pk)
        return page, {c["col"].field: c for c in row["cells"]}

    def test_quote_cells_have_a_hover_marker_not_an_expansion(self):
        page, cells = self.quote_cells(self.mat)                       # nothing extracted, nothing typed by hand
        OrderRequest.objects.filter(pk=self.mat.pk).update(quoted_manually_corrected=False)
        page, cells = self.quote_cells(self.mat)
        self.assertEqual(cells["quoted_total_incl_vat"]["mark_text"], "טרם חולץ")
        html = page.content.decode()
        table = html[html.index("<table"):html.index("</table>")]
        self.assertNotIn("<details", table)                              # nothing to expand
        self.assertNotIn("מטבע לא ידוע", html)
        # a manual edit, a failed lookup and a good extraction
        OrderRequest.objects.filter(pk=self.mat.pk).update(quoted_manually_corrected=True)
        self.assertEqual(self.quote_cells(self.mat)[1]["quoted_total_excl_vat"]["mark_text"], "נכתב ידנית")
        OrderRequest.objects.filter(pk=self.mat.pk).update(
            quoted_manually_corrected=False, extraction_status="missing", quoted_total_excl_vat=None)
        cells = self.quote_cells(self.mat)[1]
        self.assertEqual(cells["quoted_total_excl_vat"]["mark_text"], "לא נמצא בחילוץ")
        self.assertEqual(cells["quoted_total_incl_vat"]["mark"], "")      # this one was found: no marker
        OrderRequest.objects.filter(pk=self.mat.pk).update(extraction_status="success")
        self.assertEqual(self.quote_cells(self.mat)[1]["quoted_total_incl_vat"]["mark"], "")

    def test_currency_is_its_own_column_and_editable(self):
        import json
        page, cells = self.quote_cells(self.mat)
        fields = [c.field for c in page.context["t"]["cols"]]
        self.assertLess(fields.index("quoted_total_excl_vat"), fields.index("quoted_currency"))
        self.assertEqual(cells["quoted_currency"]["value"], "ILS")
        self.assertNotIn("currency", cells["quoted_total_incl_vat"])      # the number cells carry no currency text
        TrackedCurrency.objects.get_or_create(code="EUR")
        r = self.client.post(f"/api/orders/{self.mat.pk}/", json.dumps({"field": "quoted_currency", "value": "eur"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(OrderRequest.objects.get(pk=self.mat.pk).quoted_currency, "EUR")
        self.assertEqual(self.quote_cells(self.mat)[1]["quoted_currency"]["value"], "EUR")


# --------------------------------------------------------------------------- AI assist

@override_settings(FX_AUTO_REFRESH=False)
class InstructionConstraintsTests(TestCase):
    """`compute(constraints=…)`: what the typed instructions do to the recommendation (no AI involved)."""

    def setUp(self):
        FxRate.objects.create(date=TODAY, currency="EUR", rate_to_ils=Decimal("3.4422"))
        FxRate.objects.create(date=TODAY, currency="USD", rate_to_ils=Decimal("3.078"))
        batch = ImportBatch.objects.create(source="sap_grants", file_name="t", as_of=TODAY)

        def grant(num, ccy, end, total, lines, **kw):
            g = Grant.objects.create(number=num, currency=ccy, end_date=end, balance_total=Decimal(total),
                                     last_batch=batch, **kw)
            for cat, bal in lines.items():
                GrantLine.objects.create(batch=batch, grant=g, category_id=cat, balance=Decimal(bal))
            return g

        self.soon = grant("A", "EUR", date(2026, 12, 31), "1000", {"MATERIAL": "1000", "SALARY": "1000"},
                          sponsor="Horizon")
        self.reserve = grant("R", "USD", date(2029, 12, 31), "100000", {"GENERAL": "100000"}, is_reserve=True)
        self.later = grant("L", "ILS", date(2027, 9, 30), "50000", {"MATERIAL": "50000", "SALARY": "50000"},
                           sponsor="Innovation Authority", vat_required=False)
        self.n = 0

    def order(self, amount="1000", category="MATERIAL"):
        self.n += 1
        return OrderRequest.objects.create(
            natural_key=f"k{self.n}", item_name=f"item {self.n}", status="approved", grant_label="General Budget",
            amount_est=Decimal(amount), amount_currency="ILS", category_id=category, amount_source="manual",
            date_requested=TODAY)

    def plan(self, result, order):
        return next(p for p in result.plans if p.cand.order.pk == order.pk)

    def grant_of(self, result, order):
        return self.plan(result, order).lines[0].grant.number

    def test_baseline_small_request_goes_to_the_expiring_grant(self):
        o = self.order()
        self.assertEqual(self.grant_of(resolve.compute(today=TODAY), o), "A")

    def test_exclude_grant_keeps_it_out_but_selectable_and_ranked_last(self):
        o = self.order()
        r = resolve.compute(today=TODAY, constraints=[{"type": "exclude_grant", "grant": "A"}])
        self.assertNotEqual(self.grant_of(r, o), "A")
        opts = self.plan(r, o).options
        a = next(x for x in opts if x.state.grant.number == "A")
        self.assertTrue(a.blocked)                                   # still offered in the page's select…
        self.assertEqual(opts[-1].state.grant.number, "A")           # …but ranked after the usable ones
        self.assertNotIn("הבא בתור: A", self.plan(r, o).lines[0].reason)

    def test_avoid_grant_is_used_only_when_nothing_else_fits(self):
        o = self.order()
        r = resolve.compute(today=TODAY, constraints=[{"type": "avoid_grant", "grant": "A"}])
        self.assertNotEqual(self.grant_of(r, o), "A")
        # with every other grant banned for the category, A is still used — and the reason says so
        GrantRule.objects.create(grant=self.reserve, category_id="MATERIAL", allowed=False)
        GrantRule.objects.create(grant=self.later, category_id="MATERIAL", allowed=False)
        r = resolve.compute(today=TODAY, constraints=[{"type": "avoid_grant", "grant": "A"}])
        self.assertEqual(self.grant_of(r, o), "A")
        self.assertIn("ביקשת להימנע מהמענק הזה", self.plan(r, o).lines[0].reason)

    def test_reserve_amount_keeps_money_in_the_grant(self):
        o = self.order()                                             # ≈ 300 EUR: fits A (1000 EUR) normally
        self.assertEqual(self.grant_of(resolve.compute(today=TODAY), o), "A")
        c = [{"type": "reserve_amount", "grant": "A", "keep": 900, "currency": "EUR"}]
        r = resolve.compute(today=TODAY, constraints=c)
        self.assertNotEqual(self.grant_of(r, o), "A")                # only 100 EUR may be used
        self.assertEqual(r.held, {"A": Decimal("900.00")})
        # the amount may be given in another currency: converted at today's rate
        c = [{"type": "reserve_amount", "grant": "A", "keep": 3098.0, "currency": "ILS"}]   # ≈ 900 EUR
        self.assertNotEqual(self.grant_of(resolve.compute(today=TODAY, constraints=c), o), "A")

    def test_pin_and_forbid(self):
        o = self.order()
        r = resolve.compute(today=TODAY, constraints=[{"type": "pin", "order": o.pk, "grant": "R"}])
        self.assertEqual(self.grant_of(r, o), "R")
        self.assertIn("הוצמד למענק הזה לפי הוראתך", self.plan(r, o).lines[0].reason)
        r = resolve.compute(today=TODAY, constraints=[{"type": "forbid", "order": o.pk, "grant": "A"}])
        self.assertNotEqual(self.grant_of(r, o), "A")

    def test_instructions_that_cannot_apply_are_reported_not_applied(self):
        o = self.order()
        r = resolve.compute(today=TODAY, constraints=[
            {"type": "exclude_grant", "grant": "NOPE"},
            {"type": "pin", "order": 999999, "grant": "A"},
            {"type": "pin", "order": o.pk, "grant": "A"},
            {"type": "exclude_grant", "grant": "A"}])
        self.assertEqual(len(r.notes), 3)                            # unknown grant, unknown request, pin vs exclude
        self.assertNotEqual(self.grant_of(r, o), "A")

    def test_category_rules_use_grants_that_do_not_pay_vat_for_salary(self):
        salary, material = self.order("1000", "SALARY"), self.order("1000", "MATERIAL")
        base = resolve.compute(today=TODAY)
        self.assertEqual(self.grant_of(base, salary), "A")           # the expiring grant, as usual
        prefer = [{"type": "prefer_for_category", "category": "SALARY", "vat": "free", "strength": "strong"}]
        r = resolve.compute(today=TODAY, constraints=prefer)
        self.assertEqual(self.grant_of(r, salary), "L")              # L is the grant that does not pay VAT
        self.assertEqual(self.grant_of(r, material), self.grant_of(base, material))   # other categories untouched
        require = [{"type": "require_for_category", "category": "SALARY", "vat": "free"}]
        r = resolve.compute(today=TODAY, constraints=require)
        self.assertEqual(self.grant_of(r, salary), "L")
        blocked = next(o for o in self.plan(r, salary).options if o.state.grant.number == "A")
        self.assertTrue(blocked.blocked)
        # a rule that no active grant can satisfy is reported and ignored (it would leave the category unfunded)
        r = resolve.compute(today=TODAY, constraints=[
            {"type": "require_for_category", "category": "SALARY", "sponsor": "Nobody"}])
        self.assertEqual(len(r.notes), 1)
        self.assertEqual(self.plan(r, salary).state, "ok")

    def test_require_for_category_can_leave_a_request_unfunded_with_a_clear_reason(self):
        salary = self.order("1000", "SALARY")
        r = resolve.compute(today=TODAY, constraints=[              # the only VAT-free grant is excluded as well
            {"type": "require_for_category", "category": "SALARY", "vat": "free"},
            {"type": "exclude_grant", "grant": "L"}])
        plan = self.plan(r, salary)
        self.assertEqual(plan.state, "unfunded")
        self.assertIn("הוחרגו או נחסמו", plan.lines[0].reason)

    def test_currency_order_weak_or_strong_first(self):
        o = self.order("1000")
        weak = resolve.compute(today=TODAY, constraints=[{"type": "currency_order", "direction": "weak_first",
                                                          "strength": "strong"}])
        self.assertEqual(self.grant_of(weak, o), "L")                # ₪ is the least valuable currency here
        strong = resolve.compute(today=TODAY, constraints=[{"type": "currency_order", "direction": "strong_first",
                                                            "strength": "strong"}])
        self.assertEqual(self.grant_of(strong, o), "A")              # € (3.44 ₪) beats $ (3.08 ₪)

    def test_unticked_requests_are_left_out_and_use_no_balance(self):
        o1, o2 = self.order("3000"), self.order("3000")              # only one of them fits A (≈ 1,000 €)
        both = resolve.compute(today=TODAY)
        self.assertEqual(sorted(self.grant_of(both, o) for o in (o1, o2)), ["A", "L"])
        only = resolve.compute(today=TODAY, skip_orders=[o2.pk])
        self.assertEqual([p.cand.order.pk for p in only.plans], [o1.pk])
        self.assertEqual(self.grant_of(only, o1), "A")
        self.assertEqual(only.user_skipped, {o2.pk})
        self.assertIn(o2.pk, [o.pk for o, _ in only.skipped])

    def test_nothing_is_written(self):
        self.order()
        before = (Allocation.objects.count(), ResolveRun.objects.count())
        resolve.compute(today=TODAY, constraints=[{"type": "exclude_grant", "grant": "A"}])
        self.assertEqual((Allocation.objects.count(), ResolveRun.objects.count()), before)


@override_settings(FX_AUTO_REFRESH=False)
class AiAssistTests(TestCase):
    """The model call, validation and the page flow. Bedrock is mocked; nothing here needs a key."""

    ENV = {"AWS_BEARER_TOKEN_BEDROCK": "test-key", "AWS_REGION": "eu-west-1"}

    def setUp(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        FxRate.objects.create(date=timezone_today(), currency="EUR", rate_to_ils=Decimal("3.4422"))
        batch = ImportBatch.objects.create(source="sap_grants", file_name="t", as_of=timezone_today())
        for num, sponsor in (("A", "Horizon"), ("B", "Israel Innovation Authority")):
            g = Grant.objects.create(number=num, currency="EUR", end_date=date(2099, 6, 1), sponsor=sponsor,
                                     description=f"{sponsor} grant", balance_total=Decimal("5000"), last_batch=batch)
            GrantLine.objects.create(batch=batch, grant=g, category_id="MATERIAL", balance=Decimal("5000"))
        self.order = OrderRequest.objects.create(
            natural_key="k", item_name="Pipettes", status="approved", amount_est=Decimal("100"),
            amount_currency="EUR", category_id="MATERIAL", amount_source="manual", date_requested=timezone_today())

    # ---- the model call

    def converse_reply(self, obj, stop="end_turn"):
        import json as _json

        class R:
            status_code = 200
            headers = {}
            def json(self_inner):
                return {"output": {"message": {"role": "assistant", "content": [{"text": _json.dumps(obj)}]}},
                        "stopReason": stop, "usage": {"inputTokens": 1, "outputTokens": 1}}
        return R()

    def test_request_is_made_like_the_pdf_review(self):
        from budget import ai_assist
        with mock.patch.dict("os.environ", self.ENV, clear=True), \
                mock.patch("budget.quote_bedrock._post_once", return_value=self.converse_reply({"constraints": []})) as post:
            out = ai_assist.ask("x" * 1000, "CURRENT:\nnone")
        self.assertEqual(out, {"constraints": []})
        url = post.call_args.args[0]
        body = post.call_args.kwargs["json"]
        self.assertTrue(url.startswith("https://bedrock-runtime.eu-west-1.amazonaws.com/model/"))
        self.assertIn("global.anthropic.claude-haiku-5-5/converse", url)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(body["inferenceConfig"], {"maxTokens": 600})              # no temperature
        self.assertNotIn("toolConfig", body)                                       # JSON is asked for in the prompt
        self.assertEqual(body["additionalModelRequestFields"], {"thinking": {"type": "disabled"}})
        sent = body["messages"][0]["content"][0]["text"]
        self.assertLessEqual(len(sent.split("INSTRUCTION:\n")[1]), ai_assist.MAX_INSTRUCTION_CHARS)
        self.assertIn("exclude_grant", body["system"][0]["text"])

    def test_model_id_comes_from_the_same_variable_as_the_pdf_review(self):
        from budget import ai_assist
        env = {**self.ENV, "BEDROCK_MODEL_ID": "eu.anthropic.claude-haiku-5-5"}
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch("budget.quote_bedrock._post_once", return_value=self.converse_reply({"constraints": []})) as post:
            ai_assist.ask("hi", "ctx")
        self.assertIn("eu.anthropic.claude-haiku-5-5/converse", post.call_args.args[0])

    def test_reply_in_a_markdown_fence_is_accepted_and_truncation_is_not(self):
        from budget import ai_assist
        class Fenced:
            status_code = 200
            headers = {}
            def json(self):
                return {"output": {"message": {"role": "assistant", "content": [
                    {"text": "```json\n{\"constraints\": [], \"summary_he\": \"ok\"}\n```"}]}}, "stopReason": "end_turn"}
        with mock.patch.dict("os.environ", self.ENV, clear=True), mock.patch("budget.quote_bedrock._post_once", return_value=Fenced()):
            self.assertEqual(ai_assist.ask("hi", "ctx")["summary_he"], "ok")
        with mock.patch.dict("os.environ", self.ENV, clear=True), \
                mock.patch("budget.quote_bedrock._post_once", return_value=self.converse_reply({}, stop="max_tokens")):
            with self.assertRaises(ai_assist.AiError):
                ai_assist.ask("hi", "ctx")

    def test_http_error_is_reported_with_the_message(self):
        from budget import ai_assist
        class R:
            status_code = 400
            text = ""
            def json(self):
                return {"message": "The provided model identifier is invalid."}
        with mock.patch.dict("os.environ", self.ENV), mock.patch("budget.quote_bedrock._post_once", return_value=R()):
            with self.assertRaises(ai_assist.AiError) as cm:
                ai_assist.ask("hi", "ctx")
        self.assertIn("400", str(cm.exception))
        self.assertIn("model identifier", str(cm.exception))

    def test_capitalised_message_and_hint_on_403(self):
        from budget import ai_assist
        class R:
            status_code = 403
            text = ""
            headers = {"x-amzn-errortype": "AccessDeniedException:http://internal.amazon.com/"}
            def json(self):
                return {"Message": "Bearer Token has expired"}
        with mock.patch.dict("os.environ", self.ENV), mock.patch("budget.quote_bedrock._post_once", return_value=R()):
            with self.assertRaises(ai_assist.AiError) as cm:
                ai_assist.ask("hi", "ctx")
        text = str(cm.exception)
        self.assertIn("403 AccessDeniedException", text)
        self.assertIn("Bearer Token has expired", text)
        self.assertIn("bedrock:InvokeModel", text)
        self.assertNotIn("test-key", text)

    def test_key_is_cleaned_of_quotes_spaces_and_a_bearer_prefix(self):
        from budget import ai_assist
        for pasted in ('  "abc123" \n', "Bearer abc123", "abc123"):
            with mock.patch.dict("os.environ", {**self.ENV, "AWS_BEARER_TOKEN_BEDROCK": pasted}), \
                    mock.patch("budget.quote_bedrock._post_once", return_value=self.converse_reply({"constraints": []})) as post:
                ai_assist.ask("hi", "ctx")
            self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer abc123")

    def test_validation_drops_what_was_not_sent_and_merge_replaces_earlier_instructions(self):
        from budget import ai_assist
        valid, dropped = ai_assist.validate({"constraints": [
            {"type": "exclude_grant", "grant": "A"},
            {"type": "exclude_grant", "grant": "ZZZ"},                      # not a grant we sent
            {"type": "pin", "order": 7, "grant": "A"},                       # not a request we sent
            {"type": "reserve_amount", "grant": "B", "keep": -5},            # nonsense amount
            {"type": "reserve_amount", "grant": "B", "keep": 100},           # currency defaults to the grant's
            {"type": "format_disk"},
        ]}, {"A": "EUR", "B": "ILS"}, {1, 2})
        self.assertEqual(valid, [{"type": "exclude_grant", "grant": "A"},
                                 {"type": "reserve_amount", "grant": "B", "keep": 100.0, "currency": "ILS"}])
        self.assertEqual(len(dropped), 4)
        merged = ai_assist.merge(valid, [{"type": "prefer_grant", "grant": "A"},
                                         {"type": "reserve_amount", "grant": "B", "keep": 50.0, "currency": "ILS"}])
        self.assertEqual(merged, [{"type": "prefer_grant", "grant": "A"},
                                  {"type": "reserve_amount", "grant": "B", "keep": 50.0, "currency": "ILS"}])
        self.assertEqual(ai_assist.merge(merged, [{"type": "clear"}]), [])

    def test_new_vocabulary_is_validated_and_sponsors_are_expanded(self):
        from budget import ai_assist
        grants = {"A": "EUR", "B": "EUR", "C": "USD"}
        sponsors = {"horizon": ("Horizon", ["A", "B"]), "nih": ("NIH", ["C"])}
        cats = {"SALARY": "שכר", "MATERIAL": "חומרים"}
        valid, dropped = ai_assist.validate({"constraints": [
            {"type": "exclude_grant", "sponsor": "Horizon"},                                  # → A and B
            {"type": "prefer_for_category", "category": "SALARY", "vat": "free", "strength": "strong"},
            {"type": "require_for_category", "category": "SALARY", "sponsor": "nih"},
            {"type": "currency_order", "direction": "weak_first"},
            {"type": "prefer_for_category", "category": "NOPE", "vat": "free"},               # unknown category
            {"type": "prefer_for_category", "category": "SALARY"},                            # no selector
            {"type": "currency_order", "direction": "sideways"},
            {"type": "pin", "sponsor": "Horizon", "order": 1},                                # pin takes one grant
        ]}, grants, {1}, cats, sponsors)
        self.assertEqual(valid, [
            {"type": "exclude_grant", "grant": "A"}, {"type": "exclude_grant", "grant": "B"},
            {"type": "prefer_for_category", "category": "SALARY", "vat": "free", "strength": "strong"},
            {"type": "require_for_category", "category": "SALARY", "sponsor": "NIH"},
            {"type": "currency_order", "direction": "weak_first", "strength": "soft"}])
        self.assertEqual(len(dropped), 4)
        # a later rule on the same category/selector replaces the earlier one
        merged = ai_assist.merge(valid, [{"type": "prefer_for_category", "category": "SALARY", "vat": "free",
                                          "strength": "soft"},
                                         {"type": "currency_order", "direction": "strong_first", "strength": "soft"}])
        self.assertEqual(sum(c["type"] == "prefer_for_category" for c in merged), 1)
        self.assertEqual([c for c in merged if c["type"] == "currency_order"],
                         [{"type": "currency_order", "direction": "strong_first", "strength": "soft"}])

    def test_prompt_knows_the_new_instructions_and_the_expiry_limitation(self):
        from budget import ai_assist
        for word in ("prefer_for_category", "require_for_category", "currency_order", "weak_first"):
            self.assertIn(word, ai_assist.SYSTEM_PROMPT)
        self.assertIn(ai_assist.EXPIRY_UNSUPPORTED, ai_assist.SYSTEM_PROMPT)

    def test_context_is_compact_and_lists_only_what_exists(self):
        from budget import ai_assist
        result = resolve.compute()
        ctx = ai_assist.build_context(result, [])
        self.assertEqual(ctx.grants, {"A": "EUR", "B": "EUR"})
        self.assertEqual(ctx.orders, {self.order.pk})
        self.assertIn("G|B|Israel Innovation Authority|", ctx.text)
        self.assertIn(f"R|{self.order.pk}|Pipettes|", ctx.text)
        self.assertIn("C|SALARY|", ctx.text)                                  # categories the model may name
        self.assertEqual(ctx.sponsors["horizon"], ("Horizon", ["A"]))

    # ---- the page

    def post(self, text="", **extra):
        return self.client.post("/resolve/ai/", {"instruction": text, **extra}, follow=True)

    def test_instruction_changes_the_page_and_nothing_in_the_database(self):
        before = (Allocation.objects.count(), Grant.objects.get(pk="B").balance_total)
        reply = {"constraints": [{"type": "exclude_grant", "grant": "B"}], "summary_he": "לא אשתמש ברשות החדשנות."}
        with mock.patch.dict("os.environ", self.ENV), mock.patch("budget.ai_assist.ask", return_value=reply):
            page = self.post("בלי הרשות לחדשנות")
        self.assertContains(page, "לא להשתמש במענק B · Israel Innovation Authority")      # the chip
        self.assertContains(page, "לא אשתמש ברשות החדשנות.")
        self.assertEqual(self.client.session["ai_constraints"], [{"type": "exclude_grant", "grant": "B"}])
        row = page.context["rows"][0]
        self.assertEqual(row["lines"][0].grant.number, "A")
        self.assertTrue(next(o for o in row["options"] if o["num"] == "B")["blocked"])
        self.assertEqual((Allocation.objects.count(), Grant.objects.get(pk="B").balance_total), before)
        # the chip's ✕ removes it, and "אפס הכול" clears everything
        page = self.post(remove="0")
        self.assertEqual(self.client.session["ai_constraints"], [])
        with mock.patch.dict("os.environ", self.ENV), mock.patch("budget.ai_assist.ask", return_value=reply):
            self.post("again")
        self.post(clear="1")
        self.assertNotIn("ai_constraints", self.client.session)

    def test_unticked_requests_are_left_out_of_the_recalculation(self):
        second = OrderRequest.objects.create(
            natural_key="k2", item_name="Gloves", status="approved", amount_est=Decimal("50"),
            amount_currency="EUR", category_id="MATERIAL", amount_source="manual", date_requested=timezone_today())
        seen = {}

        def fake_ask(text, ctx):
            seen["ctx"] = ctx
            return {"constraints": [], "summary_he": ""}

        with mock.patch.dict("os.environ", self.ENV), mock.patch("budget.ai_assist.ask", side_effect=fake_ask):
            self.post("anything", selection="1", skip=str(second.pk))
        self.assertNotIn(f"R|{second.pk}|", seen["ctx"])                      # the model is not shown it either
        self.assertIn(f"R|{self.order.pk}|", seen["ctx"])
        self.assertEqual(self.client.session["ai_skip"], [second.pk])
        page = self.client.get("/resolve/")
        self.assertEqual([r["order"].pk for r in page.context["rows"] if r["state"] == "ok"], [self.order.pk])
        row = next(r for r in page.context["rows"] if r["order"].pk == second.pk)
        self.assertTrue(row["user_skipped"])
        self.assertContains(page, 'class="pick-skipped"')
        self.assertContains(page, "מחוץ לחישוב")
        # recalculating without the model, with the second request ticked again, brings it back
        with mock.patch("budget.ai_assist.ask", side_effect=AssertionError("no model call expected")):
            self.post(recalc="1", selection="1")
        self.assertNotIn("ai_skip", self.client.session)
        page = self.client.get("/resolve/")
        self.assertEqual(sorted(r["order"].pk for r in page.context["rows"] if r["state"] == "ok"),
                         sorted([self.order.pk, second.pk]))
        # the chip's ✕ ("unskip") also restores them all
        self.post(selection="1", skip=str(second.pk), recalc="1")
        self.assertEqual(self.client.session["ai_skip"], [second.pk])
        self.post(unskip="1")
        self.assertNotIn("ai_skip", self.client.session)

    def test_clarifying_question_is_shown_and_changes_nothing(self):
        reply = {"constraints": [], "summary_he": "", "clarify": "איזה מענק הכוונה: A או B?"}
        with mock.patch.dict("os.environ", self.ENV), mock.patch("budget.ai_assist.ask", return_value=reply):
            page = self.post("בלי המענק האירופי")
        self.assertContains(page, "איזה מענק הכוונה: A או B?")
        self.assertNotIn("ai_constraints", self.client.session)

    def test_unknown_ids_from_the_model_are_ignored(self):
        reply = {"constraints": [{"type": "exclude_grant", "grant": "HALLUCINATED"}], "summary_he": "x"}
        with mock.patch.dict("os.environ", self.ENV), mock.patch("budget.ai_assist.ask", return_value=reply):
            self.post("whatever")
        self.assertNotIn("ai_constraints", self.client.session)

    def test_not_configured_disables_the_box_and_the_post_explains(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            page = self.client.get("/resolve/")
            self.assertContains(page, "AWS_BEARER_TOKEN_BEDROCK")
            self.assertFalse(page.context["ai_ready"])
            page = self.post("hi")
        self.assertContains(page, "ה-AI לא מוגדר")

    def test_rate_limit(self):
        from budget import ai_assist
        reply = {"constraints": [], "summary_he": ""}
        with mock.patch.dict("os.environ", self.ENV), mock.patch("budget.ai_assist.ask", return_value=reply) as ask:
            for _ in range(ai_assist.CALLS_PER_HOUR):
                self.post("x")
            page = self.post("one too many")
        self.assertEqual(ask.call_count, ai_assist.CALLS_PER_HOUR)
        self.assertContains(page, "מגבלה")
