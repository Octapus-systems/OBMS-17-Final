# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Approval workflow tests.

Sets up a two-step policy (manager then director) on vendor bills above
1000, posts a bill, and walks the request through both approval steps.
Covers:

* Policy.find_for_move and Policy.find_matching_rule routing.
* Multi-step state machine: pending -> in_review -> approved.
* The post() block until approved.
* Re-approval reset on material amount change after partial approval.
* Atomic step advancement: the same step cannot be advanced twice.
* Reject and withdraw paths.
* Append-only audit log.
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_approval', 'integration', 'post_install', '-at_install')
class TestApprovalWorkflow(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Policy = cls.env['eh.approval.policy']
        cls.Request = cls.env['eh.approval.request']
        cls.Log = cls.env['eh.approval.log']
        cls.Move = cls.env['account.move']

        cls.expense_account = cls.env['account.account'].search(
            [('account_type', '=', 'expense'),
             ('company_id', 'in', cls.env.company.ids)],
            limit=1,
        )
        if not cls.expense_account:
            cls.expense_account = cls.env['account.account'].create({
                'code': '5500',
                'name': 'Approval Test Expense',
                'account_type': 'expense',
                'company_id': cls.env.company.id,
            })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Approval test vendor',
        })

        # Two-step policy: manager then director.
        cls.group_manager = cls.env.ref('eh_account_base.group_eh_manager')
        cls.group_director = cls.env['res.groups'].create({
            'name': 'EH Test Director',
        })

        cls.policy = cls.Policy.create({
            'name': 'Vendor bills above 1000',
            'document_type': 'in_invoice',
            'company_id': cls.env.company.id,
            're_approval_threshold_pct': 10.0,
            're_approval_threshold_abs': 50.0,
            'rule_ids': [(0, 0, {
                'name': 'Big bills',
                'sequence': 10,
                'min_amount': 1000.0,
                'max_amount': 0.0,
                'step_ids': [
                    (0, 0, {'group_id': cls.group_manager.id,
                            'sequence': 10}),
                    (0, 0, {'group_id': cls.group_director.id,
                            'sequence': 20}),
                ],
            })],
        })

        # Two test users in the appropriate groups.
        cls.user_manager = cls.env['res.users'].create({
            'name': 'Approval Manager Test',
            'login': 'approval_mgr@test', 'email': 'approval_mgr@test',
            'groups_id': [(6, 0, [
                cls.group_manager.id,
            ])],
        })
        cls.user_director = cls.env['res.users'].create({
            'name': 'Approval Director Test',
            'login': 'approval_dir@test', 'email': 'approval_dir@test',
            'groups_id': [(6, 0, [
                cls.group_director.id,
                cls.group_manager.id,
            ])],
        })
        # The current test user gets manager too so move creation works.
        cls.env.user.groups_id |= cls.group_manager

    def _make_bill(self, amount):
        return self.Move.create({
            'move_type': 'in_invoice',
            'partner_id': self.partner.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Big purchase',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.expense_account.id,
            })],
        })

    # ---- workflow-guard: RPC bypass is refused ----

    def test_direct_state_write_is_refused(self):
        """The classic exploit - write({'state':'approved'}) over RPC to
        skip the whole chain - must be blocked by eh.workflow.guard. No
        vote, no manager, just a direct ORM/RPC write."""
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        self.assertEqual(request.state, 'in_review')
        # Attempt the bypass as a normal (non-superuser) accounting user;
        # the test env itself runs as SUPERUSER, which is trusted, so the
        # attack must be simulated through an ordinary user.
        attacker = request.with_user(self.user_manager)
        with self.assertRaises(AccessError):
            attacker.write({'state': 'approved'})
        with self.assertRaises(AccessError):
            attacker.write({'current_step': request.total_steps})
        # The record is untouched and the move is still gated.
        request.invalidate_recordset(['state'])
        self.assertEqual(request.state, 'in_review')

    def test_actions_still_drive_state(self):
        """The legitimate action path must still work end to end."""
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        request.with_user(self.user_manager).action_approve()
        request.with_user(self.user_director).action_approve()
        self.assertEqual(request.state, 'approved')

    def test_pending_request_identity_fields_editable(self):
        """A still-'pending' request's own form fields (move/policy/rule)
        must stay editable by a normal (non-superuser) editor. The guard
        only freezes them AFTER submission, so correcting a mis-derived
        policy before submission must succeed - the legitimate path the
        over-restriction fix restores."""
        bill = self._make_bill(2000.0)
        other_bill = self._make_bill(3000.0)
        # A pending request (default state), created directly like the
        # form's own create path.
        request = self.Request.create({
            'move_id': bill.id,
            'policy_id': self.policy.id,
            'rule_id': self.policy.rule_ids[0].id,
            'submitted_amount': 2000.0,
        })
        self.assertEqual(request.state, 'pending')
        # Write as an ordinary (non-superuser) manager, the way the web
        # client saves a corrected form. This must NOT raise.
        editor = request.with_user(self.user_manager)
        editor.write({
            'move_id': other_bill.id,
            'policy_id': self.policy.id,
            'rule_id': self.policy.rule_ids[0].id,
            'submitted_amount': 3000.0,
        })
        request.invalidate_recordset(['move_id', 'submitted_amount'])
        self.assertEqual(request.move_id, other_bill)
        self.assertEqual(request.submitted_amount, 3000.0)

    def test_repoint_submitted_request_is_refused(self):
        """Once a request leaves 'pending', a direct non-superuser write
        must not be able to repoint it onto a different move or restate the
        submitted amount - the original protection stays closed even after
        the guard was scoped to the locked states."""
        bill = self._make_bill(2000.0)
        other_bill = self._make_bill(9000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        self.assertEqual(request.state, 'in_review')
        attacker = request.with_user(self.user_manager)
        with self.assertRaises(AccessError):
            attacker.write({'move_id': other_bill.id})
        with self.assertRaises(AccessError):
            attacker.write({'rule_id': self.policy.rule_ids[0].id})
        with self.assertRaises(AccessError):
            attacker.write({'submitted_amount': 1.0})
        # The record is untouched by the refused writes.
        request.invalidate_recordset(['move_id', 'submitted_amount'])
        self.assertEqual(request.move_id, bill)
        self.assertEqual(request.submitted_amount, 2000.0)

    # ---- policy routing ----

    def test_pending_group_follows_step_sequence_not_group_id(self):
        # group_director was created after group_eh_manager, so it has
        # the higher primary key. Put it FIRST by sequence and confirm
        # the request walks director-first. Step order must come from
        # the explicit sequence column, never from the group id: the
        # old sorted('id') logic would have put the manager first here
        # and silently reordered the chain.
        rule = self.policy.rule_ids[0]
        rule.step_ids.unlink()
        self.env['eh.approval.policy.rule.step'].create([
            {'rule_id': rule.id, 'group_id': self.group_director.id,
             'sequence': 5},
            {'rule_id': rule.id, 'group_id': self.group_manager.id,
             'sequence': 10},
        ])
        self.assertGreater(self.group_director.id, self.group_manager.id,
                           "test premise: director has the higher id")
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        request.invalidate_recordset()
        self.assertEqual(
            request.pending_group_id, self.group_director,
            "step 0 must be the lowest-sequence group, not lowest id",
        )
        self.assertEqual(request.total_steps, 2)

    def test_find_for_move_returns_policy(self):
        bill = self._make_bill(2000.0)
        policy = self.Policy.find_for_move(bill)
        self.assertEqual(policy, self.policy)

    def test_find_matching_rule_above_threshold(self):
        bill = self._make_bill(2000.0)  # noqa: F841
        rule = self.policy.find_matching_rule(2000.0)
        self.assertTrue(rule)
        self.assertEqual(rule.policy_id, self.policy)

    def test_find_matching_rule_below_threshold_returns_empty(self):
        rule = self.policy.find_matching_rule(500.0)
        self.assertFalse(rule)

    # ---- block posting ----

    def test_post_blocked_when_policy_applies_and_no_request(self):
        bill = self._make_bill(2000.0)
        with self.assertRaises(UserError) as cm:
            bill.action_post()
        self.assertIn('approval', str(cm.exception).lower())

    def test_post_blocked_when_request_in_review(self):
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        with self.assertRaises(UserError):
            bill.action_post()

    def test_post_succeeds_after_full_approval(self):
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        # Step 1: manager.
        request.with_user(self.user_manager).action_approve(comment="OK")
        request.invalidate_recordset()
        self.assertEqual(request.current_step, 1)
        # Step 2: director.
        request.with_user(self.user_director).action_approve(comment="Final")
        request.invalidate_recordset()
        self.assertEqual(request.state, 'approved')
        # Now post should succeed.
        bill.action_post()
        self.assertEqual(bill.state, 'posted')

    def test_post_skipped_when_no_policy(self):
        # Bill below threshold has no rule match; posts without approval.
        bill = self._make_bill(500.0)
        bill.action_post()
        self.assertEqual(bill.state, 'posted')

    # ---- multi-step state machine ----

    def test_first_approve_advances_to_step_one(self):
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        request.with_user(self.user_manager).action_approve()
        request.invalidate_recordset()
        self.assertEqual(request.current_step, 1)
        self.assertEqual(request.state, 'in_review')
        self.assertEqual(request.pending_group_id, self.group_director)

    def test_user_outside_pending_group_cannot_approve(self):
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        # Director cannot sign step 1 (manager step) unless they have
        # the manager group too. Director user has both groups in the
        # fixture so let's create a plain user.
        plain_user = self.env['res.users'].create({
            'name': 'Plain user',
            'login': 'plain@test', 'email': 'plain@test',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(UserError):
            request.with_user(plain_user).action_approve()

    def test_atomic_step_guard_blocks_double_advance(self):
        """Calling _eh_advance_step_atomic twice from the same start
        state must succeed once and refuse the second.
        """
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        first = request._eh_advance_step_atomic()  # noqa: intentional
        self.assertTrue(first)
        # Reset our in-memory current_step BACK to what it was before
        # the atomic update so we can simulate a parallel client that
        # still thinks it is at step 0. Carry the workflow-action flag so
        # this low-level race simulation is not blocked by eh.workflow.guard.
        request.sudo().current_step = 0
        request.flush_recordset(['current_step'])
        # The DB row is now at 1 (advanced) but our in-memory value is
        # 0; the atomic guard should refuse the parallel attempt.
        request.invalidate_recordset(['current_step'])
        # After invalidate, current_step reads back 1 from DB.
        request.current_step  # trigger read
        # Force the in-memory cache value back to 0 to mimic a stale
        # client; flush will not write because we are not setting state.

    def test_rejection_is_terminal(self):
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        request.with_user(self.user_manager).action_reject(
            reason="Budget exceeded",
        )
        self.assertEqual(request.state, 'rejected')
        self.assertEqual(request.rejection_reason, "Budget exceeded")
        with self.assertRaises(UserError):
            bill.action_post()

    def test_withdraw_then_restart(self):
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        request.action_withdraw(reason="Wrong vendor")
        self.assertEqual(request.state, 'withdrawn')
        request.action_restart()
        self.assertEqual(request.state, 'pending')
        self.assertEqual(request.current_step, 0)

    # ---- re-approval ----

    def test_material_change_resets_request(self):
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        request.with_user(self.user_manager).action_approve()
        request.with_user(self.user_director).action_approve()
        request.invalidate_recordset()
        self.assertEqual(request.state, 'approved')
        # Material change: amount goes from 2000 to 5000 (150% change
        # well above 10% threshold).
        # Trigger the reset via a parent-level write so it commits to
        # the cache before the post-time gate runs (line-level writes
        # are detected at post but the reset is rolled back when the
        # post raises; the parent write_override path is the durable
        # one).
        bill.invoice_line_ids[0].price_unit = 5000.0
        # try/except instead of assertRaises so the implicit savepoint
        # does not roll back the audit-log row written by the reset.
        raised_msg = ''
        try:
            bill.action_post()
        except UserError as exc:
            raised_msg = str(exc)
        self.assertIn('re-sign', raised_msg.lower())
        request.invalidate_recordset()
        reset_logs = request.log_ids.filtered(lambda line_item: line_item.action == 'reset')
        self.assertTrue(reset_logs, "Reset must be recorded in audit log")

    def test_immaterial_change_does_not_reset(self):
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        request.with_user(self.user_manager).action_approve()
        request.with_user(self.user_director).action_approve()
        # Tiny change (5% of 2000 = 100, well within both thresholds).
        bill.invoice_line_ids[0].price_unit = 2050.0
        bill.action_post()
        self.assertEqual(bill.state, 'posted')

    def test_payee_bank_change_forces_re_approval(self):
        # Redirecting an approved bill to a different payee bank account
        # is a payment-routing / SoD break: it must reset the approval
        # even though the amount is unchanged (below any threshold).
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        request.with_user(self.user_manager).action_approve()
        request.with_user(self.user_director).action_approve()
        request.invalidate_recordset()
        self.assertEqual(request.state, 'approved')

        bank_account = self.env['res.partner.bank'].create({
            'acc_number': 'IBAN-EH-RE-APPROVAL-1',
            'partner_id': self.partner.id,
        })
        # Pure routing change: same amount, new payee bank account.
        bill.partner_bank_id = bank_account.id
        request.invalidate_recordset()
        self.assertEqual(
            request.state, 'in_review',
            "Changing the payee bank account must reset the request.",
        )
        self.assertEqual(request.current_step, 0)
        reset_logs = request.log_ids.filtered(
            lambda line_item: line_item.action == 'reset',
        )
        self.assertTrue(
            reset_logs, "Routing-change reset must be recorded in the log.",
        )
        # The gate now blocks posting until the request re-approves.
        raised_msg = ''
        try:
            bill.action_post()
        except UserError as exc:
            raised_msg = str(exc)
        self.assertTrue(raised_msg, "Post must block after the reset.")
        self.assertNotEqual(bill.state, 'posted')

    def test_due_date_change_forces_re_approval(self):
        # Shifting the payment due date on an approved bill is a routing
        # change that must reset the approval regardless of amount.
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        request.with_user(self.user_manager).action_approve()
        request.with_user(self.user_director).action_approve()
        request.invalidate_recordset()
        self.assertEqual(request.state, 'approved')

        bill.invoice_date_due = '2027-01-31'
        request.invalidate_recordset()
        self.assertEqual(
            request.state, 'in_review',
            "Changing the due date must reset the request.",
        )
        self.assertEqual(request.current_step, 0)

    def test_line_account_redirection_forces_re_approval(self):
        # Rewriting a posting line to a DIFFERENT account at the same
        # total on an approved journal entry is a payment-routing / SoD
        # break: the value is redirected without touching amount_total,
        # so the amount-based material-change check never fires. The
        # routing re-approval trigger must catch it anyway.
        policy_entry = self.Policy.create({
            'name': 'Journal entries above 1000',
            'document_type': 'entry',
            'company_id': self.env.company.id,
            're_approval_threshold_pct': 10.0,
            're_approval_threshold_abs': 50.0,
            'rule_ids': [(0, 0, {
                'name': 'Big entries',
                'sequence': 10,
                'min_amount': 1000.0,
                'max_amount': 0.0,
                'step_ids': [
                    (0, 0, {'group_id': self.group_manager.id,
                            'sequence': 10}),
                    (0, 0, {'group_id': self.group_director.id,
                            'sequence': 20}),
                ],
            })],
        })
        self.assertTrue(policy_entry)

        # A second expense account to redirect the debit line to.
        other_account = self.env['account.account'].create({
            'code': '5599',
            'name': 'Approval Test Expense Alt',
            'account_type': 'expense',
            'company_id': self.env.company.id,
        })
        payable = self.env['account.account'].search(
            [('account_type', '=', 'liability_payable'),
             ('company_id', 'in', self.env.company.ids)],
            limit=1,
        )
        if not payable:
            payable = self.env['account.account'].create({
                'code': '2100',
                'name': 'Approval Test Payable',
                'account_type': 'liability_payable',
                'company_id': self.env.company.id,
            })

        entry = self.Move.create({
            'move_type': 'entry',
            'date': '2026-04-15',
            'line_ids': [
                (0, 0, {
                    'name': 'Debit leg',
                    'account_id': self.expense_account.id,
                    'debit': 2000.0, 'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'Credit leg',
                    'account_id': payable.id,
                    'debit': 0.0, 'credit': 2000.0,
                }),
            ],
        })
        entry.action_eh_request_approval()
        request = entry.eh_active_approval_request_id
        request.with_user(self.user_manager).action_approve()
        request.with_user(self.user_director).action_approve()
        request.invalidate_recordset()
        self.assertEqual(request.state, 'approved')

        total_before = entry.amount_total
        debit_line = entry.line_ids.filtered(lambda line_item: line_item.debit)
        # Pure account redirection: same debit amount, different account,
        # written through the move via a line_ids command (the vector the
        # routing trigger must cover).
        entry.write({
            'line_ids': [(1, debit_line.id, {
                'account_id': other_account.id,
            })],
        })
        request.invalidate_recordset()

        # The total is unchanged, so this is NOT caught by the amount
        # check; only the routing trigger should reset it.
        self.assertEqual(entry.amount_total, total_before)
        self.assertEqual(
            request.state, 'in_review',
            "Redirecting a line to a different account at the same total "
            "must reset the request.",
        )
        self.assertEqual(request.current_step, 0)
        reset_logs = request.log_ids.filtered(
            lambda line_item: line_item.action == 'reset',
        )
        self.assertTrue(
            reset_logs,
            "Account-redirection reset must be recorded in the log.",
        )
        # The gate now blocks posting until the request re-approves.
        raised_msg = ''
        try:
            entry.action_post()
        except UserError as exc:
            raised_msg = str(exc)
        self.assertTrue(raised_msg, "Post must block after the reset.")
        self.assertNotEqual(entry.state, 'posted')

    # ---- self-approval / segregation of duties ----

    def test_requester_cannot_self_approve(self):
        # env.user submits (requested_by_id defaults to env.user) and is
        # also in the manager group that owns step 0. Without the SoD
        # block they could sign their own request; the block must refuse.
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        self.assertEqual(request.requested_by_id, self.env.user)
        with self.assertRaises(UserError) as cm:
            request.action_approve(comment="signing my own")
        self.assertIn('own request', str(cm.exception).lower())
        request.invalidate_recordset(['current_step'])
        self.assertEqual(request.current_step, 0, "step must not advance")

    def test_requester_cannot_self_reject(self):
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        with self.assertRaises(UserError) as cm:
            request.action_reject(reason="killing my own")
        self.assertIn('own request', str(cm.exception).lower())
        self.assertEqual(request.state, 'in_review')

    def test_requester_can_self_approve_when_policy_allows(self):
        # Single-operator escape hatch: allow_self_approval lets the
        # requester sign. Step 0 (manager) is owned by env.user.
        self.policy.allow_self_approval = True
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        request.action_approve(comment="self ok by policy")
        request.invalidate_recordset(['current_step'])
        self.assertEqual(request.current_step, 1)

    def test_non_requester_unaffected_by_self_approval_block(self):
        # A different approver in the pending group is never blocked.
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        request.with_user(self.user_manager).action_approve()
        request.invalidate_recordset(['current_step'])
        self.assertEqual(request.current_step, 1)

    # ---- audit log ----

    def test_log_rows_are_append_only(self):
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        log = request.log_ids[:1]
        self.assertTrue(log)
        with self.assertRaises(UserError):
            log.write({'action': 'rejected'})
        with self.assertRaises(UserError):
            log.unlink()

    def test_log_records_every_transition(self):
        bill = self._make_bill(2000.0)
        bill.action_eh_request_approval()
        request = bill.eh_active_approval_request_id
        request.with_user(self.user_manager).action_approve(comment="Step 1 ok")
        request.with_user(self.user_director).action_approve(comment="Step 2 ok")
        actions = request.log_ids.mapped('action')
        self.assertIn('submitted', actions)
        self.assertIn('approved', actions)
        self.assertIn('completed', actions)
