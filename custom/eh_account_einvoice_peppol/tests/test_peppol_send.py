# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Integration tests for the outbound Peppol transmission flow.

Exercise action_eh_send_peppol against the manual default (queued), a
registered mock adapter (delivered with a transmission id), and an
adapter that fails (error recorded + UserError). No network.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_einvoice_peppol.tools import access_point_registry


class _MockAccessPoint:
    def __init__(self, config):
        self.config = config

    def submit(self, xml_bytes, recipient_endpoint_id,
               recipient_endpoint_scheme):
        return {'transmission_id': 'TX-1', 'status': 'delivered',
                'raw_response': 'OK'}

    def poll(self, since_iso):
        return []


class _ErrorAccessPoint:
    def __init__(self, config):
        self.config = config

    def submit(self, xml_bytes, recipient_endpoint_id,
               recipient_endpoint_scheme):
        raise access_point_registry.AccessPointError("gateway down")

    def poll(self, since_iso):
        return []


access_point_registry.register_adapter(
    'eh_test_mock_ap', lambda config: _MockAccessPoint(config))
access_point_registry.register_adapter(
    'eh_test_err_ap', lambda config: _ErrorAccessPoint(config))


@tagged('post_install', '-at_install')
class PeppolSendTest(EhAccountIntegrationTestCase):

    def setUp(self):
        super().setUp()
        # The void-for-re-cut action is manager-gated; run the suite as an EH
        # Accounting Manager so the sanctioned void/re-send path is exercised.
        # The dedicated non-manager test uses with_user(a plain user).
        self.env.user.groups_id |= self.env.ref(
            'eh_account_base.group_eh_manager')
        au = self.env.ref('base.au')
        self.env.company.write({
            'country_id': au.id, 'vat': '83914571673',
            'street': '1 Seller Street', 'city': 'Sydney', 'zip': '2000',
        })
        self.env.company.partner_id.write({
            'eh_peppol_endpoint_scheme': '0151',
            'eh_peppol_endpoint_id': '83914571673',
        })
        self.partner = self.env['res.partner'].create({
            'name': 'Buyer Co', 'is_company': True, 'country_id': au.id,
            'vat': '53004085616',
            'eh_peppol_endpoint_scheme': '0151',
            'eh_peppol_endpoint_id': '53004085616',
            'street': '2 Buyer Street', 'city': 'Sydney', 'zip': '2000',
        })
        if not self.env['account.journal'].search(
            [('type', '=', 'sale'),
             ('company_id', '=', self.env.company.id)], limit=1):
            self.env['account.journal'].create({
                'name': 'Sales', 'code': 'INV', 'type': 'sale',
                'company_id': self.env.company.id,
            })

    def _post_invoice(self):
        product = self.env['product.product'].create({
            'name': 'Test Service', 'type': 'service',
        })
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': self.env.company.currency_id.id,
            'invoice_date': '2026-05-01',
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id, 'name': 'Consulting',
                'quantity': 10.0, 'price_unit': 100.0,
                'account_id': self.account_revenue.id,
            })],
        })
        move.action_post()
        return move

    def test_send_manual_is_queued(self):
        self.env.company.eh_peppol_access_point_key = 'manual'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        self.assertEqual(move.eh_peppol_ap_key, 'manual')
        self.assertTrue(move.eh_peppol_sent_at)

    def test_send_via_adapter_records_delivery(self):
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        self.env.company.eh_peppol_ap_config = '{"api_key": "k"}'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')
        self.assertEqual(move.eh_peppol_transmission_id, 'TX-1')
        self.assertEqual(move.eh_peppol_ap_key, 'eh_test_mock_ap')

    def test_send_adapter_error_records_and_notifies(self):
        self.env.company.eh_peppol_access_point_key = 'eh_test_err_ap'
        move = self._post_invoice()
        # No raise: the error is recorded on the move and returned as a
        # danger notification so the status survives the transaction.
        result = move.action_eh_send_peppol()
        self.assertEqual(result.get('tag'), 'display_notification')
        self.assertEqual(result['params']['type'], 'danger')
        self.assertEqual(move.eh_peppol_transmission_status, 'error')
        self.assertIn('gateway down',
                      move.eh_peppol_transmission_message or '')

    def test_send_refuses_retransmission(self):
        """A second send of an already-transmitted invoice is refused, so a
        double click / retry cannot emit the legal e-invoice twice.
        """
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        self.env.company.eh_peppol_ap_config = '{"api_key": "k"}'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')
        with self.assertRaises(UserError):
            move.action_eh_send_peppol()
        # The recorded transmission is unchanged after the refused re-send.
        self.assertEqual(move.eh_peppol_transmission_id, 'TX-1')

    def test_send_manual_queued_is_not_resendable(self):
        """The manual default reaches 'queued'; that too blocks a re-send."""
        self.env.company.eh_peppol_access_point_key = 'manual'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        with self.assertRaises(UserError):
            move.action_eh_send_peppol()

    def test_send_permitted_after_error(self):
        """An 'error' status is not a completed transmission, so a re-send is
        allowed once a working adapter is configured.
        """
        self.env.company.eh_peppol_access_point_key = 'eh_test_err_ap'
        move = self._post_invoice()
        move.action_eh_send_peppol()  # records 'error', no raise
        self.assertEqual(move.eh_peppol_transmission_status, 'error')
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        self.env.company.eh_peppol_ap_config = '{"api_key": "k"}'
        # Must NOT be blocked: re-send from error succeeds.
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')

    # ---- void / re-cut escape hatch (over-restriction refinement) ----

    def test_void_manual_queued_then_resend(self):
        """Legitimate re-cut path: the out-of-box 'manual' adapter parks the
        move at 'queued'; a manager voids that unconfirmed transmission and
        sends again once the out-of-band routing is corrected.
        """
        self.env.company.eh_peppol_access_point_key = 'manual'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        # Void resets the unconfirmed transmission back to a re-sendable state.
        move.action_eh_void_peppol_transmission()
        self.assertEqual(move.eh_peppol_transmission_status, 'not_sent')
        self.assertFalse(move.eh_peppol_transmission_id)
        self.assertFalse(move.eh_peppol_ap_key)
        # A real adapter is now configured and the corrected file is sent.
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        self.env.company.eh_peppol_ap_config = '{"api_key": "k"}'
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')
        self.assertEqual(move.eh_peppol_transmission_id, 'TX-1')

    def test_void_refuses_delivered_transmission(self):
        """The original protection stays closed: a confirmed 'delivered'
        transmission can NEVER be voided/re-cut, so the legal e-invoice cannot
        be emitted twice via the escape hatch.
        """
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        self.env.company.eh_peppol_ap_config = '{"api_key": "k"}'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')
        with self.assertRaises(UserError):
            move.action_eh_void_peppol_transmission()
        # The confirmed transmission is untouched by the refused void.
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')
        self.assertEqual(move.eh_peppol_transmission_id, 'TX-1')

    def test_void_refuses_not_sent(self):
        """A move that never left has nothing to void."""
        move = self._post_invoice()
        self.assertEqual(move.eh_peppol_transmission_status, 'not_sent')
        with self.assertRaises(UserError):
            move.action_eh_void_peppol_transmission()

    def test_non_manager_cannot_void(self):
        """The escape hatch is manager-gated: an ordinary EH accounting user
        (who can read/send moves) cannot reset a 'queued' transmission, so the
        re-send guard cannot be defeated by a non-manager.
        """
        try:
            eh_user = self.env['res.users'].create({
                'name': 'Peppol EH User',
                'login': 'eh_peppol_send_user',
                'company_id': self.env.company.id,
                'groups_id': [(6, 0, [
                    self.env.ref('eh_account_base.group_eh_user').id,
                ])],
            })
        except Exception:  # noqa: BLE001
            self.skipTest("Could not create a non-manager EH user.")
        self.env.company.eh_peppol_access_point_key = 'manual'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        with self.assertRaises(UserError):
            move.with_user(eh_user).action_eh_void_peppol_transmission()
        # The guard held: the transmission status is unchanged.
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
