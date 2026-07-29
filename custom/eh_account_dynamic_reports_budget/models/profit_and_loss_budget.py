# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Budget-vs-actual columns on the Profit and Loss report.

Inherits the P&L handler and, when options carry a budget_id, appends a
Budget column and a Budget Variance column to the computed payload:
per account (summed from the budget lines), per section total, and on
Net Profit (derived by account type so the figures reconcile).
"""

from collections import defaultdict

from odoo import _, api, fields, models


class EhProfitAndLossBudgetHandler(models.AbstractModel):
    _inherit = 'eh.account.dynamic.report.handler.profit_and_loss'

    @api.model
    def compute(self, options):
        payload = super().compute(options)
        budget_id = options.get('budget_id')
        if not budget_id:
            return payload
        budget = self.env['eh.budget.budget'].browse(int(budget_id)).exists()
        if not budget:
            return payload

        # Scope the budget to the report's date window the same way the
        # actuals returned by super() are scoped to options['date'].
        # Without this a Q1 report would compare Q1 actuals against the
        # full-year budget. A budget line whose period overlaps the window
        # is time-apportioned to the fraction of its period that falls
        # inside the window (by overlapping days), so a full-year line
        # reported over Q1 contributes roughly one quarter, not the whole
        # amount. A line with no period is always counted in full.
        date_opt = options.get('date') or {}
        rep_from = fields.Date.to_date(date_opt.get('date_from'))
        rep_to = fields.Date.to_date(date_opt.get('date_to'))

        amount_by_account = defaultdict(float)
        type_by_account = {}
        for bline in budget.line_ids:
            amount = bline.budgeted_amount or 0.0
            if (rep_from and rep_to
                    and bline.period_from and bline.period_to):
                # Overlap of [period_from, period_to] with [rep_from, rep_to],
                # both inclusive, measured in days.
                ov_from = max(bline.period_from, rep_from)
                ov_to = min(bline.period_to, rep_to)
                if ov_to < ov_from:
                    # No overlap; the line falls entirely outside the window.
                    continue
                period_days = (bline.period_to - bline.period_from).days + 1
                overlap_days = (ov_to - ov_from).days + 1
                if period_days > 0:
                    amount = amount * overlap_days / period_days
            account = bline.account_id
            amount_by_account[account.id] += amount
            type_by_account[account.id] = account.account_type

        income_budget = round(sum(
            amt for acc, amt in amount_by_account.items()
            if type_by_account.get(acc) in self.INCOME_TYPES), 2)
        expense_budget = round(sum(
            amt for acc, amt in amount_by_account.items()
            if type_by_account.get(acc) in self.EXPENSE_TYPES), 2)
        net_budget = round(income_budget - expense_budget, 2)
        section_budget = {
            'section-income-total': income_budget,
            'section-expenses-total': expense_budget,
        }

        payload['columns'].append({
            'expression_label': 'budget', 'name': _("Budget"),
            'figure_type': 'monetary'})
        payload['columns'].append({
            'expression_label': 'budget_variance', 'name': _("Budget Var"),
            'figure_type': 'monetary'})

        for line in payload.get('lines', []):
            line_id = line.get('id') or ''
            budget_value = 0.0
            if line_id.startswith('account-'):
                try:
                    budget_value = amount_by_account.get(
                        int(line_id.split('-', 1)[1]), 0.0)
                except ValueError:
                    budget_value = 0.0
            elif line_id in section_budget:
                budget_value = section_budget[line_id]
            elif line_id == 'net_profit':
                budget_value = net_budget
            budget_value = round(budget_value, 2)
            actual = next(
                (c['value'] for c in line.get('columns', [])
                 if c['expression_label'] == 'amount'), 0.0) or 0.0
            line['columns'].append({
                'expression_label': 'budget', 'value': budget_value})
            line['columns'].append({
                'expression_label': 'budget_variance',
                'value': round(actual - budget_value, 2)})

        totals = payload.setdefault('totals', {})
        totals['income_budget'] = income_budget
        totals['expense_budget'] = expense_budget
        totals['net_budget'] = net_budget
        payload.setdefault('meta', {})['budget_id'] = budget.id
        return payload
