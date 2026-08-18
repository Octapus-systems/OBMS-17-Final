# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Intake lifecycle tests: state transitions, parsing, sequence assignment.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhApTestCase


SAMPLE_BILL_TEXT = """
ACME Supplies
Invoice: INV-2026-00042
Date: 2026-04-30

WIDGET-001 10 12.50
GADGET-002 5 25.00

Subtotal: 250.00
Total: 250.00
"""


@tagged('eh_account_ap_automation', 'integration', 'post_install', '-at_install')
class TestIntakeLifecycle(EhApTestCase):

    def test_sequence_assigned_on_create(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        self.assertNotEqual(intake.name, '/')
        self.assertTrue(intake.name.startswith('APIN/'))

    def test_parse_extracts_header_and_lines(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': SAMPLE_BILL_TEXT,
        })
        intake.action_parse()
        self.assertEqual(intake.state, 'parsed')
        self.assertEqual(intake.vendor_reference, 'INV-2026-00042')
        self.assertAlmostEqual(intake.extracted_total, 250.0, places=2)
        self.assertEqual(len(intake.line_ids), 2)
        line_a = intake.line_ids.filtered(lambda line_item: line_item.product_code == 'WIDGET-001')
        self.assertTrue(line_a)
        self.assertEqual(line_a.invoice_qty, 10.0)
        self.assertEqual(line_a.invoice_price, 12.5)
        self.assertEqual(line_a.product_id, self.product_a)

    def test_parse_blocks_without_text(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        with self.assertRaises(UserError):
            intake.action_parse()

    def test_parse_does_not_clobber_existing_lines(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': SAMPLE_BILL_TEXT,
        })
        # Manually seed a line first.
        self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_code': 'MANUAL-1',
            'invoice_qty': 1.0,
            'invoice_price': 9.99,
        })
        intake.action_parse()
        # Should not have appended the parsed lines because manual lines exist.
        self.assertEqual(len(intake.line_ids), 1)
        self.assertEqual(intake.line_ids.product_code, 'MANUAL-1')

    def test_match_blocks_when_no_lines(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': 'Garbage with no parseable lines',
        })
        intake.action_parse()
        with self.assertRaises(UserError):
            intake.action_match()

    def test_post_blocks_in_received_state(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        with self.assertRaises(UserError):
            intake.action_post()

    def test_reject_intake(self):
        self.env.user.groups_id |= self.env.ref('account.group_account_manager')
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        intake.rejection_reason = 'Duplicate bill'
        intake.action_reject()
        self.assertEqual(intake.state, 'rejected')
        self.assertEqual(intake.rejection_reason, 'Duplicate bill')

    def test_parse_decimal_handles_commas(self):
        from odoo.addons.eh_account_ap_automation.models.ap_intake import (
            EhApIntake,
        )
        self.assertEqual(EhApIntake._parse_decimal('1,234.56'), 1234.56)
        self.assertEqual(EhApIntake._parse_decimal('1234,56'), 1234.56)
        self.assertEqual(EhApIntake._parse_decimal('1234.56'), 1234.56)

    def test_partner_specific_regex(self):
        self.partner_a.eh_ap_invoice_ref_regex = r'(?im)REF[:#\s]+([A-Z0-9-]+)'
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': 'Some text REF: CUSTOM-9999\n',
        })
        intake.action_parse()
        self.assertEqual(intake.vendor_reference, 'CUSTOM-9999')
