# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""eh.workflow.guard regression: a plain user cannot RPC-write the state of
an investment property to skip an action and its journal entry (IAS 40)."""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_investment_property', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.prop_account = cls._ensure_account(
            cls.env, '1660', 'Investment Property', 'asset_non_current')
        cls.fv_gl = cls._ensure_account(
            cls.env, '4660', 'Investment Property FV Gain/Loss',
            'income_other')
        # A low-privilege interactive user: no EH manager group. The guard
        # only fires when NOT self.env.su, so the write must be attempted as
        # this user via with_user(), never as the superuser test env.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'IP Plain User',
            'login': 'ip_plain_user',
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id])],
        })

    def _draft_prop(self):
        return self.env['eh.investment.property'].create({
            'name': '/', 'model_basis': 'fair_value', 'initial_cost': 500000.0,
            'property_account_id': self.prop_account.id,
            'fv_gain_loss_account_id': self.fv_gl.id,
            'journal_id': self.journal_misc.id,
        })

    def test_plain_user_cannot_jump_state(self):
        """A non-superuser direct write to a guarded field is refused."""
        prop = self._draft_prop()
        self.assertEqual(prop.state, 'draft')
        with self.assertRaises(AccessError):
            prop.with_user(self.plain_user).write({'state': 'held'})
        # State did not move; no journal entry was manufactured.
        self.assertEqual(prop.state, 'draft')
        self.assertFalse(prop.move_ids)

    def test_action_path_still_works(self):
        """The sanctioned action carries the flag and posts as normal."""
        prop = self._draft_prop()
        prop.action_activate()
        self.assertEqual(prop.state, 'held')
