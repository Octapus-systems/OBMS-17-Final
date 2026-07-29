# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Regression guard: the collections next-action compute must never invoke
a live AI provider on the read path.

The three suggestion fields are non-stored and rendered on the case
form, so they recompute on every read. If the compute passed the live
company provider into next_action_suggester.suggest, a configured
non-manual provider would fire a blocking outbound HTTPS POST on every
form/list access and starve the HTTP workers. The compute must force
provider_key='manual'; the live provider belongs only to the explicit
action_eh_ai_refresh_suggestion button, which writes a stored narrative.
"""

from unittest.mock import patch

from odoo.tests import tagged

from odoo.addons.eh_account_ai_agent.tools import next_action_suggester
from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_ai_collections', 'post_install', '-at_install')
class TestNoNetworkCompute(EhAccountIntegrationTestCase):

    def _case(self, days=200, total=5000.0):
        return self.env['eh.collections.case'].create({
            'partner_id': self.partner_a.id,
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
            'days_overdue_max': days,
            'total_overdue_amount': total,
        })

    def _spy_suggest(self):
        """Wrap next_action_suggester.suggest to record the provider it
        is called with, always delegating with the manual provider so no
        real network provider can ever be reached from the test.
        """
        real = next_action_suggester.suggest
        calls = []

        def spy(case, provider_key='manual', provider_config=None):
            calls.append((provider_key, provider_config))
            return real(case, provider_key='manual')

        return calls, patch.object(
            next_action_suggester, 'suggest', side_effect=spy,
        )

    def test_compute_forces_manual_provider_on_read(self):
        # A live provider is configured on the company, exactly as the
        # UI instructs.
        self.company.eh_ai_provider_key = 'openai'
        self.company.sudo().eh_ai_provider_config = '{"api_key": "x"}'
        case = self._case()
        case.invalidate_recordset([
            'eh_ai_suggested_action',
            'eh_ai_suggested_priority',
            'eh_ai_suggestion_rationale',
        ])
        calls, cm = self._spy_suggest()
        with cm:
            # Reading the non-stored field triggers the compute.
            action = case.eh_ai_suggested_action
        self.assertTrue(calls, "compute did not call the suggester")
        # Every read-path call must force the manual provider and pass no
        # provider config, so no outbound network I/O can occur.
        for provider_key, provider_config in calls:
            self.assertEqual(
                provider_key, 'manual',
                "read-path compute passed a live provider (%s); it would "
                "fire a blocking HTTPS POST and starve workers"
                % provider_key,
            )
            self.assertIsNone(
                provider_config,
                "read-path compute leaked provider config to the suggester",
            )
        # The deterministic suggestion still renders.
        self.assertEqual(action, 'consider_write_off')

    def test_button_uses_live_provider_and_stores_narrative(self):
        self.company.eh_ai_provider_key = 'openai'
        self.company.sudo().eh_ai_provider_config = '{"api_key": "x"}'
        case = self._case()
        self.assertFalse(
            case.eh_ai_suggestion_narrative,
            "narrative must be empty until the button is pressed",
        )
        calls, cm = self._spy_suggest()
        with cm:
            case.action_eh_ai_refresh_suggestion()
        # The button, and only the button, hands the live provider to the
        # suggester.
        self.assertTrue(calls, "button did not call the suggester")
        self.assertEqual(calls[-1][0], 'openai')
        self.assertEqual(calls[-1][1], '{"api_key": "x"}')
        # The enriched result is written to the stored field.
        self.assertTrue(
            case.eh_ai_suggestion_narrative,
            "button did not store a narrative",
        )
        self.assertIn('consider_write_off', case.eh_ai_suggestion_narrative)
