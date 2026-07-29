# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: cheque state cannot be RPC-written past actions.

A state machine protected only by a readonly statusbar is not protected: a
draft cheque's state is not frozen, so a plain user could
``write({'state': 'cleared'})`` straight past ``action_clear`` and its journal
entry. The eh.workflow.guard mixin blocks any direct non-superuser write to a
guarded field; only the record's own actions (which run under sudo) let the
write through.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import EhPdcTestCase


@tagged('eh_account_pdc', 'post_install', '-at_install')
class TestPdcWorkflowGuard(EhPdcTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A plain accounting user: read/write on the model but NOT a manager
        # and NOT superuser. This is the RPC vector the guard closes. The test
        # env itself runs as SUPERUSER, so the guard would (correctly) not
        # fire; we must exercise it as a genuine non-superuser user.
        group_xmlids = ['base.group_user']
        eh_user = cls.env.ref(
            'eh_account_base.group_eh_user', raise_if_not_found=False)
        if eh_user:
            group_xmlids.append('eh_account_base.group_eh_user')
        try:
            cls.plain_user = cls.env['res.users'].create({
                'name': 'Plain PDC User',
                'login': 'plain_pdc_user',
                'groups_id': [(6, 0, [
                    cls.env.ref(x).id for x in group_xmlids])],
            })
        except Exception:  # pragma: no cover - env cannot create a user
            cls.plain_user = False
        # A partner created here rather than referenced from demo data
        # (base.res_partner_2), which is absent when the suite loads nodemo.
        cls.partner = cls.env['res.partner'].create({'name': 'PDC Guard Co'})

    def test_direct_cheque_state_write_is_blocked(self):
        if not self.plain_user:
            self.skipTest("No non-superuser user available in this env.")
        rec = self.env['eh.cheque'].create({
            'direction': 'incoming',
            'cheque_number': '900001',
            'journal_id': self.bank_journal.id,
            'partner_id': self.partner.id,
            'amount': 500.0,
            'currency_id': self.company.currency_id.id,
            'value_date': self.today,
        })
        self.assertEqual(rec.state, 'draft')
        # The bypass: a plain user RPC-writing state past action_clear.
        with self.assertRaises(AccessError):
            rec.with_user(self.plain_user).write({'state': 'cleared'})
        # State is unchanged: the bypass did not take effect.
        rec.invalidate_recordset(['state'])
        self.assertEqual(rec.state, 'draft')

    def test_direct_book_state_write_is_blocked(self):
        if not self.plain_user:
            self.skipTest("No non-superuser user available in this env.")
        book = self.env['eh.cheque.book'].create({
            'name': 'Guard Book 2000-2010',
            'journal_id': self.bank_journal.id,
            'start_number': 2000,
            'end_number': 2010,
            'next_number': 2000,
        })
        self.assertEqual(book.state, 'draft')
        with self.assertRaises(AccessError):
            book.with_user(self.plain_user).write({'state': 'in_use'})
        book.invalidate_recordset(['state'])
        self.assertEqual(book.state, 'draft')
