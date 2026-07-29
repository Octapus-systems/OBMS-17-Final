# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
Anomaly detector tests (deterministic + LLM-fallback safety).
"""

import datetime
from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_ai_agent.tools.anomaly_detector import (
    detect, JournalEntrySummary,
)


def _make_entries():
    """Five entries: median is around 175."""
    return [
        JournalEntrySummary(
            id=1, date=datetime.date(2026, 6, 1),
            amount=100.0, posted_by='alice',
            account_codes=('5100',),
        ),
        JournalEntrySummary(
            id=2, date=datetime.date(2026, 6, 2),
            amount=200.0, posted_by='alice',
        ),
        JournalEntrySummary(
            id=3, date=datetime.date(2026, 6, 7),  # Sunday
            amount=150.0, posted_by='bob',
        ),
        JournalEntrySummary(
            id=4, date=datetime.date(2026, 6, 4),
            amount=10000.0, posted_by='alice',  # round outlier
        ),
        JournalEntrySummary(
            id=5, date=datetime.date(2026, 6, 5),
            amount=4900.0, posted_by='bob',  # just under 5000
        ),
    ]


@tagged('post_install', '-at_install')
class AnomalyDetectorTest(TransactionCase):

    def test_round_outlier_flagged(self):
        findings = detect(_make_entries(), approval_threshold=0)
        rules = {f.rule for f in findings}
        self.assertIn('round_outlier', rules)

    def test_weekend_post_flagged(self):
        findings = detect(_make_entries(), approval_threshold=0)
        weekend = [f for f in findings if f.rule == 'weekend_post']
        self.assertGreater(len(weekend), 0)

    def test_just_under_threshold_flagged(self):
        findings = detect(_make_entries(), approval_threshold=5000.0)
        rules = {f.rule for f in findings}
        self.assertIn('just_under_threshold', rules)

    def test_threshold_zero_disables_rule(self):
        findings = detect(_make_entries(), approval_threshold=0)
        rules = {f.rule for f in findings}
        self.assertNotIn('just_under_threshold', rules)

    def test_reversal_pair_flagged(self):
        # Two entries by the same user on the same accounts on the
        # same day with the same amount.
        same_day = datetime.date(2026, 6, 10)
        entries = [
            JournalEntrySummary(
                id=10, date=same_day, amount=500.0,
                posted_by='alice', account_codes=('1100', '5200'),
            ),
            JournalEntrySummary(
                id=11, date=same_day, amount=500.0,
                posted_by='alice', account_codes=('5200', '1100'),
            ),
            JournalEntrySummary(
                id=12, date=same_day, amount=300.0,
                posted_by='bob',
            ),
        ]
        findings = detect(entries, approval_threshold=0)
        reversal = [f for f in findings if f.rule == 'reversal_pair']
        self.assertEqual(len(reversal), 2)

    def test_severity_assigned(self):
        findings = detect(_make_entries(), approval_threshold=5000.0)
        for f in findings:
            self.assertIn(f.severity, ('low', 'medium', 'high'))

    def test_empty_input_returns_empty(self):
        self.assertEqual(detect([]), [])

    def test_llm_provider_failure_safe(self):
        """A failing LLM provider must not break the deterministic path."""
        findings_manual = detect(_make_entries(),
                                 approval_threshold=5000.0)
        findings_with_stub = detect(_make_entries(),
                                    approval_threshold=5000.0,
                                    provider_key='claude',
                                    provider_config={'api_key': 'x',
                                                     'model': 'y'})
        # The Claude stub raises ProviderError; the deterministic
        # findings should still flow through identically.
        self.assertEqual(len(findings_manual), len(findings_with_stub))
