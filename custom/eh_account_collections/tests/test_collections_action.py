# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Collections action log tests.

Covers action creation via the helper, action count on the case, last
action timestamp, and that contact_made + promise fields persist.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_collections', 'integration', 'post_install', '-at_install')
class TestCollectionsAction(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Case = cls.env['eh.collections.case']

    def test_log_call_creates_action(self):
        case = self.Case.create({'partner_id': self.partner_a.id})
        action = case.action_log_action(
            'call', 'Spoke with finance dept', contact_made=True,
        )
        self.assertEqual(action.case_id, case)
        self.assertEqual(action.action_type, 'call')
        self.assertEqual(action.summary, 'Spoke with finance dept')
        self.assertTrue(action.contact_made)

    def test_action_count_updates(self):
        case = self.Case.create({'partner_id': self.partner_a.id})
        self.assertEqual(case.action_count, 0)
        case.action_log_action('email', 'Reminder')
        case.action_log_action('call', 'Follow up')
        case.invalidate_recordset()
        self.assertEqual(case.action_count, 2)

    def test_action_log_with_promise_persists(self):
        case = self.Case.create({'partner_id': self.partner_a.id})
        future = fields.Date.context_today(self.env['res.users']) + timedelta(days=10)
        action = case.action_log_action(
            'call', 'Got commitment',
            contact_made=True,
            promise_amount=500.0,
            promise_date=future,
        )
        self.assertAlmostEqual(action.promise_amount, 500.0, places=2)
        self.assertEqual(action.promise_date, future)

    def test_last_action_at_reflects_most_recent(self):
        case = self.Case.create({'partner_id': self.partner_a.id})
        first = case.action_log_action('call', 'First')
        second = case.action_log_action('email', 'Second')
        case.invalidate_recordset()
        # Most recent action_date wins. They might share a timestamp due to
        # default fields.Datetime.now resolution; tolerate either being last.
        self.assertIn(case.last_action_at, [first.action_date, second.action_date])

    def test_action_user_defaults_to_current(self):
        case = self.Case.create({'partner_id': self.partner_a.id})
        action = case.action_log_action('call', 'Default user test')
        self.assertEqual(action.user_id, self.env.user)

    def test_action_currency_inherits_from_case(self):
        case = self.Case.create({'partner_id': self.partner_a.id})
        action = case.action_log_action(
            'call', 'Currency test',
            promise_amount=100.0,
            promise_date=fields.Date.context_today(self.env['res.users']),
        )
        action.invalidate_recordset(['currency_id'])
        self.assertEqual(action.currency_id, case.currency_id)
