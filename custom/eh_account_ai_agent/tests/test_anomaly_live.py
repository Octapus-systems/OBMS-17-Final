# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Live anomaly-wiring integration tests.

These prove the deterministic detector is actually reachable inside
Odoo: posted journal entries get scanned, findings are persisted
against the move, the scan is idempotent, the evidence is immutable,
and the scheduled cron surfaces an outlier unattended.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_ai_agent', 'integration', 'post_install', '-at_install')
class TestAnomalyLive(EhAccountIntegrationTestCase):

    WEEKDAY = '2026-04-15'   # Wednesday
    WEEKEND = '2026-01-03'   # Saturday

    def _entry(self, amount, date, debit_account=None, credit_account=None):
        debit_account = debit_account or self.account_expense
        credit_account = credit_account or self.account_revenue
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': date,
            'line_ids': [
                (0, 0, {'name': 'D', 'account_id': debit_account.id,
                        'debit': amount, 'credit': 0.0}),
                (0, 0, {'name': 'C', 'account_id': credit_account.id,
                        'debit': 0.0, 'credit': amount}),
            ],
        })
        move.action_post()
        return move

    def _findings(self, move, rule=None):
        findings = move.eh_anomaly_finding_ids
        if rule:
            findings = findings.filtered(lambda f: f.rule == rule)
        return findings

    # ---- rule coverage ----

    def test_round_outlier_flagged(self):
        base = (
            self._entry(100.0, self.WEEKDAY)
            + self._entry(200.0, self.WEEKDAY)
            + self._entry(300.0, self.WEEKDAY)
        )
        outlier = self._entry(60000.0, self.WEEKDAY)
        (base + outlier).eh_scan_anomalies()
        self.assertTrue(
            self._findings(outlier, 'round_outlier'),
            "round-number outlier must be flagged",
        )
        for m in base:
            self.assertFalse(
                self._findings(m, 'round_outlier'),
                "baseline entries must not be flagged as outliers",
            )

    def test_weekend_post_flagged(self):
        move = self._entry(125.0, self.WEEKEND)
        move.eh_scan_anomalies()
        self.assertTrue(self._findings(move, 'weekend_post'))

    def test_just_under_threshold_flagged(self):
        self.company.eh_ai_anomaly_approval_threshold = 10000.0
        move = self._entry(9700.0, self.WEEKDAY)
        move.eh_scan_anomalies()
        self.assertTrue(self._findings(move, 'just_under_threshold'))

    def test_threshold_disabled_by_default(self):
        # Default threshold 0 means the structuring rule never fires.
        self.company.eh_ai_anomaly_approval_threshold = 0.0
        move = self._entry(9700.0, self.WEEKDAY)
        move.eh_scan_anomalies()
        self.assertFalse(self._findings(move, 'just_under_threshold'))

    def test_reversal_pair_flagged(self):
        a = self._entry(500.0, self.WEEKDAY)
        b = self._entry(500.0, self.WEEKDAY)
        (a + b).eh_scan_anomalies()
        self.assertTrue(self._findings(a, 'reversal_pair'))
        self.assertTrue(self._findings(b, 'reversal_pair'))

    def test_draft_moves_not_scanned(self):
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': self.WEEKEND,
            'line_ids': [
                (0, 0, {'name': 'D', 'account_id': self.account_expense.id,
                        'debit': 125.0, 'credit': 0.0}),
                (0, 0, {'name': 'C', 'account_id': self.account_revenue.id,
                        'debit': 0.0, 'credit': 125.0}),
            ],
        })
        # left in draft on purpose
        move.eh_scan_anomalies()
        self.assertFalse(move.eh_anomaly_finding_ids)

    # ---- persistence behaviour ----

    def test_rescan_is_idempotent(self):
        move = self._entry(125.0, self.WEEKEND)
        move.eh_scan_anomalies()
        count = len(move.eh_anomaly_finding_ids)
        self.assertTrue(count)
        move.eh_scan_anomalies()
        move.invalidate_recordset(['eh_anomaly_finding_ids'])
        self.assertEqual(
            len(move.eh_anomaly_finding_ids), count,
            "re-scan must not duplicate findings",
        )

    def test_count_compute(self):
        move = self._entry(125.0, self.WEEKEND)
        move.eh_scan_anomalies()
        move.invalidate_recordset(['eh_anomaly_count', 'eh_anomaly_open_count'])
        self.assertEqual(move.eh_anomaly_count, 1)
        self.assertEqual(move.eh_anomaly_open_count, 1)
        move.eh_anomaly_finding_ids.action_dismiss()
        move.invalidate_recordset(['eh_anomaly_open_count'])
        self.assertEqual(move.eh_anomaly_open_count, 0)

    def test_evidence_immutable_triage_editable(self):
        move = self._entry(125.0, self.WEEKEND)
        move.eh_scan_anomalies()
        finding = move.eh_anomaly_finding_ids[:1]
        self.assertTrue(finding)
        with self.assertRaises(UserError):
            finding.description = "tampered"
        with self.assertRaises(UserError):
            finding.amount = 1.0
        # triage fields are editable
        finding.review_note = "looked into it"
        finding.action_mark_reviewed()
        self.assertEqual(finding.state, 'reviewed')
        self.assertEqual(finding.reviewed_by_id, self.env.user)

    # ---- cron ----

    def test_cron_surfaces_outlier(self):
        today = fields.Date.context_today(self.env['res.users'])
        # A few small baseline entries plus a very large round outlier,
        # all dated today so the default 30-day lookback catches them.
        self._entry(100.0, today)
        self._entry(200.0, today)
        self._entry(300.0, today)
        outlier = self._entry(9999000.0, today)
        self.env['account.move']._cron_scan_anomalies()
        outlier.invalidate_recordset(['eh_anomaly_finding_ids'])
        self.assertTrue(
            self._findings(outlier, 'round_outlier'),
            "scheduled scan must flag the outlier",
        )
