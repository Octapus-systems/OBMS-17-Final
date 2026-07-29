# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Open-item customer statement: a period settlement of a prior invoice must
reduce the amount due, not leave a phantom balance (regression for the
overstatement where a clearing payment was excluded by the residual filter)."""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestCustomerStatement(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.customer_statement']
        cls.partner = cls.env['res.partner'].create({'name': 'Stmt Cust'})
        cls.income = cls._ensure_account(
            cls.env, '4001', 'Stmt Income', 'income')
        cls.sale_journal = cls.env['account.journal'].search(
            [('type', '=', 'sale'),
             ('company_id', '=', cls.env.company.id)], limit=1)
        if not cls.sale_journal:
            cls.sale_journal = cls.env['account.journal'].create({
                'name': 'Stmt Sales', 'code': 'STMTS', 'type': 'sale',
                'company_id': cls.env.company.id})
        cls.bank_journal = cls.env['account.journal'].search(
            [('type', '=', 'bank'),
             ('company_id', '=', cls.env.company.id)], limit=1)
        if not cls.bank_journal:
            cls.bank_journal = cls.env['account.journal'].create({
                'name': 'Stmt Bank', 'code': 'STMTB', 'type': 'bank',
                'company_id': cls.env.company.id})

    def _invoice(self, amount, date):
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': date, 'journal_id': self.sale_journal.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'x', 'quantity': 1, 'price_unit': amount,
                'account_id': self.income.id})]})
        inv.action_post()
        return inv

    def _pay(self, inv, date):
        self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=inv.ids).create({
                'payment_date': date,
                'journal_id': self.bank_journal.id,
            })._create_payments()

    def _amount_due(self, date_from, date_to, stype='open_item'):
        payload = self.handler.compute({
            'partner_id': self.partner.id,
            'date': {'date_from': date_from, 'date_to': date_to},
            'company_ids': self.env.company.ids,
            'statement_type': stype,
        })
        return abs(payload['totals']['amount_due'])

    def test_open_item_period_settlement_clears_amount_due(self):
        inv = self._invoice(1000.0, '2026-03-10')  # prior period
        # Before the payment the prior invoice is outstanding at date_to.
        self.assertAlmostEqual(
            self._amount_due('2026-06-01', '2026-06-30'), 1000.0, places=2)
        self._pay(inv, '2026-06-15')  # cleared IN the statement period
        # The bug: the clearing payment was excluded, so the running balance
        # kept the 1,000. Fixed: the item drops to residual 0 -> amount due 0.
        self.assertAlmostEqual(
            self._amount_due('2026-06-01', '2026-06-30'), 0.0, places=2)

    def test_open_item_shows_still_open_invoice(self):
        self._invoice(400.0, '2026-06-05')
        self.assertAlmostEqual(
            self._amount_due('2026-06-01', '2026-06-30'), 400.0, places=2)
