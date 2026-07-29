# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Regression test for the eh.workflow.guard retrofit.

The budget state machine (and the commitment reserve/release lifecycle) is
enforced by lifecycle actions that run as su. Without the guard a plain user
could RPC ``write({'state': 'confirmed'})`` straight past ``action_confirm``
and its no-empty-lines check. These tests assert that a NON-superuser direct
write to the guarded ``state`` field is refused, while the sanctioned action
(running as su) still advances it.
"""

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_budget_pro', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.Commitment = cls.env['eh.budget.commitment']
        # A plain internal user (no elevated rights). res.users on Odoo 19
        # uses group_ids, not groups_id.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Budget Plain User',
            'login': 'eh_budget_plain_user',
            'email': 'eh_budget_plain_user@example.com',
            'groups_id': [
                (6, 0, [cls.env.ref('base.group_user').id]),
            ],
        })

    def _make_budget(self, code='guard_budget', with_line=False):
        vals = {
            'code': code,
            'name': 'Guard Budget',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
        }
        if with_line:
            vals['line_ids'] = [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 1000.0,
            })]
        return self.Budget.create(vals)

    def test_direct_state_write_blocked_for_plain_user(self):
        """A non-superuser cannot RPC-write state past the action."""
        if not self.plain_user:
            self.skipTest("No plain user available in this environment.")
        budget = self._make_budget()
        self.assertEqual(budget.state, 'draft')
        with self.assertRaises(AccessError):
            budget.with_user(self.plain_user).write({'state': 'confirmed'})
        # State is unchanged after the refused write.
        self.assertEqual(budget.state, 'draft')

    def test_action_still_advances_state(self):
        """The sanctioned action (run as su) still moves the state."""
        budget = self._make_budget(code='guard_budget_confirm', with_line=True)
        budget.action_confirm()
        self.assertEqual(budget.state, 'confirmed')

    def test_commitment_state_write_blocked_for_plain_user(self):
        """The commitment reserve/release lifecycle is guarded too."""
        if not self.plain_user:
            self.skipTest("No plain user available in this environment.")
        budget = self._make_budget(code='guard_budget_commit', with_line=True)
        line = budget.line_ids[0]
        commitment = self.Commitment.create({
            'budget_line_id': line.id,
            'amount': 100.0,
        })
        self.assertEqual(commitment.state, 'draft')
        with self.assertRaises(AccessError):
            commitment.with_user(self.plain_user).write({'state': 'reserved'})
        self.assertEqual(commitment.state, 'draft')
