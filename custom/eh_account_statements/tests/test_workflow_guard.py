# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the workflow state machines are enforced by eh.workflow.guard.

Each primary-statement model (eh.soci, eh.soce) and the cross-statement
tie-out control (eh.statement.tieout) advances its state only through its own
sudo-running actions. A plain user who RPC/ORM-writes the state directly must
be blocked, otherwise action_confirm / action_check and their IAS 1 tie-out
checks (and, for adopters that post, the journal entry) can be skipped.

The test env runs as SUPERUSER, for which env.su is True and the guard is a
no-op by design, so the negative assertions must be made through a plain
non-superuser user (with_user).
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_statements', 'post_install', '-at_install')
class TestWorkflowGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        # A plain internal user with the EH manager group. The manager group
        # grants write/create on all three models, so a state write that is
        # refused is refused by the workflow guard (env.su False), not by a
        # missing ORM access right - which would make the test pass for the
        # wrong reason. The manager is still NOT the superuser, so the guard
        # applies to it.
        group_xmlids = ['base.group_user']
        mgr = cls.env.ref(
            'eh_account_base.group_eh_manager', raise_if_not_found=False)
        if mgr:
            group_xmlids.append('eh_account_base.group_eh_manager')
        try:
            cls.user = cls.env['res.users'].create({
                'name': 'EH Statements Guard Tester',
                'login': 'eh_statements_guard_tester',
                'email': 'eh_statements_guard_tester@example.com',
                # Odoo 19: res.users uses group_ids (not groups_id).
                'groups_id': [
                    (6, 0, [cls.env.ref(x).id for x in group_xmlids])],
                'company_id': cls.company.id,
                'company_id': cls.company.id,
            })
        except Exception:  # pragma: no cover - environment-dependent
            cls.user = cls.env['res.users']

    def _skip_if_no_user(self):
        if not self.user:
            self.skipTest("Could not create a non-superuser test user.")

    def test_soci_state_write_blocked_for_plain_user(self):
        """A plain user cannot RPC-write eh.soci.state past action_confirm."""
        self._skip_if_no_user()
        doc = self.env['eh.soci'].create({
            'company_id': self.company.id,
            'period_start': '2026-01-01',
            'period_end': '2026-12-31',
        })
        self.assertEqual(doc.state, 'draft')
        with self.assertRaises(AccessError):
            doc.with_user(self.user).write({'state': 'confirmed'})
        # The record is unchanged, and the sanctioned sudo path still works.
        doc.invalidate_recordset(['state'])
        self.assertEqual(doc.state, 'draft')
        doc.sudo().write({'state': 'confirmed'})
        self.assertEqual(doc.state, 'confirmed')

    def test_soce_state_write_blocked_for_plain_user(self):
        """A plain user cannot RPC-write eh.soce.state past action_confirm."""
        self._skip_if_no_user()
        doc = self.env['eh.soce'].create({
            'company_id': self.company.id,
            'period_start': '2026-01-01',
            'period_end': '2026-12-31',
        })
        self.assertEqual(doc.state, 'draft')
        with self.assertRaises(AccessError):
            doc.with_user(self.user).write({'state': 'confirmed'})
        doc.invalidate_recordset(['state'])
        self.assertEqual(doc.state, 'draft')

    def test_tieout_state_write_blocked_for_plain_user(self):
        """A plain user cannot RPC-write eh.statement.tieout.state past
        action_check."""
        self._skip_if_no_user()
        doc = self.env['eh.statement.tieout'].create({
            'company_id': self.company.id,
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
        })
        self.assertEqual(doc.state, 'draft')
        with self.assertRaises(AccessError):
            doc.with_user(self.user).write({'state': 'checked'})
        doc.invalidate_recordset(['state'])
        self.assertEqual(doc.state, 'draft')

    def test_create_in_guarded_state_stripped_for_plain_user(self):
        """A plain user cannot make a record BORN confirmed to skip the
        workflow; the guarded state is stripped so it starts at draft."""
        self._skip_if_no_user()
        doc = self.env['eh.soci'].with_user(self.user).create({
            'company_id': self.company.id,
            'period_start': '2026-01-01',
            'period_end': '2026-12-31',
            'state': 'confirmed',
        })
        self.assertEqual(
            doc.state, 'draft',
            "A non-superuser create must not seed a guarded state.")
