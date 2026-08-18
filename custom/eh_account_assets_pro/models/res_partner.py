# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.partner extension: linked-asset and lease counters.

Surfaces stat-buttons on the partner form for vendors / lessors:

  * "Assets supplied" - count of fixed assets where this partner is
    the vendor (eh.asset.partner_id).
  * "Leases" - count of IFRS-16 lease contracts where this partner
    is the lessor.

Both buttons hide when the count is zero so a regular customer's
partner form stays clean.
"""

from odoo import api, fields, models
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_asset_count = fields.Integer(
        compute='_compute_eh_asset_count',
        help="Fixed assets supplied by this partner (vendor link).",
    )
    eh_lease_count = fields.Integer(
        compute='_compute_eh_lease_count',
        help="IFRS-16 lease contracts with this partner as lessor.",
    )

    @api.depends_context('uid')
    def _compute_eh_asset_count(self):
        Asset = self.env['eh.asset'].sudo()
        groups = read_group_compat(Asset,
            [('partner_id', 'in', self.ids), ('state', '!=', 'disposed')],  # noqa: E128
            groupby=['partner_id'],
            aggregates=['__count'],
        )
        counts = {p.id: c for p, c in groups}
        for partner in self:
            partner.eh_asset_count = counts.get(partner.id, 0)

    @api.depends_context('uid')
    def _compute_eh_lease_count(self):
        Lease = self.env['eh.lease.contract'].sudo()
        groups = read_group_compat(Lease,
            [('lessor_id', 'in', self.ids)],  # noqa: E128
            groupby=['lessor_id'],
            aggregates=['__count'],
        )
        counts = {p.id: c for p, c in groups}
        for partner in self:
            partner.eh_lease_count = counts.get(partner.id, 0)

    def action_view_eh_assets(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Assets supplied',
            'res_model': 'eh.asset',
            'view_mode': 'kanban,list,form',
            'views': [(False, 'kanban'), (False, 'list'), (False, 'form')],
            'domain': [('partner_id', '=', self.id)],
        }

    def action_view_eh_leases(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Lease contracts',
            'res_model': 'eh.lease.contract',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('lessor_id', '=', self.id)],
        }
