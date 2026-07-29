# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Shared fixtures for PDC tests."""

from odoo import fields

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


class EhPdcTestCase(EhAccountIntegrationTestCase):
    """Adds a bank journal and a cheque book on top of the base fixtures."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bank_journal = cls.env['account.journal'].search(
            [
                ('company_id', '=', cls.company.id),
                ('type', '=', 'bank'),
            ],
            limit=1,
        )
        if not cls.bank_journal:
            cls.bank_journal = cls.env['account.journal'].create({
                'name': 'Test Bank',
                'code': 'TBNK',
                'type': 'bank',
                'company_id': cls.company.id,
            })

        cls.book = cls.env['eh.cheque.book'].create({
            'name': 'Test Book 0001-0010',
            'journal_id': cls.bank_journal.id,
            'start_number': 1,
            'end_number': 10,
            'next_number': 1,
        })
        cls.book.action_activate()

        cls.reason_funds = cls.env.ref(
            'eh_account_pdc.bounce_reason_insufficient_funds',
        )
        cls.reason_closed = cls.env.ref(
            'eh_account_pdc.bounce_reason_account_closed',
        )

        cls.today = fields.Date.context_today(cls.env['eh.cheque'])
