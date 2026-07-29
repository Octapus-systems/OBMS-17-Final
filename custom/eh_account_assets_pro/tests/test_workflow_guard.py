# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the eh.workflow.guard mixin must block a plain user from
RPC-writing a workflow state straight past the model's own actions (and the
journal entries / posting checks those actions run).

The guard only fires for a non-superuser without the eh_workflow_action
context flag. The test env runs as SUPERUSER, so every attempt below is made
with_user(a normal, non-manager accounting user); a superuser write would
(correctly) not be blocked.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'post_install', '-at_install')
class TestWorkflowGuard(EhAssetTestCase):

    def setUp(self):
        super().setUp()
        # A plain accounting user: has read/write ACL on all three models,
        # so any refusal below comes from the workflow guard, not a missing
        # access-control-list right.
        self.clerk = self._make_non_manager_user()

    def test_asset_state_write_blocked_for_plain_user(self):
        asset = self._make_asset()
        self.assertEqual(asset.state, 'draft')
        # Sanity: the clerk CAN write a non-guarded field, proving the
        # refusal below is the guard and not a blanket ACL denial.
        asset.with_user(self.clerk).write({'code': 'IT-RENAMED'})
        # Jumping straight to 'running' skips action_activate, its posting
        # setup validation and schedule build. The guard must refuse it.
        with self.assertRaises(AccessError):
            asset.with_user(self.clerk).write({'state': 'running'})
        # And the terminal 'disposed' state (normally the dispose wizard's
        # balanced gain/loss entry) is equally protected.
        with self.assertRaises(AccessError):
            asset.with_user(self.clerk).write({'state': 'disposed'})
        self.assertEqual(asset.state, 'draft')

    def test_lease_state_write_blocked_for_plain_user(self):
        lease = self._make_lease()
        self.assertEqual(lease.state, 'draft')
        with self.assertRaises(AccessError):
            lease.with_user(self.clerk).write({'state': 'active'})
        self.assertEqual(lease.state, 'draft')

    def test_impairment_state_write_blocked_for_plain_user(self):
        asset = self._make_asset()
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-06-30',
            'amount': 5_000.0,
            'is_reversal': False,
            'reason': 'Recoverable amount fell below carrying amount',
        })
        self.assertEqual(impairment.state, 'draft')
        # Flipping to 'posted' by hand skips action_post and its GL entry.
        with self.assertRaises(AccessError):
            impairment.with_user(self.clerk).write({'state': 'posted'})
        self.assertEqual(impairment.state, 'draft')

    def test_legitimate_action_still_transitions_state(self):
        # The guard must not break the sanctioned path: action_activate
        # (run as the manager test user) still moves draft -> running.
        asset = self._make_asset()
        asset.action_activate()
        self.assertEqual(asset.state, 'running')
