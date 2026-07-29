# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Multi-book + AU tax depreciation tests.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestMultiBook(EhAssetTestCase):

    def test_book_can_be_added_with_independent_method(self):
        asset = self._make_asset(method='straight_line')
        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'AU Tax book',
            'book_type': 'tax',
            'method': 'diminishing_value',
            'useful_life_months': 60,
            'declining_factor': 2.0,
            'salvage_value': 0.0,
            'prorate_first_period': False,
        })
        self.assertEqual(asset.book_count, 1)
        self.assertEqual(book.method, 'diminishing_value')

    def test_book_compute_schedule_emits_lines(self):
        asset = self._make_asset(method='straight_line')
        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'AU Tax book',
            'book_type': 'tax',
            'method': 'prime_cost',
            'useful_life_months': 36,
            'salvage_value': 0.0,
            'prorate_first_period': False,
        })
        book.action_compute_schedule()
        # Prime cost over 36 months on AUD 36k = 1000/month
        self.assertEqual(len(book.line_ids), 36)
        self.assertAlmostEqual(book.line_ids[0].amount, 1000.0, places=2)
        self.assertAlmostEqual(book.total_depreciation, 36000.0, places=2)

    def test_book_diminishing_value_first_period_higher(self):
        asset = self._make_asset(method='straight_line')
        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'DV book',
            'book_type': 'tax',
            'method': 'diminishing_value',
            'useful_life_months': 36,
            'declining_factor': 2.0,
            'salvage_value': 0.0,
            'prorate_first_period': False,
        })
        book.action_compute_schedule()
        # First period DV amount > last period DV amount.
        first = book.line_ids[0].amount
        last = book.line_ids[-1].amount
        self.assertGreater(first, last,
                           "Diminishing value should depreciate more in early periods")
        # Total still equals depreciable (cost - salvage).
        self.assertAlmostEqual(book.total_depreciation, 36000.0, places=2)

    def test_book_recompute_blocks_when_lines_posted(self):
        asset = self._make_asset()
        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'Posted book',
            'book_type': 'tax',
            'method': 'prime_cost',
            'useful_life_months': 12,
        })
        book.action_compute_schedule()
        # Mark a line as posted to simulate prior posting.
        book.line_ids[0].is_posted = True
        with self.assertRaises(UserError):
            book.action_compute_schedule()

    def test_books_independent_of_primary_schedule(self):
        """Adding a tax book does not modify the primary GL schedule."""
        asset = self._make_asset(method='straight_line')
        asset.action_compute_schedule()
        primary_count = len(asset.depreciation_line_ids)
        primary_total = sum(asset.depreciation_line_ids.mapped('amount'))

        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'Tax book',
            'book_type': 'tax',
            'method': 'diminishing_value',
            'useful_life_months': 60,
            'salvage_value': 0.0,
        })
        book.action_compute_schedule()
        self.assertEqual(len(asset.depreciation_line_ids), primary_count)
        self.assertAlmostEqual(
            sum(asset.depreciation_line_ids.mapped('amount')),
            primary_total, places=2,
        )

    def test_book_salvage_above_cost_blocked(self):
        asset = self._make_asset(acquisition_cost=10000.0)
        with self.assertRaises(Exception):
            self.env['eh.asset.book'].create({
                'asset_id': asset.id,
                'name': 'Bad salvage',
                'book_type': 'tax',
                'method': 'prime_cost',
                'useful_life_months': 12,
                'salvage_value': 99999.0,
            })


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestAuPrimaryMethods(EhAssetTestCase):

    def test_prime_cost_on_primary_matches_straight_line(self):
        a1 = self._make_asset(method='straight_line', useful_life_months=12)
        a2 = self._make_asset(
            method='prime_cost', useful_life_months=12, code='IT-PC-1',
        )
        a1.action_compute_schedule()
        a2.action_compute_schedule()
        # Total depreciation identical; per-period amounts identical.
        self.assertEqual(len(a1.depreciation_line_ids),
                         len(a2.depreciation_line_ids))
        self.assertAlmostEqual(
            sum(a1.depreciation_line_ids.mapped('amount')),
            sum(a2.depreciation_line_ids.mapped('amount')),
            places=2,
        )

    def test_diminishing_value_on_primary_uses_factor_2(self):
        a = self._make_asset(
            method='diminishing_value',
            useful_life_months=36,
            declining_factor=2.0,
        )
        a.action_compute_schedule()
        # First period > last period.
        first = a.depreciation_line_ids[0].amount
        last = a.depreciation_line_ids[-1].amount
        self.assertGreater(first, last)


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestInstantWriteOff(EhAssetTestCase):

    def test_instant_write_off_emits_one_line(self):
        a = self._make_asset(
            acquisition_cost=18000.0,
            salvage_value=0.0,
            is_instant_write_off=True,
            method='straight_line',  # ignored when instant write-off
            useful_life_months=60,
        )
        a.action_compute_schedule()
        self.assertEqual(len(a.depreciation_line_ids), 1)
        self.assertAlmostEqual(
            a.depreciation_line_ids[0].amount, 18000.0, places=2,
        )

    def test_instant_write_off_honours_salvage(self):
        a = self._make_asset(
            acquisition_cost=20000.0,
            salvage_value=2000.0,
            is_instant_write_off=True,
        )
        a.action_compute_schedule()
        self.assertEqual(len(a.depreciation_line_ids), 1)
        self.assertAlmostEqual(
            a.depreciation_line_ids[0].amount, 18000.0, places=2,
        )

    def test_instant_write_off_overrides_useful_life(self):
        """Even with useful_life_months=120, only one line emits."""
        a = self._make_asset(
            acquisition_cost=15000.0,
            is_instant_write_off=True,
            useful_life_months=120,
        )
        a.action_compute_schedule()
        self.assertEqual(len(a.depreciation_line_ids), 1)
