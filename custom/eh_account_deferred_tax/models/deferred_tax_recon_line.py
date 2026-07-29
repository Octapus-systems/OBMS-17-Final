# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.deferred.tax.recon.line: one row of the IAS 12.81(c) effective-tax-rate
reconciliation.

The reconciliation explains the gap between the expected tax (accounting
profit at the statutory rate) and the actual total tax expense. Auto rows
(expected tax, rate-change remeasurement, unrecognised-DTA movement, the
permanent-difference header input, and the balancing residual) are rebuilt
on every run compute; manual rows (prior-year adjustments, tax credits,
extra permanent items) are keyed by the user and preserved across
recomputes. The residual row auto-balances so the rows always tie to the
total tax expense.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_FROZEN_RUN_STATES = frozenset({'posted', 'reversed'})


class EhDeferredTaxReconLine(models.Model):
    _name = 'eh.deferred.tax.recon.line'
    _description = "Effective tax rate reconciliation line"
    _order = 'run_id, sequence, id'

    run_id = fields.Many2one(
        'eh.deferred.tax.run', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=50)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True,
    )
    kind = fields.Selection(
        [
            ('expected', "Expected tax at statutory rate"),
            ('permanent', "Permanent differences"),
            ('rate_change', "Change in tax rates"),
            ('prior_year', "Prior-year adjustments"),
            ('credits', "Tax credits / incentives"),
            ('unrecognised', "Unrecognised deferred tax assets"),
            ('other', "Other reconciling items"),
        ],
        required=True, default='other',
        help="Reconciling-item category per IAS 12.81(c). Auto rows are "
             "rebuilt on compute; manual rows are preserved.",
    )
    name = fields.Char(required=True, string="Label")
    amount = fields.Monetary(
        currency_field='currency_id',
        help="Signed tax amount: positive increases the total tax expense.",
    )
    is_auto = fields.Boolean(
        string="Auto", readonly=True,
        help="Computed by the run on Compute and rebuilt on every "
             "recompute. Manual rows keep this unset and are preserved.",
    )

    # -- Integrity: the reconciliation is disclosure basis; freeze with the
    # -- run once the movement is posted, mirroring the difference lines.

    @api.model_create_multi
    def create(self, vals_list):
        runs = self.env['eh.deferred.tax.run'].browse(
            [v.get('run_id') for v in vals_list if v.get('run_id')])
        if any(r.state in _FROZEN_RUN_STATES for r in runs):
            raise UserError(_(
                "Cannot add reconciliation rows to a deferred tax run that "
                "is already posted or reversed."))
        return super().create(vals_list)

    def write(self, vals):
        if any(r.state in _FROZEN_RUN_STATES for r in self.run_id):
            raise UserError(_(
                "The tax reconciliation is locked once the run is posted or "
                "reversed. Reverse the run to reopen it."))
        if vals.get('run_id'):
            target = self.env['eh.deferred.tax.run'].browse(vals['run_id'])
            if target.state in _FROZEN_RUN_STATES:
                raise UserError(_(
                    "Cannot move a reconciliation row into a deferred tax "
                    "run that is already posted or reversed."))
        return super().write(vals)

    def unlink(self):
        if any(r.state in _FROZEN_RUN_STATES for r in self.run_id):
            raise UserError(_(
                "Cannot delete reconciliation rows of a posted or reversed "
                "deferred tax run."))
        return super().unlink()
