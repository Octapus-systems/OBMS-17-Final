# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.partner extension: pending approvals smart counter.

Surfaces "this partner has N approval requests in flight" on the partner
form. The number is computed (no storage, recomputed on form load) so a
new request appearing immediately reflects in the counter.

Why this lives here and not in res.partner core: each suite module
contributes its own smart-button counter for the metric it owns,
keeping coupling minimal. A buyer opening a vendor form sees:

  * Pending approvals  (from this module)
  * Active collections  (from eh_account_collections)
  * Open cheques  (from eh_account_pdc)
  * Overdue AR/AP  (from eh_account_credit_limit)
  * ...

Each card is one click into a filtered list of the underlying records.
"""

from odoo import api, fields, models
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_pending_approval_count = fields.Integer(
        compute='_compute_eh_pending_approval_count',
        help=(
            "Open approval requests on invoices/bills for this "
            "partner. Pending and in_review states count; approved, "
            "rejected, withdrawn and archived do not."
        ),
    )

    @api.depends_context('uid')
    def _compute_eh_pending_approval_count(self):
        # Single grouped query over the partner set so the form
        # doesn't fire one search per partner when used on a kanban
        # or list view that materialises many records.
        Request = self.env['eh.approval.request'].sudo()
        domain = [
            ('partner_id', 'in', self.ids),
            ('state', 'in', ('pending', 'in_review')),
        ]
        groups = read_group_compat(Request,
            domain,  # noqa: E128
            groupby=['partner_id'],  # noqa: E128
            aggregates=['__count'],
        )
        counts = {p.id: c for p, c in groups}
        for partner in self:
            partner.eh_pending_approval_count = counts.get(partner.id, 0)

    def action_view_eh_pending_approvals(self):
        """Open the approval-request list filtered to this partner."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Pending approvals',
            'res_model': 'eh.approval.request',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('partner_id', '=', self.id),
                ('state', 'in', ('pending', 'in_review')),
            ],
            'context': {'default_move_id': False},
        }
