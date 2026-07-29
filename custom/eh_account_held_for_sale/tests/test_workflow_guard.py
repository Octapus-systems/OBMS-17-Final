# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the state machine is enforced in the ORM, not just the UI.

A plain interactive user must not be able to RPC-write ``state`` straight to
a posted value, skipping ``action_classify`` / ``action_sell`` and the
journal entry they post. The ``eh.workflow.guard`` mixin blocks any direct
write to a guarded field unless it originates from the record's own action.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_held_for_sale', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.asset = cls._ensure_account(
            cls.env, '1700', 'Assets Held for Sale', 'asset_current')
        cls.impairment = cls._ensure_account(
            cls.env, '5170', 'Held-for-sale Impairment', 'expense')
        # A non-superuser, non-manager internal user. The test env itself runs
        # as superuser, for which the guard is deliberately inert, so the
        # guarded write must be attempted through with_user(this user).
        cls.plain_user = cls.env['res.users'].create({
            'name': 'HFS Plain User',
            'login': 'eh_hfs_plain_user',
            'email': 'eh_hfs_plain_user@example.com',
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id])],
        })

    def test_held_for_sale_state_write_blocked_for_plain_user(self):
        item = self.env['eh.held.for.sale'].create({
            'name': '/',
            'carrying_amount': 1000.0,
            'fair_value_less_costs': 800.0,
            'asset_account_id': self.asset.id,
            'impairment_account_id': self.impairment.id,
            'journal_id': self.journal_misc.id,
        })
        self.assertEqual(item.state, 'draft')
        # RPC-writing the state to 'held' would skip the write-down journal
        # entry that action_classify posts. The guard must refuse it.
        with self.assertRaises(AccessError):
            item.with_user(self.plain_user).write({'state': 'held'})
        # And the value must not have moved.
        self.assertEqual(item.state, 'draft')

    def test_disposal_group_state_write_blocked_for_plain_user(self):
        group = self.env['eh.disposal.group'].create({
            'name': '/',
            'fair_value_less_costs': 800.0,
            'impairment_account_id': self.impairment.id,
            'asset_account_id': self.asset.id,
            'journal_id': self.journal_misc.id,
        })
        self.assertEqual(group.state, 'draft')
        with self.assertRaises(AccessError):
            group.with_user(self.plain_user).write({'state': 'sold'})
        self.assertEqual(group.state, 'draft')
