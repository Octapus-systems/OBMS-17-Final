# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Render regression test for the Budget vs Actual PDF report.

The two shipped report bugs were caught only because a render test was
missing. This exercises the QWeb template end to end (HTML render, so no
wkhtmltopdf dependency) and proves the report produces non-empty output
for a valid budget record with lines.
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBudgetReport(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']

    def _budget(self, code='rpt', lines=None):
        return self.Budget.create({
            'code': code,
            'name': 'Report render budget',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, line) for line in (lines or [])],
        })

    def test_report_renders_with_lines(self):
        budget = self._budget(lines=[{
            'account_id': self.account_expense.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
            'budgeted_amount': 1200.0,
        }])
        report = self.env.ref('eh_account_budget_pro.action_report_budget')
        html, ftype = report._render_qweb_html(
            report.report_name, budget.ids)
        self.assertEqual(ftype, 'html')
        # Non-empty output proves no KeyError / render failure.
        self.assertTrue(html)
        # Stable string: the seeded budget name always appears in the header.
        self.assertIn(b'Report render budget', html)

    def test_report_renders_without_lines(self):
        # The "no lines" branch is a separate template path; render it too.
        budget = self._budget(code='rpt_empty')
        report = self.env.ref('eh_account_budget_pro.action_report_budget')
        html, ftype = report._render_qweb_html(
            report.report_name, budget.ids)
        self.assertEqual(ftype, 'html')
        self.assertTrue(html)
        self.assertIn(b'This budget has no lines.', html)
