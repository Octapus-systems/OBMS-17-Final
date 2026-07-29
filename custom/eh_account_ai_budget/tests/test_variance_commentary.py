# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Integration tests for the live budget variance commentary.

These prove the deterministic commentary helper is wired live onto
eh.budget.budget: the field renders a non-empty narrative, includes the
period label, maps the income flag from the account type, and refreshes
when the lines change.
"""

from datetime import date

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_ai_budget', 'integration', 'post_install', '-at_install')
class TestVarianceCommentary(EhAccountIntegrationTestCase):

    def _budget(self, lines=True, name="FY Test Budget"):
        today = fields.Date.context_today(self.env['res.users'])
        date_from = date(today.year, 1, 1)
        date_to = date(today.year, 12, 31)
        vals = {
            'name': name,
            'code': 'FYTEST',
            'company_id': self.company.id,
            'date_from': date_from,
            'date_to': date_to,
        }
        if lines:
            vals['line_ids'] = [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'period_from': date_from, 'period_to': date_to,
                    'budgeted_amount': 10000.0,
                }),
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'period_from': date_from, 'period_to': date_to,
                    'budgeted_amount': 5000.0,
                }),
            ]
        return self.env['eh.budget.budget'].create(vals)

    def test_commentary_non_empty_with_period_label(self):
        budget = self._budget()
        text = budget.eh_ai_variance_commentary
        self.assertTrue(text)
        self.assertIn('budget', text.lower())
        self.assertIn('FY Test Budget', text)

    def test_income_flag_mapped_from_account_type(self):
        budget = self._budget()
        snapshot = budget._eh_build_budget_snapshot()
        by_income = {s.is_income for s in snapshot}
        self.assertIn(True, by_income, "revenue line must map is_income=True")
        self.assertIn(False, by_income, "expense line must map is_income=False")
        revenue_snap = next(
            s for s in snapshot
            if s.label == self.account_revenue.display_name
        )
        self.assertTrue(revenue_snap.is_income)

    def test_empty_budget_returns_placeholder(self):
        budget = self._budget(lines=False)
        self.assertTrue(budget.eh_ai_variance_commentary)

    def test_commentary_recomputes_on_line_change(self):
        budget = self._budget()
        budget.line_ids.filtered(
            lambda line: line.account_id == self.account_revenue
        ).budgeted_amount = 99999.0
        budget.invalidate_recordset(['eh_ai_variance_commentary'])
        # New total budget = 99999 + 5000 = 104999.
        self.assertIn('104999', budget.eh_ai_variance_commentary)
