# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Workflow-guard regression: the eh.sepa.export state machine
(generated -> downloaded -> superseded) must be unreachable by a direct
RPC/ORM write. A plain user with model write access can still not flip
state to 'downloaded' to skip action_download and its attachment check,
nor bury an audit row as 'superseded'. State advances only through the
model's own actions, which run as su.
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_sepa_ct', 'integration', 'post_install', '-at_install')
class TestSepaExportWorkflowGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.bank_journal = cls.env['account.journal'].search(
            [('type', '=', 'bank'),
             ('company_id', '=', cls.env.company.id)],
            limit=1,
        )
        if not cls.bank_journal:
            cls.bank_journal = cls.env['account.journal'].create({
                'name': 'Test Bank',
                'code': 'TBK',
                'type': 'bank',
                'company_id': cls.env.company.id,
            })

        cls.batch = cls.env['eh.batch.payment'].create({
            'journal_id': cls.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })

        # A fresh export is born in the initial state 'generated'.
        cls.rec = cls.env['eh.sepa.export'].create({
            'batch_id': cls.batch.id,
            'message_id': 'MSG-GUARD-001',
        })

        # A plain user WITH ORM write access to the model, so the block we
        # observe is the workflow guard - not ir.model.access.
        group = cls.env.ref('eh_account_base.group_eh_user', False)
        try:
            cls.plain_user = cls.env['res.users'].create({
                'name': 'eh_sepa_guard_user',
                'login': 'eh_sepa_guard_user',
                'email': 'eh_sepa_guard_user@example.com',
                'groups_id': [(4, group.id)] if group else [],
            })
        except Exception:
            cls.plain_user = cls.env['res.users'].browse()

    def test_direct_state_write_blocked_for_plain_user(self):
        """A non-superuser cannot RPC-write state past action_download."""
        if not self.plain_user:
            self.skipTest("No plain user available in this environment.")
        self.assertEqual(self.rec.state, 'generated')
        with self.assertRaises(AccessError):
            self.rec.with_user(self.plain_user).write({'state': 'downloaded'})
        # State is unchanged after the blocked write.
        self.assertEqual(self.rec.state, 'generated')

    def test_supersede_write_blocked_for_plain_user(self):
        """A plain user cannot bury an audit row by flipping it superseded."""
        if not self.plain_user:
            self.skipTest("No plain user available in this environment.")
        with self.assertRaises(AccessError):
            self.rec.with_user(self.plain_user).write({'state': 'superseded'})
        self.assertEqual(self.rec.state, 'generated')

    def test_action_download_moves_state_as_su(self):
        """The sanctioned action still advances the state (positive path)."""
        attachment = self.env['ir.attachment'].create({
            'name': 'guard_probe.xml',
            'type': 'binary',
            'datas': b'',
            'res_model': 'eh.sepa.export',
            'res_id': self.rec.id,
        })
        self.rec.attachment_id = attachment
        self.rec.action_download()
        self.assertEqual(self.rec.state, 'downloaded')
