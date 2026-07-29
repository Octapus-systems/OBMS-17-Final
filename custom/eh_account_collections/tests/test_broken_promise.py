# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Broken-promise auto-escalation tests.
"""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_collections', 'integration', 'post_install', '-at_install')
class TestBrokenPromise(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Case = cls.env['eh.collections.case']
        cls.Action = cls.env['eh.collections.action']

    def _make_case(self, days_overdue=45, total=500.0):
        return self.Case.create({
            'partner_id': self.partner_a.id,
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
            'total_overdue_amount': total,
            'days_overdue_max': days_overdue,
            'priority': '0',
        })

    def _add_promise(self, case, days_offset, amount=200.0):
        promise_date = fields.Date.context_today(self.env['res.users']) + (
            timedelta(days=days_offset)
        )
        return self.Action.create({
            'case_id': case.id,
            'action_type': 'call',
            'summary': "Promise",
            'contact_made': True,
            'promise_amount': amount,
            'promise_date': promise_date,
        })

    def test_active_promise_not_broken(self):
        case = self._make_case()
        self._add_promise(case, days_offset=7)
        case.invalidate_recordset(['has_active_promise', 'broken_promise'])
        self.assertTrue(case.has_active_promise)
        self.assertFalse(case.broken_promise)

    def test_lapsed_promise_is_broken(self):
        case = self._make_case()
        self._add_promise(case, days_offset=-3)
        case.invalidate_recordset(['has_active_promise', 'broken_promise'])
        self.assertFalse(case.has_active_promise)
        self.assertTrue(case.broken_promise)

    def test_resolved_case_not_broken_even_if_promise_lapsed(self):
        case = self._make_case()
        self._add_promise(case, days_offset=-3)
        resolved_stage = self.env['eh.collections.stage'].search(
            [('is_resolved', '=', True)], limit=1,
        )
        case.stage_id = resolved_stage.id
        case.invalidate_recordset(['broken_promise'])
        self.assertFalse(case.broken_promise)

    def test_handle_broken_promise_bumps_priority(self):
        case = self._make_case()
        self.assertEqual(case.priority, '0')
        self._add_promise(case, days_offset=-2)
        case.invalidate_recordset(['broken_promise'])
        case.action_handle_broken_promise()
        self.assertEqual(case.priority, '1')
        self.assertTrue(case.last_broken_promise_handled_at)

    def test_handle_broken_promise_moves_to_escalated_stage(self):
        case = self._make_case()
        self._add_promise(case, days_offset=-2)
        case.invalidate_recordset(['broken_promise'])
        case.action_handle_broken_promise()
        escalated = self.env['eh.collections.stage'].search(
            [('is_escalated', '=', True)], limit=1,
        )
        self.assertTrue(escalated)
        self.assertEqual(case.stage_id, escalated)

    def test_handle_broken_promise_logs_action(self):
        case = self._make_case()
        self._add_promise(case, days_offset=-2)
        before = len(case.action_ids)
        case.invalidate_recordset(['broken_promise'])
        case.action_handle_broken_promise()
        self.assertGreater(len(case.action_ids), before)

    def test_cron_handles_only_unhandled_in_24h(self):
        case = self._make_case()
        self._add_promise(case, days_offset=-2)
        case.invalidate_recordset(['broken_promise'])
        # First cron pass: escalates.
        result = self.Case._cron_handle_broken_promises()
        self.assertEqual(result['escalated'], 1)
        priority_after_first = case.priority
        # Second cron pass within 24h: no new escalation.
        result = self.Case._cron_handle_broken_promises()
        self.assertEqual(result['escalated'], 0)
        self.assertEqual(case.priority, priority_after_first)

    def test_action_button_raises_when_no_broken_promise(self):
        case = self._make_case()
        # No promises at all.
        with self.assertRaises(UserError):
            case.action_handle_broken_promise()
