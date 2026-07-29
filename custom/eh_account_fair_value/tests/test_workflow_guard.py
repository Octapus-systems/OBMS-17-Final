# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the state machine must be enforced at the ORM write layer.

The transition buttons are UI-only affordances; without the eh.workflow.guard
mixin a plain user with model write access could RPC ``write({'state':
'measured'})`` straight past ``action_remeasure`` and its journal entry. These
tests assert the guarded fields refuse a direct write from a non-superuser.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_fair_value', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.fv_asset = cls._ensure_account(
            cls.env, '1600', 'Investments at FV', 'asset_current')
        cls.fv_gain = cls._ensure_account(
            cls.env, '4600', 'Fair Value Gain/Loss', 'income_other')
        cls.fv_oci = cls._ensure_account(
            cls.env, '3600', 'FVOCI Reserve', 'equity')
        # A plain accounting user: has read/write ACL on the model
        # (group_eh_user) but is NOT a manager. So a raw write is permitted by
        # the access rules and only the workflow guard can block it, which is
        # exactly the bypass vector under test.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'FV Plain User',
            'login': 'fv_plain@test',
            'email': 'fv_plain@test',
            'groups_id': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

    def _item(self, **vals):
        base = {
            'name': '/', 'nature': 'financial_asset', 'level': '1',
            'prior_carrying': 1000.0, 'fair_value': 1200.0, 'routing': 'pl',
            'balance_sheet_account_id': self.fv_asset.id,
            'gain_loss_account_id': self.fv_gain.id,
            'oci_account_id': self.fv_oci.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.fair.value.item'].create(base)

    def test_direct_state_write_blocked_for_plain_user(self):
        # A draft item's state is not frozen by the measured-guard, so without
        # the workflow guard a plain user could jump it to 'measured' and skip
        # the remeasurement posting entirely.
        item = self._item()
        self.assertEqual(item.state, 'draft')
        with self.assertRaises(AccessError):
            item.with_user(self.plain_user).write({'state': 'measured'})
        # The state did not move.
        self.assertEqual(item.state, 'draft')

    def test_direct_recycled_write_blocked_for_plain_user(self):
        # 'recycled' is guarded too: flipping it would make action_recycle /
        # action_derecognise believe the OCI reserve was already settled.
        item = self._item()
        with self.assertRaises(AccessError):
            item.with_user(self.plain_user).write({'recycled': True})

    def test_legitimate_action_still_posts(self):
        # The guard must not break the real flow: the manager's action carries
        # the context flag and the transition succeeds.
        item = self._item()
        item.action_remeasure()
        self.assertEqual(item.state, 'measured')

    def test_rollforward_direct_close_blocked_for_plain_user(self):
        item = self._item(level='3')
        line = self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id,
            'period_start': item.measurement_date,
            'period_end': item.measurement_date,
            'opening_balance': 0.0,
        })
        self.assertEqual(line.state, 'draft')
        with self.assertRaises(AccessError):
            line.with_user(self.plain_user).write({'state': 'closed'})
        self.assertEqual(line.state, 'draft')
