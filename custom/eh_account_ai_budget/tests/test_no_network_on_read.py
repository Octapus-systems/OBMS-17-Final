# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Regression: the budget-variance commentary compute must never invoke the
AI provider, because a live provider performs a blocking outbound HTTPS
POST and a store=False compute fires on every form/list read. The
provider is allowed only on the explicit Refresh AI commentary button,
which writes the stored eh_ai_variance_narrative field.

The test registers a sentinel non-manual provider whose chat() records
that it was called (and returns a marker). Reading the compute field with
that provider configured on the company must NOT call chat(); pressing the
button MUST call it and store the marker.
"""

from datetime import date

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_ai_agent.tools import provider_registry


_CALLS = []
_SENTINEL_KEY = 'eh_test_sentinel_budget'
_MARKER = "SENTINEL-LLM-NARRATIVE"


class _SentinelProvider:
    """Non-manual provider that flags any chat() invocation.

    A chat() call from the read path is the exact defect under test:
    it means the compute reached the network layer.
    """

    is_manual = False

    def __init__(self, config):
        self.config = config or {}

    def chat(self, messages, **kwargs):
        _CALLS.append(messages)
        return _MARKER


@tagged('eh_account_ai_budget', 'post_install', '-at_install')
class TestNoNetworkOnRead(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        provider_registry.register_provider(_SENTINEL_KEY, _SentinelProvider)
        # Configure the company to use the live (non-manual) provider so
        # the pre-fix compute would have called chat() on read.
        cls.company.eh_ai_provider_key = _SENTINEL_KEY
        cls.company.eh_ai_provider_config = '{"api_key": "x", "model": "y"}'

    def setUp(self):
        super().setUp()
        _CALLS.clear()

    def _budget(self):
        today = fields.Date.context_today(self.env['res.users'])
        date_from = date(today.year, 1, 1)
        date_to = date(today.year, 12, 31)
        return self.env['eh.budget.budget'].create({
            'name': "Sentinel Budget",
            'code': 'SENTBUD',
            'company_id': self.company.id,
            'date_from': date_from,
            'date_to': date_to,
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'period_from': date_from, 'period_to': date_to,
                    'budgeted_amount': 10000.0,
                }),
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'period_from': date_from, 'period_to': date_to,
                    'budgeted_amount': 5000.0,
                }),
            ],
        })

    def test_read_does_not_call_provider(self):
        """Reading the computed field with a live provider configured
        must render the deterministic template with zero chat() calls."""
        budget = self._budget()
        budget.invalidate_recordset(['eh_ai_variance_commentary'])
        text = budget.eh_ai_variance_commentary
        self.assertTrue(text, "compute must still render deterministic text")
        self.assertEqual(
            _CALLS, [],
            "read path invoked the AI provider (network I/O in compute)",
        )
        self.assertNotIn(
            _MARKER, text,
            "compute field must not contain provider output",
        )

    def test_recompute_on_line_change_still_no_call(self):
        """Editing a line marks the field dirty; the next read recompute
        must still be deterministic-only (no provider call)."""
        budget = self._budget()
        budget.eh_ai_variance_commentary  # prime
        _CALLS.clear()
        budget.line_ids.filtered(
            lambda line: line.account_id == self.account_revenue
        ).budgeted_amount = 88888.0
        budget.invalidate_recordset(['eh_ai_variance_commentary'])
        text = budget.eh_ai_variance_commentary
        self.assertIn('88888', text)
        self.assertEqual(
            _CALLS, [],
            "recompute after a line edit invoked the AI provider",
        )

    def test_button_invokes_provider_and_stores_narrative(self):
        """The explicit button is the ONLY place the provider runs; it
        writes the enriched narrative to the stored field."""
        budget = self._budget()
        self.assertFalse(budget.eh_ai_variance_narrative)
        budget.action_eh_ai_refresh_commentary()
        self.assertEqual(
            len(_CALLS), 1,
            "button must invoke the configured provider exactly once",
        )
        self.assertEqual(budget.eh_ai_variance_narrative, _MARKER)
        # The stored narrative persists across reads without re-calling.
        _CALLS.clear()
        budget.invalidate_recordset(['eh_ai_variance_narrative'])
        self.assertEqual(budget.eh_ai_variance_narrative, _MARKER)
        self.assertEqual(
            _CALLS, [],
            "reading the stored narrative re-invoked the provider",
        )
