# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Cheque book lifecycle and serial allocation tests."""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhPdcTestCase


@tagged('eh_account_pdc', 'integration', 'post_install', '-at_install')
class TestChequeBook(EhPdcTestCase):

    def test_default_next_number_is_start(self):
        book = self.env['eh.cheque.book'].create({
            'name': 'Book A',
            'journal_id': self.bank_journal.id,
            'start_number': 100,
            'end_number': 110,
        })
        self.assertEqual(book.next_number, 100)
        self.assertEqual(book.remaining_count, 11)
        self.assertEqual(book.state, 'draft')

    def test_activate_blocks_when_another_active(self):
        # self.book is already in_use
        new_book = self.env['eh.cheque.book'].create({
            'name': 'Book B',
            'journal_id': self.bank_journal.id,
            'start_number': 50,
            'end_number': 60,
        })
        with self.assertRaises(UserError):
            new_book.action_activate()

    def test_consume_next_serial(self):
        # Fresh book on a different journal so we don't clash with self.book.
        other_journal = self.env['account.journal'].create({
            'name': 'Other Bank',
            'code': 'OBNK',
            'type': 'bank',
            'company_id': self.company.id,
        })
        book = self.env['eh.cheque.book'].create({
            'name': 'Other Book',
            'journal_id': other_journal.id,
            'start_number': 1,
            'end_number': 2,
        })
        book.action_activate()
        s1 = book.consume_next_serial()
        s2 = book.consume_next_serial()
        self.assertEqual(s1, 1)
        self.assertEqual(s2, 2)
        self.assertEqual(book.state, 'exhausted')
        with self.assertRaises(UserError):
            book.consume_next_serial()

    def test_close_blocks_consume(self):
        self.book.action_close()
        with self.assertRaises(UserError):
            self.book.consume_next_serial()
