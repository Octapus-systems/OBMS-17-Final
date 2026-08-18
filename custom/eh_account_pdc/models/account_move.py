# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move extension: linked-cheque counter for invoice forms.

Surfaces a stat-button on the invoice form: "Linked cheques: N", which
opens the filtered cheque list when there are any post-dated cheques
recorded against this invoice. Without this, users have to navigate
through the menu to confirm whether a customer has lodged a PDC
against their open invoice.
"""

from odoo import api, fields, models
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_cheque_count = fields.Integer(
        compute='_compute_eh_cheque_count',
        help="Number of post-dated cheques linked to this invoice.",
    )

    @api.depends_context('uid')
    def _compute_eh_cheque_count(self):
        Cheque = self.env['eh.cheque'].sudo()
        groups = read_group_compat(Cheque,
            [('invoice_id', 'in', self.ids)],  # noqa: E128
            groupby=['invoice_id'],
            aggregates=['__count'],
        )
        counts = {m.id: c for m, c in groups}
        for move in self:
            move.eh_cheque_count = counts.get(move.id, 0)

    def action_view_eh_cheques(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Linked cheques',
            'res_model': 'eh.cheque',
            'view_mode': 'kanban,list,form',
            'views': [(False, 'kanban'), (False, 'list'), (False, 'form')],
            'domain': [('invoice_id', '=', self.id)],
        }


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    eh_cheque_count = fields.Integer(
        compute='_compute_eh_payment_cheque_count',
        help="Number of post-dated cheques linked to this payment.",
    )

    @api.depends_context('uid')
    def _compute_eh_payment_cheque_count(self):
        Cheque = self.env['eh.cheque'].sudo()
        groups = read_group_compat(Cheque,
            [('payment_id', 'in', self.ids)],  # noqa: E128
            groupby=['payment_id'],
            aggregates=['__count'],
        )
        counts = {p.id: c for p, c in groups}
        for payment in self:
            payment.eh_cheque_count = counts.get(payment.id, 0)

    def action_view_eh_cheques(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Linked cheques',
            'res_model': 'eh.cheque',
            'view_mode': 'kanban,list,form',
            'views': [(False, 'kanban'), (False, 'list'), (False, 'form')],
            'domain': [('payment_id', '=', self.id)],
        }
