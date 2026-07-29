# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Inalterability seal on posted sub-ledger GL entries.

A move stamped eh_sealed by an ERP Heritage sub-ledger cannot, once posted, be
reset to draft or cancelled, nor can its figures be edited / added / removed in
place; the sanctioned reversal path carries the eh_allow_unpost context flag.
A move that is NOT sealed (a normal journal entry / invoice) is unaffected.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_base', 'integration', 'post_install', '-at_install')
class TestMoveSeal(EhAccountIntegrationTestCase):

    def _posted_move(self):
        return self.post_balanced_move([
            {'account': self.account_expense, 'debit': 100.0},
            {'account': self.account_cash, 'credit': 100.0},
        ])

    def test_unsealed_move_is_unaffected(self):
        move = self._posted_move()
        self.assertFalse(move.eh_sealed)
        move.button_draft()
        self.assertEqual(move.state, 'draft')

    def test_sealed_move_cannot_be_reset_or_edited(self):
        move = self._posted_move()
        move.eh_sealed = True
        self.assertEqual(move.state, 'posted')
        with self.assertRaises(UserError):
            move.button_draft()
        with self.assertRaises(UserError):
            move.button_cancel()
        with self.assertRaises(UserError):
            move.write({'state': 'draft'})
        # A material figure edit on a line is refused.
        with self.assertRaises(UserError):
            move.line_ids[0].debit = 5.0
        # Adding / removing a line is refused.
        with self.assertRaises(UserError):
            move.line_ids[0].unlink()
        self.assertEqual(move.state, 'posted')

    def test_sanctioned_context_bypasses_seal(self):
        move = self._posted_move()
        move.eh_sealed = True
        # The reversal / reset paths run under eh_allow_unpost.
        move.with_context(eh_allow_unpost=True).button_draft()
        self.assertEqual(move.state, 'draft')
