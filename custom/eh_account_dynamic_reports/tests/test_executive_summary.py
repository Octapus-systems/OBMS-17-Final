# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Executive Summary handler tests.

Seeds income / expense / cash / AR / AP and asserts:

* Revenue and Net Profit reconcile to the dashboard's period_revenue /
  period_net for the same window (regression lock against the board).
* Margins = profit / revenue; current ratio = current assets / current
  liabilities; DSO math.
* Divide-by-zero ratios return a safe sentinel (n/a), never raise.
* The comparison column is populated when a comparison is set.
* Orchestrator wiring: the report record renders without error.
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestExecutiveSummaryHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.executive_summary'
        ]
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'executive_summary')], limit=1)
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'executive_summary',
                'name': 'Executive Summary',
                'handler_model':
                    'eh.account.dynamic.report.handler.executive_summary',
            })

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
        }

    def _post(self, lines, date_str='2026-06-15'):
        return self.post_balanced_move(
            lines, date=fields.Date.from_string(date_str))

    @staticmethod
    def _line_by_id(result, line_id):
        for line in result['lines']:
            if line['id'] == line_id:
                return line
        return None

    @staticmethod
    def _value(line):
        if line is None:
            return None
        for col in line['columns']:
            if col['expression_label'] == 'value':
                return col['value']
        return None

    @staticmethod
    def _raw(line):
        if line is None:
            return None
        return (line.get('meta') or {}).get('raw_value')

    def _seed_full_set(self):
        # Revenue 10,000 (Dr AR / Cr Revenue).
        self._post([
            {'account': self.account_receivable, 'debit': 10000.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 10000.0},
        ])
        # Expense 4,000 (Dr Expense / Cr AP).
        self._post([
            {'account': self.account_expense, 'debit': 4000.0},
            {'account': self.account_payable, 'credit': 4000.0,
             'partner': self.partner_b},
        ])
        # Cash receipt 6,000 (Dr Cash / Cr AR), partial collection.
        self._post([
            {'account': self.account_cash, 'debit': 6000.0},
            {'account': self.account_receivable, 'credit': 6000.0,
             'partner': self.partner_a},
        ])

    # ---- reconciliation regression lock vs dashboard ----

    def test_revenue_and_net_reconcile_to_dashboard(self):
        self._seed_full_set()
        result = self.handler.compute(self.options)

        revenue = self._raw(self._line_by_id(result, 'exec-revenue'))
        net = self._raw(self._line_by_id(result, 'exec-net_profit'))
        self.assertAlmostEqual(revenue, 10000.0, places=2)
        self.assertAlmostEqual(net, 6000.0, places=2)

        # Cross-check against the live dashboard for the same window when
        # the dashboard module is installed (it is an optional sibling, not
        # a dependency of this module).
        if 'eh.account.dashboard' in self.env:
            board = self.env['eh.account.dashboard'].create({
                'company_id': self.company.id,
                'posted_only': True,
                'period_date_from': '2026-01-01',
                'period_date_to': '2026-12-31',
            })
            self.assertAlmostEqual(revenue, board.period_revenue, places=2)
            self.assertAlmostEqual(net, board.period_net, places=2)

    def test_margins_and_balances(self):
        self._seed_full_set()
        result = self.handler.compute(self.options)

        # Net margin = net / revenue = 6000 / 10000 = 0.6.
        net_margin = self._raw(self._line_by_id(result, 'exec-net_margin'))
        self.assertAlmostEqual(net_margin, 0.6, places=4)

        # Cash balance cumulative to date_to = 6000.
        cash = self._raw(self._line_by_id(result, 'exec-cash'))
        self.assertAlmostEqual(cash, 6000.0, places=2)
        # Receivables remaining = 10000 - 6000 = 4000.
        receivables = self._raw(self._line_by_id(result, 'exec-receivables'))
        self.assertAlmostEqual(receivables, 4000.0, places=2)
        # Payables = 4000 (presented positive).
        payables = self._raw(self._line_by_id(result, 'exec-payables'))
        self.assertAlmostEqual(payables, 4000.0, places=2)

    def test_current_ratio_math(self):
        self._seed_full_set()
        result = self.handler.compute(self.options)
        # Current assets = cash 6000 + AR 4000 = 10000.
        # Current liabilities = AP 4000.
        # Current ratio = 10000 / 4000 = 2.5.
        current_ratio = self._raw(
            self._line_by_id(result, 'exec-current_ratio'))
        self.assertAlmostEqual(current_ratio, 2.5, places=4)

    def test_dso_math(self):
        self._seed_full_set()
        result = self.handler.compute(self.options)
        # DSO = receivables / revenue * period_days
        #     = 4000 / 10000 * 365 = 146.0.
        dso = self._raw(self._line_by_id(result, 'exec-dso'))
        self.assertAlmostEqual(dso, 146.0, places=0)

    # ---- divide-by-zero safety ----

    def test_zero_revenue_ratios_are_safe(self):
        # No data at all: every ratio is undefined, none raises.
        result = self.handler.compute(self.options)
        for line_id in (
            'exec-net_margin', 'exec-gross_margin', 'exec-operating_margin',
            'exec-current_ratio', 'exec-quick_ratio', 'exec-dso', 'exec-dpo',
            'exec-return_on_assets',
        ):
            line = self._line_by_id(result, line_id)
            self.assertIsNotNone(line)
            # n/a sentinel, raw value None.
            self.assertIsNone(self._raw(line))
            self.assertEqual(self._value(line), 'n/a')

    def test_safe_ratio_helper(self):
        self.assertIsNone(self.handler._safe_ratio(5.0, 0))
        self.assertIsNone(self.handler._safe_ratio(5.0, None))
        self.assertAlmostEqual(
            self.handler._safe_ratio(6.0, 3.0), 2.0, places=4)

    # ---- comparison column ----

    def test_comparison_column_populated(self):
        # Prior year revenue 8000.
        self._post([
            {'account': self.account_receivable, 'debit': 8000.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 8000.0},
        ], date_str='2025-06-15')
        self._seed_full_set()
        result = self.handler.compute(dict(
            self.options, comparison='previous_year'))
        # Three columns now: metric + value + prior_value.
        labels = [c['expression_label'] for c in result['columns']]
        self.assertIn('prior_value', labels)
        revenue_line = self._line_by_id(result, 'exec-revenue')
        prior_col = [c for c in revenue_line['columns']
                     if c['expression_label'] == 'prior_value']
        self.assertEqual(len(prior_col), 1)
        self.assertAlmostEqual(
            (revenue_line['meta'] or {}).get('prior_raw_value'),
            8000.0, places=2)

    # ---- orchestrator wiring ----

    def test_report_renders_through_orchestrator(self):
        self._seed_full_set()
        payload = self.report.render(self.options)
        self.assertIn('lines', payload)
        self.assertIn('columns', payload)
        self.assertTrue(any(
            l['id'] == 'exec-revenue' for l in payload['lines']))

    # ---- drilldown ----

    def test_cash_row_drills_to_journal_items(self):
        self._seed_full_set()
        action = self.handler.get_drilldown_action(self.options, 'exec-cash')
        self.assertIsInstance(action, dict)
        self.assertEqual(action['res_model'], 'account.move.line')
        # Ratio rows do not drill.
        self.assertIsNone(
            self.handler.get_drilldown_action(
                self.options, 'exec-current_ratio'))
