# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Year-end closing tests.

Posts a small fiscal year of income and expense moves, runs the
closing, and asserts:

* The breakdown lists every contributing account with the right
  signed balance.
* The closing entry is balanced (sum debits == sum credits).
* The closing entry zeroes the income and expense accounts.
* Net profit lands on the retained earnings account.
* Reverse generates a balanced inverse move dated one day after.
* Lock date is bumped on post when lock_after_post is True.
"""

from datetime import date
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_year_end', 'integration', 'post_install', '-at_install')
class TestYearEndClose(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Run = cls.env['eh.year.end.run']
        cls.fy_start = date(2026, 1, 1)
        cls.fy_end = date(2026, 12, 31)

        cls.retained_earnings = cls._ensure_account(
            cls.env, '3100', 'Retained Earnings', 'equity',
        )
        cls.expense2 = cls._ensure_account(
            cls.env, '5050', 'Wages Expense', 'expense',
        )
        cls.revenue2 = cls._ensure_account(
            cls.env, '4050', 'Service Revenue', 'income_other',
        )

        # Post a small fiscal year. Two revenue lines and two expense.
        cls._post_at(date(2026, 3, 15), [
            {'account': cls.account_revenue, 'credit': 5000.0},
            {'account': cls.account_cash, 'debit': 5000.0},
        ])
        cls._post_at(date(2026, 6, 30), [
            {'account': cls.revenue2, 'credit': 3000.0},
            {'account': cls.account_cash, 'debit': 3000.0},
        ])
        cls._post_at(date(2026, 4, 1), [
            {'account': cls.account_expense, 'debit': 1500.0},
            {'account': cls.account_cash, 'credit': 1500.0},
        ])
        cls._post_at(date(2026, 11, 30), [
            {'account': cls.expense2, 'debit': 2000.0},
            {'account': cls.account_cash, 'credit': 2000.0},
        ])

        # Promote the test user so action_post passes the manager guard.
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )

    @classmethod
    def _post_at(cls, on_date, lines):
        return cls.post_balanced_move(lines, date=on_date)

    def _make_run(self, **overrides):
        vals = {
            'fiscal_year_start': self.fy_start,
            'fiscal_year_end': self.fy_end,
            'company_id': self.env.company.id,
            'journal_id': self.journal_misc.id,
            'retained_earnings_account_id': self.retained_earnings.id,
            'lock_after_post': False,
            # Disabling the lock requires a documented reason (logged in
            # chatter); the lock behaviour itself is exercised separately.
            'no_lock_reason': 'test fixture: lock exercised separately',
        }
        vals.update(overrides)
        return self.Run.create(vals)

    # ---- compute ----

    def test_compute_picks_up_revenue_and_expense_only(self):
        run = self._make_run()
        run.action_compute()
        accounts = set(run.line_ids.mapped('account_id'))
        self.assertIn(self.account_revenue, accounts)
        self.assertIn(self.revenue2, accounts)
        self.assertIn(self.account_expense, accounts)
        self.assertIn(self.expense2, accounts)
        # Cash account should NOT appear.
        self.assertNotIn(self.account_cash, accounts)

    def test_compute_totals_match_postings(self):
        run = self._make_run()
        run.action_compute()
        # Revenue 5000 + 3000 = 8000
        # Expense 1500 + 2000 = 3500
        self.assertAlmostEqual(run.total_income, 8000.0)
        self.assertAlmostEqual(run.total_expense, 3500.0)
        self.assertAlmostEqual(run.net_profit, 4500.0)

    def test_compute_idempotent(self):
        run = self._make_run()
        run.action_compute()
        first_count = run.line_count
        run.action_compute()
        self.assertEqual(run.line_count, first_count)

    # ---- post ----

    def test_post_blocks_when_not_computed(self):
        run = self._make_run()
        with self.assertRaises(UserError):
            run.action_post()

    def test_post_creates_balanced_entry(self):
        run = self._make_run()
        run.action_compute()
        run.action_post()
        move = run.move_id
        self.assertTrue(move and move.state == 'posted')
        debit = sum(move.line_ids.mapped('debit'))
        credit = sum(move.line_ids.mapped('credit'))
        self.assertAlmostEqual(debit, credit)

    def test_post_zeroes_income_accounts(self):
        run = self._make_run()
        run.action_compute()
        run.action_post()
        # Sum of all entries on revenue account in fiscal year should
        # be zero AFTER posting the close.
        AML = self.env['account.move.line']
        revenue_balance = sum(AML.search([
            ('account_id', '=', self.account_revenue.id),
            ('parent_state', '=', 'posted'),
            ('date', '>=', self.fy_start),
            ('date', '<=', self.fy_end),
        ]).mapped('balance'))
        self.assertAlmostEqual(revenue_balance, 0.0)

    def test_post_zeroes_expense_accounts(self):
        run = self._make_run()
        run.action_compute()
        run.action_post()
        AML = self.env['account.move.line']
        expense_balance = sum(AML.search([
            ('account_id', '=', self.account_expense.id),
            ('parent_state', '=', 'posted'),
            ('date', '>=', self.fy_start),
            ('date', '<=', self.fy_end),
        ]).mapped('balance'))
        self.assertAlmostEqual(expense_balance, 0.0)

    def test_post_sweeps_other_expenses(self):
        """Accounts of type ``expense_other`` (IAS 1 Other Expenses) must be
        closed to retained earnings, symmetric with ``income_other`` on the
        revenue side. Omitting them leaves the Other-Expenses balance
        standing and overstates retained earnings by that amount on every
        close.
        """
        type_field = self.env['account.account']._fields['account_type']
        type_values = dict(type_field.selection).keys()
        if 'expense_other' not in type_values:
            self.skipTest(
                "account_type 'expense_other' is Odoo 19+; earlier cores "
                "have no Other-Expenses type to sweep",
            )
        other_exp = self._ensure_account(
            self.env, '5090', 'Other Expenses', 'expense_other',
        )
        self._post_at(date(2026, 5, 20), [
            {'account': other_exp, 'debit': 700.0},
            {'account': self.account_cash, 'credit': 700.0},
        ])
        run = self._make_run()
        run.action_compute()
        # The Other-Expenses account is in scope of the close.
        self.assertIn(other_exp, set(run.line_ids.mapped('account_id')))
        # It contributes to the expense total (3500 base + 700).
        self.assertAlmostEqual(run.total_expense, 4200.0)
        run.action_post()
        AML = self.env['account.move.line']
        balance = sum(AML.search([
            ('account_id', '=', other_exp.id),
            ('parent_state', '=', 'posted'),
            ('date', '>=', self.fy_start),
            ('date', '<=', self.fy_end),
        ]).mapped('balance'))
        self.assertAlmostEqual(balance, 0.0)

    def test_post_pushes_net_to_retained_earnings(self):
        run = self._make_run()
        run.action_compute()
        run.action_post()
        move = run.move_id
        re_legs = move.line_ids.filtered(
            lambda line_item: line_item.account_id == self.retained_earnings,
        )
        # Net profit 4500 -> retained earnings credited 4500.
        self.assertEqual(len(re_legs), 1)
        self.assertAlmostEqual(re_legs.credit, 4500.0)
        self.assertAlmostEqual(re_legs.debit, 0.0)

    def test_post_handles_net_loss(self):
        # Add another big expense that turns the net into a loss.
        self._post_at(date(2026, 10, 15), [
            {'account': self.expense2, 'debit': 10000.0},
            {'account': self.account_cash, 'credit': 10000.0},
        ])
        run = self._make_run(
            fiscal_year_end=date(2026, 12, 31),
        )
        run.action_compute()
        # Income 8000 - Expense (3500 + 10000) = -5500
        self.assertAlmostEqual(run.net_profit, -5500.0)
        run.action_post()
        re_legs = run.move_id.line_ids.filtered(
            lambda line_item: line_item.account_id == self.retained_earnings,
        )
        # Net loss 5500 -> retained earnings debited 5500.
        self.assertAlmostEqual(re_legs.debit, 5500.0)
        self.assertAlmostEqual(re_legs.credit, 0.0)

    # ---- reverse ----

    def test_reverse_creates_inverse_dated_after(self):
        run = self._make_run()
        run.action_compute()
        run.action_post()
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        rev = run.reversal_move_id
        self.assertTrue(rev and rev.state == 'posted')
        self.assertEqual(rev.date, date(2027, 1, 1))
        # Reversal also balances.
        self.assertAlmostEqual(
            sum(rev.line_ids.mapped('debit')),
            sum(rev.line_ids.mapped('credit')),
        )

    # ---- lock date ----

    def test_lock_after_post_advances_lock_date(self):
        run = self._make_run(lock_after_post=True)
        run.action_compute()
        run.action_post()
        self.assertEqual(
            self.env.company.fiscalyear_lock_date,
            self.fy_end,
        )

    def test_lock_skipped_when_already_past(self):
        # Pre-set the lock date to a year BEFORE the one being closed:
        # the advance-lock logic should still bump it forward. Using a
        # lock date at or past fiscal_year_end would (correctly) trip
        # the pre-post lock guard, which is exercised separately.
        self.env.company.sudo().fiscalyear_lock_date = date(2025, 12, 31)
        run = self._make_run(lock_after_post=True)
        run.action_compute()
        run.action_post()
        # Lock date advanced to this run's fiscal year end.
        self.assertEqual(
            self.env.company.fiscalyear_lock_date,
            self.fy_end,
        )

    # ---- constraints ----

    def test_unique_run_per_company_year(self):
        self._make_run()
        with self.assertRaises(Exception):
            self._make_run()

    def test_post_blocked_by_overlapping_posted_run(self):
        # First close posts cleanly.
        first = self._make_run()
        first.action_compute()
        first.action_post()
        self.assertEqual(first.state, 'posted')
        # A second run over an OVERLAPPING fiscal window (different end
        # date, so the DB unique constraint does not catch it) must be
        # rejected at post time by the pre-post overlap guard.
        second = self._make_run(
            fiscal_year_start=date(2026, 7, 1),
            fiscal_year_end=date(2027, 6, 30),
        )
        second.action_compute()
        with self.assertRaises(UserError):
            second.action_post()
        self.assertEqual(second.state, 'computed')

    def test_post_blocked_when_year_already_locked(self):
        # Company lock date already sits at or past the fiscal year end:
        # the year is closed and frozen, so posting a close into it must
        # be rejected by the pre-post lock guard.
        self.env.company.sudo().fiscalyear_lock_date = self.fy_end
        run = self._make_run(lock_after_post=False)
        run.action_compute()
        with self.assertRaises(UserError):
            run.action_post()
        self.assertEqual(run.state, 'computed')

    def test_invalid_year_dates_rejected(self):
        with self.assertRaises(Exception):
            self.Run.create({
                'fiscal_year_start': date(2026, 12, 31),
                'fiscal_year_end': date(2026, 1, 1),
                'company_id': self.env.company.id,
                'journal_id': self.journal_misc.id,
                'retained_earnings_account_id': self.retained_earnings.id,
            })

    # ---- performance ----

    def test_build_lines_uses_single_batched_create(self):
        # Four contributing accounts (two income, two expense) must
        # produce the breakdown in ONE batched create, not one create
        # per account. Guards the N+1 regression on a full chart.
        run = self._make_run()
        Line = type(self.env['eh.year.end.line'])
        original_create = Line.create
        calls = []

        def counting_create(self2, vals):
            calls.append(vals)
            return original_create(self2, vals)

        with patch.object(Line, 'create', counting_create):
            run.action_compute()

        self.assertEqual(
            len(calls), 1,
            "breakdown lines must be created in one batched create call",
        )
        self.assertIsInstance(calls[0], list)
        self.assertEqual(len(calls[0]), 4)
        self.assertEqual(len(run.line_ids), 4)
