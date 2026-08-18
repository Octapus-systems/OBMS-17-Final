# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Mirror a confirmed inter-company purchase order to a draft sales
order in the vendor's company."""

from odoo import _, fields, models


class PurchaseOrder(models.Model):
    _name = 'purchase.order'
    _inherit = ['purchase.order', 'eh.ic.order.mixin']

    eh_ic_mirror_so_id = fields.Many2one(
        'sale.order', readonly=True, copy=False,
        string="Inter-company mirror SO",
    )
    eh_ic_origin_so_id = fields.Many2one(
        'sale.order', readonly=True, copy=False, index=True,
        string="Inter-company source SO",
        help="Set when this order was generated as the mirror of a "
             "sales order in another company.",
    )

    # One mirror sales-order-mirror PO per source sale order per company.
    # PostgreSQL treats NULLs as distinct, so ordinary purchase orders
    # (origin id NULL) are unaffected; this only binds the mirror rows and
    # closes the race where two concurrent confirms both pass the search-
    # before-create guard and create duplicate mirror purchase orders.
    _sql_constraints = [
        ('unique_ic_mirror_po', 'unique(eh_ic_origin_so_id, company_id)', "An inter-company mirror purchase order already exists for this "  # noqa: E501
        "source sales order in this company."),  # noqa: E128
    ]

    def button_confirm(self):
        res = super().button_confirm()
        for order in self:
            order._eh_ic_create_mirror_so()
        return res

    def _eh_ic_create_mirror_so(self):
        self.ensure_one()
        if self.eh_ic_origin_so_id or self.eh_ic_mirror_so_id:
            return
        dest = self._eh_ic_dest_company()
        if not dest:
            return
        config = self._eh_ic_config(dest)
        if not config:
            return
        # The mirror lives in the destination company; value it in that
        # company's currency at the order's date.
        src_currency = self.currency_id
        dest_currency = dest.currency_id
        rate_date = self.date_order or fields.Datetime.now()
        # The unit-of-measure field on the target (sale.order.line) is
        # product_uom_id on 18/19 and product_uom on 16/17. Resolve the
        # write key from the target model so create() does not break on the
        # earlier series, mirroring the getattr used on the read side.
        sol_fields = self.env['sale.order.line']._fields
        uom_key = ('product_uom_id' if 'product_uom_id' in sol_fields
                   else 'product_uom')
        line_vals = []
        for pol in self.order_line.filtered(
            lambda line: line.product_id and not line.display_type
        ):
            product = pol.product_id
            # Carry the SOURCE line unit of measure, not the product
            # default. product_uom_id is the field name on 19; the earlier
            # series call it product_uom, so read whichever exists.
            line_uom = (getattr(pol, 'product_uom_id', False)
                        or getattr(pol, 'product_uom', False))
            price_unit = pol.price_unit
            if src_currency and dest_currency \
                    and src_currency != dest_currency:
                price_unit = src_currency._convert(
                    price_unit, dest_currency, dest, rate_date)
            line_vals.append((0, 0, {
                'product_id': product.id,
                'name': pol.name,
                'product_uom_qty': pol.product_qty,
                uom_key: line_uom.id if line_uom else product.uom_id.id,
                'price_unit': price_unit,
            }))
        if not line_vals:
            return
        so_model = self._eh_ic_apply_user(self.env['sale.order'], config)
        sale = so_model.with_company(dest).create({
            'company_id': dest.id,
            'partner_id': self.company_id.partner_id.id,
            'origin': self.name,
            'eh_ic_origin_po_id': self.id,
            'order_line': line_vals,
        })
        self.eh_ic_mirror_so_id = sale.id
        self.message_post(body=_(
            "Inter-company sales order %(so)s drafted in %(company)s.",
            so=sale.name, company=dest.display_name,
        ))
