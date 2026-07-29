# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: the lifecycle state of a grant and of a grant
condition may change only through the record's own action_* methods, never a
direct RPC/ORM write. This closes the "state machine enforced in the UI only"
bypass, where a plain user RPC-writes ``state`` straight to a posted value and
so skips the action and the journal entry it posts.

The test environment runs as SUPERUSER (env.su is True), for which the guard
deliberately does not fire, so every negative case acts through
``with_user(a normal user)``.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_grants', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        # A plain accounting user: has write access to the models (per the
        # module ACLs) but is not in superuser mode, so the guard applies.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Grant Guard Plain',
            'login': 'grant_guard_plain@test',
            'email': 'grant_guard_plain@test',
            'groups_id': [(6, 0, [
                cls.env.ref('eh_account_base.group_eh_user').id])],
        })

    def test_grant_state_direct_write_refused_for_plain_user(self):
        """A draft grant's state is not otherwise frozen, so a plain user
        could historically RPC ``write({'state': 'received'})`` past
        ``action_receive`` and its journal entry. The guard refuses it."""
        grant = self.env['eh.gov.grant'].create({'amount': 1000.0})
        self.assertEqual(grant.state, 'draft')
        with self.assertRaises(AccessError):
            grant.with_user(self.plain_user).write({'state': 'received'})
        # State unchanged, and no journal entry was posted.
        self.assertEqual(grant.state, 'draft')
        self.assertFalse(grant.move_ids)

    def test_grant_condition_state_direct_write_refused_for_plain_user(self):
        """A condition's state gates income release and triggers the breach
        clawback, so a plain user may not RPC-write it either."""
        grant = self.env['eh.gov.grant'].create({'amount': 1000.0})
        cond = self.env['eh.gov.grant.condition'].create({
            'grant_id': grant.id, 'name': 'Employ 20 apprentices'})
        self.assertEqual(cond.state, 'open')
        with self.assertRaises(AccessError):
            cond.with_user(self.plain_user).write({'state': 'fulfilled'})
        self.assertEqual(cond.state, 'open')

    def test_action_path_still_transitions_state(self):
        """The sanctioned, manager-gated action path carries the flag and so
        still moves the state (the guard blocks only direct writes)."""
        grant = self.env['eh.gov.grant'].create({
            'amount': 1000.0,
            'cash_account_id': self.account_cash.id,
            'deferred_income_account_id': self._ensure_account(
                self.env, '2600', 'Deferred Grant Income',
                'liability_current').id,
            'journal_id': self.journal_misc.id,
        })
        grant.action_receive()
        self.assertEqual(grant.state, 'received')
        self.assertTrue(grant.move_ids)
