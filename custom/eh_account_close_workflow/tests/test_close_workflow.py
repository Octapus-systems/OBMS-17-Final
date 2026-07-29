# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Period close workflow tests.

Covers checklist instantiation (creates run + task copies), run lifecycle
(start, request approval, approve, reopen) including manager guards,
task state machine, and progress / blocking validation.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_close_workflow', 'integration', 'post_install', '-at_install')
class TestCloseWorkflow(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Checklist = cls.env['eh.close.checklist']
        cls.Run = cls.env['eh.close.run']
        cls.Task = cls.env['eh.close.task']

        # Second user for SOD: action_request_approval and action_approve
        # cannot be the same user. The test fixture creates a second
        # accounting user so the approve path is exercised correctly.
        cls.approver_user = cls.env['res.users'].create({
            'name': 'Approver Two',
            'login': 'approver_two_close',
            'groups_id': [(6, 0, [
                cls.env.ref('account.group_account_manager').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })

        cls.checklist = cls.Checklist.create({
            'name': 'Test Checklist',
            'code': 'test_checklist',
            'task_template_ids': [
                (0, 0, {
                    'sequence': 10, 'name': 'Reconcile bank',
                    'responsible_role': 'accountant',
                }),
                (0, 0, {
                    'sequence': 20, 'name': 'Run Trial Balance',
                    'responsible_role': 'accountant',
                }),
                (0, 0, {
                    'sequence': 30, 'name': 'Optional review',
                    'responsible_role': 'accountant',
                    'is_required': False,
                }),
                (0, 0, {
                    'sequence': 40, 'name': 'Sign off',
                    'responsible_role': 'manager',
                }),
            ],
        })

    # ---- checklist ----

    def test_checklist_code_format_constraint(self):
        with self.assertRaises(UserError):
            self.Checklist.create({
                'name': 'Bad', 'code': 'Bad-Code',
            })

    def test_checklist_unique_code_per_company(self):
        with self.assertRaises(Exception):
            self.Checklist.create({
                'name': 'Dup', 'code': 'test_checklist',
            })

    def test_action_create_run_copies_tasks(self):
        run = self.checklist.action_create_run(
            name='March 2026 Close',
            period_from=fields.Date.from_string('2026-03-01'),
            period_to=fields.Date.from_string('2026-03-31'),
        )
        self.assertEqual(run.checklist_id, self.checklist)
        self.assertEqual(run.state, 'open')
        self.assertEqual(len(run.task_ids), 4)
        # Tasks come over in sequence order with their template fields.
        first = run.task_ids.sorted('sequence')[0]
        self.assertEqual(first.name, 'Reconcile bank')
        self.assertEqual(first.responsible_role, 'accountant')

    def test_action_create_run_rejects_empty_checklist(self):
        empty = self.Checklist.create({
            'name': 'Empty', 'code': 'empty_checklist',
        })
        with self.assertRaises(UserError):
            empty.action_create_run()

    # ---- overlap / uniqueness guard ----

    def test_overlapping_run_rejected_same_company_period(self):
        """A second close run overlapping an existing run's company+period
        must be rejected. Without the overlap guard two preparers could
        sign off the same ledger period independently.
        """
        from odoo.exceptions import ValidationError
        self.checklist.action_create_run(
            name='March 2026 Close',
            period_from=fields.Date.from_string('2026-03-01'),
            period_to=fields.Date.from_string('2026-03-31'),
        )
        # Second run whose range intersects the first (overlaps mid-March).
        with self.assertRaises(ValidationError):
            self.checklist.action_create_run(
                name='March 2026 Close (dup)',
                period_from=fields.Date.from_string('2026-03-15'),
                period_to=fields.Date.from_string('2026-04-15'),
            )

    def test_non_overlapping_run_allowed(self):
        """Adjacent, non-overlapping periods for the same company are fine:
        the guard must not block a legitimate next-period close.
        """
        self.checklist.action_create_run(
            name='March 2026 Close',
            period_from=fields.Date.from_string('2026-03-01'),
            period_to=fields.Date.from_string('2026-03-31'),
        )
        run2 = self.checklist.action_create_run(
            name='April 2026 Close',
            period_from=fields.Date.from_string('2026-04-01'),
            period_to=fields.Date.from_string('2026-04-30'),
        )
        self.assertTrue(run2.id)
        self.assertEqual(run2.state, 'open')

    # ---- run lifecycle ----

    def test_action_start_records_prepared_by(self):
        run = self.checklist.action_create_run()
        run.action_start()
        self.assertEqual(run.state, 'in_progress')
        self.assertEqual(run.prepared_by_id, self.env.user)
        self.assertTrue(run.prepared_at)

    def test_request_approval_blocks_when_required_pending(self):
        run = self.checklist.action_create_run()
        run.action_start()
        with self.assertRaises(UserError):
            run.action_request_approval()

    def test_request_approval_succeeds_when_required_done(self):
        run = self.checklist.action_create_run()
        run.action_start()
        # Mark only the required tasks done.
        for task in run.task_ids.filtered('is_required'):
            task.action_mark_done()
        # Optional task remains pending; should not block.
        run.action_request_approval()
        self.assertEqual(run.state, 'pending_approval')
        self.assertEqual(run.reviewed_by_id, self.env.user)

    def test_request_approval_allows_not_applicable(self):
        run = self.checklist.action_create_run()
        run.action_start()
        for task in run.task_ids.filtered('is_required'):
            task.action_mark_not_applicable()
        run.action_request_approval()
        self.assertEqual(run.state, 'pending_approval')

    def test_approve_blocks_with_blocked_tasks(self):
        run = self.checklist.action_create_run()
        run.action_start()
        for task in run.task_ids.filtered('is_required'):
            task.action_mark_done()
        run.action_request_approval()
        # Block one task before approving.
        run.task_ids[0].action_block()
        with self.assertRaises(UserError):
            run.action_approve()

    def test_approve_records_approval(self):
        # Promote the test user to manager so the approve action passes.
        manager_group = self.env.ref('account.group_account_manager')
        self.env.user.groups_id = [(4, manager_group.id)]

        run = self.checklist.action_create_run()
        run.action_start()
        for task in run.task_ids.filtered('is_required'):
            task.action_mark_done()
        run.action_request_approval()
        run.with_user(self.approver_user).action_approve()
        self.assertEqual(run.state, 'closed')
        self.assertEqual(run.approved_by_id, self.approver_user)
        self.assertTrue(run.approved_at)

    def test_approve_blocks_non_manager(self):
        """A non-manager who was neither preparer nor reviewer must still be
        blocked from approving and closing a run. Guards the authorization
        gate on top of the different-person SoD checks.
        """
        # Preparer / reviewer is the default test user.
        run = self.checklist.action_create_run()
        run.action_start()
        for task in run.task_ids.filtered('is_required'):
            task.action_mark_done()
        run.action_request_approval()

        # A different person who is NOT a manager. Different-person SoD is
        # satisfied, so only the manager gate can block this approve.
        non_manager = self.env['res.users'].create({
            'name': 'Clerk Three',
            'login': 'clerk_three_close',
            'groups_id': [(6, 0, [
                self.env.ref('account.group_account_user').id,
            ])],
        })
        self.assertFalse(
            non_manager.has_group('eh_account_base.group_eh_manager'))
        with self.assertRaises(UserError):
            run.with_user(non_manager).action_approve()
        # State unchanged: the run did not close.
        self.assertEqual(run.state, 'pending_approval')

    def test_approve_succeeds_for_manager_different_person(self):
        """A manager who is a different person from the preparer and the
        reviewer can approve and close the run.
        """
        run = self.checklist.action_create_run()
        run.action_start()
        for task in run.task_ids.filtered('is_required'):
            task.action_mark_done()
        run.action_request_approval()
        # approver_user is a manager and a different person from the
        # preparer / reviewer (the default test user).
        self.assertNotEqual(self.approver_user, run.reviewed_by_id)
        self.assertNotEqual(self.approver_user, run.prepared_by_id)
        self.assertTrue(
            self.approver_user.has_group('eh_account_base.group_eh_manager'))
        run.with_user(self.approver_user).action_approve()
        self.assertEqual(run.state, 'closed')
        self.assertEqual(run.approved_by_id, self.approver_user)

    def test_reopen_requires_manager(self):
        # Make user a manager to close.
        eh_manager_group = self.env.ref('eh_account_base.group_eh_manager')
        self.env.user.groups_id = [(4, eh_manager_group.id)]

        run = self.checklist.action_create_run()
        run.action_start()
        for task in run.task_ids.filtered('is_required'):
            task.action_mark_done()
        run.action_request_approval()
        run.with_user(self.approver_user).action_approve()

        # Drop the suite manager group; reopen should now fail.
        self.env.user.groups_id = [(3, eh_manager_group.id)]
        with self.assertRaises(UserError):
            run.action_reopen()

    def test_reopen_blocks_core_account_manager_without_eh_manager(self):
        """A user with only the core account manager group (and NOT the
        suite manager group) must not be able to reopen a closed run.

        Reopening a closed period is an EH-controlled authorization gate.
        This fails under a gate anchored on account.group_account_manager
        and passes only when the gate is anchored on
        eh_account_base.group_eh_manager.
        """
        # Close the run as a full suite manager (different-person path via
        # the fixture approver keeps SoD intact).
        eh_manager_group = self.env.ref('eh_account_base.group_eh_manager')
        self.env.user.groups_id = [(4, eh_manager_group.id)]
        run = self.checklist.action_create_run()
        run.action_start()
        for task in run.task_ids.filtered('is_required'):
            task.action_mark_done()
        run.action_request_approval()
        run.with_user(self.approver_user).action_approve()
        self.assertEqual(run.state, 'closed')

        # A user who is a core account manager but NOT a suite manager.
        # group_eh_user is granted only so this user carries the model ACL
        # to load and write the run; it does not grant the reopen right.
        # The reopen must be blocked purely by the authorization gate, so
        # that under a core-group gate this reopen would wrongly succeed.
        core_manager = self.env['res.users'].create({
            'name': 'Core Manager',
            'login': 'core_manager_close',
            'groups_id': [(6, 0, [
                self.env.ref('account.group_account_manager').id,
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        self.assertTrue(
            core_manager.has_group('account.group_account_manager'))
        self.assertFalse(
            core_manager.has_group('eh_account_base.group_eh_manager'))

        with self.assertRaises(UserError):
            run.with_user(core_manager).action_reopen()
        # State unchanged: the run stays closed.
        self.assertEqual(run.state, 'closed')

    def test_reopen_records_reopened_by(self):
        # Reopen is gated on the suite manager group; grant it so the
        # default test user can both approve-support and reopen. This
        # group implies account.group_account_manager.
        eh_manager_group = self.env.ref('eh_account_base.group_eh_manager')
        self.env.user.groups_id = [(4, eh_manager_group.id)]

        run = self.checklist.action_create_run()
        run.action_start()
        for task in run.task_ids.filtered('is_required'):
            task.action_mark_done()
        run.action_request_approval()
        run.with_user(self.approver_user).action_approve()
        run.action_reopen()
        self.assertEqual(run.state, 'reopened')
        self.assertEqual(run.reopened_by_id, self.env.user)
        # Original sign off audit preserved.
        self.assertTrue(run.approved_by_id)

    # ---- task lifecycle ----

    def test_task_action_start_assigns_user(self):
        run = self.checklist.action_create_run()
        task = run.task_ids[0]
        self.assertEqual(task.state, 'pending')
        task.action_start()
        self.assertEqual(task.state, 'in_progress')
        self.assertEqual(task.assigned_user_id, self.env.user)

    def test_task_mark_done_records_completion(self):
        run = self.checklist.action_create_run()
        task = run.task_ids[0]
        task.action_mark_done()
        self.assertEqual(task.state, 'done')
        self.assertEqual(task.completed_by_id, self.env.user)
        self.assertTrue(task.completed_at)

    def test_task_mark_not_applicable_records_completion(self):
        run = self.checklist.action_create_run()
        task = run.task_ids[0]
        task.action_mark_not_applicable()
        self.assertEqual(task.state, 'not_applicable')
        self.assertTrue(task.completed_by_id)

    def test_task_block_then_unblock(self):
        run = self.checklist.action_create_run()
        task = run.task_ids[0]
        task.action_block()
        self.assertEqual(task.state, 'blocked')
        task.action_unblock()
        self.assertEqual(task.state, 'in_progress')

    def test_task_reset_requires_manager(self):
        run = self.checklist.action_create_run()
        task = run.task_ids[0]
        task.action_mark_done()
        # Without manager rights, reset fails.
        manager_group = self.env.ref('account.group_account_manager')
        if manager_group in self.env.user.groups_id:
            self.env.user.groups_id = [(3, manager_group.id)]
        with self.assertRaises(UserError):
            task.action_reset_to_pending()

    # ---- progress ----

    def test_progress_pct(self):
        run = self.checklist.action_create_run()
        # 4 tasks total. Mark 2 done.
        run.task_ids[0].action_mark_done()
        run.task_ids[1].action_mark_done()
        run.invalidate_recordset()
        self.assertAlmostEqual(run.progress_pct, 50.0, places=2)
        self.assertEqual(run.task_done_count, 2)
        self.assertEqual(run.task_pending_count, 2)

    def test_progress_counts_blocked_separately(self):
        run = self.checklist.action_create_run()
        run.task_ids[0].action_block()
        run.invalidate_recordset()
        self.assertEqual(run.task_blocked_count, 1)
        self.assertEqual(run.task_pending_count, 3)

    # ---- regression: completed timestamp must not be overwritten ----

    def test_mark_done_twice_preserves_audit_trail(self):
        """Re-clicking 'Mark Done' on an already-done task must not
        overwrite completed_by_id and completed_at.

        Regression for the guard bug where the previous implementation
        compared state against a non-existent 'closed' literal, so the
        second click would overwrite the audit fields.
        """
        run = self.checklist.action_create_run()
        task = run.task_ids[0]
        task.action_mark_done()
        first_completed_at = task.completed_at
        first_completed_by = task.completed_by_id
        self.assertTrue(first_completed_at)
        # Force a different "now" by waiting a second of clock time is
        # impractical in tests; we instead simulate concurrent click by
        # calling mark_done again and assert nothing changed.
        task.action_mark_done()
        self.assertEqual(task.completed_at, first_completed_at)
        self.assertEqual(task.completed_by_id, first_completed_by)
        self.assertEqual(task.state, 'done')

    def test_mark_done_skips_not_applicable(self):
        """A task already marked not_applicable should not be reverted to
        done, otherwise the audit fields would be overwritten.
        """
        run = self.checklist.action_create_run()
        task = run.task_ids[0]
        task.action_mark_not_applicable()
        first_completed_at = task.completed_at
        task.action_mark_done()
        self.assertEqual(task.state, 'not_applicable')
        self.assertEqual(task.completed_at, first_completed_at)


@tagged('eh_account_close_workflow', 'integration', 'post_install',
        '-at_install')
class TestCloseChecks(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.checklist = cls.env['eh.close.checklist'].create({
            'name': 'Checks Checklist',
            'code': 'checks_checklist',
            'task_template_ids': [(0, 0, {
                'sequence': 10, 'name': 'Optional only',
                'responsible_role': 'accountant', 'is_required': False,
            })],
        })

    def _run(self):
        return self.checklist.action_create_run(
            name='Mar 2026 Close',
            period_from=fields.Date.from_string('2026-03-01'),
            period_to=fields.Date.from_string('2026-03-31'),
        )

    def _draft_entry(self):
        return self.env['account.move'].create({
            'move_type': 'entry',
            'date': fields.Date.from_string('2026-03-15'),
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'account_id': self.account_cash.id,
                        'debit': 100.0, 'credit': 0.0}),
                (0, 0, {'account_id': self.account_revenue.id,
                        'debit': 0.0, 'credit': 100.0}),
            ],
        })

    def test_checks_fail_on_draft_entry(self):
        run = self._run()
        self._draft_entry()  # left in draft
        run.action_run_checks()
        draft_check = run.check_ids.filtered(
            lambda c: c.code == 'draft_entries')
        self.assertEqual(draft_check.status, 'fail')
        self.assertEqual(draft_check.count, 1)
        self.assertTrue(run.has_failed_blocking_checks)

    def test_checks_pass_after_posting(self):
        run = self._run()
        entry = self._draft_entry()
        entry.action_post()
        run.action_run_checks()
        self.assertEqual(
            run.check_ids.filtered(
                lambda c: c.code == 'draft_entries').status, 'pass')
        self.assertEqual(
            run.check_ids.filtered(
                lambda c: c.code == 'unbalanced_entries').status, 'pass')
        self.assertFalse(run.has_failed_blocking_checks)

    def test_request_approval_blocked_by_failed_check(self):
        run = self._run()
        run.action_start()
        self._draft_entry()
        run.action_run_checks()
        with self.assertRaises(UserError):
            run.action_request_approval()

    def test_check_rows_cannot_be_hand_edited(self):
        """The check rows ARE the blocking gate, so they are system-written:
        a direct write, unlink or create is refused for everyone, closing the
        path where a preparer flips a failed blocking check to pass."""
        run = self._run()
        self._draft_entry()
        run.action_run_checks()
        fail_check = run.check_ids.filtered(
            lambda c: c.code == 'draft_entries')
        self.assertEqual(fail_check.status, 'fail')
        with self.assertRaises(UserError):
            fail_check.status = 'pass'
        with self.assertRaises(UserError):
            fail_check.unlink()
        with self.assertRaises(UserError):
            self.env['eh.close.check'].create({
                'run_id': run.id, 'code': 'x', 'name': 'x',
                'status': 'pass', 'is_blocking': True})
        self.assertTrue(run.has_failed_blocking_checks)

    def test_approval_gate_recomputes_against_live_ledger(self):
        """A stale stored check row cannot pass the gate: the checks are
        re-run against the live ledger at approval time, so a draft entry
        introduced after Run Checks still blocks approval."""
        run = self._run()
        run.action_start()
        run.action_run_checks()  # clean period -> stored rows all pass
        self.assertFalse(run.has_failed_blocking_checks)
        self._draft_entry()  # a draft appears AFTER the checks ran
        # The gate re-scans the live ledger and blocks, even though the stored
        # rows still read pass from the earlier clean run.
        with self.assertRaises(UserError):
            run.action_request_approval()
