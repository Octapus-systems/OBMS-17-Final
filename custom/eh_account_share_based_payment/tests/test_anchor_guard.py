# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""recognised_cumulative is a posted-figure anchor, not a free field.

eh.sbp.plan.recognised_cumulative is the cumulative IFRS 2 expense (and, for
a cash-settled plan, the IFRS 2.45 liability carrying amount) that the period
engine trues up against: the next run charges ``target - recognised_cumulative``.
A ``readonly`` widget does not stop an ORM/RPC write, so a low-privilege user
with model write access could restate the anchor and make the next manager-run
double-charge the expense (or suppress a legitimate true-up), with no audit
trail. recognised_cumulative is therefore guarded by eh.workflow.guard.

Two directions are asserted:

* a non-superuser user cannot RPC-write the anchor (the guard fires only when
  env.su is False, so the write must be attempted with_user a normal user);
* the sanctioned action paths (period-run Post, Cancel acceleration, cash
  Settle) still set the anchor when run by a real, non-superuser manager -
  the writes were routed through su, so the guard retrofit does not lock the
  authorised user out of the legitimate flow.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_share_based_payment', 'post_install', '-at_install')
class TestRecognisedCumulativeAnchorGuard(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The default test env runs as superuser; add the manager group so the
        # superuser-driven setup can activate plans and post runs.
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.sbp_expense = cls._ensure_account(
            cls.env, '6150', 'Share-based Payment Expense', 'expense')
        cls.sbp_reserve = cls._ensure_account(
            cls.env, '3150', 'SBP Equity Reserve', 'equity')
        cls.sbp_liability = cls._ensure_account(
            cls.env, '2350', 'SBP Liability', 'liability_current')

        cls._user_error = None
        # A non-superuser with only the EH *user* group: has model write
        # access (perm_write=1) but is not a manager, so it is the exact actor
        # the anchor guard must stop from restating the figure.
        cls.normal_user = cls._make_user(
            'sbp_anchor_user', 'eh_account_base.group_eh_user')
        # A non-superuser *manager*: authorised to run the actions. Used to
        # prove the sudo-routed action writes are not refused by the guard.
        cls.manager_user = cls._make_user(
            'sbp_anchor_mgr', 'eh_account_base.group_eh_manager')

    @classmethod
    def _make_user(cls, login, group_xmlid):
        try:
            return cls.env['res.users'].with_context(
                mail_create_nosubscribe=True,
                no_reset_password=True,
            ).create({
                'name': login,
                'login': login,
                # A sender address so Odoo 16 can post the tracking chatter the
                # actions raise (17+ tolerate a missing author email).
                'email': '%s@example.com' % login,
                'company_id': cls.company.id,
                'groups_id': [(6, 0, [cls.env.ref(group_xmlid).id])],
            })
        except Exception as exc:  # pragma: no cover - env cascade quirk
            cls._user_error = exc
            return cls.env['res.users']

    def _require(self, user):
        if not user:
            self.skipTest(
                "environment cannot create a test user: %s"
                % getattr(self, '_user_error', 'unknown'))

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------
    def _equity_plan(self, **vals):
        base = {
            'name': '/',
            'settlement': 'equity',
            'condition_kind': 'service',
            'grant_date': '2026-01-01',
            'vesting_years': 3,
            'expense_account_id': self.sbp_expense.id,
            'equity_account_id': self.sbp_reserve.id,
            'liability_account_id': self.sbp_liability.id,
            'settlement_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, {
                'partner_id': self.partner_a.id,
                'instruments_granted': 300,
                'grant_date_fair_value': 10.0,
            })],
        }
        base.update(vals)
        plan = self.env['eh.sbp.plan'].create(base)
        plan.action_activate()
        return plan

    # ------------------------------------------------------------------
    # negative: the anchor cannot be restated by a direct write
    # ------------------------------------------------------------------
    def test_anchor_write_refused_for_low_privilege_user(self):
        """After a run posts, a non-superuser cannot RPC-write the anchor."""
        self._require(self.normal_user)
        plan = self._equity_plan()
        # Post a run so the anchor carries a real booked figure:
        # 300 x 10.00 x 12/36 = 1,000.00.
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01'})
        run.action_post()
        booked = plan.recognised_cumulative
        self.assertAlmostEqual(booked, 1000.00, places=2,
                               msg='the posted run must book 1,000.00')

        with self.assertRaises(AccessError):
            plan.with_user(self.normal_user).write(
                {'recognised_cumulative': 0.0})

        plan.invalidate_recordset(['recognised_cumulative'])
        self.assertAlmostEqual(
            plan.recognised_cumulative, booked, places=2,
            msg='the booked anchor must be unchanged after a refused write')

    def test_anchor_write_refused_even_on_draft_plan(self):
        """The guard is unconditional: even before any run posts, a
        low-privilege user cannot seed the anchor by a direct write."""
        self._require(self.normal_user)
        plan = self._equity_plan()
        with self.assertRaises(AccessError):
            plan.with_user(self.normal_user).write(
                {'recognised_cumulative': 5000.0})

    # ------------------------------------------------------------------
    # positive: the sanctioned actions still set the anchor as a
    # real (non-superuser) manager
    # ------------------------------------------------------------------
    def test_run_post_sets_anchor_as_non_superuser_manager(self):
        self._require(self.manager_user)
        plan = self._equity_plan()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01'})
        run.with_user(self.manager_user).action_post()
        plan.invalidate_recordset(['recognised_cumulative'])
        self.assertAlmostEqual(
            plan.recognised_cumulative, 1000.00, places=2,
            msg='a non-superuser manager post must set the anchor via su')

    def test_cancel_acceleration_sets_anchor_as_non_superuser_manager(self):
        self._require(self.manager_user)
        plan = self._equity_plan()
        # Cancellation accelerates the full unrecognised balance immediately
        # (IFRS 2.28(a)): 300 x 10.00 = 3,000.00.
        plan.with_user(self.manager_user).action_cancel()
        plan.invalidate_recordset(['recognised_cumulative'])
        self.assertEqual(plan.state, 'cancelled')
        self.assertAlmostEqual(
            plan.recognised_cumulative, 3000.00, places=2,
            msg='cancellation acceleration must set the anchor via su')

    def test_cash_settle_clears_anchor_as_non_superuser_manager(self):
        self._require(self.manager_user)
        plan = self._equity_plan(
            settlement='cash', settlement_amount=1200.0)
        # Post a cash run so a liability is carried:
        # 300 x 12.00 x 12/36 = 1,200.00.
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01',
            'current_fair_value': 12.0})
        run.action_post()
        self.assertAlmostEqual(plan.recognised_cumulative, 1200.00, places=2)
        # Settle trues the liability to the settlement amount and pays it,
        # then zeroes the anchor (IFRS 2.30) - all under su.
        plan.with_user(self.manager_user).action_settle()
        plan.invalidate_recordset(['recognised_cumulative'])
        self.assertEqual(plan.state, 'settled')
        self.assertAlmostEqual(
            plan.recognised_cumulative, 0.0, places=2,
            msg='cash settlement must clear the anchor via su')
