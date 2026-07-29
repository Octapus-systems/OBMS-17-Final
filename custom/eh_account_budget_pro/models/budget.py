# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Budget Pro models: budget header + budget lines.

A budget is a named period (date_from to date_to) carrying a list of
lines. Each line pins one account to a sub period within the budget and
sets a budgeted_amount. The actual_amount is computed from posted
journal items, in batch per budget, so a budget with hundreds of lines
runs one SQL query rather than one per line.

Versioning: action_create_version copies a budget to a new draft version
with a parent_id link. The v1 / v1.1 / v2 evolution is auditable through
the parent chain.

Lifecycle: draft -> confirmed -> closed. Actuals are computed at all
states so users can monitor a draft vs reality before confirming.
"""

import re
from collections import defaultdict

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import SQL

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery


_CODE_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]*$')

# Credit-natured account types: actual balances on these post negative in
# Odoo convention while budget figures are entered as positive magnitudes,
# so variance-style computes flip the actual sign before comparing. Kept
# as one module constant so every compute normalises identically.
_INCOME_LIKE = (
    'income', 'income_other', 'liability_payable',
    'liability_credit_card', 'liability_current',
    'liability_non_current', 'equity', 'equity_unaffected',
)


class EhBudget(models.Model):
    _name = 'eh.budget.budget'
    _description = "Budget"
    _order = 'date_from desc, name'
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.cron.batch.mixin',
        'eh.workflow.guard',
    ]
    _rec_name = 'name'

    # State advances only through the lifecycle actions below (each runs as
    # su via _eh_workflow_action); a direct non-superuser write to state is
    # refused by eh.workflow.guard, closing the RPC-bypass of action_confirm
    # and its checks.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, tracking=True)
    code = fields.Char(required=True, copy=False)

    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id',
        store=True,
        readonly=True,
    )

    date_from = fields.Date(required=True, tracking=True)
    date_to = fields.Date(required=True, tracking=True)

    parent_id = fields.Many2one(
        'eh.budget.budget',
        ondelete='set null',
        help="Previous version of this budget. Forms a versioning chain.",
    )
    version_label = fields.Char(default="v1")
    is_rolling = fields.Boolean(
        default=False,
        help="When enabled, a future cron can extend this budget rolling forward.",
    )
    overrun_policy = fields.Selection(
        [
            ('off', "Off (no commitment tracking)"),
            ('warn', "Warn (commit + warn at confirm)"),
            ('block', "Block (commit + block at confirm)"),
        ],
        default='off', required=True, tracking=True,
        help=(
            "Encumbrance behaviour on this budget. off: ignore "
            "purchase orders entirely (the historical default). "
            "warn: create a commitment when a PO is confirmed and "
            "post a warning to the budget chatter when the line "
            "would go negative. block: prevent the PO confirm when "
            "available is insufficient. Per-budget rather than "
            "per-line so a single policy decision covers an entire "
            "budget version."
        ),
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('confirmed', "Confirmed"),
            ('revised', "Revised"),
            ('closed', "Closed"),
        ],
        default='draft',
        required=True,
        tracking=True,
        index=True,
    )
    budget_type = fields.Selection(
        [
            ('expense', "Expense"),
            ('revenue', "Revenue"),
            ('both', "Revenue & Expense"),
        ],
        default='both', required=True, tracking=True,
        help=(
            "Scope of the budget. Drives which account types the split "
            "wizard offers and documents the budget's intent; 'both' "
            "places no restriction."
        ),
    )
    revised_budget_id = fields.Many2one(
        'eh.budget.budget', readonly=True, copy=False,
        string="Superseded by",
        help="The revision that superseded this budget, if any.",
    )

    line_ids = fields.One2many(
        'eh.budget.line',
        'budget_id',
        copy=True,
    )
    line_count = fields.Integer(compute='_compute_line_count')

    total_budgeted = fields.Monetary(
        compute='_compute_totals',
        currency_field='currency_id',
    )
    total_actual = fields.Monetary(
        compute='_compute_totals',
        currency_field='currency_id',
    )
    total_variance = fields.Monetary(
        compute='_compute_totals',
        currency_field='currency_id',
    )
    total_variance_pct = fields.Float(
        compute='_compute_totals',
        digits=(8, 2),
    )
    total_theoretical = fields.Monetary(
        compute='_compute_totals',
        currency_field='currency_id',
        help="Sum of line theoretical (time-prorated) amounts to date.",
    )

    # ---- flexible budget ----
    activity_uom = fields.Char(
        string="Activity Unit",
        help=(
            "Label for the activity measure driving flexible-budget "
            "lines (units sold, machine hours, seats...). Documentation "
            "only; the maths uses the activity register values."
        ),
    )
    activity_period_ids = fields.One2many(
        'eh.budget.activity', 'budget_id',
        copy=True,
        help=(
            "Manual activity register: one row per measurement period "
            "with the budgeted and actual activity level. Lines whose "
            "driver is the activity register sum the rows overlapping "
            "their own period."
        ),
    )
    total_flexed = fields.Monetary(
        compute='_compute_flex_totals',
        currency_field='currency_id',
        help="Sum of line flexed amounts (budget restated at actual activity).",
    )
    total_flexed_variance = fields.Monetary(
        compute='_compute_flex_totals',
        currency_field='currency_id',
        help="Sum of line flexed variances (actual minus flexed budget).",
    )
    total_volume_variance = fields.Monetary(
        compute='_compute_flex_totals',
        currency_field='currency_id',
        help="Sum of line volume variances (flexed minus static budget).",
    )

    # ---- rolling reforecast ----
    forecast_revision_ids = fields.One2many(
        'eh.budget.forecast.revision', 'budget_id',
        copy=False,
        help=(
            "Period-stamped reforecast snapshots. The baseline lines are "
            "never overwritten; each reforecast stores a new revision."
        ),
    )
    active_revision_id = fields.Many2one(
        'eh.budget.forecast.revision',
        compute='_compute_active_revision',
        string="Latest Reforecast",
        help="Most recent forecast revision (by revision date, then id).",
    )
    revision_count = fields.Integer(compute='_compute_active_revision')

    notes = fields.Html()

    _sql_constraints = [
        ('unique_code_company', 'unique(code, company_id)', 'Budget code must be unique per company.'),
        ('date_range', 'check(date_from <= date_to)', 'date_from must be before or equal to date_to.'),
    ]

    @api.constrains('code')
    def _check_code_format(self):
        for rec in self:
            if not _CODE_RE.match(rec.code or ''):
                raise ValidationError(_(
                    "Budget code must match [a-zA-Z][a-zA-Z0-9_]* (got %r).",
                ) % rec.code)

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.depends('line_ids.budgeted_amount', 'line_ids.actual_amount',
                 'line_ids.theoretical_amount')
    def _compute_totals(self):
        for rec in self:
            budgeted = sum(rec.line_ids.mapped('budgeted_amount'))
            actual = sum(rec.line_ids.mapped('actual_amount'))
            rec.total_budgeted = budgeted
            rec.total_actual = actual
            rec.total_variance = actual - budgeted
            rec.total_variance_pct = self._safe_variance_pct(budgeted, actual)
            rec.total_theoretical = sum(
                rec.line_ids.mapped('theoretical_amount'))

    @staticmethod
    def _safe_variance_pct(budgeted, actual):
        """Variance percentage that does not silently swallow overruns.

        With a zero budget, the standard formula would divide by zero so
        the previous code returned 0%, which read as 'no variance' even
        when actual was non zero. Now we return 100.0 to signal a full
        overrun against the zero baseline; views can pair this with the
        is_zero_budget flag to render an explicit 'n/a' badge instead of
        a misleading number.
        """
        if budgeted:
            return (actual - budgeted) / budgeted * 100.0
        if actual:
            return 100.0
        return 0.0

    @api.depends('line_ids.flexed_amount', 'line_ids.flexed_variance',
                 'line_ids.volume_variance')
    def _compute_flex_totals(self):
        for rec in self:
            rec.total_flexed = sum(rec.line_ids.mapped('flexed_amount'))
            rec.total_flexed_variance = sum(
                rec.line_ids.mapped('flexed_variance'))
            rec.total_volume_variance = sum(
                rec.line_ids.mapped('volume_variance'))

    @api.depends('forecast_revision_ids.revision_date')
    def _compute_active_revision(self):
        for rec in self:
            revisions = rec.forecast_revision_ids.sorted(
                key=lambda r: (
                    r.revision_date or fields.Date.today(), r.id or 0,
                ),
                reverse=True,
            )
            rec.active_revision_id = revisions[:1]
            rec.revision_count = len(revisions)

    # ---- lifecycle ----

    def action_confirm(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'draft':
                continue
            if not rec.line_ids:
                raise UserError(_(
                    "Cannot confirm a budget with no lines: %s",
                ) % rec.name)
            rec.state = 'confirmed'
        return True

    def action_close(self):
        self = self._eh_workflow_action()
        for rec in self:
            rec.state = 'closed'
        return True

    def action_reset_draft(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'closed':
                rec.state = 'draft'
        return True

    def action_recompute_actuals(self):
        """Force a recompute of actual_amount on every line."""
        for rec in self:
            rec.line_ids.invalidate_recordset(['actual_amount'])
        return True

    def action_create_version(self):
        """Copy each budget as a new draft version with parent_id link."""
        if len(self) > 1:
            raise UserError(_("Create one version at a time."))
        self = self._eh_workflow_action()
        rec = self
        new_label = self._next_version_label(rec.version_label)
        new_code = self._next_version_code(rec.code, new_label)
        copy = rec.copy({
            'name': "%s (%s)" % (rec.name, new_label),
            'code': new_code,
            'version_label': new_label,
            'parent_id': rec.id,
            'state': 'draft',
        })
        # Supersede the prior version: link it forward and, if it was
        # live, mark it revised so only the new version is the active one.
        supersede_vals = {'revised_budget_id': copy.id}
        if rec.state == 'confirmed':
            supersede_vals['state'] = 'revised'
        rec.write(supersede_vals)
        return {
            'type': 'ir.actions.act_window',
            'name': _("New Budget Version"),
            'res_model': self._name,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'res_id': copy.id,
            'target': 'current',
        }

    @staticmethod
    def _next_version_label(current):
        if not current:
            return 'v2'
        match = re.match(r'^v(\d+)(?:\.(\d+))?$', current.strip())
        if not match:
            return current + ' next'
        major = int(match.group(1))
        minor = int(match.group(2)) if match.group(2) else 0
        return "v%d.%d" % (major, minor + 1)

    @staticmethod
    def _next_version_code(current_code, label):
        suffix = '_' + label.replace('.', '_')
        if current_code.endswith(suffix):
            return current_code
        return current_code + suffix

    def action_open_split_wizard(self):
        """Open the grid split wizard (account x analytic x period)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Split budget into a grid"),
            'res_model': 'eh.budget.split.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_budget_id': self.id},
        }

    def action_split_lines_by_month(self):
        """Replace each existing line with twelve monthly slices.

        Splits the line's budgeted_amount equally across the calendar
        months that overlap the budget period. Useful for converting an
        annual draft into monthly tracking lines.
        """
        from calendar import monthrange
        for budget in self:
            if budget.state != 'draft':
                raise UserError(_(
                    "Only draft budgets can be split (got %s).",
                ) % budget.state)
            new_lines = []
            for line in budget.line_ids:
                start = line.period_from
                end = line.period_to
                cur = start.replace(day=1)
                slices = []
                while cur <= end:
                    last = cur.replace(
                        day=monthrange(cur.year, cur.month)[1],
                    )
                    slices.append((max(cur, start), min(last, end)))
                    next_month = cur.month + 1
                    next_year = cur.year + (1 if next_month > 12 else 0)
                    next_month = next_month if next_month <= 12 else 1
                    cur = cur.replace(year=next_year, month=next_month, day=1)
                if not slices:
                    continue
                slice_amount = round(
                    line.budgeted_amount / len(slices), 2,
                )
                # Cost behaviour survives the split; a semi-variable
                # fixed portion is divided equally like the amount.
                slice_fixed = round(
                    (line.fixed_portion or 0.0) / len(slices), 2,
                )
                for i, (sf, st) in enumerate(slices):
                    new_lines.append({
                        'budget_id': budget.id,
                        'sequence': line.sequence * 100 + i,
                        'account_id': line.account_id.id,
                        'analytic_account_id': line.analytic_account_id.id,
                        'period_from': sf,
                        'period_to': st,
                        'budgeted_amount': slice_amount,
                        'behaviour': line.behaviour,
                        'fixed_portion': slice_fixed,
                        'notes': line.notes,
                    })
            if new_lines:
                budget.line_ids.unlink()
                self.env['eh.budget.line'].create(new_lines)
        return True

    @api.model
    def cron_roll_forward(self):
        """Extend confirmed rolling budgets one calendar month forward.

        For each `eh.budget.budget` with `is_rolling=True` whose `date_to`
        is less than 31 days in the future, append one calendar month of
        lines copied from the most recent monthly slice. The cron runs
        with a per-budget savepoint so a single bad budget never freezes
        the queue.
        """
        from calendar import monthrange
        from datetime import timedelta as _td
        Budget = self.env['eh.budget.budget']
        today = fields.Date.context_today(Budget)
        horizon = today + _td(days=31)
        candidates = Budget.search([
            ('is_rolling', '=', True),
            ('state', '=', 'confirmed'),
            ('date_to', '<=', horizon),
        ])
        # Per-budget savepoint via the shared batch mixin so a single
        # bad budget rolls back only its own slice and never aborts the
        # rest of the queue.
        def _roll(budget):
            # Find the latest monthly slice on this budget; copy those
            # lines forward, shifted by one month.
            last_lines = budget.line_ids.filtered(
                lambda l: l.period_to == budget.date_to,
            )
            if not last_lines:
                return
            next_from = budget.date_to + _td(days=1)
            next_year = next_from.year
            next_month = next_from.month
            next_to = next_from.replace(
                day=monthrange(next_year, next_month)[1],
            )
            new_vals = []
            for line in last_lines:
                new_vals.append({
                    'budget_id': budget.id,
                    'sequence': line.sequence,
                    'account_id': line.account_id.id,
                    'analytic_account_id': line.analytic_account_id.id,
                    'period_from': next_from,
                    'period_to': next_to,
                    'budgeted_amount': line.budgeted_amount,
                    # Carry the cost-behaviour classification forward;
                    # the driver stays on the activity register default
                    # because a proxy line reference would point at the
                    # prior month's line.
                    'behaviour': line.behaviour,
                    'fixed_portion': line.fixed_portion,
                    'budgeted_qty': line.budgeted_qty,
                    'budgeted_unit_price': line.budgeted_unit_price,
                    'notes': line.notes,
                })
            self.env['eh.budget.line'].create(new_vals)
            budget.write({'date_to': next_to})
            budget.message_post(body=_(
                "Rolling budget extended forward to %s "
                "(cron_roll_forward)."
            ) % next_to.isoformat())

        self._eh_for_each_savepoint(
            candidates, _roll, log_label="Rolling budget roll-forward",
        )
        return True

    def action_seed_from_forecast(self):
        """Replace lines with one monthly line per income/expense account,
        budgeted_amount projected by Holt-Winters from the last 24 months
        of posted balances.

        Falls back to linear trend on accounts with fewer than two seasons
        of history; falls back to the prior-year mean for accounts with
        less than 12 months. The fallbacks are explicit, not silent: each
        line records the algorithm used in `notes` so the auditor can
        verify the projection's basis.
        """
        from calendar import monthrange
        from datetime import timedelta as _td
        from odoo.addons.eh_account_budget_pro.tools.forecast import (
            ForecastError, holt_winters_additive, linear_trend,
            project_trend,
        )

        for budget in self:
            if budget.state != 'draft':
                raise UserError(_(
                    "Only draft budgets can be seeded (got %s).",
                ) % budget.state)
            company_id = budget.company_id.id
            forecast_to = budget.date_from - _td(days=1)
            # relativedelta keeps a 29-Feb base date valid when the target
            # year is not a leap year (date.replace(year=...) would raise).
            forecast_from = (
                forecast_to - relativedelta(years=2)
            ) + _td(days=1)
            history = self._fetch_monthly_history_per_account(
                company_id=company_id,
                date_from=forecast_from,
                date_to=forecast_to,
            )
            if not history:
                continue

            # Months covered in the budget window.
            months = self._months_in_range(budget.date_from, budget.date_to)
            if not months:
                continue

            new_lines = []
            seq = 10
            for account_id, monthly_series in history.items():
                projections, algo = self._project_account_series(
                    monthly_series, len(months),
                    holt_winters_additive, linear_trend, project_trend,
                    ForecastError,
                )
                for i, (period_from, period_to) in enumerate(months):
                    amount = abs(round(projections[i], 2))
                    new_lines.append({
                        'budget_id': budget.id,
                        'sequence': seq,
                        'account_id': account_id,
                        'period_from': period_from,
                        'period_to': period_to,
                        'budgeted_amount': amount,
                        'notes': "forecast: %s" % algo,
                    })
                    seq += 1

            if budget.line_ids:
                budget.line_ids.unlink()
            if new_lines:
                self.env['eh.budget.line'].create(new_lines)
        return True

    def action_reforecast(self):
        """Store a rolling reforecast snapshot; never touch the baseline.

        For every account carried by the budget's lines the snapshot
        holds one row per calendar month of the budget window:

        * elapsed months (period fully before today) carry the posted
          actual for that account and month, source 'actual';
        * remaining months carry a re-projection produced by the same
          engine stack as action_seed_from_forecast, run over the
          trailing 24 calendar months of posted history ending at the
          last elapsed month. Leading all-zero months are dropped from
          the series first so the algorithm choice (Holt-Winters >=24
          points, linear trend >=6, mean otherwise) reflects real
          history length, not empty pre-history.

        Amounts are stored as positive magnitudes (abs, rounded 2dp),
        matching the budgeted_amount convention. The budget's own lines
        are the untouched baseline; variance reporting can compare
        actuals against either the baseline (variance_amount) or the
        latest revision (revision_variance).
        """
        from datetime import timedelta as _td
        from odoo.addons.eh_account_budget_pro.tools.forecast import (
            ForecastError, holt_winters_additive, linear_trend,
            project_trend,
        )
        revision = self.env['eh.budget.forecast.revision']
        for budget in self:
            if budget.state not in ('confirmed', 'revised'):
                raise UserError(_(
                    "Only confirmed budgets can be reforecast (got %s). "
                    "The baseline must be locked in before revisions "
                    "make sense.",
                ) % budget.state)
            if not budget.line_ids:
                raise UserError(_(
                    "Cannot reforecast a budget with no lines: %s",
                ) % budget.name)
            today = fields.Date.context_today(budget)
            months = self._months_in_range(budget.date_from, budget.date_to)
            elapsed = [m for m in months if m[1] < today]
            remaining = [m for m in months if m[1] >= today]
            accounts = budget.line_ids.mapped('account_id')

            actuals = {}
            if elapsed:
                actuals = self._fetch_monthly_history_per_account(
                    company_id=budget.company_id.id,
                    date_from=elapsed[0][0],
                    date_to=elapsed[-1][1],
                )
            history = {}
            if remaining:
                hist_end = (
                    elapsed[-1][1] if elapsed
                    else budget.date_from - _td(days=1)
                )
                hist_from = hist_end.replace(day=1) - relativedelta(months=23)
                history = self._fetch_monthly_history_per_account(
                    company_id=budget.company_id.id,
                    date_from=hist_from,
                    date_to=hist_end,
                )
                hist_months = len(self._months_in_range(hist_from, hist_end))

            line_vals = []
            for account in accounts:
                actual_series = actuals.get(account.id, [0.0] * len(elapsed))
                for i, (period_from, period_to) in enumerate(elapsed):
                    line_vals.append({
                        'account_id': account.id,
                        'period_from': period_from,
                        'period_to': period_to,
                        'amount': abs(round(actual_series[i], 2)),
                        'source': 'actual',
                    })
                if not remaining:
                    continue
                series = list(history.get(account.id, [0.0] * hist_months))
                # Drop leading all-zero months: an account first posted
                # to six months ago has six points of history, not 24.
                while series and not series[0]:
                    series.pop(0)
                if series:
                    projections, algo = self._project_account_series(
                        series, len(remaining),
                        holt_winters_additive, linear_trend, project_trend,
                        ForecastError,
                    )
                else:
                    projections, algo = [0.0] * len(remaining), 'mean'
                for i, (period_from, period_to) in enumerate(remaining):
                    line_vals.append({
                        'account_id': account.id,
                        'period_from': period_from,
                        'period_to': period_to,
                        'amount': abs(round(projections[i], 2)),
                        'source': algo,
                    })

            revision = self.env['eh.budget.forecast.revision'].create({
                'budget_id': budget.id,
                'name': "R%d (%s)" % (
                    len(budget.forecast_revision_ids) + 1,
                    today.isoformat(),
                ),
                'revision_date': today,
                'line_ids': [(0, 0, vals) for vals in line_vals],
            })
            budget.message_post(body=_(
                "Rolling reforecast stored as %s: %d elapsed month(s) at "
                "actuals, %d remaining month(s) re-projected. Baseline "
                "lines unchanged."
            ) % (revision.name, len(elapsed), len(remaining)))
        if len(self) == 1 and revision:
            return {
                'type': 'ir.actions.act_window',
                'name': _("Forecast Revision"),
                'res_model': 'eh.budget.forecast.revision',
                'view_mode': 'form',
                'views': [(False, 'form')],
                'res_id': revision.id,
                'target': 'current',
            }
        return True

    @api.model
    def _months_in_range(self, date_from, date_to):
        """Yield (start, end) pairs for each calendar month touched by
        the closed interval [date_from, date_to]."""
        from calendar import monthrange
        out = []
        cur = date_from.replace(day=1)
        while cur <= date_to:
            last = cur.replace(day=monthrange(cur.year, cur.month)[1])
            slice_to = min(last, date_to)
            slice_from = max(cur, date_from)
            out.append((slice_from, slice_to))
            next_month = cur.month + 1
            next_year = cur.year + (1 if next_month > 12 else 0)
            next_month = next_month if next_month <= 12 else 1
            cur = cur.replace(year=next_year, month=next_month, day=1)
        return out

    @api.model
    def _fetch_monthly_history_per_account(
        self, company_id, date_from, date_to,
    ):
        """Return {account_id: [m0_total, m1_total, ...]} where each list
        is one entry per calendar month between date_from and date_to.

        Income and expense accounts only. Months with no posting receive
        zero so the series stays evenly spaced for forecasting.
        """
        query = MoveLineQuery(self.env, company_ids=[company_id])
        query.where_date_range(date_from=date_from, date_to=date_to)
        query.where_posted_only()
        query.where_account_types((
            'income', 'income_other',
            'expense', 'expense_depreciation', 'expense_direct_cost',
        ))
        query.select_field('account_id')
        query.select(SQL("date_trunc('month', aml.date)"), 'month_start')
        query.select(SQL("SUM(aml.balance)"), 'balance')
        query.group_by(SQL("aml.account_id"), SQL("date_trunc('month', aml.date)"))
        rows = query.execute()
        if not rows:
            return {}

        months = self._months_in_range(date_from, date_to)
        by_account = {}
        index_by_month = {m[0].replace(day=1): i for i, m in enumerate(months)}
        for row in rows:
            account_id = row['account_id']
            ms = row['month_start']
            if hasattr(ms, 'date'):
                ms = ms.date()
            month_key = ms.replace(day=1)
            idx = index_by_month.get(month_key)
            if idx is None:
                continue
            series = by_account.setdefault(
                account_id, [0.0] * len(months),
            )
            series[idx] = float(row['balance'] or 0.0)
        return by_account

    @api.model
    def _project_account_series(
        self, series, periods_ahead,
        holt_winters_fn, linear_fn, project_fn, forecast_error,
    ):
        """Pick a forecasting algorithm appropriate to series length.

        Returns (projections, algorithm_name). Algorithms preferred in
        order: Holt-Winters when there are at least two seasons of
        monthly data; linear trend when there are at least 6 points;
        prior-year mean otherwise.
        """
        n = len(series)
        if n >= 24:
            try:
                return (
                    holt_winters_fn(series, 12, periods_ahead),
                    'holt_winters',
                )
            except forecast_error:
                pass
        if n >= 6:
            try:
                slope, intercept = linear_fn(series)
                return (
                    [project_fn(slope, intercept, n + i)
                     for i in range(periods_ahead)],
                    'linear_trend',
                )
            except forecast_error:
                pass
        # Last resort: mean of available history.
        mean = sum(series) / max(n, 1)
        return [mean] * periods_ahead, 'mean'

    def action_seed_from_prior_year(self):
        """Replace lines with one line per account, budgeted_amount equal
        to that account's posted balance over the prior 12 months.

        Adds a starting baseline so users can adjust amounts up or down
        rather than typing each line from scratch. Only runs on empty or
        draft budgets to avoid destroying user work.
        """
        from datetime import timedelta
        for budget in self:
            if budget.state != 'draft':
                raise UserError(_(
                    "Only draft budgets can be seeded (got %s).",
                ) % budget.state)
            prior_to = budget.date_from - timedelta(days=1)
            # relativedelta avoids a 29-Feb replace() crash in non-leap years.
            prior_from = (prior_to - relativedelta(years=1)) + timedelta(days=1)
            query = MoveLineQuery(
                self.env, company_ids=[budget.company_id.id],
            )
            query.where_date_range(date_from=prior_from, date_to=prior_to)
            query.where_posted_only()
            query.where_account_types((
                'income', 'income_other',
                'expense', 'expense_depreciation', 'expense_direct_cost',
            ))
            query.select_field('account_id')
            query.select(SQL("SUM(aml.balance)"), 'balance')
            query.group_by(SQL("aml.account_id"))
            rows = query.execute()
            if not rows:
                continue
            new_lines = []
            for i, row in enumerate(rows):
                amount = abs(float(row.get('balance') or 0.0))
                if not amount:
                    continue
                new_lines.append({
                    'budget_id': budget.id,
                    'sequence': 10 + i,
                    'account_id': row['account_id'],
                    'period_from': budget.date_from,
                    'period_to': budget.date_to,
                    'budgeted_amount': amount,
                })
            if budget.line_ids:
                budget.line_ids.unlink()
            if new_lines:
                self.env['eh.budget.line'].create(new_lines)
        return True

    def action_view_lines(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Budget Lines"),
            'res_model': 'eh.budget.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('budget_id', '=', self.id)],
            'context': {'default_budget_id': self.id},
        }


