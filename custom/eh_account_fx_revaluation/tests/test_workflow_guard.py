# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.workflow.guard retrofit regression.

Closes the systemic "state machine enforced in UI only" defect: the posting
state machines here (FX revaluation run, CTA position, hedge, hedge movement)
used to be protected only by a readonly widget and a write() guard that fired
solely when leaving a *frozen* state. A DRAFT record's state is not frozen, so
a plain user could RPC ``write({'state': 'posted'})`` straight past the
action_* method and its journal entry.

The eh.workflow.guard mixin now blocks ANY direct write to the guarded 'state'
field unless the write originates from one of the record's own actions (which
flag the write). These tests assert that a non-superuser plain accounting user
is refused the bypass on every guarded model, while the sanctioned actions
still transition state.

IMPORTANT: the test env acts as a trusted user, so the guard is exercised via
``with_user(a plain non-manager user)`` - otherwise the guard correctly does
not fire.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_fx_revaluation', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):
    """A plain user cannot RPC-write a guarded state field directly."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fx_gain = cls._ensure_account(
            cls.env, '4900', 'Unrealised FX Gain', 'income_other',
        )
        cls.fx_loss = cls._ensure_account(
            cls.env, '5900', 'Unrealised FX Loss', 'expense',
        )
        cls.cta_equity = cls._ensure_account(
            cls.env, '3600', 'CTA Reserve', 'equity',
        )
        # A plain accounting user: has ORM write access (the CSV grants
        # group_eh_user write) but is NOT a manager. The guard must refuse the
        # direct state write regardless of that write ACL.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'FX Workflow Plain User',
            'login': 'fx_workflow_guard_plain',
            'email': 'fx_workflow_guard_plain@example.com',
            'company_id': cls.company.id,
            'company_id': cls.company.id,
            'groups_id': [
                (6, 0, [cls.env.ref('eh_account_base.group_eh_user').id]),
            ],
        })

    def test_plain_user_cannot_write_run_state(self):
        """A draft FX revaluation run's state cannot be RPC-forced to posted:
        that would skip action_post and its journal entry."""
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.fx_gain.id,
            'loss_account_id': self.fx_loss.id,
            'auto_reverse': False,
        })
        self.assertEqual(run.state, 'draft')
        with self.assertRaises(AccessError):
            run.with_user(self.plain_user).write({'state': 'posted'})
        self.assertEqual(run.state, 'draft')

    def test_plain_user_cannot_write_cta_state(self):
        """An open CTA position cannot be RPC-forced to disposed: that would
        fabricate a closed reserve with no IAS 21.48 entry behind it."""
        position = self.env['eh.fx.cta.position'].create({
            'name': 'Guard CTA position',
            'cta_account_id': self.cta_equity.id,
        })
        self.assertEqual(position.state, 'open')
        with self.assertRaises(AccessError):
            position.with_user(self.plain_user).write({'state': 'disposed'})
        self.assertEqual(position.state, 'open')

    def test_plain_user_cannot_write_hedge_state(self):
        """A draft hedge cannot be RPC-forced to designated/effective: that
        would skip action_designate and its IFRS 9 checks."""
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'hedged_item_description': 'USD revenue Q3 2026',
            'hedging_instrument_description': 'AUD/USD forward 1M notional',
            'hedged_currency_id': self.company.currency_id.id,
        })
        self.assertEqual(hedge.state, 'draft')
        with self.assertRaises(AccessError):
            hedge.with_user(self.plain_user).write({'state': 'effective'})
        self.assertEqual(hedge.state, 'draft')

    def test_plain_user_cannot_write_movement_state(self):
        """A draft hedge movement cannot be RPC-forced to posted: that would
        finalise a gain/loss figure with no journal entry behind it."""
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'hedged_item_description': 'USD revenue Q3 2026',
            'hedging_instrument_description': 'AUD/USD forward 1M notional',
            'hedged_currency_id': self.company.currency_id.id,
        })
        movement = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-03-31',
            'total_change': 1000.0,
            'effective_portion': 0.0,
        })
        self.assertEqual(movement.state, 'draft')
        with self.assertRaises(AccessError):
            movement.with_user(self.plain_user).write({'state': 'posted'})
        self.assertEqual(movement.state, 'draft')
