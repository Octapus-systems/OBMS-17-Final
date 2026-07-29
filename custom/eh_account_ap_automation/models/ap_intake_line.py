# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Vendor bill intake line.

Stores one extracted bill line plus the result of the 3 way match
against the matching purchase order line and the goods receipt
(stock moves) on that PO line.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhApIntakeLine(models.Model):
    _name = 'eh.ap.intake.line'
    _description = "Vendor Bill Intake Line"
    _inherit = ['eh.workflow.guard']
    _order = 'intake_id, sequence, id'

    # match_status is the per-line match outcome and a manager-gated field:
    # the matcher sets it (inside the intake's sudo action_match) and only a
    # manager may flip an exception line to 'overridden'/'rejected' through the
    # actions below. A plain user must not RPC-write match_status='overridden'
    # to push an out-of-tolerance line into the postable set and skip the
    # override gate, so it is guarded here.
    _eh_guarded_fields = ('match_status',)

    intake_id = fields.Many2one(
        'eh.ap.intake', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)

    product_code = fields.Char(
        help="Vendor or internal code as captured.",
    )
    product_id = fields.Many2one(
        'product.product', string="Product",
    )
    description = fields.Char(
        help="Optional free text description as captured.",
    )

    invoice_qty = fields.Float(
        digits='Product Unit', default=0.0,
    )
    invoice_price = fields.Float(
        digits='Product Price', default=0.0,
    )
    subtotal = fields.Float(
        digits='Account', default=0.0,
    )

    tax_ids = fields.Many2many(
        'account.tax',
        'eh_ap_intake_line_tax_rel',
        'line_id', 'tax_id',
        string="Taxes",
        domain="[('type_tax_use', '=', 'purchase')]",
        help=(
            "Taxes applied to this line on the posted vendor bill. "
            "Auto-populated from the matched PO line, or from the "
            "product's supplier taxes when no PO line is matched. "
            "Can be edited manually before posting."
        ),
    )

    purchase_order_line_id = fields.Many2one(
        'purchase.order.line', string="PO Line", readonly=True,
        ondelete='set null',
    )
    po_qty = fields.Float(readonly=True, digits='Product Unit')
    po_price = fields.Float(readonly=True, digits='Product Price')
    received_qty = fields.Float(readonly=True, digits='Product Unit')

    match_status = fields.Selection([
        ('pending', "Pending"),
        ('ok', "OK"),
        ('qty_exception', "Qty Exception"),
        ('price_exception', "Price Exception"),
        ('over_received', "Over Received"),
        ('no_match', "No Match"),
        ('overridden', "Overridden"),
        ('rejected', "Rejected"),
    ], default='pending')
    qty_diff = fields.Float(readonly=True, digits='Product Unit')
    price_diff = fields.Float(readonly=True, digits='Product Price')
    qty_diff_pct = fields.Float(readonly=True)
    price_diff_pct = fields.Float(readonly=True)
    match_notes = fields.Char(readonly=True)

    override_reason = fields.Text()
    overridden_at = fields.Datetime(readonly=True, copy=False)
    overridden_by_id = fields.Many2one('res.users', readonly=True, copy=False)

    company_id = fields.Many2one(
        related='intake_id.company_id', store=True, readonly=True,
    )

    @api.onchange('invoice_qty', 'invoice_price')
    def _onchange_compute_subtotal(self):
        for line in self:
            line.subtotal = line.invoice_qty * line.invoice_price

    # ---- match ----

    def _evaluate_match(self, profile):
        for line in self:
            line._reset_match_fields()
            po_line = line._find_po_line()
            if not po_line:
                line.match_status = 'no_match'
                line.match_notes = _("No matching PO line found.")
                # Even without a PO match, fall back to the product's
                # supplier taxes so the posted bill carries tax. A bill
                # with no taxes on a tax-aware company silently breaks
                # tax reporting; better to default and let the user
                # adjust than to drop the field on the floor.
                if line.product_id and not line.tax_ids:
                    line.tax_ids = line.product_id.supplier_taxes_id.filtered(
                        lambda t: t.company_id == line.intake_id.company_id
                    ).ids
                continue
            line.purchase_order_line_id = po_line.id
            # Compare prices in the bill currency. The PO line price is in
            # the PO's currency; convert it so a bill raised in a different
            # currency from the PO does not flag every line as a price
            # exception. po_price is the PO unit price in the bill currency.
            po_price = line._eh_po_price_in_bill_currency(po_line)
            line.po_qty = po_line.product_qty
            line.po_price = po_price
            # Inherit taxes from the matched PO line. Users can still
            # edit before posting, but the default is the PO's tax
            # treatment which is the auditable reference.
            if not line.tax_ids and po_line.taxes_id:
                line.tax_ids = po_line.taxes_id.ids
            received = line._compute_received_qty(po_line)
            line.received_qty = received
            # Bill only the quantity still un-invoiced against this PO line:
            # remaining billable = received goods less what earlier bills
            # already invoiced (po_line.qty_invoiced). Without this the same
            # goods receipt can be billed on every intake, so the same
            # delivery gets paid repeatedly. qty_invoiced is 0 for a first
            # bill, so single-bill behaviour is unchanged.
            billable = received - po_line.qty_invoiced

            qty_diff = line.invoice_qty - billable
            price_diff = line.invoice_price - po_price
            line.qty_diff = qty_diff
            line.price_diff = price_diff
            line.qty_diff_pct = (
                (qty_diff / billable * 100.0) if billable else 0.0
            )
            line.price_diff_pct = (
                (price_diff / po_price * 100.0) if po_price else 0.0
            )

            # Over receipt: invoiced qty exceeds PO qty beyond tolerance.
            over_receipt_limit = (
                max(po_line.product_qty - po_line.qty_invoiced, 0.0)
                * (1 + profile.over_receipt_pct / 100.0)
            )
            if line.invoice_qty > over_receipt_limit:
                line.match_status = 'over_received'
                line.match_notes = _(
                    "Invoice qty %(inv).4f exceeds PO qty %(po).4f beyond "
                    "tolerance %(pct).2f%%.",
                    inv=line.invoice_qty,
                    po=po_line.product_qty,
                    pct=profile.over_receipt_pct,
                )
                continue

            # Qty mismatch vs received.
            qty_tolerance = max(
                billable * profile.qty_tolerance_pct / 100.0,
                0.0,
            )
            if abs(qty_diff) > qty_tolerance:
                line.match_status = 'qty_exception'
                line.match_notes = _(
                    "Qty delta %(diff).4f exceeds tolerance %(tol).4f.",
                    diff=qty_diff, tol=qty_tolerance,
                )
                continue

            # Price mismatch vs PO (compared in the bill currency).
            price_tolerance = max(
                po_price * profile.price_tolerance_pct / 100.0,
                0.0,
            )
            line_amount_tolerance = profile.amount_tolerance
            if (
                abs(price_diff) > price_tolerance
                and abs(price_diff * line.invoice_qty) > line_amount_tolerance
            ):
                line.match_status = 'price_exception'
                line.match_notes = _(
                    "Price delta %(diff).4f exceeds tolerance "
                    "%(tol).4f and amount tolerance %(amt).2f.",
                    diff=price_diff, tol=price_tolerance,
                    amt=line_amount_tolerance,
                )
                continue

            line.match_status = 'ok'
            line.match_notes = _("Within tolerance.")

    def _eh_po_price_in_bill_currency(self, po_line):
        """Return the PO line unit price expressed in the intake (bill)
        currency.

        The PO line price is stated in the PO's currency. When the bill is
        raised in a different currency, convert at the bill date so the
        price comparison is apples to apples; otherwise every line would
        flag a price exception purely from the currency difference.
        """
        self.ensure_one()
        po_price = po_line.price_unit
        po_currency = po_line.currency_id or po_line.order_id.currency_id
        intake = self.intake_id
        bill_currency = intake.currency_id or intake.company_id.currency_id
        if po_currency and bill_currency and po_currency != bill_currency:
            conversion_date = (
                intake.bill_date or fields.Date.context_today(self)
            )
            po_price = po_currency._convert(
                po_price, bill_currency, intake.company_id, conversion_date,
            )
        return po_price

    def _reset_match_fields(self):
        for line in self:
            line.write({
                'purchase_order_line_id': False,
                'po_qty': 0.0,
                'po_price': 0.0,
                'received_qty': 0.0,
                'qty_diff': 0.0,
                'price_diff': 0.0,
                'qty_diff_pct': 0.0,
                'price_diff_pct': 0.0,
                'match_notes': False,
            })

    def _find_po_line(self):
        self.ensure_one()
        if not self.product_id:
            return self.env['purchase.order.line']
        domain = [
            ('product_id', '=', self.product_id.id),
            ('order_id.partner_id', '=', self.intake_id.partner_id.id),
            ('order_id.state', 'in', ['purchase', 'done']),
            ('order_id.company_id', '=', self.intake_id.company_id.id),
        ]
        if self.intake_id.purchase_order_id:
            domain.append(
                ('order_id', '=', self.intake_id.purchase_order_id.id),
            )
        candidates = self.env['purchase.order.line'].search(
            domain, order='create_date asc',
        )
        # Prefer a PO line that still has un-billed quantity so a fully
        # invoiced line is not reused to bill the same receipt again. Fall
        # back to the oldest line when every candidate is fully invoiced, so
        # the match still runs and flags the over-receipt / qty exception.
        open_lines = candidates.filtered(
            lambda pl: (pl.product_qty - pl.qty_invoiced) > 0,
        )
        return (open_lines[:1] or candidates[:1])

    @staticmethod
    def _compute_received_qty(po_line):
        moves = po_line.move_ids.filtered(
            lambda m: m.state == 'done'
            and m.location_dest_id.usage == 'internal',
        )
        return sum(moves.mapped('product_uom_qty'))

    # ---- override ----

    def action_override(self):
        self = self._eh_workflow_action()
        for line in self:
            if not self.env.user.has_group('account.group_account_manager'):
                raise UserError(_(
                    "Only accounting managers can override match exceptions.",
                ))
            if line.match_status not in (
                'qty_exception', 'price_exception',
                'over_received', 'no_match',
            ):
                raise UserError(_(
                    "Only exception lines can be overridden.",
                ))
            if not line.override_reason:
                raise UserError(_(
                    "Provide an override reason before approving.",
                ))
            line.write({
                'match_status': 'overridden',
                'overridden_at': fields.Datetime.now(),
                'overridden_by_id': self.env.user.id,
            })
            line.intake_id._compute_match_summary()
            if line.intake_id.exception_count == 0:
                # 'state' is guarded by eh.workflow.guard on the intake; this
                # sanctioned transition (all exceptions cleared) writes as su.
                line.intake_id._eh_workflow_write({'state': 'matched'})

    def action_reject_line(self):
        self = self._eh_workflow_action()
        for line in self:
            if not self.env.user.has_group('account.group_account_manager'):
                raise UserError(_(
                    "Only accounting managers can reject intake lines.",
                ))
            line.match_status = 'rejected'

    # ---- build move line ----

    def _build_invoice_line_vals(self):
        self.ensure_one()
        vals = {
            'name': (
                self.description
                or (self.product_id and self.product_id.display_name)
                or self.product_code
                or '/'
            ),
            'quantity': self.invoice_qty,
            'price_unit': self.invoice_price,
        }
        if self.product_id:
            vals['product_id'] = self.product_id.id
        if self.purchase_order_line_id:
            vals['purchase_line_id'] = self.purchase_order_line_id.id
        if self.tax_ids:
            vals['tax_ids'] = [(6, 0, self.tax_ids.ids)]
        return vals
