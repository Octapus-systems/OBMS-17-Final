# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.eps.share.movement: the number of ordinary shares outstanding from a date.

The weighted average number of shares is the sum of each period's shares
outstanding weighted by the fraction of the reporting period they were in
issue (IAS 33.20). Each movement records the total shares outstanding from
its effective date until the next movement.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhEpsShareMovement(models.Model):
    _name = 'eh.eps.share.movement'
    _description = "EPS share movement"
    _order = 'run_id, effective_date, id'

    run_id = fields.Many2one(
        'eh.eps.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)

    effective_date = fields.Date(
        required=True,
        help="Date from which this number of shares was outstanding.")
    shares_outstanding = fields.Float(
        digits=(16, 2), required=True,
        help="Total ordinary shares outstanding from the effective date "
             "until the next movement.")
    note = fields.Char()

    _sql_constraints = [
        ('check_shares', 'CHECK (shares_outstanding >= 0)', 'Shares outstanding cannot be negative.'),
    ]

    def _check_run_frozen(self):
        # A share movement feeds the weighted average, so editing or deleting
        # it after the run is computed would silently move the reported shares
        # and EPS away from the disclosed figures. A parent-only guard would be
        # bypassed by editing the child directly, so freeze here too.
        for mv in self:
            if mv.run_id.state == 'computed':
                raise UserError(_(
                    "Share movements are frozen once run %s is computed. Set "
                    "the run back to draft to change them (IAS 33).",
                    mv.run_id.display_name))

    @api.model_create_multi
    def create(self, vals_list):
        # Appending a movement to an already-computed run would silently swing
        # the weighted-average shares and EPS. Block create on a frozen run;
        # a write/unlink-only guard leaves this append hole open.
        Run = self.env['eh.eps.run']
        for vals in vals_list:
            run = Run.browse(vals['run_id']) if vals.get('run_id') else None
            if run and run.state == 'computed':
                raise UserError(_(
                    "Share movements cannot be added to run %s once it is "
                    "computed. Set the run back to draft first (IAS 33).",
                    run.display_name))
        return super().create(vals_list)

    def write(self, vals):
        self._check_run_frozen()
        return super().write(vals)

    def unlink(self):
        self._check_run_frozen()
        return super().unlink()
