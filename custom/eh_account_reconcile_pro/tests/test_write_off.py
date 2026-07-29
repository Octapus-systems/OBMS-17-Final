# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Write-off and FX write-off actually clear the statement line."""

from odoo.tests import tagged

from odoo.addons.eh_account_reconcile_pro.tests.common import (
    EhReconcileIntegrationTestCase,
)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestWriteOff(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suspense = cls.bank_journal.suspense_account_id
        if not suspense:
            suspense = cls._ensure_account(
                cls.env, '1990', 'Bank Suspense', 'asset_current')
            cls.bank_journal.suspense_account_id = suspense
        suspense.reconcile = True

    def test_write_off_clears_line(self):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(7.50, payment_ref='Bank fee')

        session.apply_write_off(
            line.id, self.account_expense.id, label='Bank fee')

        line.invalidate_recordset()
        self.assertTrue(line.is_reconciled)
        # The suspense residual was carried onto the write-off account via a
        # separate adjusting entry; the posted statement move is untouched.
        self.assertNotIn(
            self.account_expense,
            line.move_id.line_ids.mapped('account_id'))
        written = self.env['account.move.line'].search([
            ('account_id', '=', self.account_expense.id),
            ('move_id', '!=', line.move_id.id),
            ('parent_state', '=', 'posted'),
        ])
        self.assertTrue(written)

    def test_fx_writeoff_clears_to_exchange_account(self):
        gain = self._ensure_account(
            self.env, '7800', 'FX Gain', 'income_other')
        loss = self._ensure_account(
            self.env, '8800', 'FX Loss', 'expense')
        self.company.income_currency_exchange_account_id = gain
        self.company.expense_currency_exchange_account_id = loss

        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(2.0, payment_ref='FX rounding')

        session.apply_fx_writeoff(line.id, max_amount=5.0)

        line.invalidate_recordset()
        self.assertTrue(line.is_reconciled)
        # The FX residual lands on the exchange account via a separate
        # adjusting entry, not on the posted statement move.
        self.assertFalse(
            line.move_id.line_ids.filtered(
                lambda l: l.account_id in (gain | loss)))
        moved = self.env['account.move.line'].search([
            ('account_id', 'in', (gain | loss).ids),
            ('move_id', '!=', line.move_id.id),
            ('parent_state', '=', 'posted'),
        ])
        self.assertTrue(moved)
