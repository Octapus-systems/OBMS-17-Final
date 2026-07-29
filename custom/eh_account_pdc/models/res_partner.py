# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.partner extension: open post-dated cheque counter.

Adds a smart-button "Open cheques: N" on the partner form, where
"open" means cheques in registered, presented, or bounced state (i.e.
not yet cleared/replaced/cancelled). One click drills into the cheque
list filtered to this partner.
"""

from odoo import api, fields, models
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat


_OPEN_STATES = ('registered', 'presented', 'bounced')


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_open_cheque_count = fields.Integer(
        compute='_compute_eh_open_cheque_count',
        help=(
            "Number of cheques on this partner whose state is not yet "
            "cleared, replaced, or cancelled. Both incoming (received "
            "from customer) and outgoing (issued to vendor) cheques "
            "count."
        ),
    )

    @api.depends_context('uid')
    def _compute_eh_open_cheque_count(self):
        Cheque = self.env['eh.cheque'].sudo()
        groups = read_group_compat(Cheque, 
            [
                ('partner_id', 'in', self.ids),
                ('state', 'in', list(_OPEN_STATES)),
            ],
            groupby=['partner_id'],
            aggregates=['__count'],
        )
        counts = {p.id: c for p, c in groups}
        for partner in self:
            partner.eh_open_cheque_count = counts.get(partner.id, 0)

    def action_view_eh_open_cheques(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Open cheques',
            'res_model': 'eh.cheque',
            'view_mode': 'kanban,list,form',
            'views': [(False, 'kanban'), (False, 'list'), (False, 'form')],
            'domain': [
                ('partner_id', '=', self.id),
                ('state', 'in', list(_OPEN_STATES)),
            ],
        }
