# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank Reconciliation proof handler tests.

Creates a bank journal + statement (balance_start, lines) + an unreconciled
outstanding receipt + an unreconciled outstanding payment and asserts:

* Last-statement balance = balance_start + in-window line amounts.
* Book GL balance = cumulative aml SUM on the journal's bank account.
* The bridge: GL = last_stmt + outstanding_receipts - outstanding_payments
  + difference, with the difference zero when everything ties and non-zero
  (a balance_check line emitted) when a stray GL entry is injected.
* Multi-journal scope yields one section per journal.
* A journal with no statement yields an empty last-statement section
  (book balance only) without raising.
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestBankReconciliationHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.bank_reconciliation']
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'bank_reconciliation')], limit=1)
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'bank_reconciliation',
                'name': 'Bank Reconciliation',
                'handler_model':
                    'eh.account.dynamic.report.handler.bank_reconciliation',
            })

        # Bank account (GL side) and suspense accounts.
        cls.account_bank = cls._ensure_account(
            cls.env, '1010', 'Bank Current Account', 'asset_cash')
        cls.account_inbound = cls._ensure_account(
            cls.env, '1011', 'Outstanding Receipts', 'asset_current')
        cls.account_outbound = cls._ensure_account(
            cls.env, '1012', 'Outstanding Payments', 'asset_current')
        # Outstanding-payment suspense accounts are reconcilable in a real
        # configuration; mark them so amount_residual computes (the handler
        # also falls back to balance, so the proof holds either way).
        (cls.account_inbound | cls.account_outbound).write(
            {'reconcile': True})

        cls.bank_journal = cls.env['account.journal'].create({
            'name': 'Proof Bank',
            'code': 'PBNK',
            'type': 'bank',
            'company_id': cls.company.id,
            'default_account_id': cls.account_bank.id,
        })
        # Pin the suspense accounts on the journal's payment method lines so
        # the outstanding-account accessors return known accounts.
        cls.bank_journal.inbound_payment_method_line_ids[:1].write({
            'payment_account_id': cls.account_inbound.id,
        })
        cls.bank_journal.outbound_payment_method_line_ids[:1].write({
            'payment_account_id': cls.account_outbound.id,
        })

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'journal_ids': [self.bank_journal.id],
        }

    @staticmethod
    def _line_by_id(result, line_id):
        for line in result['lines']:
            if line['id'] == line_id:
                return line
        return None

    @staticmethod
    def _amount(line):
        if line is None:
            return None
        for col in line['columns']:
            if col['expression_label'] == 'amount':
                return col['value']
        return None

    def _post(self, lines, date_str='2026-06-15'):
        return self.post_balanced_move(
            lines, journal=self.bank_journal,
            date=fields.Date.from_string(date_str))

    def _make_statement(self, balance_start, line_amounts, date_str):
        # A statement binds to its journal through its lines (journal_id is
        # a computed field on the statement). When the caller passes no
        # movement lines we still need at least one line on the target
        # journal so the statement is discoverable; a zero-amount line binds
        # the journal and dates the statement without changing the in-window
        # sum or the bank GL balance.
        amounts = list(line_amounts) if line_amounts else [0.0]
        line_ids = [
            (0, 0, {
                'date': fields.Date.from_string(date_str),
                'amount': amt,
                'payment_ref': 'stmt line %s' % amt,
                'journal_id': self.bank_journal.id,
            })
            for amt in amounts
        ]
        return self.env['account.bank.statement'].create({
            'name': 'Stmt %s' % date_str,
            'balance_start': balance_start,
            'line_ids': line_ids,
        })

    def _make_outstanding(self, account, amount, debit=True,
                          date_str='2026-06-20'):
        """Post a balanced move that leaves an unreconciled residual on the
        given suspense account."""
        if debit:
            lines = [
                {'account': account, 'debit': amount},
                {'account': self.account_revenue, 'credit': amount},
            ]
        else:
            lines = [
                {'account': self.account_expense, 'debit': amount},
                {'account': account, 'credit': amount},
            ]
        return self._post(lines, date_str=date_str)

    # ---- last statement balance ----

    def test_last_statement_balance(self):
        self._make_statement(
            balance_start=1000.0, line_amounts=[500.0, -200.0],
            date_str='2026-06-10')
        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)
        last_stmt = self._line_by_id(result, '%s-last-stmt' % sid)
        # 1000 + 500 - 200 = 1300.
        self.assertAlmostEqual(self._amount(last_stmt), 1300.0, places=2)

    def test_no_statement_journal_yields_empty_last_statement(self):
        # No statement created. Must not raise; last-statement = 0.
        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)
        last_stmt = self._line_by_id(result, '%s-last-stmt' % sid)
        self.assertIsNotNone(last_stmt)
        self.assertAlmostEqual(self._amount(last_stmt), 0.0, places=2)

    # ---- book balance ----

    def test_book_balance_is_cumulative_gl_sum(self):
        # Two postings touching the bank account: +1000 and -300.
        self._post([
            {'account': self.account_bank, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 1000.0},
        ], date_str='2026-03-01')
        self._post([
            {'account': self.account_expense, 'debit': 300.0},
            {'account': self.account_bank, 'credit': 300.0},
        ], date_str='2026-04-01')
        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)
        book = self._line_by_id(result, '%s-book' % sid)
        self.assertAlmostEqual(self._amount(book), 700.0, places=2)

    # ---- the bridge ----

    def test_bridge_ties_to_zero_when_reconciled(self):
        # Statement reports the bank at 700; the GL bank account also nets
        # to 700; no outstanding items -> difference is zero.
        self._make_statement(
            balance_start=700.0, line_amounts=[], date_str='2026-02-01')
        self._post([
            {'account': self.account_bank, 'debit': 700.0},
            {'account': self.account_revenue, 'credit': 700.0},
        ], date_str='2026-03-01')
        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)
        diff = self._line_by_id(result, '%s-difference' % sid)
        self.assertAlmostEqual(self._amount(diff), 0.0, places=2)
        # bridge identity: GL = adjusted bank + difference.
        book = self._amount(self._line_by_id(result, '%s-book' % sid))
        adjusted = self._amount(self._line_by_id(result, '%s-adjusted' % sid))
        self.assertAlmostEqual(
            book, adjusted + self._amount(diff), places=2)

    def test_outstanding_items_feed_the_bridge(self):
        # Statement at 1000; GL bank at 1000; plus an outstanding receipt of
        # 250 (deposit in transit) and an outstanding payment of 100.
        self._make_statement(
            balance_start=1000.0, line_amounts=[], date_str='2026-02-01')
        self._post([
            {'account': self.account_bank, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 1000.0},
        ], date_str='2026-03-01')
        self._make_outstanding(self.account_inbound, 250.0, debit=True)
        self._make_outstanding(self.account_outbound, 100.0, debit=False)

        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)

        receipts = self._line_by_id(
            result, '%s-receipts-header' % sid)
        payments = self._line_by_id(
            result, '%s-payments-header' % sid)
        self.assertAlmostEqual(self._amount(receipts), 250.0, places=2)
        self.assertAlmostEqual(self._amount(payments), 100.0, places=2)

        # Adjusted bank = 1000 + 250 - 100 = 1150. Book = 1000.
        adjusted = self._amount(self._line_by_id(result, '%s-adjusted' % sid))
        self.assertAlmostEqual(adjusted, 1150.0, places=2)
        diff = self._amount(self._line_by_id(result, '%s-difference' % sid))
        # Difference = book - adjusted = 1000 - 1150 = -150.
        self.assertAlmostEqual(diff, -150.0, places=2)

    def test_stray_gl_entry_produces_nonzero_difference(self):
        # Tie statement and GL at 500, then inject a stray bank posting the
        # statement never saw -> the difference must surface it.
        self._make_statement(
            balance_start=500.0, line_amounts=[], date_str='2026-02-01')
        self._post([
            {'account': self.account_bank, 'debit': 500.0},
            {'account': self.account_revenue, 'credit': 500.0},
        ], date_str='2026-03-01')
        # Stray entry: bank debited 90, no statement line, no outstanding.
        self._post([
            {'account': self.account_bank, 'debit': 90.0},
            {'account': self.account_revenue, 'credit': 90.0},
        ], date_str='2026-05-01')
        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)
        diff_line = self._line_by_id(result, '%s-difference' % sid)
        self.assertAlmostEqual(self._amount(diff_line), 90.0, places=2)
        # A balance_check kind is emitted so the renderer paints it.
        self.assertEqual(
            (diff_line.get('meta') or {}).get('kind'), 'balance_check')

    # ---- multi-journal ----

    def test_multi_journal_one_section_each(self):
        bank2 = self.env['account.journal'].create({
            'name': 'Proof Bank 2',
            'code': 'PBN2',
            'type': 'bank',
            'company_id': self.company.id,
            'default_account_id': self.account_bank.id,
        })
        opts = dict(self.options,
                    journal_ids=[self.bank_journal.id, bank2.id])
        result = self.handler.compute(opts)
        headers = [
            line_item for line_item in result['lines']
            if line_item['id'].endswith('-header')
            and (line_item.get('meta') or {}).get('kind') == 'section_header']
        # One section header per journal.
        journal_headers = [
            h for h in headers
            if (h.get('meta') or {}).get('journal_id') in (
                self.bank_journal.id, bank2.id)]
        self.assertEqual(len(journal_headers), 2)

    # ---- orchestrator + drilldown ----

    def test_report_renders_through_orchestrator(self):
        self._make_statement(
            balance_start=100.0, line_amounts=[], date_str='2026-02-01')
        payload = self.report.render(self.options)
        self.assertIn('lines', payload)
        self.assertIn('columns', payload)

    def test_outstanding_row_drilldown(self):
        self._make_outstanding(self.account_inbound, 250.0, debit=True)
        result = self.handler.compute(self.options)
        outstanding = [
            line_item for line_item in result['lines']
            if line_item['id'].startswith('outstanding-')]
        self.assertTrue(outstanding)
        action = self.handler.get_drilldown_action(
            self.options, outstanding[0]['id'])
        self.assertIsInstance(action, dict)
        self.assertEqual(action['res_model'], 'account.move.line')
        # Computed proof lines do not drill.
        sid = 'journal-%s' % self.bank_journal.id
        self.assertIsNone(
            self.handler.get_drilldown_action(
                self.options, '%s-difference' % sid))

    def test_empty_scope_does_not_raise(self):
        # A company with no matching journals -> empty lines, note set.
        opts = dict(self.options, journal_ids=[999999999])
        result = self.handler.compute(opts)
        self.assertEqual(result['lines'], [])
        self.assertIn('note', result['meta'])
