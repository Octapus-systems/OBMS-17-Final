# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
End to end import wizard tests.

Drives the wizard through a full upload, verifies the statement and its
lines are materialised, and exercises the idempotent reimport guard
(uploading the same file twice produces one statement, not two).
"""

import base64

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_bank_statement_import', 'integration', 'post_install', '-at_install')
class TestImportWizard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wizard = cls.env['eh.account.bank.statement.import.wizard']
        cls.Profile = cls.env['eh.account.bank.statement.import.profile']
        cls.Statement = cls.env['account.bank.statement']
        cls.Log = cls.env['eh.account.bank.statement.import.log']

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

        # Keep the profile currency aligned with the journal's effective
        # currency (its own currency, or the company currency when blank)
        # so the wizard's currency guard does not reject the happy-path
        # fixtures. The mismatch case is covered by its own test below.
        cls.journal_currency = (
            cls.bank_journal.currency_id
            or cls.bank_journal.company_id.currency_id
        )

        cls.profile = cls.Profile.create({
            'name': 'Test profile',
            'journal_id': cls.bank_journal.id,
            'currency_code': cls.journal_currency.name,
            'csv_delimiter': ',',
            'csv_quotechar': '"',
            'csv_encoding': 'utf-8',
            'csv_header_rows': 1,
            'decimal_separator': '.',
            'date_format': '%Y-%m-%d',
            'col_date': 0,
            'col_amount': 1,
            'col_ref': 2,
            'col_narration': 3,
        })

    def _csv_bytes(self, body):
        return body.encode('utf-8')

    def _make_wizard(self, content):
        return self.Wizard.create({
            'journal_id': self.bank_journal.id,
            'profile_id': self.profile.id,
            'format_key': 'csv',
            'file_data': base64.b64encode(content),
            'filename': 'test.csv',
        })

    # ---- happy path ----

    def test_import_creates_statement_and_lines(self):
        content = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-04-15,150.00,INV-100,Sale to customer\n"
            "2026-04-16,-25.50,FEE,Bank service charge\n"
            "2026-04-17,300.00,INV-101,Sale to other\n",
        )
        wizard = self._make_wizard(content)
        action = wizard.action_import()
        self.assertEqual(action['res_model'], 'account.bank.statement')
        statement = self.Statement.browse(action['res_id'])
        self.assertEqual(len(statement.line_ids), 3)
        amounts = sorted(statement.line_ids.mapped('amount'))
        self.assertEqual(amounts, [-25.50, 150.00, 300.00])

    def test_import_log_records_done(self):
        content = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-04-20,42.00,X,Y\n",
        )
        wizard = self._make_wizard(content)
        wizard.action_import()
        log = self.Log.search(
            [('journal_id', '=', self.bank_journal.id)],
            order='id desc', limit=1,
        )
        self.assertEqual(log.state, 'done')
        self.assertEqual(log.line_count, 1)
        self.assertTrue(log.statement_id)
        self.assertTrue(log.file_hash)

    # ---- idempotency ----

    def test_reimport_same_file_returns_duplicate(self):
        content = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-04-21,500.00,INV-200,Big sale\n",
        )
        first = self._make_wizard(content)
        first.action_import()

        second = self._make_wizard(content)
        action = second.action_import()
        # Second import navigates to the log, not a fresh statement.
        self.assertEqual(
            action['res_model'],
            'eh.account.bank.statement.import.log',
        )
        log = self.Log.search(
            [('journal_id', '=', self.bank_journal.id)],
            order='id desc', limit=1,
        )
        self.assertEqual(log.state, 'duplicate')
        # Total statements created so far is exactly one.
        statements = self.Statement.search(
            [('journal_id', '=', self.bank_journal.id)],
        )
        self.assertEqual(len(statements), 1)

    # ---- error path ----

    def test_csv_format_requires_profile(self):
        content = self._csv_bytes("Date,Amount\n2026-01-01,100\n")
        wizard = self.Wizard.create({
            'journal_id': self.bank_journal.id,
            'profile_id': False,
            'format_key': 'csv',
            'file_data': base64.b64encode(content),
            'filename': 'no_profile.csv',
        })
        with self.assertRaises(UserError):
            wizard.action_import()

    def test_malformed_csv_logs_error(self):
        # A row that fails parsing produces a UserError AND a log row.
        # Using try/except instead of assertRaises so the test does not
        # open an implicit savepoint that would roll back the audit-log
        # row created inside action_import.
        content = self._csv_bytes(
            "Date,Amount,Ref,Memo\n"
            "not-a-date,not-a-number,X,Y\n",
        )
        wizard = self._make_wizard(content)
        raised = False
        try:
            wizard.action_import()
        except UserError:
            raised = True
        self.assertTrue(raised, "action_import must raise UserError on bad CSV")
        log = self.Log.search(
            [('journal_id', '=', self.bank_journal.id),
             ('state', '=', 'error')],
            order='id desc', limit=1,
        )
        self.assertTrue(log)
        self.assertIn("row 1", log.error_message or "")

    # ---- currency guard ----

    def test_currency_mismatch_blocks_and_logs(self):
        # A profile whose currency differs from the journal's effective
        # currency must stop the import with a UserError and leave an
        # audited error row, never book the lines at face value in the
        # wrong currency.
        other_code = 'AUD' if self.journal_currency.name != 'AUD' else 'USD'
        other_currency = self.env['res.currency'].with_context(
            active_test=False).search([('name', '=', other_code)], limit=1)
        other_currency.active = True
        mismatch_profile = self.Profile.create({
            'name': 'Mismatch profile',
            'journal_id': self.bank_journal.id,
            'currency_code': other_code,
            'csv_delimiter': ',',
            'csv_quotechar': '"',
            'csv_encoding': 'utf-8',
            'csv_header_rows': 1,
            'decimal_separator': '.',
            'date_format': '%Y-%m-%d',
            'col_date': 0,
            'col_amount': 1,
            'col_ref': 2,
            'col_narration': 3,
        })
        content = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-06-01,75.00,INV-300,Sale\n",
        )
        wizard = self.Wizard.create({
            'journal_id': self.bank_journal.id,
            'profile_id': mismatch_profile.id,
            'format_key': 'csv',
            'file_data': base64.b64encode(content),
            'filename': 'mismatch.csv',
        })
        raised = False
        try:
            wizard.action_import()
        except UserError as exc:
            raised = True
            self.assertIn(other_code, str(exc))
        self.assertTrue(raised, "currency mismatch must raise UserError")
        log = self.Log.search(
            [('journal_id', '=', self.bank_journal.id),
             ('state', '=', 'error')],
            order='id desc', limit=1)
        self.assertTrue(log)
        self.assertIn(other_code, log.error_message or '')

    # ---- partial-overlap reimport: end balance stays consistent ----

    def test_partial_overlap_reimport_keeps_consistent_end_balance(self):
        # A file with an opening/closing balance is imported, then re-imported
        # as a partial overlap: two lines repeat (skipped as duplicates) and
        # one line is new. The parsed closing_balance describes the WHOLE
        # file, so stamping it onto the second statement (which retained only
        # the one new line) would produce an inconsistent end balance. The
        # wizard must instead keep balance_end_real consistent with the lines
        # actually imported.
        wizard = self._make_wizard(self._csv_bytes("x"))  # journal/profile only
        currency = self.journal_currency

        first_parsed = {
            'statement_date': False,
            'opening_balance': 100.00,
            'closing_balance': 160.00,  # 100 + 10 + 20 + 30
            'currency_code': currency.name,
            'lines': [
                {'date': '2026-07-01', 'amount': 10.00,
                 'payment_ref': 'P1', 'narration': 'one',
                 'unique_import_ref': 'OV-1'},
                {'date': '2026-07-02', 'amount': 20.00,
                 'payment_ref': 'P2', 'narration': 'two',
                 'unique_import_ref': 'OV-2'},
                {'date': '2026-07-03', 'amount': 30.00,
                 'payment_ref': 'P3', 'narration': 'three',
                 'unique_import_ref': 'OV-3'},
            ],
        }
        stmt1, count1, skipped1 = wizard._materialise(first_parsed)
        self.assertEqual(count1, 3)
        self.assertEqual(skipped1, 0)
        # No lines dropped: the file's closing balance is authoritative.
        self.assertAlmostEqual(stmt1.balance_end_real, 160.00, places=2)

        # Re-import: two existing refs (OV-2, OV-3) overlap, one new (OV-4).
        # The file still declares its own whole-file closing balance, which
        # is now wrong for a statement that keeps only the single new line.
        second_parsed = {
            'statement_date': False,
            'opening_balance': 100.00,
            'closing_balance': 205.00,  # whole-file figure, not for the retained line
            'currency_code': currency.name,
            'lines': [
                {'date': '2026-07-02', 'amount': 20.00,
                 'payment_ref': 'P2', 'narration': 'two',
                 'unique_import_ref': 'OV-2'},
                {'date': '2026-07-03', 'amount': 30.00,
                 'payment_ref': 'P3', 'narration': 'three',
                 'unique_import_ref': 'OV-3'},
                {'date': '2026-07-04', 'amount': 45.00,
                 'payment_ref': 'P4', 'narration': 'four',
                 'unique_import_ref': 'OV-4'},
            ],
        }
        stmt2, count2, skipped2 = wizard._materialise(second_parsed)
        self.assertEqual(count2, 1)
        self.assertEqual(skipped2, 2)
        self.assertEqual(len(stmt2.line_ids), 1)

        # The end balance must match the retained line, not the whole-file
        # closing balance: opening (100) + the single new line (45) = 145,
        # never the parsed 205.
        retained_total = sum(stmt2.line_ids.mapped('amount'))
        self.assertAlmostEqual(retained_total, 45.00, places=2)
        self.assertAlmostEqual(stmt2.balance_end_real, 145.00, places=2)
        self.assertNotAlmostEqual(stmt2.balance_end_real, 205.00, places=2)
        # Statement is internally consistent: start + retained == end.
        self.assertAlmostEqual(
            stmt2.balance_start + retained_total,
            stmt2.balance_end_real, places=2)

    # ---- all-duplicate reimport: no empty (illegal) statement ----

    def test_reimport_all_duplicate_lines_creates_no_empty_statement(self):
        content = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-05-01,10.00,A1,first\n"
            "2026-05-02,20.00,A2,second\n"
            "2026-05-03,30.00,A3,third\n",
        )
        self._make_wizard(content).action_import()
        base_count = self.Statement.search_count(
            [('journal_id', '=', self.bank_journal.id)])

        # Same three rows, reordered: different file bytes (new hash) but
        # each line's unique import reference is unchanged, so all three are
        # skipped as duplicates and nothing should be created.
        reordered = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-05-03,30.00,A3,third\n"
            "2026-05-01,10.00,A1,first\n"
            "2026-05-02,20.00,A2,second\n",
        )
        action = self._make_wizard(reordered).action_import()

        # No crash, no act_window: a friendly notification instead.
        self.assertEqual(action.get('tag'), 'display_notification')
        # No new (and no empty/illegal) statement was created.
        self.assertEqual(
            self.Statement.search_count(
                [('journal_id', '=', self.bank_journal.id)]),
            base_count)
        # The outcome is logged as a duplicate with zero lines.
        log = self.Log.search(
            [('journal_id', '=', self.bank_journal.id),
             ('state', '=', 'duplicate')], order='id desc', limit=1)
        self.assertTrue(log)
        self.assertEqual(log.line_count, 0)
        self.assertEqual(log.skipped_count, 3)
