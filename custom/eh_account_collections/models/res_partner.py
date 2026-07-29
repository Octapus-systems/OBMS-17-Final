# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.partner extension: active collections cases counter.

Surfaces a smart-button on the partner form: "Active collections: N".
A click drills into the kanban filtered to this partner so the
operator goes from a customer record to the live collections case in
one hop instead of navigating through the menu.
"""

from odoo import api, fields, models
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_active_collections_count = fields.Integer(
        compute='_compute_eh_active_collections_count',
        help=(
            "Number of non-resolved collections cases on this partner. "
            "Refreshed on form load; the auto-creator cron and the "
            "manual refresh action update the underlying case totals."
        ),
    )

    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_eh_active_collections_count(self):
        # The smart button lives on the base partner form (no group
        # restriction), so this compute fires for every internal user who
        # opens any partner - including users with no collections model
        # access. sudo() keeps it from raising AccessError for them, but on
        # its own it also bypasses the eh.collections.case company record
        # rule ([]) and would leak other
        # companies' case totals for a shared partner. Constrain the domain
        # to the viewer's allowed companies so the count matches exactly what
        # the record rule (and the action_view drill-down) would show.
        allowed_company_ids = self.env.companies.ids
        Case = self.env['eh.collections.case'].sudo()
        groups = read_group_compat(Case,
            [
                ('partner_id', 'in', self.ids),
                ('is_resolved', '=', False),
                ('company_id', 'in', allowed_company_ids),
            ],
            groupby=['partner_id'],
            aggregates=['__count'],
        )
        counts = {p.id: c for p, c in groups}
        for partner in self:
            partner.eh_active_collections_count = counts.get(partner.id, 0)

    def action_view_eh_active_collections(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Collections cases',
            'res_model': 'eh.collections.case',
            'view_mode': 'kanban,list,form',
            'views': [(False, 'kanban'), (False, 'list'), (False, 'form')],
            'domain': [
                ('partner_id', '=', self.id),
                ('is_resolved', '=', False),
            ],
        }
