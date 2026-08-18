# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Release-to-pay governance on vendor bills.

A native, stored status that tells AP whether a posted vendor bill is
cleared for payment. It folds two controls:

* A manual payment hold (eh_payment_hold) with a reason, so a controller
  can park a bill regardless of matching.
* A purchase bill-control check that honours each product's purchase
  method: an 'ordered' product may be billed up to the ordered quantity,
  a 'received' product only up to what has actually been received.
  Billing beyond that allowance flags an exception.

The status is queryable for the journal dashboard (count of bills awaiting
release, on hold, or in exception).
"""

from odoo import _, api, fields, models  # noqa: F401


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_payment_hold = fields.Boolean(
        string="Payment Hold", copy=False, tracking=True,
        help="When set, the bill is parked and never reports as released "
             "to pay, whatever the matching outcome.",
    )
    eh_hold_reason = fields.Char(string="Hold Reason", copy=False)
    eh_release_to_pay = fields.Selection(
        [
            ('not_ready', "Not Ready"),
            ('hold', "On Hold"),
            ('exception', "Billing Exception"),
            ('released', "Released to Pay"),
        ],
        string="Release to Pay", compute='_compute_eh_release_to_pay',
        store=True,
        help="Released only when the bill is posted, not on hold, and "
             "within the purchase bill-control allowance.",
    )

    @api.depends(
        'state', 'move_type', 'eh_payment_hold',
        'invoice_line_ids.purchase_line_id',
        'invoice_line_ids.purchase_line_id.qty_received',
        'invoice_line_ids.purchase_line_id.qty_invoiced',
        'invoice_line_ids.purchase_line_id.product_qty',
    )
    def _compute_eh_release_to_pay(self):
        for move in self:
            if move.move_type != 'in_invoice' or move.state != 'posted':
                move.eh_release_to_pay = 'not_ready'
            elif move.eh_payment_hold:
                move.eh_release_to_pay = 'hold'
            elif move._eh_has_billing_exception():
                move.eh_release_to_pay = 'exception'
            else:
                move.eh_release_to_pay = 'released'

    def _eh_has_billing_exception(self, tolerance=0.0):
        """True when any line is billed beyond its purchase-method
        allowance (ordered qty for 'purchase' products, received qty for
        'receive' products). Lines with no purchase link never flag."""
        self.ensure_one()
        for line in self.invoice_line_ids:
            po_line = line.purchase_line_id
            if not po_line:
                continue
            method = po_line.product_id.purchase_method
            if method == 'receive':
                allowance = po_line.qty_received
            else:
                allowance = po_line.product_qty
            if (po_line.qty_invoiced or 0.0) > allowance + tolerance:
                return True
        return False

    def action_eh_hold_payment(self):
        for move in self:
            move.eh_payment_hold = True

    def action_eh_release_payment(self):
        for move in self:
            move.eh_payment_hold = False
            move.eh_hold_reason = False
