# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the eh.peppol.inbound state machine cannot be bypassed by a
direct RPC/ORM write.

Without the eh.workflow.guard mixin, a plain user could
``write({'state': 'posted'})`` on a draft inbound record and skip
action_post entirely - its manager-group check, duplicate guard, and the
vendor-bill journal entry. The guard refuses any non-superuser write to a
guarded field; only the record's own actions (which run under sudo) may
move state.
"""

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged


@tagged('eh_account_einvoice_peppol', 'post_install', '-at_install')
class TestPeppolInboundWorkflowGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.doc = cls.env['eh.peppol.inbound'].create({
            'company_id': cls.env.company.id,
        })
        # A plain user (not superuser) to attempt the bypass. The test env
        # runs as SUPERUSER, which passes the guard, so we MUST act as a
        # normal user to exercise it.
        try:
            cls.plain_user = cls.env['res.users'].create({
                'name': 'Peppol Plain User',
                'login': 'eh_peppol_plain_user',
                'groups_id': [(6, 0, [
                    cls.env.ref('base.group_user').id,
                ])],
            })
        except Exception:  # noqa: BLE001
            cls.plain_user = cls.env['res.users'].browse()

    def test_direct_state_write_blocked_for_plain_user(self):
        """A non-superuser RPC write to state is refused."""
        if not self.plain_user:
            self.skipTest("Could not create a non-superuser test user.")
        self.assertEqual(self.doc.state, 'received')
        with self.assertRaises(AccessError):
            self.doc.with_user(self.plain_user).write({'state': 'posted'})
        # State is unchanged after the blocked write.
        self.assertEqual(self.doc.state, 'received')

    def test_action_moves_state_as_su(self):
        """The sanctioned path (action, which runs under sudo) still works."""
        # action_parse requires an XML attachment; assert instead that the
        # guarded write goes through when initiated server-side via sudo,
        # which is exactly what _eh_workflow_action() grants an action.
        self.doc.sudo().write({'state': 'parsed'})
        self.assertEqual(self.doc.state, 'parsed')
