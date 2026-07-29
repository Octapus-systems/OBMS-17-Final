# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 15 revenue recognition tests."""

from dateutil.relativedelta import relativedelta

from odoo.exceptions import UserError
from odoo.fields import Date
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_revenue', 'integration', 'post_install', '-at_install')
class TestRevenue(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.contract_asset_acc = cls._ensure_account(
            cls.env, '1350', 'Contract Asset', 'asset_current')
        cls.contract_liab_acc = cls._ensure_account(
            cls.env, '2350', 'Contract Liability', 'liability_current')

    def _contract(self, price=1000.0, obligations=None):
        c = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': price,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, o) for o in (obligations or [])],
        })
        return c

    def _bal(self, account):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', account.id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    def test_allocation_by_ssp(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Licence', 'standalone_price': 600.0},
            {'name': 'Support', 'standalone_price': 400.0},
        ])
        obs = c.obligation_ids.sorted('standalone_price', reverse=True)
        self.assertAlmostEqual(obs[0].allocated_price, 600.0, places=2)
        self.assertAlmostEqual(obs[1].allocated_price, 400.0, places=2)

    def test_point_in_time_recognition(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Licence', 'standalone_price': 600.0},
            {'name': 'Support', 'standalone_price': 400.0},
        ])
        c.action_activate()
        licence = c.obligation_ids.filtered(lambda o: o.name == 'Licence')
        licence.satisfied = True
        c.action_recognise()
        self.assertAlmostEqual(c.amount_recognised, 600.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_revenue), -600.0, places=2)
        self.assertAlmostEqual(self._bal(self.contract_asset_acc), 600.0,
                               places=2)
        self.assertAlmostEqual(c.contract_asset, 600.0, places=2)

    def test_over_time_recognition_by_progress(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 40.0},
        ])
        c.action_activate()
        c.action_recognise()
        self.assertAlmostEqual(c.amount_recognised, 400.0, places=2)
        # Progress to 70%: increment 300.
        c.obligation_ids.percent_complete = 70.0
        c.action_recognise()
        self.assertAlmostEqual(c.amount_recognised, 700.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_revenue), -700.0,
                               places=2)

    def test_billing_ahead_creates_liability(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time'},
        ])
        c.action_activate()
        c.bill_amount = 300.0
        c.action_bill()
        self.assertAlmostEqual(c.amount_billed, 300.0, places=2)
        self.assertAlmostEqual(c.contract_liability, 300.0, places=2)
        self.assertAlmostEqual(self._bal(self.contract_liab_acc), -300.0,
                               places=2)
        self.assertAlmostEqual(self._bal(self.account_receivable), 300.0,
                               places=2)
        # Recognise 400: releases the 300 liability, remainder 100 to asset.
        c.obligation_ids.percent_complete = 40.0
        c.action_recognise()
        self.assertAlmostEqual(self._bal(self.contract_liab_acc), 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.contract_asset_acc), 100.0,
                               places=2)

    def test_recognise_then_bill_clears_asset(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 50.0},
        ])
        c.action_activate()
        c.action_recognise()
        self.assertAlmostEqual(c.contract_asset, 500.0, places=2)
        c.bill_amount = 500.0
        c.action_bill()
        self.assertAlmostEqual(c.contract_asset, 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.contract_asset_acc), 0.0,
                               places=2)

    def test_entries_balance(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 30.0},
        ])
        c.action_activate()
        c.action_recognise()
        for move in c.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_recognise_requires_manager(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'x', 'standalone_price': 1000.0, 'satisfied': True},
        ])
        c.action_activate()
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'rev_plain@test', 'email': 'rev_plain@test',
            'groups_id': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            c.with_user(user).action_recognise()

    def test_activate_requires_obligations(self):
        c = self._contract(price=1000.0, obligations=[])
        with self.assertRaises(UserError):
            c.action_activate()

    def test_downward_correction_posts_balanced_reversal(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 60.0},
        ])
        c.action_activate()
        c.action_recognise()
        self.assertAlmostEqual(c.amount_recognised, 600.0, places=2)
        move_count = len(c.move_ids)
        # Correct the progress down to 40%: recognised should fall to 400 and a
        # balanced reversing entry (debit revenue, credit contract asset) posts.
        c.obligation_ids.percent_complete = 40.0
        c.action_recognise()
        self.assertAlmostEqual(c.amount_recognised, 400.0, places=2)
        self.assertEqual(len(c.move_ids), move_count + 1)
        self.assertAlmostEqual(self._bal(self.account_revenue), -400.0,
                               places=2)
        self.assertAlmostEqual(self._bal(self.contract_asset_acc), 400.0,
                               places=2)
        for move in c.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)
        # The correction move debits revenue (reduces recognised revenue).
        correction = c.move_ids.sorted('id')[-1]
        rev_lines = correction.line_ids.filtered(
            lambda l: l.account_id == self.account_revenue)
        self.assertAlmostEqual(sum(rev_lines.mapped('debit')), 200.0, places=2)

    def test_downward_correction_requires_manager(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 60.0},
        ])
        c.action_activate()
        c.action_recognise()
        c.obligation_ids.percent_complete = 40.0
        user = self.env['res.users'].create({
            'name': 'p2', 'login': 'rev_plain2@test',
            'email': 'rev_plain2@test',
            'groups_id': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            c.with_user(user).action_recognise()

    def test_standalone_price_frozen_after_posting(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 50.0},
        ])
        c.action_activate()
        c.action_recognise()
        with self.assertRaises(UserError):
            c.obligation_ids.standalone_price = 2000.0

    def test_contract_accounts_frozen_after_posting(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 50.0},
        ])
        c.action_activate()
        c.action_recognise()
        other = self._ensure_account(
            self.env, '4001', 'Other Revenue', 'income')
        with self.assertRaises(UserError):
            c.revenue_account_id = other

    # ------------------------------------------------------------------
    # (1) Variable consideration + constraint (IFRS 15.50-59)
    # ------------------------------------------------------------------

    def test_variable_consideration_constrained_into_price(self):
        # A performance bonus estimated at 200 but only 120 is highly probable
        # not to reverse: the constraint caps the amount added to the allocated
        # price at 120, not 200 (IFRS 15.56). Without the feature the allocated
        # price would be the plain 1000.
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Service', 'standalone_price': 1000.0,
             'satisfaction': 'point_in_time',
             'variable_consideration': True,
             'variable_method': 'expected_value',
             'variable_estimate': 200.0,
             'variable_constraint': 120.0},
        ])
        ob = c.obligation_ids
        self.assertAlmostEqual(ob.variable_included, 120.0, places=2)
        self.assertAlmostEqual(ob.allocated_price, 1120.0, places=2)
        c.action_activate()
        ob.satisfied = True
        c.action_recognise()
        # Revenue recognised includes the constrained variable amount.
        self.assertAlmostEqual(c.amount_recognised, 1120.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_revenue), -1120.0,
                               places=2)
        for move in c.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_variable_constraint_of_zero_includes_nothing(self):
        # Estimate present but constraint zero: none of it is highly probable,
        # so nothing is added and the allocated price is the plain amount.
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Service', 'standalone_price': 1000.0,
             'variable_consideration': True,
             'variable_estimate': 500.0,
             'variable_constraint': 0.0},
        ])
        self.assertAlmostEqual(c.obligation_ids.variable_included, 0.0,
                               places=2)
        self.assertAlmostEqual(c.obligation_ids.allocated_price, 1000.0,
                               places=2)

    # ------------------------------------------------------------------
    # (2) Significant financing component (IFRS 15.60-65)
    # ------------------------------------------------------------------

    def test_financing_arrears_recognises_pv_and_interest_income(self):
        # Deliver now, customer pays 1000 in two years at 10%: revenue is the
        # present value 1000 / 1.1^2 = 826.45, the shortfall 173.55 is interest
        # income recognised over the same schedule. Without the feature the
        # full 1000 would hit revenue.
        interest_acc = self._ensure_account(
            self.env, '4200', 'Interest Income', 'income_other')
        c = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'financing_component': True,
            'financing_direction': 'arrears',
            'financing_rate': 0.10,
            'financing_period_months': 24,
            'financing_account_id': interest_acc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Goods', 'standalone_price': 1000.0,
                'satisfaction': 'point_in_time'})],
        })
        self.assertAlmostEqual(c.financing_pv, 826.45, places=2)
        self.assertAlmostEqual(c.financing_component_amount, 173.55, places=2)
        c.action_activate()
        c.obligation_ids.satisfied = True
        c.action_recognise()
        # Revenue is the present value; interest income is the remainder; the
        # contract asset carries the full 1000 cash-basis amount.
        self.assertAlmostEqual(self._bal(self.account_revenue), -826.45,
                               places=2)
        self.assertAlmostEqual(self._bal(interest_acc), -173.55, places=2)
        self.assertAlmostEqual(self._bal(self.contract_asset_acc), 1000.0,
                               places=2)
        for move in c.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_financing_advance_recognises_more_than_cash_interest_expense(self):
        # Customer prepays 1000 now, goods delivered in two years at 10%:
        # revenue is the accreted 1000 * 1.1^2 = 1210, the excess 210 is
        # interest expense.
        interest_acc = self._ensure_account(
            self.env, '5200', 'Interest Expense', 'expense')
        c = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'financing_component': True,
            'financing_direction': 'advance',
            'financing_rate': 0.10,
            'financing_period_months': 24,
            'financing_account_id': interest_acc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Goods', 'standalone_price': 1000.0,
                'satisfaction': 'point_in_time'})],
        })
        self.assertAlmostEqual(c.financing_pv, 1210.0, places=2)
        self.assertAlmostEqual(c.financing_component_amount, 210.0, places=2)
        c.action_activate()
        c.obligation_ids.satisfied = True
        c.action_recognise()
        self.assertAlmostEqual(self._bal(self.account_revenue), -1210.0,
                               places=2)
        self.assertAlmostEqual(self._bal(interest_acc), 210.0, places=2)
        self.assertAlmostEqual(self._bal(self.contract_asset_acc), 1000.0,
                               places=2)
        for move in c.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_financing_requires_interest_account(self):
        c = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'financing_component': True,
            'financing_direction': 'arrears',
            'financing_rate': 0.10,
            'financing_period_months': 24,
            'obligation_ids': [(0, 0, {
                'name': 'Goods', 'standalone_price': 1000.0,
                'satisfaction': 'point_in_time', 'satisfied': True})],
        })
        c.action_activate()
        with self.assertRaises(UserError):
            c.action_recognise()

    def test_financing_interest_accretes_over_time_not_at_t0(self):
        # IFRS 15.65: for a point-in-time obligation with a significant
        # financing component and a payment date, the discount must unwind on a
        # time / effective-interest basis to the payment date, independent of
        # performance-obligation progress. It must NOT front-load the whole
        # interest at t0. Total interest on 1000 paid in arrears in 24 months
        # at 10% is 173.55. With the contract dated 12 months ago and payment
        # due in 12 months, roughly half the period has elapsed, so only part
        # of the interest should have accrued, and the goods are not yet
        # transferred so no revenue has been recognised.
        interest_acc = self._ensure_account(
            self.env, '4205', 'Interest Income Time', 'income_other')
        today = Date.context_today(self.env.user)
        c = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'contract_date': today - relativedelta(months=12),
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'financing_component': True,
            'financing_direction': 'arrears',
            'financing_rate': 0.10,
            'financing_period_months': 24,
            'financing_payment_date': today + relativedelta(months=12),
            'financing_account_id': interest_acc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Goods', 'standalone_price': 1000.0,
                'satisfaction': 'point_in_time'})],
        })
        self.assertAlmostEqual(c.financing_pv, 826.45, places=2)
        c.action_activate()
        # The obligation is not yet satisfied: no revenue is due, but the
        # financing interest has been accreting on a time basis and can be
        # posted independently of performance progress.
        c.action_accrue_financing()
        accrued = -self._bal(interest_acc)
        # Interest has genuinely accreted (not zero) but is only a portion of
        # the full 173.55: it has NOT been front-loaded at t0.
        self.assertGreater(accrued, 0.0)
        self.assertLess(accrued, 173.55 - 1.0)
        # No revenue has been recognised while the obligation is unsatisfied.
        self.assertAlmostEqual(self._bal(self.account_revenue), 0.0, places=2)
        # Every posted entry is balanced by construction.
        for move in c.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)
        # Satisfy and recognise: revenue is the present value, the remaining
        # interest continues to accrete on its own schedule, and the total
        # interest never exceeds the full financing component.
        c.obligation_ids.satisfied = True
        c.action_recognise()
        self.assertAlmostEqual(self._bal(self.account_revenue), -826.45,
                               places=2)
        self.assertLessEqual(-self._bal(interest_acc), 173.55 + 0.01)
        for move in c.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    # ------------------------------------------------------------------
    # (3) Contract modifications (IFRS 15.18-21)
    # ------------------------------------------------------------------

    def test_modification_separate_contract(self):
        # Added distinct goods at their SSP: a new separate contract is
        # created, the original is untouched (IFRS 15.20).
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Licence', 'standalone_price': 1000.0, 'satisfied': True},
        ])
        c.action_activate()
        c.action_recognise()
        self.assertAlmostEqual(c.amount_recognised, 1000.0, places=2)
        new = c._apply_modification(
            method='separate',
            added_obligations=[{'name': 'Add-on', 'standalone_price': 300.0}],
            new_transaction_price=300.0)
        self.assertNotEqual(new.id, c.id)
        self.assertEqual(new.state, 'active')
        self.assertAlmostEqual(new.transaction_price, 300.0, places=2)
        self.assertEqual(len(new.obligation_ids), 1)
        self.assertEqual(new.obligation_ids.name, 'Add-on')
        # Original transaction price and recognised revenue unchanged.
        self.assertAlmostEqual(c.transaction_price, 1000.0, places=2)
        self.assertAlmostEqual(c.amount_recognised, 1000.0, places=2)
        self.assertEqual(c.modification_count, 1)

    def test_modification_prospective_reallocates_remaining(self):
        # Two obligations, licence (satisfied, 600 recognised) and a build
        # over time. A prospective modification adds a new obligation and lifts
        # the price; the already-recognised licence keeps its 600 and takes no
        # share of the remaining price, which is reallocated across the open
        # obligations (IFRS 15.21(a)). No catch-up posts on the licence.
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Licence', 'standalone_price': 600.0,
             'satisfaction': 'point_in_time'},
            {'name': 'Build', 'standalone_price': 400.0,
             'satisfaction': 'over_time', 'percent_complete': 0.0},
        ])
        c.action_activate()
        licence = c.obligation_ids.filtered(lambda o: o.name == 'Licence')
        licence.satisfied = True
        c.action_recognise()
        self.assertAlmostEqual(c.amount_recognised, 600.0, places=2)
        rev_before = self._bal(self.account_revenue)
        c._apply_modification(
            method='prospective',
            added_obligations=[{'name': 'Extra', 'standalone_price': 300.0,
                                'satisfaction': 'over_time'}],
            new_transaction_price=1300.0)
        # Licence is pinned at 600 and no reversal/catch-up posted for it.
        self.assertAlmostEqual(self._bal(self.account_revenue), rev_before,
                               places=2)
        licence = c.obligation_ids.filtered(lambda o: o.name == 'Licence')
        self.assertTrue(licence.allocation_frozen)
        self.assertAlmostEqual(licence.allocated_price, 600.0, places=2)
        # Remaining 700 (1300 - 600 pinned) allocated across Build (400 SSP)
        # and Extra (300 SSP): 400 and 300.
        build = c.obligation_ids.filtered(lambda o: o.name == 'Build')
        extra = c.obligation_ids.filtered(lambda o: o.name == 'Extra')
        self.assertAlmostEqual(build.allocated_price, 400.0, places=2)
        self.assertAlmostEqual(extra.allocated_price, 300.0, places=2)

    def test_modification_catch_up_trues_up_revenue(self):
        # A single over-time obligation half complete, then the price is
        # revised up. Because the remaining goods are not distinct, a
        # cumulative catch-up trues up revenue at the modification date
        # (IFRS 15.21(b)).
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 50.0},
        ])
        c.action_activate()
        c.action_recognise()
        self.assertAlmostEqual(c.amount_recognised, 500.0, places=2)
        move_count = len(c.move_ids)
        c._apply_modification(
            method='catch_up', new_transaction_price=1400.0)
        # New target at 50% of 1400 = 700; catch-up posts the 200 difference.
        self.assertAlmostEqual(c.amount_recognised, 700.0, places=2)
        self.assertEqual(len(c.move_ids), move_count + 1)
        self.assertAlmostEqual(self._bal(self.account_revenue), -700.0,
                               places=2)
        for move in c.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_modification_requires_manager(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 50.0},
        ])
        c.action_activate()
        user = self.env['res.users'].create({
            'name': 'pm', 'login': 'rev_mod@test', 'email': 'rev_mod@test',
            'groups_id': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            c.with_user(user)._apply_modification(
                method='catch_up', new_transaction_price=1200.0)

    def test_cannot_add_obligation_to_posted_contract_directly(self):
        # Adding an obligation to a posted contract outside a modification is
        # blocked so the allocation basis behind posted revenue cannot shift
        # silently.
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 50.0},
        ])
        c.action_activate()
        c.action_recognise()
        with self.assertRaises(UserError):
            c.obligation_ids = [(0, 0, {
                'name': 'Sneaky', 'standalone_price': 500.0})]

    # ------------------------------------------------------------------
    # (4) Discount allocation to specific obligations (IFRS 15.81-83)
    # ------------------------------------------------------------------

    def test_specific_discount_allocated_to_named_obligation(self):
        # SSPs 700 + 400 = 1100; a 100 discount observably relates only to the
        # support line, so the price 1000 is allocated 700 to the licence and
        # 300 to support, not pro-rata (which would be 636.36 / 363.64).
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Licence', 'standalone_price': 700.0},
            {'name': 'Support', 'standalone_price': 400.0,
             'discount_specific': 100.0},
        ])
        licence = c.obligation_ids.filtered(lambda o: o.name == 'Licence')
        support = c.obligation_ids.filtered(lambda o: o.name == 'Support')
        self.assertAlmostEqual(licence.allocated_price, 700.0, places=2)
        self.assertAlmostEqual(support.allocated_price, 300.0, places=2)
        # Allocation still sums to the transaction price.
        self.assertAlmostEqual(
            licence.allocated_price + support.allocated_price, 1000.0,
            places=2)

    def test_specific_discount_recognition_balances(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Licence', 'standalone_price': 700.0, 'satisfied': True},
            {'name': 'Support', 'standalone_price': 400.0,
             'discount_specific': 100.0, 'satisfied': True},
        ])
        c.action_activate()
        c.action_recognise()
        self.assertAlmostEqual(c.amount_recognised, 1000.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_revenue), -1000.0,
                               places=2)
        for move in c.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_no_features_is_byte_identical(self):
        # A contract that opts into none of the four mechanics recognises
        # exactly as before: full price to revenue, no interest, pro-rata
        # allocation, ratio 1.0.
        c = self._contract(price=1000.0, obligations=[
            {'name': 'A', 'standalone_price': 600.0},
            {'name': 'B', 'standalone_price': 400.0},
        ])
        self.assertEqual(c._financing_revenue_ratio(), 1.0)
        a = c.obligation_ids.filtered(lambda o: o.name == 'A')
        b = c.obligation_ids.filtered(lambda o: o.name == 'B')
        self.assertAlmostEqual(a.allocated_price, 600.0, places=2)
        self.assertAlmostEqual(b.allocated_price, 400.0, places=2)
        self.assertEqual(a.variable_included, 0.0)
        self.assertFalse(a.allocation_frozen)

    def test_posted_contract_frozen_and_undeletable_flow_intact(self):
        # (a) a contract with posted revenue has its transaction price / account
        # inputs frozen at the ORM write layer; (b) it cannot be unlinked (its
        # posted GL moves would be orphaned); (c) the normal activate/recognise
        # flow still works.
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Licence', 'standalone_price': 600.0},
            {'name': 'Support', 'standalone_price': 400.0},
        ])
        # (c) activate and recognise: a point-in-time obligation posts.
        c.action_activate()
        c.obligation_ids.filtered(lambda o: o.name == 'Licence').satisfied = True
        c.action_recognise()
        self.assertAlmostEqual(c.amount_recognised, 600.0, places=2)
        # (a) the transaction price is frozen once revenue has posted.
        with self.assertRaises(UserError):
            c.write({'transaction_price': 2000.0})
        # (b) a contract with posted revenue cannot be unlinked.
        with self.assertRaises(UserError):
            c.unlink()
        # A draft contract with no posted revenue stays deletable.
        draft = self._contract(price=500.0, obligations=[
            {'name': 'X', 'standalone_price': 500.0},
        ])
        draft.unlink()
