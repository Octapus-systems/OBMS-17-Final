# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.ecl.scenario: one probability-weighted forward-looking scenario of a run.

IFRS 9.5.5.17(a) requires the ECL to be an unbiased, probability-weighted
amount evaluated over a range of possible outcomes, and IFRS 9.5.5.17(c)
requires reasonable and supportable forward-looking information. A scenario
scales the buckets' PD and LGD by macro adjustment factors and carries the
scenario probability; the run's general-approach ECL is the weighted sum over
its scenarios. A run without scenarios measures on a single implicit
base scenario (weight 1, factors 1).
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_FROZEN = frozenset({'posted', 'reversed'})

# Tolerance for the sum-of-weights check: scenario probabilities keyed to four
# decimals must sum to one, allowing only float representation noise.
_WEIGHT_TOLERANCE = 0.0001


class EhEclScenario(models.Model):
    _name = 'eh.ecl.scenario'
    _description = "ECL forward-looking scenario"
    _order = 'run_id, id'

    run_id = fields.Many2one(
        'eh.ecl.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    name = fields.Char(
        required=True, help="Label, e.g. 'Base', 'Upside', 'Downside'.")
    weight = fields.Float(
        digits=(7, 4), required=True, default=1.0,
        help="Probability of this scenario. The weights of a run's scenarios "
             "must sum to 1 (IFRS 9.5.5.17(a)).")
    pd_factor = fields.Float(
        digits=(7, 4), default=1.0,
        help="Macro adjustment multiplier applied to each bucket's PD under "
             "this scenario; the adjusted PD is capped at 100%.")
    lgd_factor = fields.Float(
        digits=(7, 4), default=1.0,
        help="Macro adjustment multiplier applied to each bucket's LGD under "
             "this scenario; the adjusted LGD is capped at 100%.")

    _sql_constraints = [
        ('check_weight', 'CHECK (weight >= 0 AND weight <= 1)', 'A scenario weight must be between 0 and 1.'),
        ('check_factors', 'CHECK (pd_factor >= 0 AND lgd_factor >= 0)', 'Scenario PD and LGD factors cannot be negative.'),  # noqa: E501
    ]

    @api.constrains('weight', 'run_id')
    def _check_weights_sum(self):
        for run in self.run_id:
            total = sum(run.scenario_ids.mapped('weight'))
            if abs(total - 1.0) > _WEIGHT_TOLERANCE:
                raise ValidationError(_(
                    "The scenario weights on run %(run)s sum to %(total)s; "
                    "a probability-weighted ECL requires them to sum to 1 "
                    "(IFRS 9.5.5.17(a)).",
                    run=run.display_name, total=round(total, 4)))

    def _check_run_not_posted(self):
        # A scenario reweights the posted allowance the same way a bucket
        # edit would, so the posted-run freeze extends here (IFRS 9.5.5.8).
        if any(r.state in _FROZEN for r in self.run_id):
            raise UserError(_(
                "This ECL run is posted; its scenarios can no longer change. "
                "Reverse it to reopen (EH Accounting Manager only)."))

    @api.model_create_multi
    def create(self, vals_list):
        run_ids = {v.get('run_id') for v in vals_list if v.get('run_id')}
        if run_ids:
            frozen = self.env['eh.ecl.run'].browse(run_ids).filtered(
                lambda r: r.state in _FROZEN)
            if frozen:
                raise UserError(_(
                    "Scenarios cannot be added to a posted ECL run; reverse "
                    "the run to reopen it (EH Accounting Manager only)."))
        return super().create(vals_list)

    def write(self, vals):
        self._check_run_not_posted()
        if vals.get('run_id'):
            target = self.env['eh.ecl.run'].browse(vals['run_id'])
            if target.state in _FROZEN:
                raise UserError(_(
                    "Scenarios cannot be moved into a posted ECL run; "
                    "reverse the run to reopen it (EH Accounting Manager "
                    "only)."))
        return super().write(vals)

    def unlink(self):
        self._check_run_not_posted()
        return super().unlink()
