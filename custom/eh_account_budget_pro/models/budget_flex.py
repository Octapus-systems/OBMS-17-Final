# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Flexible-budget support models.

Two building blocks live here:

* eh.budget.activity -- the manual activity register. One row per
  measurement period on a budget (e.g. "Q1: 1,000 units budgeted,
  1,200 units actual"). Budget lines whose driver is the activity
  register resolve their activity pair by summing the register rows
  that overlap the line's own period.

* eh.budget.forecast.revision (+ line) -- period-stamped rolling
  reforecast snapshots. action_reforecast on the budget re-runs the
  seeding engines over trailing actuals plus re-projected remaining
  periods and stores the result here. The budget's own lines (the
  baseline) are never touched, so baseline-vs-latest-revision variance
  reporting stays honest.
"""

from odoo import api, fields, models


class EhBudgetActivity(models.Model):
    _name = 'eh.budget.activity'
    _description = "Budget activity register (flexible budget driver)"
    _order = 'period_from, id'

    budget_id = fields.Many2one(
        'eh.budget.budget',
        required=True,
        ondelete='cascade',
        index=True,
    )
    name = fields.Char(
        help="Optional label for the measurement period (e.g. 'Q1 units').",
    )
    period_from = fields.Date(required=True)
    period_to = fields.Date(required=True)
    budgeted_activity = fields.Float(
        required=True, default=0.0, digits=(16, 4),
        help=(
            "Planned activity level for this period, in the budget's "
            "activity unit (units sold, machine hours, headcount...)."
        ),
    )
    actual_activity = fields.Float(
        default=0.0, digits=(16, 4),
        help="Measured activity level for this period.",
    )

    _sql_constraints = [
        ('activity_period_range', 'check(period_from <= period_to)', 'Activity period_from must be before or equal to period_to.'),  # noqa: E501
        ('activity_non_negative', 'check(budgeted_activity >= 0 AND actual_activity >= 0)', 'Activity levels must be zero or positive.'),  # noqa: E501
    ]


class EhBudgetForecastRevision(models.Model):
    _name = 'eh.budget.forecast.revision'
    _description = "Budget forecast revision (rolling reforecast snapshot)"
    _order = 'revision_date desc, id desc'
    _rec_name = 'name'

    budget_id = fields.Many2one(
        'eh.budget.budget',
        required=True,
        ondelete='cascade',
        index=True,
    )
    name = fields.Char(required=True)
    revision_date = fields.Date(
        required=True,
        default=fields.Date.context_today,
        help="Date the reforecast snapshot was taken.",
    )
    company_id = fields.Many2one(
        related='budget_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='budget_id.currency_id', store=False, readonly=True,
    )
    line_ids = fields.One2many(
        'eh.budget.forecast.revision.line', 'revision_id',
    )
    line_count = fields.Integer(compute='_compute_totals')
    total_amount = fields.Monetary(
        compute='_compute_totals',
        currency_field='currency_id',
        help="Sum of the snapshot line amounts (actuals + projections).",
    )
    notes = fields.Char()

    @api.depends('line_ids.amount')
    def _compute_totals(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)
            rec.total_amount = sum(rec.line_ids.mapped('amount'))


class EhBudgetForecastRevisionLine(models.Model):
    _name = 'eh.budget.forecast.revision.line'
    _description = "Budget forecast revision line"
    _order = 'period_from, account_id, id'

    revision_id = fields.Many2one(
        'eh.budget.forecast.revision',
        required=True,
        ondelete='cascade',
        index=True,
    )
    account_id = fields.Many2one(
        'account.account',
        required=True,
        ondelete='cascade',
        index=True,
    )
    period_from = fields.Date(required=True)
    period_to = fields.Date(required=True)
    amount = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Snapshot amount for this account and period: the posted "
            "actual for elapsed periods, the re-projected figure for "
            "remaining periods. Always a positive magnitude, matching "
            "the budgeted_amount convention on budget lines."
        ),
    )
    source = fields.Char(
        help=(
            "Where the amount came from: 'actual' for elapsed periods, "
            "otherwise the projection algorithm used (holt_winters, "
            "linear_trend, mean)."
        ),
    )
    currency_id = fields.Many2one(
        related='revision_id.currency_id', store=False, readonly=True,
    )

    _sql_constraints = [
        ('revision_period_range', 'check(period_from <= period_to)', 'Revision line period_from must be before or equal to period_to.'),  # noqa: E501
    ]
