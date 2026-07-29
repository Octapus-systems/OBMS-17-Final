# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Asset lifecycle: create, compute schedule, activate, post lines.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestAssetLifecycle(EhAssetTestCase):

    def test_create_asset_assigns_sequence(self):
        asset = self._make_asset(code='SEQ-001')
        self.assertNotEqual(asset.name, '/')
        self.assertTrue(asset.name.startswith('FA/'))

    def test_compute_schedule_straight_line(self):
        asset = self._make_asset(
            acquisition_cost=36000.0,
            useful_life_months=36,
            method='straight_line',
            prorate_first_period=False,
        )
        asset.action_compute_schedule()
        self.assertEqual(len(asset.depreciation_line_ids), 36)
        # 36 months at 1000 each.
        self.assertEqual(
            asset.depreciation_line_ids[0].amount, 1000.0,
        )
        # All lines sum to depreciable (cost - salvage).
        total = sum(asset.depreciation_line_ids.mapped('amount'))
        self.assertEqual(total, 36000.0)

    def test_compute_schedule_with_salvage(self):
        asset = self._make_asset(
            acquisition_cost=10000.0,
            salvage_value=1000.0,
            useful_life_months=36,
            method='straight_line',
            prorate_first_period=False,
        )
        asset.action_compute_schedule()
        # Total depreciable = 9000, last NBV should be salvage.
        total = sum(asset.depreciation_line_ids.mapped('amount'))
        self.assertAlmostEqual(total, 9000.0, places=2)
        self.assertAlmostEqual(
            asset.depreciation_line_ids[-1].remaining_value, 1000.0, places=2,
        )

    def test_compute_schedule_reducing_balance(self):
        asset = self._make_asset(
            acquisition_cost=10000.0,
            useful_life_months=24,
            method='reducing_balance',
            declining_factor=2.0,
            prorate_first_period=False,
        )
        asset.action_compute_schedule()
        self.assertGreater(len(asset.depreciation_line_ids), 0)
        # Total must equal cost minus salvage (0).
        total = sum(asset.depreciation_line_ids.mapped('amount'))
        self.assertAlmostEqual(total, 10000.0, places=1)

    def test_activate_requires_posting_setup(self):
        asset = self._make_asset(
            code='IT-NS-1',
            depreciation_account_id=False,
        )
        with self.assertRaises(UserError):
            asset.action_activate()

    def test_activate_generates_schedule_if_missing(self):
        asset = self._make_asset(code='IT-AUTO-1')
        # No explicit compute call; activation should build the schedule.
        asset.action_activate()
        self.assertEqual(asset.state, 'running')
        self.assertEqual(len(asset.depreciation_line_ids), 36)

    def test_pause_and_resume(self):
        asset = self._make_asset(code='IT-PR-1')
        asset.action_activate()
        asset.action_pause()
        self.assertEqual(asset.state, 'paused')
        asset.action_resume()
        self.assertEqual(asset.state, 'running')

    def test_post_due_lines_creates_moves(self):
        asset = self._make_asset(
            code='IT-POST-1',
            in_service_date='2025-01-31',
            acquisition_cost=12000.0,
            useful_life_months=12,
        )
        asset.action_activate()
        asset.action_post_due_lines()
        posted = asset.depreciation_line_ids.filtered(lambda l: l.is_posted)
        self.assertGreater(len(posted), 0)
        for line in posted:
            self.assertTrue(line.move_id)
            self.assertEqual(line.move_id.state, 'posted')
        self.assertGreater(asset.total_depreciated, 0)

    def test_set_to_draft_blocked_after_post(self):
        asset = self._make_asset(
            code='IT-D-1',
            in_service_date='2025-01-31',
            acquisition_cost=12000.0,
            useful_life_months=12,
        )
        asset.action_activate()
        asset.action_post_due_lines()
        with self.assertRaises(UserError):
            asset.action_set_to_draft()

    def test_set_to_draft_when_no_post(self):
        asset = self._make_asset(code='IT-D-2')
        asset.action_activate()
        asset.action_set_to_draft()
        self.assertEqual(asset.state, 'draft')

    def test_recompute_schedule_in_draft_only(self):
        asset = self._make_asset(code='IT-RC-1')
        asset.action_compute_schedule()
        asset.action_activate()
        with self.assertRaises(UserError):
            asset.action_compute_schedule()
