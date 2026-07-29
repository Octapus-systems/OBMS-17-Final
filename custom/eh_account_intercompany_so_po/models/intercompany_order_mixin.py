# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Shared helpers for inter-company order mirroring.

Both sale.order and purchase.order resolve the destination company and
its inter-company configuration the same way, and create the mirror as
the configured inter-company user (or with elevated rights). This
abstract mixin holds that logic so both sides stay consistent with the
invoice-mirroring engine in eh_account_intercompany.
"""

from odoo import api, models


class EhIcOrderMixin(models.AbstractModel):
    _name = 'eh.ic.order.mixin'
    _description = "Inter-company order mirroring helpers"

    def _eh_ic_dest_company(self):
        """Company the order's partner represents, or empty when the
        order is not inter-company."""
        self.ensure_one()
        commercial = self.partner_id.commercial_partner_id
        empty = self.env['res.company'].browse()
        if not commercial:
            return empty
        dest = (commercial.eh_represented_company_id
                or commercial.company_id)
        if not dest or dest == self.company_id:
            return empty
        return dest

    def _eh_ic_config(self, dest_company):
        return self.env['eh.intercompany.config'].sudo().search(
            [('company_id', '=', dest_company.id), ('enabled', '=', True)],
            limit=1,
        )

    @api.model
    def _eh_ic_apply_user(self, recordset, config):
        if config.intercompany_user_id:
            return recordset.with_user(config.intercompany_user_id)
        return recordset.sudo()
