# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Integration tests for the live collections next-action suggestion.

Each test builds a case in a specific state and asserts the
deterministic dunning ladder surfaces the right action and priority on
the case record. This proves the helper is genuinely wired live, not
just importable.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_ai_collections', 'integration', 'post_install',
        '-at_install')
class TestCaseSuggestion(EhAccountIntegrationTestCase):

    def _today(self):
        return fields.Date.context_today(self.env['res.users'])

    def _case(self, days=0, total=0.0, partner=None):
        return self.env['eh.collections.case'].create({
            'partner_id': (partner or self.partner_a).id,
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
            'days_overdue_max': days,
            'total_overdue_amount': total,
        })

    def test_active_promise_suppresses_escalation(self):
        case = self._case(days=200, total=5000.0)
        case.action_log_action(
            'call', "Promised to pay", contact_made=True,
            promise_amount=500.0, promise_date=self._today() + timedelta(days=30),
        )
        self.assertTrue(case.has_active_promise)
        self.assertEqual(case.eh_ai_suggested_action, 'monitor')
        self.assertEqual(case.eh_ai_suggested_priority, 'low')

    def test_broken_promise_escalates(self):
        case = self._case(days=90, total=3000.0)
        case.action_log_action(
            'call', "Promise lapsed",
            promise_amount=500.0, promise_date=self._today() - timedelta(days=10),
        )
        self.assertTrue(case.broken_promise)
        self.assertEqual(case.eh_ai_suggested_action, 'escalate_to_manager')
        self.assertEqual(case.eh_ai_suggested_priority, 'high')

    def test_consider_write_off_when_very_overdue(self):
        case = self._case(days=200, total=8000.0)
        self.assertEqual(case.eh_ai_suggested_action, 'consider_write_off')
        self.assertEqual(case.eh_ai_suggested_priority, 'high')

    def test_send_demand_letter_at_90(self):
        case = self._case(days=95, total=4000.0)
        self.assertEqual(case.eh_ai_suggested_action, 'send_demand_letter')

    def test_agency_referral_after_letter(self):
        case = self._case(days=130, total=4000.0)
        case.action_log_action(
            'letter', "Demand letter sent", contact_made=True,
        )
        self.assertEqual(
            case.eh_ai_suggested_action, 'refer_to_collections_agency',
        )

    def test_first_contact_email_when_no_contact(self):
        case = self._case(days=35, total=1000.0)
        self.assertEqual(case.eh_ai_suggested_action, 'first_contact_email')

    def test_phone_call_after_contact(self):
        case = self._case(days=50, total=1000.0)
        case.action_log_action('email', "First email", contact_made=True)
        self.assertEqual(case.eh_ai_suggested_action, 'phone_call')

    def test_suggestion_recomputes_on_state_change(self):
        case = self._case(days=35, total=1000.0)
        self.assertEqual(case.eh_ai_suggested_action, 'first_contact_email')
        # Logging a contact while still under 45 days drops it out of
        # every contact rule -> manual review. Non-stored field must
        # reflect this on the next read.
        case.action_log_action('email', "First email", contact_made=True)
        case.invalidate_recordset(['eh_ai_suggested_action'])
        self.assertEqual(case.eh_ai_suggested_action, 'manual_review')

    def test_demand_letter_detected_from_action_log(self):
        case = self._case(days=130, total=4000.0)
        snap = case._eh_build_case_snapshot()
        self.assertFalse(snap.has_demand_letter)
        case.action_log_action('letter', "Demand letter")
        snap = case._eh_build_case_snapshot()
        self.assertTrue(snap.has_demand_letter)
