# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Schedule tests.

Covers recipient resolution, advance_next_run for daily / weekly /
monthly cadences, _cron_run_due picks up due schedules, error isolation
when delivery fails, and the pause / resume actions.

Email delivery itself goes through Odoo's mail.mail and is not exercised
here; we assert that the mail record gets created with the right
attachments and recipients.
"""

import json
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestSchedule(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        DynRep = cls.env['eh.account.dynamic.report']
        cls.report = DynRep.search([('code', '=', 'trial_balance')], limit=1)
        if not cls.report:
            cls.report = DynRep.create({
                'code': 'trial_balance',
                'name': 'Trial Balance',
                'handler_model':
                    'eh.account.dynamic.report.handler.trial_balance',
            })
        cls.Schedule = cls.env['eh.report.schedule']
        # Seed at least one move so render does not return an empty payload.
        cls.post_balanced_move(
            [
                {'account': cls.account_revenue, 'credit': 1000.0},
                {'account': cls.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )

    def _make_schedule(self, **overrides):
        vals = {
            'name': 'Monthly TB',
            'report_id': self.report.id,
            'options_json': json.dumps({
                'date': {
                    'date_from': '2026-01-01',
                    'date_to': '2026-12-31',
                },
                'company_ids': [self.company.id],
                'posted_only': True,
                'show_zero': False,
            }),
            'interval': 1,
            'interval_unit': 'month',
            'next_run': fields.Datetime.now() - timedelta(minutes=1),
            'subject': 'Test report',
            'body': '<p>Body</p>',
            'recipient_emails': 'recipient@example.com',
            'delivery_format': 'xlsx',
        }
        vals.update(overrides)
        return self.Schedule.create(vals)

    # ---- recipient resolution ----

    def test_resolve_recipient_emails_combines_all_sources(self):
        partner = self.env['res.partner'].create({
            'name': 'Recipient One', 'email': 'one@example.com',
        })
        user = self.env['res.users'].create({
            'name': 'User Two', 'login': 'sched_user_two',
            'email': 'two@example.com',
        })
        schedule = self._make_schedule(
            recipient_emails='extra@example.com,duplicate@example.com',
            recipient_partner_ids=[(6, 0, [partner.id])],
            recipient_user_ids=[(6, 0, [user.id])],
        )
        emails = schedule._resolve_recipient_emails()
        self.assertIn('one@example.com', emails)
        self.assertIn('two@example.com', emails)
        self.assertIn('extra@example.com', emails)
        self.assertIn('duplicate@example.com', emails)
        self.assertEqual(len(emails), len(set(emails)))

    def test_resolve_skips_invalid_email_fragments(self):
        schedule = self._make_schedule(
            recipient_emails=' , not-an-email, real@example.com , ',
        )
        emails = schedule._resolve_recipient_emails()
        self.assertEqual(emails, ['real@example.com'])

    def test_send_now_raises_when_no_recipients(self):
        schedule = self._make_schedule(recipient_emails=False)
        with self.assertRaises(UserError):
            schedule._send_now()

    # ---- cadence ----

    def test_advance_next_run_monthly(self):
        schedule = self._make_schedule(
            interval=1, interval_unit='month',
            next_run=fields.Datetime.now() - timedelta(minutes=1),
        )
        before = schedule.next_run  # noqa: F841
        schedule.last_run = fields.Datetime.from_string('2026-06-15 12:00:00')
        schedule._advance_next_run()
        self.assertEqual(
            schedule.next_run,
            fields.Datetime.from_string('2026-07-15 12:00:00'),
        )

    def test_advance_next_run_weekly(self):
        schedule = self._make_schedule(
            interval=2, interval_unit='week',
            next_run=fields.Datetime.now(),
        )
        schedule.last_run = fields.Datetime.from_string('2026-06-15 12:00:00')
        schedule._advance_next_run()
        self.assertEqual(
            schedule.next_run,
            fields.Datetime.from_string('2026-06-29 12:00:00'),
        )

    def test_advance_next_run_daily(self):
        schedule = self._make_schedule(
            interval=3, interval_unit='day',
            next_run=fields.Datetime.now(),
        )
        schedule.last_run = fields.Datetime.from_string('2026-06-15 12:00:00')
        schedule._advance_next_run()
        self.assertEqual(
            schedule.next_run,
            fields.Datetime.from_string('2026-06-18 12:00:00'),
        )

    # ---- send / cron ----

    def test_send_now_creates_attachment_and_records_success(self):
        schedule = self._make_schedule()
        schedule._send_now()
        self.assertEqual(schedule.last_run_status, 'success')
        self.assertTrue(schedule.last_run)
        self.assertEqual(schedule.last_attachment_count, 1)
        self.assertFalse(schedule.last_error)

    def test_action_run_now_advances_next_run(self):
        schedule = self._make_schedule(
            interval=1, interval_unit='month',
        )
        before = schedule.next_run
        schedule.action_run_now()
        self.assertEqual(schedule.last_run_status, 'success')
        self.assertGreater(schedule.next_run, before)

    def test_cron_picks_up_due_schedule(self):
        schedule = self._make_schedule(
            next_run=fields.Datetime.now() - timedelta(hours=1),
        )
        self.Schedule._cron_run_due()
        schedule.invalidate_recordset()
        self.assertEqual(schedule.last_run_status, 'success')

    def test_cron_skips_inactive(self):
        schedule = self._make_schedule(
            next_run=fields.Datetime.now() - timedelta(hours=1),
            active=False,
        )
        self.Schedule._cron_run_due()
        schedule.invalidate_recordset()
        self.assertFalse(schedule.last_run_status)

    def test_cron_skips_future(self):
        schedule = self._make_schedule(
            next_run=fields.Datetime.now() + timedelta(hours=1),
        )
        self.Schedule._cron_run_due()
        schedule.invalidate_recordset()
        self.assertFalse(schedule.last_run_status)

    # ---- pause / resume ----

    def test_action_pause_and_resume(self):
        schedule = self._make_schedule()
        schedule.action_pause()
        self.assertFalse(schedule.active)
        schedule.action_resume()
        self.assertTrue(schedule.active)

    # ---- error isolation in cron ----

    def test_cron_isolates_failure(self):
        good = self._make_schedule(name='Good')
        bad = self._make_schedule(name='Bad', recipient_emails=False)
        self.Schedule._cron_run_due()
        good.invalidate_recordset()
        bad.invalidate_recordset()
        self.assertEqual(good.last_run_status, 'success')
        self.assertEqual(bad.last_run_status, 'error')
        self.assertIn('recipient', (bad.last_error or '').lower())
