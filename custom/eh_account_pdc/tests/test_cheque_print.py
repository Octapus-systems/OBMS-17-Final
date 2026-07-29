# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""B6 cheque printing: amount-in-words and the physical cheque report."""

from odoo.tests import tagged

from odoo.addons.eh_account_pdc.tests.common import EhPdcTestCase


@tagged('eh_account_pdc', 'integration', 'post_install', '-at_install')
class TestChequePrint(EhPdcTestCase):

    def _make_outgoing(self, **overrides):
        vals = {
            'direction': 'outgoing',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'book_id': self.book.id,
            'cheque_number': '1',
            'amount': 1000.0,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': self.today,
        }
        vals.update(overrides)
        return self.env['eh.cheque'].create(vals)

    def test_amount_in_words_tracks_amount(self):
        cheque = self._make_outgoing(amount=1000.0)
        self.assertTrue(cheque.amount_in_words)
        other = self._make_outgoing(cheque_number='2', amount=2000.0)
        self.assertNotEqual(cheque.amount_in_words, other.amount_in_words)

    def test_print_action_returns_report(self):
        cheque = self._make_outgoing()
        action = cheque.action_print_cheque()
        self.assertEqual(action.get('type'), 'ir.actions.report')

    def test_report_renders_without_error(self):
        cheque = self._make_outgoing()
        report = self.env.ref('eh_account_pdc.action_report_cheque_print')
        html, _dummy = self.env['ir.actions.report']._render_qweb_html(
            report.report_name, cheque.ids)
        self.assertIn(b'Cheque', html)
