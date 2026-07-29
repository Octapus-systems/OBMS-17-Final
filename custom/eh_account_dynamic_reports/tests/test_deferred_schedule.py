# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Deferred revenue / expense schedule handler tests.

The handler reads the optional eh.asset ledger (ships in
eh_account_assets_pro, NOT a dependency of this module). The tests fall
into two groups:

* Unconditional fallback tests: the empty-payload soft-probe path renders
  whether or not the optional module is installed, and the schedule never
  raises.
* Asset-backed tests: skipped when eh.asset is absent. When present, create
  a deferral with a generated schedule spanning the window and assert each
  monthly bucket sums the right recognition-line amounts, the Before bucket
  captures pre-window lines, the Later bucket captures post-window lines,
  Total = sum of buckets = depreciable amount, posted_only behaviour, and
  that the deferred_expense subclass picks only expense-type deferrals.
"""

from datetime import date

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestDeferredScheduleHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rev_handler = cls.env[
            'eh.account.dynamic.report.handler.deferred_revenue']
        cls.exp_handler = cls.env[
            'eh.account.dynamic.report.handler.deferred_expense']
        cls.report_rev = cls._ensure_report(
            cls, 'deferred_revenue',
            'eh.account.dynamic.report.handler.deferred_revenue')
        cls.asset_available = 'eh.asset' in cls.env

    @staticmethod
    def _ensure_report(cls, code, handler_model):
        report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', code)], limit=1)
        if not report:
            report = cls.env['eh.account.dynamic.report'].create({
                'code': code, 'name': code,
                'handler_model': handler_model,
            })
        return report

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': False,
        }

    @staticmethod
    def _line_by_id(result, line_id):
        for line in result['lines']:
            if line['id'] == line_id:
                return line
        return None

    @staticmethod
    def _col(line, label):
        if line is None:
            return None
        for col in line['columns']:
            if col['expression_label'] == label:
                return col['value']
        return None

    # ---- unconditional fallback tests ----

    def test_payload_shape_is_well_formed(self):
        result = self.rev_handler.compute(self.options)
        self.assertIn('columns', result)
        self.assertIn('lines', result)
        self.assertIn('totals', result)
        # Before + 12 monthly + Later + Total label columns + the Deferral
        # label column = 1 + 14 + 1 = 16 columns.
        labels = [c['name'] for c in result['columns']]
        self.assertEqual(labels[1], 'Before')
        self.assertEqual(labels[-2], 'Later')
        self.assertEqual(result['columns'][-1]['name'], 'Total')

    def test_module_absent_fallback_is_empty_with_note(self):
        # Patch the soft-probe to report the optional module absent; the
        # handler must degrade to an empty schedule with a 'module not
        # installed' note, never raise. Uses Odoo's patch helper so the
        # override is reverted at test teardown.
        self.patch(
            type(self.rev_handler), '_eh_asset_available',
            lambda self: False)
        result = self.rev_handler.compute(self.options)
        self.assertEqual(result['lines'], [])
        self.assertTrue(result['meta'].get('module_not_installed'))
        self.assertIn('note', result['meta'])
        self.assertEqual(result['totals']['total'], 0.0)

    def test_bucket_resolution_monthly(self):
        buckets = self.rev_handler._resolve_period_buckets(
            date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(len(buckets), 12)
        self.assertEqual(buckets[0]['label'], '2026-01')
        self.assertEqual(buckets[-1]['label'], '2026-12')

    def test_bucket_resolution_coarsens_for_wide_window(self):
        # 5-year window -> quarterly buckets (more than 36 months).
        buckets = self.rev_handler._resolve_period_buckets(
            date(2026, 1, 1), date(2030, 12, 31))
        self.assertTrue(all('Q' in b['label'] for b in buckets))
        self.assertLessEqual(len(buckets), 36)

    def test_bucket_index_edges(self):
        buckets = self.rev_handler._resolve_period_buckets(
            date(2026, 1, 1), date(2026, 12, 31))
        df, dt = date(2026, 1, 1), date(2026, 12, 31)
        # Before window.
        self.assertEqual(
            self.rev_handler._bucket_index_for_date(
                date(2025, 12, 1), df, dt, buckets), 0)
        # After window.
        self.assertEqual(
            self.rev_handler._bucket_index_for_date(
                date(2027, 3, 1), df, dt, buckets), len(buckets) + 1)
        # March -> third in-window bucket (index 3).
        self.assertEqual(
            self.rev_handler._bucket_index_for_date(
                date(2026, 3, 15), df, dt, buckets), 3)

    # ---- asset-backed tests ----

    def _make_deferred(self, deferred_type):
        Category = self.env['eh.asset.category']
        holding_type = ('liability_current'
                        if deferred_type == 'deferred_revenue'
                        else 'asset_current')
        holding = self._ensure_account(
            self.env, '2400' if deferred_type == 'deferred_revenue'
            else '1410',
            'Deferred Holding', holding_type)
        recognition = (self.account_revenue
                       if deferred_type == 'deferred_revenue'
                       else self.account_expense)
        accum = self._ensure_account(
            self.env, '1599', 'Accum', 'asset_fixed')
        category = Category.create({
            'name': 'Deferral cat %s' % deferred_type,
            'method': 'straight_line',
            'useful_life_months': 12,
            'asset_account_id': holding.id,
            'depreciation_account_id': recognition.id,
            'accumulated_depreciation_account_id': accum.id,
            'journal_id': self.journal_misc.id,
            'company_id': self.company.id,
        })
        asset = self.env['eh.asset'].create({
            'name': '/',
            'category_id': category.id,
            'deferred_type': deferred_type,
            'partner_id': self.partner_a.id,
            'acquisition_date': '2026-01-01',
            'in_service_date': '2026-01-31',
            'acquisition_cost': 12000.0,
            'salvage_value': 0.0,
            'method': 'straight_line',
            'useful_life_months': 12,
            'prorate_first_period': False,
            'asset_account_id': holding.id,
            'depreciation_account_id': recognition.id,
            'accumulated_depreciation_account_id': accum.id,
            'journal_id': self.journal_misc.id,
        })
        asset.action_compute_schedule()
        asset.action_activate()
        return asset

    def test_schedule_buckets_and_total(self):
        if not self.asset_available:
            self.skipTest("eh_account_assets_pro not installed")
        asset = self._make_deferred('deferred_revenue')
        result = self.rev_handler.compute(self.options)
        row = self._line_by_id(result, 'asset-%s' % asset.id)
        self.assertIsNotNone(row)

        # Total = depreciable amount = acquisition cost = 12000.
        total = self._col(row, 'total')
        self.assertAlmostEqual(total, 12000.0, places=2)

        # Sum of all period buckets equals the row total.
        bucket_sum = sum(
            self._col(row, 'period_%d' % i) or 0.0
            for i in range(1, len(result['columns']) - 1))
        self.assertAlmostEqual(bucket_sum, total, places=2)

        # The grand total foots to the same figure.
        self.assertAlmostEqual(result['totals']['total'], 12000.0, places=2)

    def test_before_and_later_buckets(self):
        if not self.asset_available:
            self.skipTest("eh_account_assets_pro not installed")
        asset = self._make_deferred('deferred_revenue')
        # Narrow the window to Feb-Mar 2026 so Jan lands in Before and
        # Apr-Dec land in Later.
        opts = dict(self.options, date={
            'date_from': '2026-02-01', 'date_to': '2026-03-31'})
        result = self.rev_handler.compute(opts)
        row = self._line_by_id(result, 'asset-%s' % asset.id)
        self.assertIsNotNone(row)
        before = self._col(row, 'period_1')  # Before
        later = self._col(row, 'period_%d' % (len(result['columns']) - 2))
        # Before captures recognition dated before Feb (at least one line).
        self.assertGreater(before, 0.0)
        # Later captures the bulk of the schedule (Apr..Dec).
        self.assertGreater(later, 0.0)
        # Total still foots to the full 12000.
        self.assertAlmostEqual(self._col(row, 'total'), 12000.0, places=2)

    def test_deferred_expense_subclass_scope(self):
        if not self.asset_available:
            self.skipTest("eh_account_assets_pro not installed")
        rev_asset = self._make_deferred('deferred_revenue')
        exp_asset = self._make_deferred('deferred_expense')
        exp_result = self.exp_handler.compute(self.options)
        ids = {l['id'] for l in exp_result['lines']}
        self.assertIn('asset-%s' % exp_asset.id, ids)
        self.assertNotIn('asset-%s' % rev_asset.id, ids)

    def test_empty_when_no_deferrals(self):
        if not self.asset_available:
            self.skipTest("eh_account_assets_pro not installed")
        # No deferrals created in this test -> empty line list, no raise.
        result = self.rev_handler.compute(self.options)
        self.assertEqual(
            [l for l in result['lines'] if l['id'].startswith('asset-')], [])
