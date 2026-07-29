# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
SEPA originator model tests: verifies IBAN/BIC validation runs on
write, that the canonicalised forms are persisted, and that name
length is enforced per the scheme.
"""

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_sepa_ct', 'integration', 'post_install', '-at_install')
class TestSepaOriginator(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Originator = cls.env['eh.sepa.originator']
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

    def test_create_canonicalises_iban_and_bic(self):
        rec = self.Originator.create({
            'journal_id': self.bank_journal.id,
            'initiating_party_name': 'Demo Co',
            'iban': 'de89 3704 0044 0532 0130 00',
            'bic': 'deutdeff',
        })
        self.assertEqual(rec.iban, 'DE89370400440532013000')
        self.assertEqual(rec.bic, 'DEUTDEFFXXX')

    def test_invalid_iban_rejected(self):
        with self.assertRaises(ValidationError):
            self.Originator.create({
                'journal_id': self.bank_journal.id,
                'initiating_party_name': 'Demo Co',
                'iban': 'DE99370400440532013000',  # tampered check digits
            })

    def test_invalid_bic_rejected(self):
        with self.assertRaises(ValidationError):
            self.Originator.create({
                'journal_id': self.bank_journal.id,
                'initiating_party_name': 'Demo Co',
                'iban': 'DE89370400440532013000',
                'bic': 'TOO_SHORT',
            })

    def test_too_long_party_name_rejected(self):
        with self.assertRaises(ValidationError):
            self.Originator.create({
                'journal_id': self.bank_journal.id,
                'initiating_party_name': 'X' * 71,
                'iban': 'DE89370400440532013000',
            })

    def test_unique_per_journal(self):
        self.Originator.create({
            'journal_id': self.bank_journal.id,
            'initiating_party_name': 'Demo Co',
            'iban': 'DE89370400440532013000',
        })
        with self.assertRaises(Exception):
            self.Originator.create({
                'journal_id': self.bank_journal.id,
                'initiating_party_name': 'Demo Co Duplicate',
                'iban': 'DE89370400440532013000',
            })
