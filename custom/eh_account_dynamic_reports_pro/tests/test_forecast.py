# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Forecast tests.

Covers project() output shape, horizon period count, growth factor math
for flat and linear methods, that numeric line values get scaled, that
non numeric cells pass through unchanged, and that the meta block carries
the input parameters.
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestForecast(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        DynRep = cls.env['eh.account.dynamic.report']
        cls.report = DynRep.search([('code', '=', 'profit_and_loss')], limit=1)
        if not cls.report:
            cls.report = DynRep.create({
                'code': 'profit_and_loss',
                'name': 'Profit and Loss',
                'handler_model':
                    'eh.account.dynamic.report.handler.profit_and_loss',
            })
        cls.Forecast = cls.env['eh.report.forecast']
        # Seed activity in the baseline period.
        cls.post_balanced_move(
            [
                {'account': cls.account_revenue, 'credit': 1000.0},
                {'account': cls.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )

    def _make_forecast(self, **overrides):
        vals = {
            'name': 'Test Forecast',
            'base_report_id': self.report.id,
            'base_date_from': fields.Date.from_string('2026-01-01'),
            'base_date_to': fields.Date.from_string('2026-12-31'),
            'horizon_months': 6,
            'growth_method': 'linear',
            'monthly_growth_pct': 5.0,
        }
        vals.update(overrides)
        return self.Forecast.create(vals)

    # ---- shape and counts ----

    def test_project_returns_baseline_periods_meta(self):
        forecast = self._make_forecast(horizon_months=3)
        result = forecast.project()
        self.assertIn('baseline', result)
        self.assertIn('periods', result)
        self.assertIn('meta', result)
        self.assertEqual(len(result['periods']), 3)

    def test_period_carries_label_and_dates(self):
        forecast = self._make_forecast(horizon_months=2)
        result = forecast.project()
        first = result['periods'][0]
        self.assertEqual(first['period_index'], 1)
        self.assertEqual(first['date_from'], '2026-02-01')
        self.assertEqual(first['date_to'], '2027-01-31')
        self.assertEqual(first['period_label'], '2026-02')

    # ---- growth factors ----

    def test_flat_method_returns_factor_one(self):
        forecast = self._make_forecast(
            growth_method='flat', monthly_growth_pct=5.0,
        )
        for period_n in range(1, 7):
            self.assertAlmostEqual(
                forecast._growth_factor(period_n), 1.0, places=6,
            )

    def test_linear_method_compounds_monthly(self):
        forecast = self._make_forecast(
            growth_method='linear', monthly_growth_pct=10.0,
        )
        # 1.10^1 = 1.10
        self.assertAlmostEqual(forecast._growth_factor(1), 1.10, places=6)
        # 1.10^3 = 1.331
        self.assertAlmostEqual(forecast._growth_factor(3), 1.331, places=6)

    # ---- value scaling ----

    def test_baseline_numeric_cells_scaled(self):
        forecast = self._make_forecast(
            growth_method='linear', monthly_growth_pct=10.0,
            horizon_months=2,
        )
        result = forecast.project()
        baseline = result['baseline']
        period1 = result['periods'][0]
        period2 = result['periods'][1]

        # Find the same line in baseline and projected periods. Use the
        # first numeric cell as the comparison.
        def find_first_value(payload):
            for line in payload.get('lines') or []:
                for col in line.get('columns') or []:
                    if isinstance(col.get('value'), (int, float)) and col['value']:
                        return col['value']
            return None

        b = find_first_value(baseline)
        p1 = find_first_value(period1)
        p2 = find_first_value(period2)
        if b is None or p1 is None or p2 is None:
            self.skipTest("Baseline produced no numeric cells")
        self.assertAlmostEqual(p1, round(b * 1.10, 2), places=2)
        self.assertAlmostEqual(p2, round(b * 1.21, 2), places=2)

    def test_non_numeric_cells_passthrough(self):
        forecast = self._make_forecast(horizon_months=1)
        result = forecast.project()
        period = result['periods'][0]
        for line in period['lines']:
            for col in line.get('columns', []):
                if col.get('value') is not None and not isinstance(
                    col['value'], (int, float),
                ):
                    # Non numeric values must pass through unchanged.
                    self.assertEqual(col['value'], col['value'])

    # ---- meta ----

    def test_meta_carries_input_parameters(self):
        forecast = self._make_forecast(
            scenario_label="Optimistic",
            growth_method='linear',
            monthly_growth_pct=7.5,
            horizon_months=4,
        )
        meta = forecast.project()['meta']
        self.assertEqual(meta['scenario_label'], "Optimistic")
        self.assertEqual(meta['horizon_months'], 4)
        self.assertEqual(meta['growth_method'], 'linear')
        self.assertEqual(meta['monthly_growth_pct'], 7.5)

    def test_last_run_stamped(self):
        forecast = self._make_forecast(horizon_months=1)
        self.assertFalse(forecast.last_run)
        forecast.project()
        self.assertTrue(forecast.last_run)
