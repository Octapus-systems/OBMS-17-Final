# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.report.forecast: multi period forecast scenarios.

A forecast picks a base report (typically Profit and Loss or Cash Flow),
a baseline period, and a growth method. project() runs the base report
once to get baseline numbers, then projects horizon_months periods
forward by applying the growth model to every numeric cell.

v1 supports two growth methods:

* flat: all forward periods equal the baseline.
* linear: each forward period is the baseline times (1 + growth)^n where
  n is the period index.

Seasonal patterns (per month factor) and scenario stacking (baseline vs
optimistic vs pessimistic side by side) are deferred to v1.1; the model
fields are placeholders ready for that work.
"""

import json

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhReportForecast(models.Model):
    _name = 'eh.report.forecast'
    _description = "Multi period report forecast scenario"
    _order = 'name'

    name = fields.Char(required=True)
    scenario_label = fields.Char(default="Baseline")
    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )

    base_report_id = fields.Many2one(
        'eh.account.dynamic.report',
        required=True,
        ondelete='cascade',
        index=True,
    )
    base_date_from = fields.Date(required=True)
    base_date_to = fields.Date(required=True)

    horizon_months = fields.Integer(
        required=True,
        default=lambda self: (
            self.env.company.eh_forecast_default_horizon or 12
        ),
        help="Number of forward periods to project.",
    )

    growth_method = fields.Selection(
        [
            ('flat', "Flat (no change)"),
            ('linear', "Linear monthly growth %"),
        ],
        default='linear',
        required=True,
    )
    monthly_growth_pct = fields.Float(
        default=0.0,
        help=(
            "Monthly growth percentage applied compoundingly under the "
            "linear growth method. 5.0 means 5 percent per month."
        ),
    )

    last_run = fields.Datetime(readonly=True)

    _sql_constraints = [
        ('positive_horizon', 'check(horizon_months > 0)', 'Forecast horizon must be at least one month.'),
    ]

    # ---- public api ----

    def project(self, options=None):
        """Run the base report and project horizon_months forward.

        :param options: optional overrides merged into the base options
            (companies, posted_only, show_zero, etc.). The date block is
            always derived from base_date_from and base_date_to.
        :return: dict with keys: baseline, periods, meta.
        """
        self.ensure_one()
        if self.base_date_from > self.base_date_to:
            raise UserError(_(
                "base_date_from cannot be later than base_date_to.",
            ))
        base_options = self._build_base_options(options or {})
        baseline = self.base_report_id.render(base_options)
        periods = self._project_periods(baseline)
        self.last_run = fields.Datetime.now()
        return {
            'baseline': baseline,
            'periods': periods,
            'meta': {
                'forecast_id': self.id,
                'name': self.name,
                'scenario_label': self.scenario_label,
                'horizon_months': self.horizon_months,
                'growth_method': self.growth_method,
                'monthly_growth_pct': self.monthly_growth_pct,
                'base_date_from': self.base_date_from.isoformat(),
                'base_date_to': self.base_date_to.isoformat(),
            },
        }

    def action_project_now(self):
        """Run the projection and return the result as a notification.

        For the OWL viewer integration in v1.1 this method will return a
        client action that opens a side-by-side scenario comparison.
        """
        for forecast in self:
            forecast.project()
        return True

    # ---- internals ----

    def _build_base_options(self, overrides):
        options = {
            'date': {
                'mode': 'range',
                'date_from': self.base_date_from.isoformat(),
                'date_to': self.base_date_to.isoformat(),
            },
            'company_ids': overrides.get(
                'company_ids', [self.company_id.id],
            ),
            'journal_ids': overrides.get('journal_ids', []),
            'partner_ids': overrides.get('partner_ids', []),
            'account_ids': overrides.get('account_ids', []),
            'posted_only': bool(overrides.get('posted_only', True)),
            'show_zero': bool(overrides.get('show_zero', False)),
        }
        return options

    def _project_periods(self, baseline):
        periods = []
        period_from = self.base_date_from
        period_to = self.base_date_to
        for index in range(self.horizon_months):
            period_from = period_from + relativedelta(months=1)
            period_to = period_to + relativedelta(months=1)
            factor = self._growth_factor(index + 1)
            projected = self._apply_factor(baseline, factor)
            periods.append({
                'period_index': index + 1,
                'date_from': period_from.isoformat(),
                'date_to': period_to.isoformat(),
                'period_label': period_from.strftime('%Y-%m'),
                'growth_factor': round(factor, 6),
                'lines': projected['lines'],
                'totals': projected['totals'],
            })
        return periods

    def _growth_factor(self, period_n):
        if self.growth_method == 'flat':
            return 1.0
        if self.growth_method == 'linear':
            monthly = 1.0 + (self.monthly_growth_pct / 100.0)
            return monthly ** period_n
        return 1.0

    @staticmethod
    def _apply_factor(baseline, factor):
        out_lines = []
        for line in baseline.get('lines') or []:
            out_columns = []
            for col in line.get('columns') or []:
                value = col.get('value')
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    out_columns.append(
                        dict(col, value=round(value * factor, 2)),
                    )
                else:
                    out_columns.append(dict(col))
            out_lines.append(dict(line, columns=out_columns))
        out_totals = {}
        for k, v in (baseline.get('totals') or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out_totals[k] = round(v * factor, 2)
            else:
                out_totals[k] = v
        return {'lines': out_lines, 'totals': out_totals}
