# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Tests for the webhook dispatcher on eh.report.schedule.

The HTTP POST itself is stubbed by replacing _dispatch_webhook with a
recorder so the tests run hermetically. Coverage:

* Payload shape per webhook_format (slack, teams, generic).
* HTML stripping in _strip_html collapses tags and trims length.
* Both-channel mode: an email failure does not block the webhook
  succeeding (and vice versa).
* Missing webhook_url raises UserError when channel='webhook'.
* Webhook-only channel does not require recipient emails.

The schedule needs a real eh.account.dynamic.report record because
_send_now calls _build_attachments which materialises the report; we
seed a minimal dynamic report so the call resolves without going
through the full handler stack.
"""

import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestWebhookPayload(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Schedule = cls.env['eh.report.schedule']

    def _make_schedule(self, **vals):
        # The report_id is a required FK; pick any existing dynamic
        # report. Seed one with a known handler if none is present.
        report = self.env['eh.account.dynamic.report'].search([], limit=1)
        if not report:
            report = self.env['eh.account.dynamic.report'].create({
                'name': 'Webhook Test Report',
                'handler_model':
                    'eh.account.dynamic.report.handler.trial_balance',
            })
        defaults = {
            'name': 'Webhook test',
            'report_id': report.id,
            'subject': 'Test Subject',
            'body': '<p>Hello <b>world</b></p>',
            'options_json': '{}',
            'delivery_format': 'pdf',
            'recipient_emails': 'noone@example.com',
        }
        defaults.update(vals)
        return self.Schedule.create(defaults)

    # ---- payload shapes ----

    def test_slack_payload_shape(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='slack',
            webhook_url='https://hooks.slack.com/services/T/B/X',
        )
        attachment_links = [
            {'name': 'tb.pdf',
             'url': 'https://example/web/content/1?download=true',
             'mimetype': 'application/pdf', 'size_bytes': 1024},
        ]
        payload = schedule._build_webhook_payload(
            subject='TB ready',
            body_text='Trial balance for April',
            attachment_links=attachment_links,
        )
        self.assertEqual(payload['text'], 'TB ready')
        self.assertEqual(len(payload['attachments']), 1)
        att = payload['attachments'][0]
        self.assertEqual(att['title'], 'tb.pdf')
        self.assertEqual(
            att['title_link'],
            'https://example/web/content/1?download=true',
        )
        self.assertEqual(att['footer'], 'ERP Heritage Accounting Suite')

    def test_slack_payload_falls_back_when_no_attachments(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='slack',
            webhook_url='https://hooks.slack.com/services/T/B/X',
        )
        payload = schedule._build_webhook_payload(
            subject='Empty', body_text='No attachments today',
            attachment_links=[],
        )
        # Slack still gets one attachment block with the body text
        # and footer.
        self.assertEqual(len(payload['attachments']), 1)
        self.assertIn('No attachments', payload['attachments'][0]['text'])

    def test_teams_payload_shape(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='teams',
            webhook_url='https://outlook.office.com/webhook/...',
        )
        attachment_links = [
            {'name': 'pl.pdf', 'url': 'https://example/p/1',
             'mimetype': 'application/pdf', 'size_bytes': 2048},
            {'name': 'bs.pdf', 'url': 'https://example/p/2',
             'mimetype': 'application/pdf', 'size_bytes': 1500},
        ]
        payload = schedule._build_webhook_payload(
            subject='Reports', body_text='Period close pack',
            attachment_links=attachment_links,
        )
        self.assertEqual(payload['@type'], 'MessageCard')
        self.assertEqual(payload['summary'], 'Reports')
        self.assertEqual(payload['themeColor'], '1A2C3D')
        # Teams sections: body section + facts section.
        self.assertEqual(len(payload['sections']), 2)
        facts = payload['sections'][1]['facts']
        self.assertEqual(facts[0]['name'], 'pl.pdf')
        self.assertEqual(facts[1]['value'], 'https://example/p/2')

    def test_generic_payload_shape(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='generic',
            webhook_url='https://example.com/hook',
        )
        attachment_links = [
            {'name': 'gl.pdf', 'url': 'https://example/p/3',
             'mimetype': 'application/pdf', 'size_bytes': 999},
        ]
        payload = schedule._build_webhook_payload(
            subject='GL', body_text='General ledger',
            attachment_links=attachment_links,
        )
        self.assertEqual(payload['subject'], 'GL')
        self.assertEqual(payload['body'], 'General ledger')
        self.assertEqual(payload['attachments'], attachment_links)
        self.assertEqual(payload['schedule'], schedule.name)

    # ---- HTML stripping ----

    def test_strip_html_removes_tags(self):
        text = self.Schedule._strip_html('<p>Hello <b>world</b></p>')
        self.assertEqual(text, 'Hello world')

    def test_strip_html_collapses_whitespace(self):
        text = self.Schedule._strip_html(
            '<p>One</p>\n\n  <p>Two</p>\n<br/>Three',
        )
        self.assertEqual(text, 'One Two Three')

    def test_strip_html_caps_length(self):
        long_html = '<p>' + ('x' * 5000) + '</p>'
        text = self.Schedule._strip_html(long_html)
        self.assertLessEqual(len(text), 1500)

    def test_strip_html_handles_empty(self):
        self.assertEqual(self.Schedule._strip_html(None), '')
        self.assertEqual(self.Schedule._strip_html(''), '')

    # ---- channel routing ----

    def test_webhook_only_does_not_require_emails(self):
        # Email channel raises when no recipients. Webhook channel
        # uses the URL as the destination, so it must not bounce on
        # missing recipient emails.
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='generic',
            webhook_url='https://example.com/hook',
            recipient_emails=False,
        )
        # Stub the dispatcher so we don't make a real HTTP call. We
        # also stub _build_attachments to avoid pulling the real
        # rendering pipeline through the test.
        with patch.object(
            type(schedule), '_build_attachments',
            return_value=[{'name': 'x.pdf', 'datas': b''}],
        ), patch.object(
            type(schedule), '_dispatch_webhook',
            return_value=None,
        ):
            schedule._send_now()
        self.assertEqual(schedule.last_run_status, 'success')

    def test_webhook_url_required_when_channel_includes_webhook(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='generic',
            webhook_url=False,
        )
        with patch.object(
            type(schedule), '_build_attachments',
            return_value=[{'name': 'x.pdf', 'datas': b''}],
        ):
            with self.assertRaises(UserError):
                schedule._send_now()

    # ---- SSRF guard on the server-issued POST ----

    def test_dispatch_webhook_blocks_internal_targets(self):
        # webhook_url is user-editable (group_eh_user) but the POST is issued
        # by the system cron. The guard must refuse internal/metadata targets
        # BEFORE any network call, so a plain user cannot pivot the server
        # into the cloud metadata service or an internal admin port.
        blocked = [
            'http://169.254.169.254/latest/meta-data/',   # cloud metadata
            'http://127.0.0.1:8069/web/session',           # loopback
            'http://10.0.0.5/internal',                    # RFC 1918
            'http://[::1]:8069/',                          # ipv6 loopback
            'file:///etc/passwd',                          # non-http scheme
            'http://metadata.google.internal/',            # metadata hostname
        ]
        for url in blocked:
            schedule = self._make_schedule(
                delivery_channel='webhook',
                webhook_format='generic',
                webhook_url=url,
            )
            with patch('urllib.request.urlopen') as urlopen:
                with self.assertRaises(UserError):
                    schedule._dispatch_webhook([], '<p>x</p>')
                # The guard fired before any network call was attempted.
                urlopen.assert_not_called()

    def test_both_channel_records_partial_success(self):
        # Email succeeds, webhook fails: schedule records success but
        # the error message captures the webhook failure for ops.
        schedule = self._make_schedule(
            delivery_channel='both',
            webhook_format='slack',
            webhook_url='https://hooks.slack.com/services/T/B/X',
            recipient_emails='ops@example.com',
        )
        with patch.object(
            type(schedule), '_build_attachments',
            return_value=[{'name': 'x.pdf', 'datas': b''}],
        ), patch.object(
            type(schedule), '_dispatch_email',
            return_value=None,
        ), patch.object(
            type(schedule), '_dispatch_webhook',
            side_effect=UserError("simulated webhook failure"),
        ):
            schedule._send_now()
        self.assertEqual(schedule.last_run_status, 'success')
        self.assertIn('simulated webhook failure', schedule.last_error or '')
