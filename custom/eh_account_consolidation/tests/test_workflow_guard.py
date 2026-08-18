# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression guard: the consolidation run and elimination state machines
cannot be skipped by a direct RPC/ORM write.

The defect this closes: a state machine enforced only by a readonly
statusbar and an input-freeze on posted figures still lets any user with
model write access RPC-write ``write({'state': 'closed'})`` straight past
``action_compute``/``action_review``/``action_close`` and the sealed
consolidation entries they produce. Both models inherit eh.workflow.guard,
so 'state' may only move through the record's own actions (which run as
su); a direct non-superuser write is refused.

The test runner env is SUPERUSER (env.su True), which would sail past the
guard, so the negative path MUST run as a non-superuser (with_user). A
manager user is used so the model-level ACL grants write and the failure
observed is the workflow guard itself, not a plain access-rights denial.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_consolidation', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.entity = cls.env['eh.consol.entity'].create({
            'name': 'Guard Group',
            'code': 'guard_group',
            'parent_company_id': cls.company.id,
            'presentation_currency_id': cls.company.currency_id.id,
        })
        cls.doc = cls.env['eh.consol.run'].create({
            'entity_id': cls.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        cls.elim = cls.env['eh.consol.elimination'].create({
            'run_id': cls.doc.id,
        })

        # A non-superuser manager: model ACL grants write, so a direct state
        # write reaching the ORM proves the WORKFLOW guard blocks it, not a
        # coarse access-rights denial. Odoo 19 res.users uses group_ids.
        try:
            cls.user = cls.env['res.users'].create({
                'name': 'Guard Manager',
                'login': 'eh_consol_guard_manager',
                'company_id': cls.company.id,  # noqa: F601
                'company_id': cls.company.id,  # noqa: F601
                'groups_id': [(4, cls.env.ref('base.group_user').id),
                              (4, cls.env.ref(
                                  'eh_account_base.group_eh_manager').id)],
            })
        except Exception:  # noqa: BLE001
            cls.user = False

    def test_run_state_write_blocked_for_normal_user(self):
        """A non-superuser cannot RPC a run past its actions into 'closed'."""
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        self.assertEqual(self.doc.state, 'draft')
        with self.assertRaises(AccessError):
            self.doc.with_user(self.user).write({'state': 'closed'})
        self.assertEqual(self.doc.state, 'draft')

    def test_elimination_state_write_blocked_for_normal_user(self):
        """A non-superuser cannot RPC an elimination straight to 'posted'."""
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        self.assertEqual(self.elim.state, 'draft')
        with self.assertRaises(AccessError):
            self.elim.with_user(self.user).write({'state': 'posted'})
        self.assertEqual(self.elim.state, 'draft')

    def test_sudo_state_write_passes(self):
        """The sanctioned server path (su) writes state normally."""
        self.assertEqual(self.doc.state, 'draft')
        self.doc.sudo().write({'state': 'computed'})
        self.assertEqual(self.doc.state, 'computed')
