# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for the LLM augmentation parsing.

These register fake in-process providers (no HTTP) that return canned
text, and assert that a real provider's output is genuinely parsed and
used: variance returns the prose, collections parses the JSON
suggestion, anomaly appends guarded findings. Malformed output must
fall back to the deterministic result.
"""

import datetime

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_ai_agent.tools import anomaly_detector
from odoo.addons.eh_account_ai_agent.tools import variance_commenter
from odoo.addons.eh_account_ai_agent.tools import next_action_suggester
from odoo.addons.eh_account_ai_agent.tools.provider_registry import (
    register_provider,
)
from odoo.addons.eh_account_ai_agent.tools.llm_parsing import (
    extract_json_object, extract_json_array,
)


def _register_canned(key, response):
    class _Canned:
        is_manual = False

        def __init__(self, config):
            self.config = config

        def chat(self, messages, **kwargs):
            return response

    register_provider(key, _Canned)


@tagged('post_install', '-at_install')
class LlmParsingTest(TransactionCase):

    def test_object_from_fenced(self):
        text = 'Sure!\n```json\n{"action": "x", "priority": "high"}\n```'
        self.assertEqual(
            extract_json_object(text),
            {'action': 'x', 'priority': 'high'},
        )

    def test_array_from_prose(self):
        text = 'Here you go: [{"move_id": 1}] done'
        self.assertEqual(extract_json_array(text), [{'move_id': 1}])

    def test_garbage_object_is_none(self):
        self.assertIsNone(extract_json_object('no json here'))

    def test_garbage_array_is_empty(self):
        self.assertEqual(extract_json_array('no json here'), [])


@tagged('post_install', '-at_install')
class VarianceAugmentTest(TransactionCase):

    def _snap(self):
        return [
            variance_commenter.BudgetLineSnapshot('Sales', 1000.0, 800.0,
                                                  is_income=True),
        ]

    def test_provider_prose_used(self):
        _register_canned('fake_var', 'ENRICHED EXECUTIVE NARRATIVE.')
        out = variance_commenter.comment(
            self._snap(), period_label='Q1',
            provider_key='fake_var', provider_config={'x': 1},
        )
        self.assertEqual(out, 'ENRICHED EXECUTIVE NARRATIVE.')


@tagged('post_install', '-at_install')
class CollectionsAugmentTest(TransactionCase):

    def _case(self):
        return next_action_suggester.CaseSnapshot(
            days_overdue=95, total_overdue=5000.0,
        )

    def test_json_suggestion_parsed(self):
        _register_canned(
            'fake_coll',
            '{"action": "call_now", "rationale": "spoke today", '
            '"priority": "high"}',
        )
        s = next_action_suggester.suggest(
            self._case(), provider_key='fake_coll',
            provider_config={'x': 1},
        )
        self.assertEqual(s.action, 'call_now')
        self.assertEqual(s.priority, 'high')
        self.assertEqual(s.rationale, 'spoke today')

    def test_malformed_falls_back(self):
        _register_canned('fake_coll_bad', 'I cannot help with that.')
        det = next_action_suggester.suggest(self._case())
        s = next_action_suggester.suggest(
            self._case(), provider_key='fake_coll_bad',
            provider_config={'x': 1},
        )
        self.assertEqual(s.action, det.action)

    def test_bad_priority_rejected(self):
        _register_canned(
            'fake_coll_badprio',
            '{"action": "x", "priority": "urgent"}',
        )
        det = next_action_suggester.suggest(self._case())
        s = next_action_suggester.suggest(
            self._case(), provider_key='fake_coll_badprio',
            provider_config={'x': 1},
        )
        self.assertEqual(s.action, det.action)


@tagged('post_install', '-at_install')
class AnomalyAugmentTest(TransactionCase):

    def _entries(self):
        d = datetime.date(2026, 4, 15)
        return [
            anomaly_detector.JournalEntrySummary(id=11, date=d, amount=100.0),
            anomaly_detector.JournalEntrySummary(id=12, date=d, amount=200.0),
            anomaly_detector.JournalEntrySummary(id=13, date=d, amount=300.0),
        ]

    def test_valid_finding_appended(self):
        _register_canned(
            'fake_anom',
            '[{"move_id": 12, "severity": "high", '
            '"description": "unusual vendor"}]',
        )
        findings = anomaly_detector.detect(
            self._entries(), provider_key='fake_anom',
            provider_config={'x': 1},
        )
        llm = [f for f in findings if f.rule == 'llm_flag']
        self.assertEqual(len(llm), 1)
        self.assertEqual(llm[0].move_id, 12)
        self.assertEqual(llm[0].severity, 'high')

    def test_hallucinated_move_id_ignored(self):
        _register_canned(
            'fake_anom_hallucinate',
            '[{"move_id": 9999, "severity": "high", "description": "x"}]',
        )
        findings = anomaly_detector.detect(
            self._entries(), provider_key='fake_anom_hallucinate',
            provider_config={'x': 1},
        )
        self.assertFalse([f for f in findings if f.rule == 'llm_flag'])

    def test_malformed_appends_nothing(self):
        _register_canned('fake_anom_bad', 'no findings to report')
        findings = anomaly_detector.detect(
            self._entries(), provider_key='fake_anom_bad',
            provider_config={'x': 1},
        )
        self.assertFalse([f for f in findings if f.rule == 'llm_flag'])
