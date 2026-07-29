# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
Next-action suggester tests.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_ai_agent.tools.next_action_suggester import (
    suggest, CaseSnapshot,
)


@tagged('post_install', '-at_install')
class NextActionSuggesterTest(TransactionCase):

    def test_broken_promise_escalates_first(self):
        case = CaseSnapshot(
            days_overdue=200, total_overdue=10000,
            has_broken_promise=True, has_demand_letter=True,
        )
        s = suggest(case)
        self.assertEqual(s.action, 'escalate_to_manager')
        self.assertEqual(s.priority, 'high')

    def test_active_promise_monitors(self):
        case = CaseSnapshot(
            days_overdue=45, total_overdue=2000,
            has_active_promise=True,
        )
        s = suggest(case)
        self.assertEqual(s.action, 'monitor')

    def test_180_days_consider_write_off(self):
        case = CaseSnapshot(
            days_overdue=185, total_overdue=5000,
        )
        s = suggest(case)
        self.assertEqual(s.action, 'consider_write_off')

    def test_120_days_with_demand_referral(self):
        case = CaseSnapshot(
            days_overdue=125, total_overdue=5000,
            has_demand_letter=True,
        )
        s = suggest(case)
        self.assertEqual(s.action, 'refer_to_collections_agency')

    def test_90_days_no_plan_send_letter(self):
        case = CaseSnapshot(
            days_overdue=95, total_overdue=5000,
        )
        s = suggest(case)
        self.assertEqual(s.action, 'send_demand_letter')

    def test_45_days_with_contact_phone_call(self):
        case = CaseSnapshot(
            days_overdue=50, total_overdue=2000,
            contact_count=2,
        )
        s = suggest(case)
        self.assertEqual(s.action, 'phone_call')

    def test_30_days_no_contact_first_email(self):
        case = CaseSnapshot(
            days_overdue=35, total_overdue=1000,
            contact_count=0,
        )
        s = suggest(case)
        self.assertEqual(s.action, 'first_contact_email')

    def test_under_30_days_manual_review(self):
        case = CaseSnapshot(
            days_overdue=10, total_overdue=500,
        )
        s = suggest(case)
        self.assertEqual(s.action, 'manual_review')

    def test_llm_provider_failure_safe(self):
        case = CaseSnapshot(days_overdue=95, total_overdue=5000)
        s_manual = suggest(case)
        s_stub = suggest(
            case,
            provider_key='claude',
            provider_config={'api_key': 'x', 'model': 'y'},
        )
        # Stub raises; deterministic suggestion stays.
        self.assertEqual(s_manual.action, s_stub.action)
