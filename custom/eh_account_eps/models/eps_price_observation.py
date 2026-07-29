# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.eps.price.observation: a dated market price of the ordinary share.

IAS 33.45 applies the treasury-stock method at the average market price of
the ordinary shares DURING the period, not a spot price. When observations
are recorded on a potential-share class, its average market price is the
arithmetic mean of the observations dated inside the run period; without
observations the manually entered scalar average stands, so existing runs
are unchanged.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhEpsPriceObservation(models.Model):
    _name = 'eh.eps.price.observation'
    _description = "EPS market price observation"
    _order = 'potential_id, date, id'

    potential_id = fields.Many2one(
        'eh.eps.potential', required=True, ondelete='cascade', index=True)
    run_id = fields.Many2one(
        related='potential_id.run_id', store=True, readonly=True)
    company_id = fields.Many2one(
        related='potential_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='potential_id.currency_id', store=True, readonly=True)

    date = fields.Date(
        required=True,
        help="Observation date. Only observations dated inside the run "
             "period enter the period average (IAS 33.45).")
    price = fields.Monetary(
        required=True, currency_field='currency_id',
        help="Observed market price of one ordinary share.")

    _sql_constraints = [
        ('check_price', 'CHECK (price > 0)', 'A market price observation must be positive.'),
    ]

    def _check_run_frozen(self):
        # Observations resolve the average market price behind the
        # treasury-stock method, so editing or deleting one after the run is
        # computed would silently move diluted EPS off the disclosed figure.
        # A parent-only guard would be bypassed by editing the child
        # directly, so freeze here too.
        for obs in self:
            if obs.potential_id.run_id.state == 'computed':
                raise UserError(_(
                    "Market price observations are frozen once run %s is "
                    "computed. Set the run back to draft to change them "
                    "(IAS 33).", obs.potential_id.run_id.display_name))

    @api.model_create_multi
    def create(self, vals_list):
        # Appending an observation to an already-computed run would silently
        # move the resolved average market price and diluted EPS. Block
        # create on a frozen run; a write/unlink-only guard leaves this
        # append hole open.
        Potential = self.env['eh.eps.potential']
        for vals in vals_list:
            pot = (Potential.browse(vals['potential_id'])
                   if vals.get('potential_id') else None)
            if pot and pot.run_id.state == 'computed':
                raise UserError(_(
                    "Market price observations cannot be added to run %s "
                    "once it is computed. Set the run back to draft first "
                    "(IAS 33).", pot.run_id.display_name))
        return super().create(vals_list)

    def write(self, vals):
        self._check_run_frozen()
        return super().write(vals)

    def unlink(self):
        self._check_run_frozen()
        return super().unlink()
