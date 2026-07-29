# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Release-to-pay governance: purchase-method bill control + manual hold."""

from odoo.tests import tagged

from odoo.addons.eh_account_ap_automation.tests.common import EhApTestCase


@tagged('eh_account_ap_automation', 'integration', 'post_install',
        '-at_install')
class TestReleaseToPay(EhApTestCase):

    def _bill_from_po(self, po):
        po.action_create_invoice()
        bill = po.invoice_ids[:1]
        bill.invoice_date = '2026-03-15'
        return bill

    def test_draft_is_not_ready(self):
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': self.partner_a.id})
        self.assertEqual(bill.eh_release_to_pay, 'not_ready')

    def test_received_method_within_receipt_is_released(self):
        self.product_a.purchase_method = 'receive'
        po = self._create_purchase_order([(self.product_a, 10, 100.0)])
        self._confirm_receipt(po)  # full 10 received
        bill = self._bill_from_po(po)
        bill.action_post()
        self.assertEqual(bill.eh_release_to_pay, 'released')

    def test_received_method_overbill_is_exception(self):
        self.product_a.purchase_method = 'receive'
        po = self._create_purchase_order([(self.product_a, 10, 100.0)])
        self._confirm_receipt(po, qty_overrides={po.order_line.id: 4})
        bill = self._bill_from_po(po)
        # Bill the full ordered 10 though only 4 were received.
        bill.invoice_line_ids.filtered(
            lambda l: l.purchase_line_id).quantity = 10
        bill.action_post()
        self.assertEqual(bill.eh_release_to_pay, 'exception')

    def test_ordered_method_bills_without_receipt(self):
        self.product_a.purchase_method = 'purchase'
        po = self._create_purchase_order([(self.product_a, 10, 100.0)])
        # No receipt at all; ordered-control allows billing the order.
        bill = self._bill_from_po(po)
        bill.action_post()
        self.assertEqual(bill.eh_release_to_pay, 'released')

    def test_manual_hold_and_release(self):
        self.product_a.purchase_method = 'purchase'
        po = self._create_purchase_order([(self.product_a, 5, 50.0)])
        bill = self._bill_from_po(po)
        bill.action_post()
        self.assertEqual(bill.eh_release_to_pay, 'released')

        bill.action_eh_hold_payment()
        self.assertEqual(bill.eh_release_to_pay, 'hold')

        bill.action_eh_release_payment()
        self.assertEqual(bill.eh_release_to_pay, 'released')
