# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Workflow-guard regression tests.

A state machine protected only by a readonly widget is not protected:
a plain user with write access to the model can RPC
``write({'state': 'active'})`` straight past the activation action and
its checks. The eh.workflow.guard mixin blocks any non-superuser write
to a guarded field; only the record's own actions (which run under
sudo) may move the state.

These tests assert the guard fires for a NON-superuser (the test env
itself runs as SUPERUSER, where the guard intentionally passes) and
that the sanctioned action path still works.
"""

from datetime import date

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_sepa_dd', 'post_install', '-at_install')
class TestWorkflowGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Mandate = cls.env['eh.sepa.mandate']
        cls.Creditor = cls.env['eh.sepa.creditor']

        cls.bank_journal = cls.env['account.journal'].search(
            [('type', '=', 'bank'),
             ('company_id', '=', cls.env.company.id)],
            limit=1,
        )
        if not cls.bank_journal:
            cls.bank_journal = cls.env['account.journal'].create({
                'name': 'Test Bank',
                'code': 'TBKG',
                'type': 'bank',
                'company_id': cls.env.company.id,
            })

        existing = cls.Creditor.search(
            [('journal_id', '=', cls.bank_journal.id)], limit=1,
        )
        if existing:
            cls.creditor = existing
        else:
            cls.creditor = cls.Creditor.create({
                'name': 'Guard creditor',
                'journal_id': cls.bank_journal.id,
                'creditor_identifier': 'DE98ZZZ09999999999',
                'creditor_name': 'Guard Co',
                'iban': 'DE89370400440532013000',
            })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Guard customer',
        })

        # A plain (non-superuser) user with write access to the mandate,
        # so the guard - not a missing ACL - is what blocks the state
        # write. group_eh_user grants read/write/create on the mandate.
        cls.plain_user = None
        try:
            group = cls.env.ref('eh_account_base.group_eh_user')
            cls.plain_user = cls.env['res.users'].create({
                'name': 'Guard plain user',
                'login': 'eh_sepa_dd_guard_user',
                'email': 'eh_sepa_dd_guard_user@example.com',
                'company_id': cls.env.company.id,  # noqa: F601
                'company_id': cls.env.company.id,  # noqa: F601
                # Odoo 19: res.users uses group_ids (not groups_id).
                'groups_id': [(4, group.id)],
            })
        except Exception:
            cls.plain_user = None

    def _make_draft_mandate(self):
        return self.Mandate.create({
            'mandate_id': 'MNDT-GUARD-001',
            'creditor_id': self.creditor.id,
            'partner_id': self.partner.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 15),
            'state': 'draft',
            'local_instrument': 'CORE',
        })

    def test_plain_user_cannot_write_state(self):
        """A non-superuser direct write to the guarded state is refused."""
        if not self.plain_user:
            self.skipTest("Could not create a non-superuser test user.")
        self.doc = self._make_draft_mandate()
        with self.assertRaises(AccessError):
            self.doc.with_user(self.plain_user).write({'state': 'active'})
        # State must be untouched by the blocked write.
        self.doc.invalidate_recordset(['state'])
        self.assertEqual(self.doc.state, 'draft')

    def test_sanctioned_action_still_activates(self):
        """The record's own action (runs under sudo) still moves state."""
        self.doc = self._make_draft_mandate()
        self.doc.action_activate()
        self.assertEqual(self.doc.state, 'active')