class EhBudgetLine(models.Model):
    _name = 'eh.budget.line'
    _description = "Budget line"
    _order = 'sequence, period_from, id'

    budget_id = fields.Many2one(
        'eh.budget.budget',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(default=10)

    account_id = fields.Many2one(
        'account.account',
        required=True,
        ondelete='cascade',
        index=True,
    )
    analytic_account_id = fields.Many2one(
        'account.analytic.account',
        ondelete='set null',
        index=True,
        help=(
            "Optional analytic account. When set, the actual_amount only "
            "counts journal items whose analytic_distribution allocates to "
            "this analytic account. Lines with the same account but "
            "different analytic accounts are independent budgets."
        ),
    )
    period_from = fields.Date(required=True)
    period_to = fields.Date(required=True)

    budgeted_amount = fields.Monetary(
        required=True,
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        related='budget_id.currency_id',
        store=False,
        readonly=True,
    )

    actual_amount = fields.Monetary(
        compute='_compute_actual',
        currency_field='currency_id',
        store=False,
        aggregator='sum',
        help=(
            "Sum of posted journal item balances on this account in the "
            "line's period. Computed in batch per budget."
        ),
    )
    commitment_ids = fields.One2many(
        'eh.budget.commitment', 'budget_line_id',
        help=(
            "Open commitments (purchase orders, contracts) reserving "
            "availability against this line."
        ),
    )
    committed_amount = fields.Monetary(
        compute='_compute_committed',
        currency_field='currency_id',
        store=False,
        aggregator='sum',
        help=(
            "Sum of reserved commitments on this line. Released and "
            "draft commitments do not count: released ones have "
            "moved into actual_amount, draft ones have not yet been "
            "approved."
        ),
    )
    available_amount = fields.Monetary(
        compute='_compute_committed',
        currency_field='currency_id',
        store=False,
        aggregator='sum',
        help=(
            "budgeted_amount minus actual_amount minus "
            "committed_amount. Negative when the budget is "
            "overcommitted."
        ),
    )
    variance_amount = fields.Monetary(
        compute='_compute_variance',
        currency_field='currency_id',
        aggregator='sum',
    )
    variance_pct = fields.Float(
        compute='_compute_variance',
        digits=(8, 2),
        aggregator='avg',
    )

    # ---- cost behaviour / flexible budget ----
    behaviour = fields.Selection(
        [
            ('fixed', "Fixed"),
            ('variable', "Variable"),
            ('semi_variable', "Semi-variable"),
        ],
        default='fixed', required=True,
        help=(
            "Cost behaviour classification. Fixed lines never flex "
            "(flexed = budgeted, the historical behaviour). Variable "
            "lines flex fully with activity. Semi-variable lines keep "
            "fixed_portion constant and flex the remainder."
        ),
    )
    fixed_portion = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Fixed component of a semi-variable line's budgeted amount. "
            "The variable component (budgeted_amount - fixed_portion) "
            "flexes with activity. Ignored for fixed and variable lines."
        ),
    )
    driver = fields.Selection(
        [
            ('activity', "Activity register"),
            ('revenue_line', "Revenue line proxy"),
        ],
        default='activity', required=True,
        help=(
            "Where the activity measure comes from. Activity register: "
            "sum the budget's activity rows overlapping this line's "
            "period. Revenue line proxy: use another line on the same "
            "budget (typically revenue) as a units-sold proxy, with "
            "budgeted activity = its budgeted amount and actual "
            "activity = its sign-normalised actual."
        ),
    )
    driver_line_id = fields.Many2one(
        'eh.budget.line',
        string="Driver Line",
        ondelete='set null',
        index=True,
        help="Line on the same budget acting as the activity proxy.",
    )
    budgeted_activity = fields.Float(
        compute='_compute_flex', digits=(16, 4),
        help="Budgeted activity level resolved from the driver.",
    )
    actual_activity = fields.Float(
        compute='_compute_flex', digits=(16, 4),
        help="Actual activity level resolved from the driver.",
    )
    activity_ratio = fields.Float(
        compute='_compute_flex', digits=(16, 4),
        help=(
            "actual_activity / budgeted_activity. 1.0 when no activity "
            "data exists, so unflexed budgets behave exactly as before."
        ),
    )
    flexed_amount = fields.Monetary(
        compute='_compute_flex',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "Budget restated at the actual activity level: fixed "
            "component + variable component x activity ratio."
        ),
    )
    flexed_variance = fields.Monetary(
        compute='_compute_flex',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "Actual minus flexed budget (sign-normalised): the "
            "spending/price side of the static variance. Positive means "
            "over the flexed allowance on expense lines. Ties exactly: "
            "static variance = flexed variance + volume variance."
        ),
    )
    volume_variance = fields.Monetary(
        compute='_compute_flex',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "Flexed minus static budget: the part of the static "
            "variance explained purely by the activity level moving."
        ),
    )

    # ---- price / efficiency split (variable lines with quantity data) ----
    budgeted_qty = fields.Float(
        digits=(16, 4),
        help="Budgeted input quantity behind the budgeted amount.",
    )
    budgeted_unit_price = fields.Float(
        digits=(16, 4),
        help="Budgeted (standard) price per input unit.",
    )
    actual_qty = fields.Float(
        digits=(16, 4),
        help="Actual input quantity consumed.",
    )
    actual_unit_price = fields.Float(
        digits=(16, 4),
        help="Actual price paid per input unit.",
    )
    flexed_qty = fields.Float(
        compute='_compute_flex', digits=(16, 4),
        help=(
            "Quantity allowance at actual activity: budgeted_qty x "
            "activity ratio."
        ),
    )
    price_variance = fields.Monetary(
        compute='_compute_flex',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "(actual price - budgeted price) x actual quantity. "
            "Positive = adverse on expense lines. Zero unless the line "
            "is variable with quantity data."
        ),
    )
    efficiency_variance = fields.Monetary(
        compute='_compute_flex',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "(actual quantity - flexed quantity) x budgeted price. "
            "Positive = adverse on expense lines. Zero unless the line "
            "is variable with quantity data."
        ),
    )
    split_residual = fields.Monetary(
        compute='_compute_flex',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "flexed variance - price variance - efficiency variance. "
            "Zero when the posted actual equals actual_qty x actual "
            "price and the budgeted amount equals budgeted_qty x "
            "budgeted price; a residual flags inconsistent quantity "
            "data against the ledger."
        ),
    )

    # ---- reforecast comparison ----
    revision_amount = fields.Monetary(
        compute='_compute_revision',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "Amount from the budget's latest forecast revision for this "
            "line's account, summed over the revision months overlapping "
            "the line period. Zero when no revision exists. Revision "
            "snapshots are account-level: analytic-split lines on the "
            "same account each see the full account figure."
        ),
    )
    revision_variance = fields.Monetary(
        compute='_compute_revision',
        currency_field='currency_id',
        aggregator='sum',
        help=(
            "Actual (sign-normalised) minus the latest revision amount: "
            "variance against the rolling reforecast instead of the "
            "baseline."
        ),
    )

    elapsed_pct = fields.Float(
        compute='_compute_theoretical', digits=(8, 2),
        help="Fraction of the line period elapsed as of today (0-100).",
    )
    theoretical_amount = fields.Monetary(
        compute='_compute_theoretical', currency_field='currency_id',
        aggregator='sum',
        help=(
            "Budget that should have been consumed by now if spending "
            "tracked the calendar evenly: budgeted x elapsed fraction."
        ),
    )
    pace_pct = fields.Float(
        compute='_compute_theoretical', digits=(8, 2),
        help=(
            "Actual as a percentage of the theoretical to-date amount. "
            "Over 100 means spending ahead of an even run-rate."
        ),
    )

    notes = fields.Char()

    _sql_constraints = [
        ('period_range', 'check(period_from <= period_to)', 'Line period_from must be before or equal to period_to.'),
        ('amount_non_negative', 'check(budgeted_amount >= 0)', 'Budgeted amount must be zero or positive. Reductions to a '
        'budget should lower the existing line, not be entered as a '
        'negative figure.'),
        ('qty_data_non_negative', 'check(budgeted_qty >= 0 AND actual_qty >= 0 AND '
        'budgeted_unit_price >= 0 AND actual_unit_price >= 0)', 'Quantities and unit prices must be zero or positive.'),
    ]
    # Budget figures are entered as positive magnitudes regardless of the
    # underlying account's natural sign (an income budget of 10k means
    # "we plan to earn 10k"; an expense budget of 5k means "we plan to
    # spend 5k"). A negative figure is almost always a data-entry error
    # and silently corrupts variance maths because _compute_variance
    # interprets the sign as direction. Block at the database level so
    # ORM bypass paths cannot insert it either.
    # Quantity data feeds the price/efficiency split multiplicatively, so
    # a negative quantity or unit price silently flips variance signs.
    # NULLs pass a CHECK, so unset rows are unaffected.

    @api.constrains('behaviour', 'fixed_portion', 'budgeted_amount')
    def _check_fixed_portion(self):
        for rec in self:
            if rec.behaviour != 'semi_variable':
                continue
            fixed = rec.fixed_portion or 0.0
            if fixed < 0.0 or fixed > (rec.budgeted_amount or 0.0) + 0.005:
                raise ValidationError(_(
                    "Semi-variable line on %(account)s: fixed portion "
                    "(%(fixed).2f) must sit between zero and the "
                    "budgeted amount (%(budgeted).2f).",
                ) % {
                    'account': rec.account_id.display_name,
                    'fixed': fixed,
                    'budgeted': rec.budgeted_amount or 0.0,
                })

    @api.constrains('driver', 'driver_line_id', 'budget_id')
    def _check_driver_line(self):
        for rec in self:
            if rec.driver != 'revenue_line':
                continue
            if not rec.driver_line_id:
                raise ValidationError(_(
                    "Line on %s uses the revenue-line proxy driver but "
                    "no driver line is set.",
                ) % rec.account_id.display_name)
            if rec.driver_line_id == rec:
                raise ValidationError(_(
                    "A line cannot be its own activity driver (%s).",
                ) % rec.account_id.display_name)
            if rec.driver_line_id.budget_id != rec.budget_id:
                raise ValidationError(_(
                    "Driver line must belong to the same budget "
                    "(line on %s).",
                ) % rec.account_id.display_name)

    @api.depends(
        'account_id', 'analytic_account_id',
        'period_from', 'period_to',
        'budget_id.company_id',
    )
    def _compute_actual(self):
        if not self:
            return
        # Group lines by budget for one batch SQL per budget.
        by_budget = defaultdict(list)
        for line in self:
            if line.budget_id:
                by_budget[line.budget_id].append(line)
            else:
                line.actual_amount = 0.0
        for budget, lines in by_budget.items():
            actuals = self._fetch_actuals_batch(budget, lines)
            for line in lines:
                key = (
                    line.account_id.id,
                    line.analytic_account_id.id or False,
                    line.period_from,
                    line.period_to,
                )
                line.actual_amount = actuals.get(key, 0.0)

    @api.depends(
        'commitment_ids.amount', 'commitment_ids.state',
        'budgeted_amount', 'actual_amount', 'account_id',
    )
    def _compute_committed(self):
        """Sum reserved commitments and derive availability.

        Income-vs-expense sign normalisation mirrors
        _compute_variance: actual on income / liability / equity
        accounts is signed credit (negative in Odoo), so we flip it
        before subtracting from the (always-positive) budget so
        availability reads in the natural economic direction.
        Commitment amount is always positive.
        """
        income_like = (
            'income', 'income_other', 'liability_payable',
            'liability_credit_card', 'liability_current',
            'liability_non_current', 'equity', 'equity_unaffected',
        )
        for line in self:
            committed = sum(
                line.commitment_ids
                .filtered(lambda c: c.state == 'reserved')
                .mapped('amount'),
            )
            actual = line.actual_amount or 0.0
            if (
                line.account_id
                and line.account_id.account_type in income_like
            ):
                actual = -actual
            line.committed_amount = committed
            line.available_amount = (
                (line.budgeted_amount or 0.0) - actual - committed
            )

    @api.depends('budgeted_amount', 'actual_amount', 'account_id')
    def _compute_variance(self):
        """Variance = actual minus budget, normalised by account sign.

        Income accounts post a credit-side balance (negative number in
        Odoo convention). The budget_amount is always entered as a
        positive figure ("we plan to earn 10k"). Without normalisation,
        a $10k income budget vs $10k actual revenue would render as
        -10k - 10k = -20k variance, ie a 200% overrun. Normalise by
        flipping the actual sign on income / liability / equity
        accounts so both sides are in the same economic direction.
        """
        income_like = (
            'income', 'income_other', 'liability_payable',
            'liability_credit_card', 'liability_current',
            'liability_non_current', 'equity', 'equity_unaffected',
        )
        for line in self:
            actual = line.actual_amount
            if line.account_id and line.account_id.account_type in income_like:
                actual = -actual
            line.variance_amount = actual - line.budgeted_amount
            line.variance_pct = EhBudget._safe_variance_pct(
                line.budgeted_amount, actual,
            )

    def _normalised_actual(self):
        """Sign-normalised actual: credit-natured accounts flip so the
        figure reads in the same economic direction as the (positive)
        budget, exactly as _compute_variance does."""
        self.ensure_one()
        actual = self.actual_amount or 0.0
        if self.account_id and self.account_id.account_type in _INCOME_LIKE:
            actual = -actual
        return actual

    def _activity_pair(self):
        """Return (budgeted_activity, actual_activity) for this line.

        Driver 'revenue_line': the proxy line's budgeted_amount and
        sign-normalised actual serve as the activity pair (a revenue
        proxy for units sold). Driver 'activity': sum the budget's
        activity register rows whose period overlaps this line's
        period. Returns (0.0, 0.0) when nothing resolves, which the
        flex compute treats as "no activity data, do not flex".
        """
        self.ensure_one()
        if self.driver == 'revenue_line' and self.driver_line_id:
            proxy = self.driver_line_id
            budgeted = proxy.budgeted_amount or 0.0
            actual = proxy._normalised_actual()
            return budgeted, actual
        if not (self.budget_id and self.period_from and self.period_to):
            return 0.0, 0.0
        rows = self.budget_id.activity_period_ids.filtered(
            lambda a: a.period_from and a.period_to
            and a.period_from <= self.period_to
            and a.period_to >= self.period_from,
        )
        return (
            sum(rows.mapped('budgeted_activity')),
            sum(rows.mapped('actual_activity')),
        )

    @api.depends(
        'behaviour', 'fixed_portion', 'budgeted_amount', 'actual_amount',
        'account_id', 'driver',
        'driver_line_id.budgeted_amount', 'driver_line_id.actual_amount',
        'driver_line_id.account_id',
        'period_from', 'period_to',
        'budget_id.activity_period_ids.period_from',
        'budget_id.activity_period_ids.period_to',
        'budget_id.activity_period_ids.budgeted_activity',
        'budget_id.activity_period_ids.actual_activity',
        'budgeted_qty', 'budgeted_unit_price',
        'actual_qty', 'actual_unit_price',
    )
    def _compute_flex(self):
        """Flexible budget, three-way variance tie, and the price /
        efficiency split.

        Conventions (documented here, asserted in the golden tests):

        * activity ratio = actual_activity / budgeted_activity; 1.0
          when budgeted activity is zero or missing, so a line without
          activity data never flexes (flexed = budgeted, the historical
          behaviour).
        * flexed = budgeted (fixed); budgeted x ratio (variable);
          fixed_portion + (budgeted - fixed_portion) x ratio
          (semi-variable).
        * volume variance = flexed - budgeted; flexed variance is
          derived as static - volume so the identity
          static = flexed variance + volume variance holds by
          construction, not merely to floating-point tolerance.
        * flexed quantity = budgeted_qty x ratio. Price variance uses
          ACTUAL quantity ((ap - bp) x aq); efficiency variance uses
          BUDGETED price ((aq - flexed qty) x bp). With consistent data
          (actual cost = aq x ap, budget = bq x bp) the two sum exactly
          to the flexed variance:
          (ap-bp)*aq + (aq-fq)*bp = aq*ap - fq*bp = actual - flexed.
        * split computed only for variable-behaviour lines carrying
          both a budgeted and an actual quantity.
        """
        for line in self:
            budgeted_act, actual_act = line._activity_pair()
            line.budgeted_activity = budgeted_act
            line.actual_activity = actual_act
            ratio = (actual_act / budgeted_act) if budgeted_act else 1.0
            line.activity_ratio = ratio

            budgeted = line.budgeted_amount or 0.0
            if line.behaviour == 'variable':
                flexed = budgeted * ratio
            elif line.behaviour == 'semi_variable':
                fixed_part = min(max(line.fixed_portion or 0.0, 0.0),
                                 budgeted)
                flexed = fixed_part + (budgeted - fixed_part) * ratio
            else:  # fixed
                flexed = budgeted
            line.flexed_amount = flexed

            static = line._normalised_actual() - budgeted
            volume = flexed - budgeted
            line.volume_variance = volume
            flexed_var = static - volume
            line.flexed_variance = flexed_var

            flexed_qty = (line.budgeted_qty or 0.0) * ratio
            line.flexed_qty = flexed_qty
            has_qty_data = (
                line.behaviour == 'variable'
                and line.budgeted_qty and line.actual_qty
            )
            if has_qty_data:
                price_var = (
                    (line.actual_unit_price or 0.0)
                    - (line.budgeted_unit_price or 0.0)
                ) * line.actual_qty
                efficiency_var = (
                    (line.actual_qty - flexed_qty)
                    * (line.budgeted_unit_price or 0.0)
                )
                line.price_variance = price_var
                line.efficiency_variance = efficiency_var
                line.split_residual = flexed_var - price_var - efficiency_var
            else:
                line.price_variance = 0.0
                line.efficiency_variance = 0.0
                line.split_residual = 0.0

    @api.depends(
        'budget_id.forecast_revision_ids.revision_date',
        'budget_id.forecast_revision_ids.line_ids.amount',
        'account_id', 'period_from', 'period_to', 'actual_amount',
    )
    def _compute_revision(self):
        """Variance against the latest reforecast revision instead of
        the baseline. The revision snapshot is account x month; a line
        picks up the snapshot rows for its account overlapping its own
        period."""
        for line in self:
            revision = (
                line.budget_id.active_revision_id
                if line.budget_id else False
            )
            amount = 0.0
            if revision and line.account_id \
                    and line.period_from and line.period_to:
                for rev_line in revision.line_ids:
                    if (rev_line.account_id == line.account_id
                            and rev_line.period_from <= line.period_to
                            and rev_line.period_to >= line.period_from):
                        amount += rev_line.amount
            line.revision_amount = amount
            line.revision_variance = line._normalised_actual() - amount

    @api.depends('budgeted_amount', 'actual_amount', 'account_id',
                 'period_from', 'period_to')
    def _compute_theoretical(self):
        """Time-prorated pacing: how much of the budget should be spent
        by today, and how the actual compares to that run-rate."""
        income_like = (
            'income', 'income_other', 'liability_payable',
            'liability_credit_card', 'liability_current',
            'liability_non_current', 'equity', 'equity_unaffected',
        )
        today = fields.Date.context_today(self)
        for line in self:
            fraction = line._elapsed_fraction(today)
            line.elapsed_pct = fraction * 100.0
            theoretical = (line.budgeted_amount or 0.0) * fraction
            line.theoretical_amount = theoretical
            actual = line.actual_amount or 0.0
            if (line.account_id
                    and line.account_id.account_type in income_like):
                actual = -actual
            line.pace_pct = (
                (actual / theoretical * 100.0) if theoretical else 0.0
            )

    def _elapsed_fraction(self, today):
        """Fraction (0..1) of this line's period elapsed as of `today`."""
        self.ensure_one()
        start, end = self.period_from, self.period_to
        if not start or not end:
            return 0.0
        if today <= start:
            return 0.0
        if today >= end:
            return 1.0
        span = (end - start).days
        if span <= 0:
            return 1.0
        return (today - start).days / span

    @api.model
    def _fetch_actuals_batch(self, budget, lines):
        """Return a dict keyed by (account_id, analytic_account_id_or_false,
        period_from, period_to) carrying the SUM(balance) for posted journal
        items in the matching period, account, and (when set) analytic
        account.

        One SQL pass per budget. Distributes results to individual lines
        in Python so a budget with hundreds of lines does not trigger
        hundreds of queries. The analytic side runs an additional
        targeted pass per analytic account because postgres jsonb keys
        for the distribution are matched per row in the WHERE clause.
        """
        if not lines:
            return {}
        account_ids = list({l.account_id.id for l in lines if l.account_id})
        if not account_ids:
            return {}
        period_from = min(l.period_from for l in lines)
        period_to = max(l.period_to for l in lines)

        non_analytic_lines = [l for l in lines if not l.analytic_account_id]
        analytic_lines = [l for l in lines if l.analytic_account_id]

        result = {}

        if non_analytic_lines:
            query = MoveLineQuery(
                self.env,
                company_ids=[budget.company_id.id],
            )
            query.where_date_range(date_from=period_from, date_to=period_to)
            query.where_accounts(account_ids)
            query.where_posted_only()
            query.select_field('account_id')
            query.select_field('date')
            query.select(SQL("aml.balance"), 'balance')
            rows = query.execute()
            rows_by_account = {}
            for row in rows:
                rows_by_account.setdefault(row['account_id'], []).append(row)
            for line in non_analytic_lines:
                account_id = line.account_id.id
                total = 0.0
                for row in rows_by_account.get(account_id, ()):
                    if line.period_from <= row['date'] <= line.period_to:
                        total += float(row.get('balance') or 0.0)
                result[(account_id, False,
                        line.period_from, line.period_to)] = total

        if analytic_lines:
            # Group by analytic so we run one query per analytic account
            # rather than O(lines) queries.
            by_analytic = defaultdict(list)
            for line in analytic_lines:
                by_analytic[line.analytic_account_id.id].append(line)
            for analytic_id, alines in by_analytic.items():
                query = MoveLineQuery(
                    self.env,
                    company_ids=[budget.company_id.id],
                )
                query.where_date_range(
                    date_from=min(l.period_from for l in alines),
                    date_to=max(l.period_to for l in alines),
                )
                query.where_accounts(
                    list({l.account_id.id for l in alines}),
                )
                query.where_posted_only()
                query.where_analytic_accounts([analytic_id])
                query.select_field('account_id')
                query.select_field('date')
                query.select(SQL(
                    "aml.balance * COALESCE(("
                    "aml.analytic_distribution ->> %s)::float, 0.0) / 100.0",
                    str(analytic_id),
                ), 'allocated_balance')
                rows = query.execute()
                rows_by_account = {}
                for row in rows:
                    rows_by_account.setdefault(
                        row['account_id'], [],
                    ).append(row)
                for line in alines:
                    account_id = line.account_id.id
                    total = 0.0
                    for row in rows_by_account.get(account_id, ()):
                        if line.period_from <= row['date'] <= line.period_to:
                            total += float(
                                row.get('allocated_balance') or 0.0,
                            )
                    result[(account_id, analytic_id,
                            line.period_from, line.period_to)] = total

        return result

    def action_view_actuals(self):
        """Open the journal items list filtered to the line's account
        and period for inspection."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Posted Journal Items"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('account_id', '=', self.account_id.id),
                ('date', '>=', self.period_from),
                ('date', '<=', self.period_to),
                ('parent_state', '=', 'posted'),
                ('company_id', '=', self.budget_id.company_id.id),
            ],
        }
