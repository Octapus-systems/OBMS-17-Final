# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.recurring.invoice.line: line item on a recurring invoice template.

Each line carries enough information to materialise an
account.move.line on a generated customer invoice: optional product,
description, income account, quantity, unit price, and taxes. When a
product is set its taxes and account default through; the explicit
fields override.
"""

from odoo import api, fields, models


class EhRecurringInvoiceLine(models.Model):
    _name = 'eh.recurring.invoice.line'
    _description = "Recurring invoice template line"
    _order = 'sequence, id'

    template_id = fields.Many2one(
        'eh.recurring.invoice.template',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(default=10)

    product_id = fields.Many2one(
        'product.product',
        help="Optional. When set, defaults price, taxes, and account.",
    )
    name = fields.Char(required=True)
    account_id = fields.Many2one(
        'account.account',
        help=(
            "Income account for this line. When blank, the product's "
            "income account or fallback is used at generation time."
        ),
    )

    quantity = fields.Float(required=True, default=1.0)
    price_unit = fields.Float(required=True, default=0.0)

    tax_ids = fields.Many2many(
        'account.tax',
        'eh_recurring_line_tax_rel',
        'line_id', 'tax_id',
        string="Taxes",
        domain="[('type_tax_use', '=', 'sale')]",
    )

    currency_id = fields.Many2one(
        related='template_id.currency_id',
        store=False,
    )
    subtotal = fields.Monetary(
        compute='_compute_subtotal',
        currency_field='currency_id',
        store=False,
    )

    @api.onchange('product_id')
    def _onchange_product_id(self):
        if not self.product_id:
            return
        prod = self.product_id
        if not self.name:
            self.name = prod.name
        if not self.price_unit:
            self.price_unit = prod.list_price
        if not self.account_id and prod.property_account_income_id:
            self.account_id = prod.property_account_income_id
        if not self.tax_ids and prod.taxes_id:
            self.tax_ids = [(6, 0, prod.taxes_id.ids)]

    @api.depends('quantity', 'price_unit')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = (line.quantity or 0.0) * (line.price_unit or 0.0)
