# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Batch auto-reconciliation: high-confidence matches applied, ambiguous
and low-confidence lines left for the operator."""

from odoo.tests import tagged

from odoo.addons.eh_account_reconcile_pro.tests.common import (
    EhReconcileIntegrationTestCase,
)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestAutoReconcile(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A minimal test chart can leave the bank journal's suspense
        # account non-reconcilable, which makes _perform_reconciliation a
        # silent no-op. Pin a reconcilable suspense so a confirmed match
        # actually clears the line.
        suspense = cls.bank_journal.suspense_account_id
        if not suspense:
            suspense = cls._ensure_account(
                cls.env, '1990', 'Bank Suspense', 'asset_current')
            cls.bank_journal.suspense_account_id = suspense
        suspense.reconcile = True

    def test_auto_reconcile_high_confidence_match(self):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        aml = self.make_open_invoice_line(
            self.partner_a, 500.0, ref='INV500')
        line = self.make_statement_line(
            500.0, partner=self.partner_a, payment_ref='INV500',
            journal=self.bank_journal)

        summary = session.auto_reconcile()

        self.assertEqual(summary['reconciled'], 1)
        self.assertEqual(summary['by_score'], 1)
        self.assertIn(line.id, summary['matched_line_ids'])
        line.invalidate_recordset()
        aml.invalidate_recordset()
        self.assertTrue(aml.reconciled)
        self.assertTrue(line.is_reconciled)

    def test_auto_reconcile_skips_low_confidence(self):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        # Open invoice that does not match the statement on amount or ref.
        self.make_open_invoice_line(self.partner_a, 500.0, ref='INVX')
        line = self.make_statement_line(
            999.0, partner=self.partner_a, payment_ref='NOMATCH',
            journal=self.bank_journal)

        summary = session.auto_reconcile()

        self.assertEqual(summary['reconciled'], 0)
        self.assertEqual(summary['skipped_low_score'], 1)
        self.assertFalse(line.is_reconciled)

    def test_auto_reconcile_no_candidate(self):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(
            123.0, partner=self.partner_b, payment_ref='ORPHAN',
            journal=self.bank_journal)

        summary = session.auto_reconcile()

        self.assertEqual(summary['reconciled'], 0)
        self.assertEqual(summary['skipped_no_candidate'], 1)
        self.assertFalse(line.is_reconciled)
