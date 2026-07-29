# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Budget-vs-actual columns on the P&L report."""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_dynamic_reports_budget', 'integration', 'post_install',
        '-at_install')
class TestPlBudgetColumns(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.profit_and_loss']
        cls.budget = cls.env['eh.budget.budget'].create({
            'code': 'pl_budget', 'name': 'P&L budget',
            'date_from': '2026-01-01', 'date_to': '2026-12-31',
            'line_ids': [
                (0, 0, {'account_id': cls.account_revenue.id,
                        'period_from': '2026-01-01',
                        'period_to': '2026-12-31',
                        'budgeted_amount': 1000.0}),
                (0, 0, {'account_id': cls.account_expense.id,
                        'period_from': '2026-01-01',
                        'period_to': '2026-12-31',
                        'budgeted_amount': 300.0}),
            ],
        })

    def _options(self):
        return {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True, 'show_zero': False,
            'budget_id': self.budget.id,
        }

    @staticmethod
    def _line(result, line_id):
        for line in result['lines']:
            if line['id'] == line_id:
                return line
        return None

    @staticmethod
    def _col(line, label):
        for col in line['columns']:
            if col['expression_label'] == label:
                return col['value']
        return None

    def test_budget_columns_present_and_correct(self):
        # Actuals: revenue 1200, expense 250.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 1200.0},
             {'account': self.account_cash, 'debit': 1200.0}],
            date=fields.Date.from_string('2026-06-15'))
        self.post_balanced_move(
            [{'account': self.account_expense, 'debit': 250.0},
             {'account': self.account_cash, 'credit': 250.0}],
            date=fields.Date.from_string('2026-06-15'))

        result = self.handler.compute(self._options())
        col_keys = [c['expression_label'] for c in result['columns']]
        self.assertIn('budget', col_keys)
        self.assertIn('budget_variance', col_keys)

        rev = self._line(result, 'account-%d' % self.account_revenue.id)
        self.assertAlmostEqual(self._col(rev, 'budget'), 1000.0, places=2)
        self.assertAlmostEqual(self._col(rev, 'amount'), 1200.0, places=2)
        self.assertAlmostEqual(
            self._col(rev, 'budget_variance'), 200.0, places=2)

        net = self._line(result, 'net_profit')
        # net actual 1200-250=950; net budget 1000-300=700; var 250.
        self.assertAlmostEqual(self._col(net, 'amount'), 950.0, places=2)
        self.assertAlmostEqual(self._col(net, 'budget'), 700.0, places=2)
        self.assertAlmostEqual(
            self._col(net, 'budget_variance'), 250.0, places=2)
        self.assertAlmostEqual(result['totals']['net_budget'], 700.0, places=2)

    def test_full_year_budget_apportioned_over_quarter(self):
        # The full-year revenue budget line is 1000 over 365 days.
        # Reported over Q1 (Jan 1 - Mar 31, 90 days) it must contribute
        # roughly a quarter (1000 * 90 / 365 ~= 246.58), not the whole
        # 1000, so a Q1 actual is not compared against a full-year budget.
        # Post small Q1 actuals so the account and net lines are present
        # (show_zero is False, so accounts without activity are omitted).
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 100.0},
             {'account': self.account_cash, 'debit': 100.0}],
            date=fields.Date.from_string('2026-02-10'))
        self.post_balanced_move(
            [{'account': self.account_expense, 'debit': 50.0},
             {'account': self.account_cash, 'credit': 50.0}],
            date=fields.Date.from_string('2026-02-10'))

        options = self._options()
        options['date'] = {'date_from': '2026-01-01', 'date_to': '2026-03-31'}
        result = self.handler.compute(options)

        rev = self._line(result, 'account-%d' % self.account_revenue.id)
        expected = 1000.0 * 90.0 / 365.0
        self.assertAlmostEqual(
            self._col(rev, 'budget'), round(expected, 2), places=2)
        # Sanity: strictly between zero and the full-year amount.
        self.assertGreater(self._col(rev, 'budget'), 0.0)
        self.assertLess(self._col(rev, 'budget'), 1000.0)

        exp = self._line(result, 'account-%d' % self.account_expense.id)
        expected_exp = 300.0 * 90.0 / 365.0
        self.assertAlmostEqual(
            self._col(exp, 'budget'), round(expected_exp, 2), places=2)

        net = self._line(result, 'net_profit')
        expected_net = round(expected, 2) - round(expected_exp, 2)
        self.assertAlmostEqual(
            self._col(net, 'budget'), round(expected_net, 2), places=2)

    def test_no_budget_columns_without_budget_id(self):
        result = self.handler.compute({
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True, 'show_zero': False,
        })
        col_keys = [c['expression_label'] for c in result['columns']]
        self.assertNotIn('budget', col_keys)
