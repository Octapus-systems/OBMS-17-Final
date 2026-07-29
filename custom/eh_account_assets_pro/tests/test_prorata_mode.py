# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""First-period proration modes (none / daily / half)."""

from odoo.tests import tagged

from odoo.addons.eh_account_assets_pro.tests.common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestProrataMode(EhAssetTestCase):

    def _lines(self, asset):
        asset.action_compute_schedule()
        return asset.depreciation_line_ids.sorted('sequence')

    def test_prorata_none_charges_full_first_period(self):
        # cost 36000, life 36 -> per period 1000.
        asset = self._make_asset(prorata_mode='none')
        lines = self._lines(asset)
        self.assertAlmostEqual(lines[0].amount, 1000.0, places=2)
        self.assertAlmostEqual(sum(lines.mapped('amount')), 36000.0, places=2)

    def test_prorata_half_charges_half_first_period(self):
        asset = self._make_asset(prorata_mode='half')
        lines = self._lines(asset)
        self.assertAlmostEqual(lines[0].amount, 500.0, places=2)
        self.assertAlmostEqual(sum(lines.mapped('amount')), 36000.0, places=2)

    def test_prorata_daily_prorates_by_days(self):
        # In service on the 31st of a 31-day month -> 1 day in service.
        asset = self._make_asset(prorata_mode='daily',
                                 in_service_date='2026-01-31')
        lines = self._lines(asset)
        self.assertAlmostEqual(lines[0].amount, 1000.0 / 31.0, places=2)

    def test_legacy_prorate_bool_still_applies(self):
        # prorata_mode blank -> falls back to prorate_first_period switch.
        asset = self._make_asset(prorate_first_period=True,
                                 in_service_date='2026-01-31')
        self.assertFalse(asset.prorata_mode)
        lines = self._lines(asset)
        self.assertAlmostEqual(lines[0].amount, 1000.0 / 31.0, places=2)
