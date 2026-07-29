# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Budget Pro tests.

Covers model constraints (code format, unique, date range), lifecycle
(draft to confirmed to closed and back), batch actual computation
correctness, variance math, and the version copy action.
"""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetModel(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.Line = cls.env['eh.budget.line']

    def _make_budget(self, code='annual_2026', name='Test Budget', lines=None):
        return self.Budget.create({
            'code': code,
            'name': name,
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, line) for line in (lines or [])],
        })

    # ---- constraints ----

    def test_code_format_constraint(self):
        with self.assertRaises(UserError):
            self._make_budget(code='Bad-Code')
        with self.assertRaises(UserError):
            self._make_budget(code='123_starts_with_number')
        # valid codes pass
        self._make_budget(code='good_code_one', name='one')

    def test_unique_code_per_company(self):
        self._make_budget(code='unique_one', name='First')
        with self.assertRaises(Exception):
            self._make_budget(code='unique_one', name='Dup')

    def test_date_range_constraint(self):
        with self.assertRaises(Exception):
            self.Budget.create({
                'code': 'reversed_dates',
                'name': 'Bad',
                'date_from': fields.Date.from_string('2026-12-31'),
                'date_to': fields.Date.from_string('2026-01-01'),
            })

    # ---- lifecycle ----

    def test_action_confirm_requires_lines(self):
        budget = self._make_budget(code='no_lines')
        with self.assertRaises(UserError):
            budget.action_confirm()

    def test_action_confirm_with_lines(self):
        budget = self._make_budget(code='with_lines', lines=[
            {
                'account_id': self.account_revenue.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': 10000.0,
            },
        ])
        budget.action_confirm()
        self.assertEqual(budget.state, 'confirmed')

    def test_action_close(self):
        budget = self._make_budget(code='close_test', lines=[
            {
                'account_id': self.account_revenue.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': 5000.0,
            },
        ])
        budget.action_confirm()
        budget.action_close()
        self.assertEqual(budget.state, 'closed')

    def test_action_reset_draft_blocked_when_closed(self):
        budget = self._make_budget(code='reset_blocked', lines=[
            {
                'account_id': self.account_revenue.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': 1000.0,
            },
        ])
        budget.action_confirm()
        budget.action_close()
        budget.action_reset_draft()
        # Should remain closed since blocked.
        self.assertEqual(budget.state, 'closed')

    def test_action_reset_draft_works_from_confirmed(self):
        budget = self._make_budget(code='reset_works', lines=[
            {
                'account_id': self.account_revenue.id,
                'period_from': fields.Date.from_string('2026-01-01'),
                'period_to': fields.Date.from_string('2026-12-31'),
                'budgeted_amount': 1000.0,
            },
        ])
        budget.action_confirm()
        budget.action_reset_draft()
        self.assertEqual(budget.state, 'draft')


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetActuals(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        # Seed posted activity used by all tests.
        cls.post_balanced_move(
            [
                {'account': cls.account_revenue, 'credit': 1000.0},
                {'account': cls.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )
        cls.post_balanced_move(
            [
                {'account': cls.account_expense, 'debit': 300.0},
                {'account': cls.account_cash, 'credit': 300.0},
            ],
            date=fields.Date.from_string('2026-07-01'),
        )

    def test_actual_amount_reflects_posted_balance(self):
        budget = self.Budget.create({
            'code': 'actual_test',
            'name': 'Actual Test',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 1500.0,
                }),
            ],
        })
        revenue_line = budget.line_ids[0]
        # Revenue is credited 1000 (balance = -1000).
        self.assertAlmostEqual(revenue_line.actual_amount, -1000.0, places=2)

    def test_variance_math(self):
        budget = self.Budget.create({
            'code': 'variance_test',
            'name': 'Variance Test',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 200.0,
                }),
            ],
        })
        line = budget.line_ids[0]
        # Expense actual = 300 (debit). Budgeted = 200. Variance = 100.
        self.assertAlmostEqual(line.actual_amount, 300.0, places=2)
        self.assertAlmostEqual(line.variance_amount, 100.0, places=2)
        self.assertAlmostEqual(line.variance_pct, 50.0, places=2)

    def test_period_filter_excludes_out_of_range(self):
        # Add an entry outside the line's period.
        self.post_balanced_move(
            [
                {'account': self.account_expense, 'debit': 9999.0},
                {'account': self.account_cash, 'credit': 9999.0},
            ],
            date=fields.Date.from_string('2027-02-01'),
        )
        budget = self.Budget.create({
            'code': 'period_filter_test',
            'name': 'Period Filter',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 200.0,
                }),
            ],
        })
        # Should still be 300 (the 2027 entry is excluded).
        self.assertAlmostEqual(
            budget.line_ids[0].actual_amount, 300.0, places=2,
        )

    def test_total_aggregates_lines(self):
        budget = self.Budget.create({
            'code': 'total_test',
            'name': 'Total Test',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 1500.0,
                }),
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 500.0,
                }),
            ],
        })
        self.assertAlmostEqual(budget.total_budgeted, 2000.0, places=2)
        # Revenue actual -1000 + Expense actual 300 = -700.
        self.assertAlmostEqual(budget.total_actual, -700.0, places=2)
        self.assertAlmostEqual(budget.total_variance, -2700.0, places=2)


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetVersioning(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']

    def test_create_version_copies_with_parent_link(self):
        original = self.Budget.create({
            'code': 'version_test',
            'name': 'Original',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'version_label': 'v1',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'period_from': fields.Date.from_string('2026-01-01'),
                    'period_to': fields.Date.from_string('2026-12-31'),
                    'budgeted_amount': 1500.0,
                }),
            ],
        })
        original.action_confirm()
        action = original.action_create_version()
        new_id = action['res_id']
        self.assertTrue(new_id)
        new_budget = self.Budget.browse(new_id)
        self.assertEqual(new_budget.parent_id, original)
        self.assertEqual(new_budget.state, 'draft')
        self.assertEqual(new_budget.version_label, 'v1.1')
        self.assertEqual(len(new_budget.line_ids), 1)

    def test_next_version_label_simple_v1(self):
        self.assertEqual(self.Budget._next_version_label('v1'), 'v1.1')
        self.assertEqual(self.Budget._next_version_label('v1.5'), 'v1.6')
        self.assertEqual(self.Budget._next_version_label('v3.0'), 'v3.1')
        self.assertEqual(
            self.Budget._next_version_label('weird'), 'weird next',
        )

    def test_create_version_rejects_multiple(self):
        b1 = self.Budget.create({
            'code': 'multi_test_one',
            'name': 'First',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
        })
        b2 = self.Budget.create({
            'code': 'multi_test_two',
            'name': 'Second',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
        })
        with self.assertRaises(UserError):
            (b1 | b2).action_create_version()

    # ---- regression: zero-budget variance must signal the overrun ----

    def test_safe_variance_pct_zero_budget_zero_actual(self):
        """A budget with zero baseline and no activity is genuinely 0%."""
        self.assertEqual(self.Budget._safe_variance_pct(0.0, 0.0), 0.0)

    def test_safe_variance_pct_zero_budget_with_actual(self):
        """A budget with zero baseline and actual spend is a 100% overrun.

        Regression: the prior code returned 0% in this case, hiding
        unbudgeted spend in variance reports.
        """
        self.assertEqual(
            self.Budget._safe_variance_pct(0.0, 1500.0), 100.0,
        )

    def test_safe_variance_pct_normal_overrun(self):
        """A 10% over-budget run reads as +10."""
        self.assertAlmostEqual(
            self.Budget._safe_variance_pct(1000.0, 1100.0), 10.0,
        )

    def test_safe_variance_pct_under_budget(self):
        """A 5% under-budget run reads as -5."""
        self.assertAlmostEqual(
            self.Budget._safe_variance_pct(1000.0, 950.0), -5.0,
        )


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetAnalyticParity(EhAccountIntegrationTestCase):
    """The SQL report view (the source for the graph and pivot measures)
    must report the same analytic-weighted actual as the ORM compute on
    the budget line. The view previously summed the full balance for any
    journal item touching the analytic account, diverging from the
    percentage-weighted compute for analytic-split lines."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        plan = cls.env['account.analytic.plan'].create({'name': 'Test Plan'})
        cls.analytic = cls.env['account.analytic.account'].create({
            'name': 'Project X',
            'plan_id': plan.id,
        })
        # Post an expense of 1,000 with 30% allocated to the analytic
        # account, so the analytic-weighted actual is 300 and the full
        # balance is 1,000.
        move = cls.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': cls.journal_misc.id,
            'date': fields.Date.from_string('2026-06-15'),
            'line_ids': [
                (0, 0, {
                    'account_id': cls.account_expense.id,
                    'debit': 1000.0,
                    'analytic_distribution': {str(cls.analytic.id): 30.0},
                }),
                (0, 0, {
                    'account_id': cls.account_cash.id,
                    'credit': 1000.0,
                }),
            ],
        })
        move.action_post()

    def _make_line_budget(self, code, **line_overrides):
        line = {
            'account_id': self.account_expense.id,
            'period_from': fields.Date.from_string('2026-01-01'),
            'period_to': fields.Date.from_string('2026-12-31'),
            'budgeted_amount': 1000.0,
        }
        line.update(line_overrides)
        budget = self.Budget.create({
            'code': code,
            'name': code,
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, line)],
        })
        return budget.line_ids[0]

    def test_report_view_matches_weighted_compute(self):
        line = self._make_line_budget(
            'analytic_parity', analytic_account_id=self.analytic.id,
        )
        # ORM compute weights by the 30% allocation: 1,000 * 0.30 = 300.
        self.assertAlmostEqual(line.actual_amount, 300.0, places=2)
        report = self.env['eh.budget.report'].search([
            ('line_id', '=', line.id),
        ])
        self.assertEqual(len(report), 1)
        # The view must report the same 300, not the full 1,000 balance.
        self.assertAlmostEqual(report.actual_amount, 300.0, places=2)
        self.assertAlmostEqual(
            report.actual_amount, line.actual_amount, places=2,
        )

    def test_non_analytic_line_view_matches_full_balance(self):
        line = self._make_line_budget('non_analytic_parity')
        # A non-analytic line counts the full 1,000 balance.
        self.assertAlmostEqual(line.actual_amount, 1000.0, places=2)
        report = self.env['eh.budget.report'].search([
            ('line_id', '=', line.id),
        ])
        self.assertAlmostEqual(report.actual_amount, 1000.0, places=2)
        self.assertAlmostEqual(
            report.actual_amount, line.actual_amount, places=2,
        )


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetRollForwardIsolation(EhAccountIntegrationTestCase):

    def _make_rolling(self, code):
        today = fields.Date.context_today(self.env['eh.budget.budget'])
        near = today + timedelta(days=5)
        budget = self.env['eh.budget.budget'].create({
            'code': code,
            'name': code,
            'date_from': today.replace(day=1),
            'date_to': near,
            'is_rolling': True,
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': today.replace(day=1),
                'period_to': near,
                'budgeted_amount': 100.0,
            })],
        })
        budget.action_confirm()
        return budget, near

    def test_cron_roll_forward_isolates_failures(self):
        # Two due rolling budgets; line creation raises for the first.
        # The per-budget savepoint (shared batch mixin) must isolate the
        # failure so the second budget still rolls forward. Before the
        # mixin the bare savepoint let the error abort the whole cron.
        b1, near1 = self._make_rolling('roll_iso_1')
        b2, near2 = self._make_rolling('roll_iso_2')
        Line = type(self.env['eh.budget.line'])
        real_create = Line.create

        def flaky(self2, vals):
            rows = vals if isinstance(vals, list) else [vals]
            if any(r.get('budget_id') == b1.id for r in rows):
                raise ValueError("forced failure")
            return real_create(self2, vals)

        with patch.object(Line, 'create', flaky):
            self.env['eh.budget.budget'].cron_roll_forward()

        b1.invalidate_recordset()
        b2.invalidate_recordset()
        self.assertEqual(
            b1.date_to, near1,
            "failed budget must roll back and keep its original date_to",
        )
        self.assertGreater(
            b2.date_to, near2,
            "second budget must roll forward despite the first failing",
        )
