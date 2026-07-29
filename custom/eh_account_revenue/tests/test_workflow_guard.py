# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression tests.

The revenue state machines (a contract's draft -> active -> done/cancelled,
and a constraint review's draft -> applied) are enforced in the transition
actions, which post the balanced journal entries and run the completeness
checks. Without the eh.workflow.guard mixin a plain user with write access
could RPC ``write({'state': ...})`` straight past those actions, skipping the
checks and the journal entries entirely. These tests assert the guard refuses
such a direct write for a non-superuser.
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_revenue', 'post_install', '-at_install')
class TestRevenueWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A plain accounting user: has write access to the model (perm_write=1
        # on group_eh_user) but is neither a manager nor the superuser, so it
        # is exactly the low-privilege RPC vector the guard closes. The test
        # environment itself runs as superuser, for which the guard correctly
        # does not fire, so every guarded write below is issued with_user().
        cls.clerk = cls.env['res.users'].create({
            'name': 'Revenue Clerk',
            'login': 'eh_rev_clerk',
            'email': 'eh_rev_clerk@example.com',
            'company_id': cls.company.id,
            'company_id': cls.company.id,
            'groups_id': [
                (4, cls.env.ref('eh_account_base.group_eh_user').id)],
        })
        # Accounts a contract needs before revenue can be recognised.
        cls.contract_asset_acc = cls._ensure_account(
            cls.env, '1350', 'Contract Asset', 'asset_current')
        cls.contract_liab_acc = cls._ensure_account(
            cls.env, '2350', 'Contract Liability', 'liability_current')

    def test_contract_state_direct_write_blocked(self):
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'obligation_ids': [
                (0, 0, {'name': 'Licence', 'standalone_price': 1000.0})],
        })
        self.assertEqual(contract.state, 'draft')
        # A direct write to state, bypassing action_activate and its checks,
        # must be refused for the non-superuser clerk.
        with self.assertRaises(AccessError):
            contract.with_user(self.clerk).write({'state': 'active'})
        contract.invalidate_recordset(['state'])
        self.assertEqual(contract.state, 'draft')

    def test_contract_action_activate_still_works(self):
        # The sanctioned path (the action, which flags the guarded write) must
        # continue to work: the guard closes only the direct-write bypass.
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'obligation_ids': [
                (0, 0, {'name': 'Licence', 'standalone_price': 1000.0})],
        })
        contract.action_activate()
        self.assertEqual(contract.state, 'active')

    def test_constraint_review_state_direct_write_blocked(self):
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'obligation_ids': [(0, 0, {
                'name': 'Bonus service',
                'standalone_price': 1000.0,
                'variable_consideration': True,
                'variable_estimate': 100.0,
                'variable_constraint': 100.0,
            })],
        })
        review = self.env['eh.revenue.constraint.review'].create({
            'contract_id': contract.id,
            'obligation_id': contract.obligation_ids[0].id,
        })
        self.assertEqual(review.state, 'draft')
        # A direct write to state, bypassing action_apply (which writes the
        # revised estimate onto the obligation and posts the balanced
        # catch-up), must be refused for the non-superuser clerk.
        with self.assertRaises(AccessError):
            review.with_user(self.clerk).write({'state': 'applied'})
        review.invalidate_recordset(['state'])
        self.assertEqual(review.state, 'draft')

    def _recognised_contract(self):
        """An active contract with one satisfied point-in-time obligation
        whose full allocated price has been posted, so recognised_amount is
        the cumulative-posted anchor (1000). Built as the env user, which is
        granted the manager group here so the recognition run passes its
        _check_manager gate."""
        self.env.user.groups_id |= self.env.ref(
            'eh_account_base.group_eh_manager')
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Licence', 'standalone_price': 1000.0,
                'satisfaction': 'point_in_time', 'satisfied': True})],
        })
        contract.action_activate()
        contract.action_recognise()
        ob = contract.obligation_ids
        # The sanctioned recognition run advances the anchor (proves the
        # context-flagged internal write still works).
        self.assertEqual(ob.recognised_amount, 1000.0)
        return contract, ob

    def test_recognised_amount_direct_write_blocked(self):
        # recognised_amount is the cumulative-posted anchor the recognition
        # run trusts (to_recognise = target - recognised_amount). A non-manager
        # with model write access must not be able to RPC-write it directly:
        # zeroing it would silently make the whole allocated price recognisable
        # again and drive a double post on the next run.
        contract, ob = self._recognised_contract()
        with self.assertRaises(UserError):
            ob.with_user(self.clerk).write({'recognised_amount': 0.0})
        ob.invalidate_recordset(['recognised_amount', 'to_recognise'])
        self.assertEqual(ob.recognised_amount, 1000.0)
        # The anchor stayed put, so nothing is left to recognise.
        self.assertEqual(ob.to_recognise, 0.0)

    def test_recognised_amount_direct_write_blocked_upward(self):
        # Symmetric guard: inflating the anchor would overstate the IFRS 15
        # contract-asset/liability figure with no GL backing. The same refusal
        # applies whether the write moves the anchor up or down.
        contract, ob = self._recognised_contract()
        with self.assertRaises(UserError):
            ob.with_user(self.clerk).write({'recognised_amount': 5000.0})
        ob.invalidate_recordset(['recognised_amount'])
        self.assertEqual(ob.recognised_amount, 1000.0)

    def test_recognised_amount_forged_context_write_blocked(self):
        # THE hole this refinement closes. The previous guard trusted a
        # context flag (eh_revenue_recognition_run) to distinguish the
        # sanctioned recognition run from a direct write. Odoo passes
        # client-supplied context straight into call_kw, so any RPC client can
        # forge that flag. A non-manager clerk supplying the flag must STILL be
        # refused: provenance is now proven by env.su (the shared
        # eh.workflow.guard mixin), which a forged context key cannot fake.
        # Were the forged write to land it would zero the cumulative-posted
        # anchor and drive a full double post on the next recognition run.
        contract, ob = self._recognised_contract()
        with self.assertRaises(AccessError):
            ob.with_user(self.clerk).with_context(
                eh_revenue_recognition_run=True).write(
                {'recognised_amount': 0.0})
        ob.invalidate_recordset(['recognised_amount', 'to_recognise'])
        # Anchor untouched, so nothing is left to recognise: no double post.
        self.assertEqual(ob.recognised_amount, 1000.0)
        self.assertEqual(ob.to_recognise, 0.0)

    def test_recognition_run_advances_anchor_via_su(self):
        # The legitimate path must keep working: the contract's recognition
        # run advances the guarded anchor through _eh_workflow_write (env.su),
        # not a context flag. Recognise 50% then 100% of an over-time
        # obligation and assert the anchor tracks the posted target each run.
        self.env.user.groups_id |= self.env.ref(
            'eh_account_base.group_eh_manager')
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Service', 'standalone_price': 1000.0,
                'satisfaction': 'over_time', 'percent_complete': 50.0})],
        })
        contract.action_activate()
        contract.action_recognise()
        ob = contract.obligation_ids
        self.assertEqual(ob.recognised_amount, 500.0)
        # Advance progress and re-run: the anchor must move to the new target
        # through the sanctioned su-elevated write, proving the legitimate
        # recognition path is intact after removing the context-flag branch.
        ob.percent_complete = 100.0
        contract.action_recognise()
        ob.invalidate_recordset(['recognised_amount', 'to_recognise'])
        self.assertEqual(ob.recognised_amount, 1000.0)
        self.assertEqual(ob.to_recognise, 0.0)
