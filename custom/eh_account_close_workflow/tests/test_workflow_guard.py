# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Workflow-guard regression tests.

The close run / task state machines are enforced by action_* methods that
carry side effects (segregation-of-duties, blocking-check rescan, manager
authorization). Without the eh.workflow.guard mixin a plain user could RPC
write({'state': 'closed'}) straight past those gates. These tests assert the
guard refuses a direct state write from a non-superuser and that the legitimate
action path still works.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_close_workflow', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Checklist = cls.env['eh.close.checklist']

        # A plain accounting user: has read/write ACL on run + task (so the
        # write reaches the guard) but is NOT a manager and NOT superuser.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Plain Clerk',
            'login': 'plain_clerk_guard',
            'groups_id': [(6, 0, [
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

        cls.checklist = cls.Checklist.create({
            'name': 'Guard Checklist',
            'code': 'guard_checklist',
            'task_template_ids': [
                (0, 0, {
                    'sequence': 10, 'name': 'Reconcile bank',
                    'responsible_role': 'accountant',
                }),
            ],
        })
        cls.close_run = cls.checklist.action_create_run()
        cls.task = cls.close_run.task_ids[0]

    def test_direct_state_write_on_run_is_refused(self):
        """A non-superuser cannot skip the close workflow by RPC-writing the
        run straight to 'closed'; the guard raises AccessError."""
        self.assertEqual(self.close_run.state, 'open')
        with self.assertRaises(AccessError):
            self.close_run.with_user(self.plain_user).write({'state': 'closed'})
        # State unchanged: the direct write was rejected before it landed.
        self.assertEqual(self.close_run.state, 'open')

    def test_direct_state_write_on_task_is_refused(self):
        """A non-superuser cannot mark a task 'done' by a direct write,
        bypassing action_mark_done and its completed_by audit stamp."""
        self.assertEqual(self.task.state, 'pending')
        with self.assertRaises(AccessError):
            self.task.with_user(self.plain_user).write({'state': 'done'})
        self.assertEqual(self.task.state, 'pending')

    def test_legitimate_action_still_transitions_state(self):
        """The guard must not break the sanctioned path: the action method
        flags its own write and the transition succeeds."""
        self.close_run.with_user(self.plain_user).action_start()
        self.assertEqual(self.close_run.state, 'in_progress')
