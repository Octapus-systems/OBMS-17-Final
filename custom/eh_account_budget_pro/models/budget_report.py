# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Budget vs Actual analytics report.

eh.budget.line carries non-stored compute fields for actual_amount and
variance_amount. Non-stored compute fields cannot be aggregated by
SQL, so the graph and pivot views cannot use them as measures (the
JS web client throws "No aggregate function has been provided").

This module exposes a read-only SQL VIEW that aggregates the same
data with stored, SQL-aggregable columns so the graph and pivot
views work natively. One row per (budget_line, account, period); the
actual is computed by an inline aggregation over posted account
moves limited to the budget's company and the line's period range.
"""

from psycopg2 import sql

from odoo import fields, models, tools


_INCOME_LIKE_ACCOUNT_TYPES = (
    'income', 'income_other', 'liability_payable',
    'liability_credit_card', 'liability_current',
    'liability_non_current', 'equity', 'equity_unaffected',
)


class EhBudgetReport(models.Model):
    _name = 'eh.budget.report'
    _description = "Budget vs Actual report (SQL view)"
    _auto = False
    _order = 'period_from desc, account_id'
    _rec_name = 'account_id'

    budget_id = fields.Many2one(
        'eh.budget.budget', string="Budget", readonly=True,
    )
    line_id = fields.Many2one(
        'eh.budget.line', string="Budget Line", readonly=True,
    )
    company_id = fields.Many2one(
        'res.company', string="Company", readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency', string="Currency", readonly=True,
    )
    account_id = fields.Many2one(
        'account.account', string="Account", readonly=True,
    )
    analytic_account_id = fields.Many2one(
        'account.analytic.account', string="Analytic Account", readonly=True,
    )
    period_from = fields.Date(string="Period From", readonly=True)
    period_to = fields.Date(string="Period To", readonly=True)
    budgeted_amount = fields.Monetary(
        string="Budgeted",
        currency_field='currency_id',
        readonly=True,
        aggregator='sum',
    )
    actual_amount = fields.Monetary(
        string="Actual",
        currency_field='currency_id',
        readonly=True,
        aggregator='sum',
    )
    variance_amount = fields.Monetary(
        string="Variance",
        currency_field='currency_id',
        readonly=True,
        aggregator='sum',
        help=(
            "Actual minus Budgeted. Sign is normalised so income and "
            "liability accounts read as the natural economic direction."
        ),
    )

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        # Identifier interpolation goes through psycopg2's sql module
        # (safe quoting of the view name); the income-like list goes
        # through a normal %s parameter so the values cross the
        # adapter boundary as bound parameters, not string literals.
        query = sql.SQL("""
CREATE OR REPLACE VIEW {table} AS (
SELECT
    bl.id AS id,
    bl.id AS line_id,
    bl.budget_id AS budget_id,
    b.company_id AS company_id,
    b.currency_id AS currency_id,
    bl.account_id AS account_id,
    bl.analytic_account_id AS analytic_account_id,
    bl.period_from AS period_from,
    bl.period_to AS period_to,
    bl.budgeted_amount AS budgeted_amount,
    COALESCE(actuals.actual_amount, 0.0) AS actual_amount,
    CASE
        WHEN aa.account_type IN %s
            THEN -COALESCE(actuals.actual_amount, 0.0)
                 - bl.budgeted_amount
        ELSE COALESCE(actuals.actual_amount, 0.0)
             - bl.budgeted_amount
    END AS variance_amount
FROM eh_budget_line bl
JOIN eh_budget_budget b ON b.id = bl.budget_id
LEFT JOIN account_account aa ON aa.id = bl.account_id
LEFT JOIN LATERAL (
    SELECT SUM(
        CASE
            WHEN bl.analytic_account_id IS NULL THEN aml.balance
            ELSE aml.balance * COALESCE(
                (aml.analytic_distribution ->> bl.analytic_account_id::text)::float,
                0.0
            ) / 100.0
        END
    ) AS actual_amount
    FROM account_move_line aml
    JOIN account_move am ON am.id = aml.move_id
    WHERE aml.company_id = b.company_id
      AND aml.account_id = bl.account_id
      AND aml.date BETWEEN bl.period_from AND bl.period_to
      AND am.state = 'posted'
      AND aml.parent_state = 'posted'
      AND (
          bl.analytic_account_id IS NULL
          OR aml.analytic_distribution ? bl.analytic_account_id::text
      )
) actuals ON TRUE
)
""").format(table=sql.Identifier(self._table))
        self.env.cr.execute(query, [_INCOME_LIKE_ACCOUNT_TYPES])
