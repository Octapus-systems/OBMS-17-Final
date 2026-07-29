# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Analytic Balance handler tests.

Regression cover for the LATERAL-join rewrite: PostgreSQL rejects a
SUM() that contains a set-returning function, so the original
`SUM(... jsonb_each_text(...) ...)` blew up at execute time. The fix
expands the distribution via CROSS JOIN LATERAL so the aggregate
operates on plain column references.
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'post_install', '-at_install')
class TestAnalyticBalanceHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.analytic_balance'
        ]
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'analytic_balance')], limit=1,
        )

        if 'account.analytic.plan' not in cls.env:
            cls.has_analytic = False
            return
        cls.has_analytic = True

        cls.plan = cls.env['account.analytic.plan'].create({'name': 'Test Plan'})
        cls.analytic_a = cls.env['account.analytic.account'].create({
            'name': 'Analytic A',
            'plan_id': cls.plan.id,
        })
        cls.analytic_b = cls.env['account.analytic.account'].create({
            'name': 'Analytic B',
            'plan_id': cls.plan.id,
        })

        # 60/40 split on a revenue line, full allocation on an expense line.
        cls.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': cls.journal_misc.id,
            'date': fields.Date.from_string('2026-06-15'),
            'line_ids': [
                (0, 0, {
                    'account_id': cls.account_revenue.id,
                    'credit': 1000.0,
                    'name': 'rev split 60/40',
                    'analytic_distribution': {
                        str(cls.analytic_a.id): 60.0,
                        str(cls.analytic_b.id): 40.0,
                    },
                }),
                (0, 0, {
                    'account_id': cls.account_cash.id,
                    'debit': 1000.0,
                    'name': 'cash leg',
                }),
            ],
        }).action_post()

        cls.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': cls.journal_misc.id,
            'date': fields.Date.from_string('2026-07-01'),
            'line_ids': [
                (0, 0, {
                    'account_id': cls.account_expense.id,
                    'debit': 200.0,
                    'name': 'expense full',
                    'analytic_distribution': {
                        str(cls.analytic_a.id): 100.0,
                    },
                }),
                (0, 0, {
                    'account_id': cls.account_cash.id,
                    'credit': 200.0,
                    'name': 'cash leg',
                }),
            ],
        }).action_post()

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def test_compute_does_not_crash_with_distribution(self):
        if not self.has_analytic:
            self.skipTest("Analytic addon not installed in this build.")
        payload = self.handler.compute(self.options)
        self.assertIn('lines', payload)
        self.assertIn('columns', payload)
        self.assertEqual(len(payload['columns']), 2)

    def test_allocation_weights_apply(self):
        if not self.has_analytic:
            self.skipTest("Analytic addon not installed in this build.")
        payload = self.handler.compute(self.options)
        # Pull the analytic-A row. Revenue contributes -600 (credit, signed
        # +1 after negation), expense contributes +200. Net = -400 as a
        # net-contribution figure (negative = net contribution per the
        # handler's docstring convention).
        rows_by_id = {}
        for line in payload['lines']:
            meta = line.get('meta') or {}
            if meta.get('kind'):
                continue
            aid = meta.get('analytic_account_id')
            if aid:
                rows_by_id[aid] = line['columns'][0]['value']
        self.assertAlmostEqual(rows_by_id.get(self.analytic_a.id, 0.0), 400.0, places=2)
        self.assertAlmostEqual(rows_by_id.get(self.analytic_b.id, 0.0), 400.0, places=2)

    def test_render_via_orchestrator(self):
        if not self.has_analytic:
            self.skipTest("Analytic addon not installed in this build.")
        if not self.report:
            self.skipTest("Analytic balance report record not seeded.")
        payload = self.report.render(self.options)
        self.assertIn('lines', payload)
        self.assertIn('execution_id', payload)

    def test_pdf_export_renders(self):
        if not self.has_analytic:
            self.skipTest("Analytic addon not installed in this build.")
        if not self.report:
            self.skipTest("Analytic balance report record not seeded.")
        content = self.report.render_pdf(self.options)
        self.assertIsInstance(content, (bytes, bytearray))
        self.assertGreater(len(content), 100)
