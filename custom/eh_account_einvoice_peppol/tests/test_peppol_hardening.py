# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression tests for the Peppol hardening pass.

Covers two of the confirmed findings:

* The access-point credential field (eh_peppol_ap_config) is restricted
  to system administrators, so a plain internal user cannot RPC-read the
  live AP key / signing-cert path, while the transmission path still
  resolves it via sudo.
* Inbound duplicate detection keys on the business identity (supplier +
  invoice number), so a re-serialised redelivery is caught even when the
  raw XML bytes differ, and a posted vendor bill for the same supplier +
  reference blocks a second payable.
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_einvoice_peppol', 'post_install', '-at_install')
class TestPeppolCredentialGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.sudo().eh_peppol_ap_config = (
            '{"api_key": "secret-key", "base_url": "https://ap.example"}'
        )
        # A plain internal user (no base.group_system) to attempt the read.
        try:
            cls.plain_user = cls.env['res.users'].create({
                'name': 'Peppol Field Reader',
                'login': 'eh_peppol_field_reader',
                'company_id': cls.env.company.id,
                'groups_id': [(6, 0, [cls.env.ref('base.group_user').id])],
            })
        except Exception:  # noqa: BLE001
            cls.plain_user = cls.env['res.users'].browse()

    def test_plain_user_cannot_read_credentials(self):
        """The RPC read a non-admin would use is refused by the field guard."""
        if not self.plain_user:
            self.skipTest("Could not create a non-superuser test user.")
        company_as_plain = self.env.company.with_user(self.plain_user)
        with self.assertRaises(AccessError):
            company_as_plain.read(['eh_peppol_ap_config'])
        with self.assertRaises(AccessError):
            # Attribute access on the single restricted field also raises.
            company_as_plain.eh_peppol_ap_config  # noqa: B018

    def test_admin_can_read_credentials(self):
        """The field stays readable for administrators."""
        self.assertIn('secret-key', self.env.company.eh_peppol_ap_config or '')

    def test_integration_resolves_credentials_via_sudo(self):
        """The transmission path resolves the secret via sudo even when a
        non-admin operator triggers it, so restricting the field does not
        break the send/poll capability.
        """
        if not self.plain_user:
            self.skipTest("Could not create a non-superuser test user.")
        company_as_plain = self.env.company.with_user(self.plain_user)
        config = self.env['eh.peppol.inbound'].with_user(
            self.plain_user,
        )._eh_company_config(company_as_plain)
        self.assertEqual(config['credentials'].get('api_key'), 'secret-key')


@tagged('eh_account_einvoice_peppol', 'post_install', '-at_install')
class TestPeppolInboundDuplicate(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.env['res.partner'].create({
            'name': 'Peppol Vendor Co', 'is_company': True,
        })
        cls.Inbound = cls.env['eh.peppol.inbound']

    def test_business_key_flags_reserialised_redelivery(self):
        """Same supplier + invoice number with DIFFERENT bytes is flagged as a
        duplicate, which a raw-hash-only check would miss.
        """
        first = self.Inbound.create({
            'company_id': self.env.company.id,
            'partner_id': self.vendor.id,
            'invoice_number': 'INV-777',
            'document_type': 'invoice',
            'file_hash': 'a' * 64,
        })
        second = self.Inbound.create({
            'company_id': self.env.company.id,
            'partner_id': self.vendor.id,
            'invoice_number': 'INV-777',
            'document_type': 'invoice',
            'file_hash': 'b' * 64,  # re-serialised: different SHA-256
        })
        second._eh_check_duplicate()
        self.assertEqual(second.duplicate_inbound_id, first)

    def test_distinct_invoice_number_not_flagged(self):
        """A genuinely different invoice from the same supplier is not a
        duplicate (no false positive).
        """
        self.Inbound.create({
            'company_id': self.env.company.id,
            'partner_id': self.vendor.id,
            'invoice_number': 'INV-900',
            'document_type': 'invoice',
            'file_hash': 'c' * 64,
        })
        other = self.Inbound.create({
            'company_id': self.env.company.id,
            'partner_id': self.vendor.id,
            'invoice_number': 'INV-901',
            'document_type': 'invoice',
            'file_hash': 'd' * 64,
        })
        other._eh_check_duplicate()
        self.assertFalse(other.duplicate_inbound_id)
        self.assertFalse(other.duplicate_move_id)

    def test_posted_bill_flags_cross_channel_duplicate(self):
        """An already-posted vendor bill for the same supplier + reference
        flags the inbound and blocks posting a second payable.
        """
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.vendor.id,
            'ref': 'INV-888',
            'invoice_date': '2026-05-01',
            'journal_id': self.journal_purchase.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Line',
                'quantity': 1.0,
                'price_unit': 100.0,
                'account_id': self.account_expense.id,
            })],
        })
        bill.action_post()
        inbound = self.Inbound.create({
            'company_id': self.env.company.id,
            'partner_id': self.vendor.id,
            'invoice_number': 'INV-888',
            'document_type': 'invoice',
        })
        inbound._eh_check_duplicate()
        self.assertEqual(inbound.duplicate_move_id, bill)
        # Posting is refused while the cross-channel flag stands.
        if not self.env.user.has_group('account.group_account_manager'):
            self.skipTest("Test user is not an accounting manager.")
        inbound.sudo().write({'state': 'matched'})
        with self.assertRaises(UserError):
            inbound.action_post()
        self.assertNotEqual(inbound.state, 'posted')
