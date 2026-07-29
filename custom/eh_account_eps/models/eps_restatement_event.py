# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.eps.restatement.event: a bonus issue, share split or share consolidation.

A change in the number of ordinary shares without a corresponding change in
resources is applied retrospectively (IAS 33.26-28, 64): the weighted-average
engine restates every share movement recorded before the event date by the
event factor, as if the event had occurred at the beginning of the earliest
period presented, and the run's cumulative factor (the product of all event
factors) restates the comparative period's weighted average and EPS.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhEpsRestatementEvent(models.Model):
    _name = 'eh.eps.restatement.event'
    _description = "EPS retrospective restatement event"
    _order = 'run_id, date, id'

    run_id = fields.Many2one(
        'eh.eps.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)

    date = fields.Date(
        required=True,
        help="Date of the bonus issue, split or consolidation. The factor "
             "restates every share movement recorded BEFORE this date; a "
             "movement effective from this date on is taken to already "
             "carry the post-event share count (IAS 33.64).")
    kind = fields.Selection(
        [('bonus', "Bonus issue"),
         ('split', "Share split"),
         ('consolidation', "Share consolidation")],
        default='bonus', required=True)
    factor = fields.Float(
        digits=(16, 6), required=True,
        help="Ordinary shares after the event per share before it. A "
             "1-for-4 bonus issue is 1.25, a 2-for-1 split is 2.0, a "
             "1-for-5 consolidation is 0.2.")
    note = fields.Char()

    _sql_constraints = [
        ('check_factor', 'CHECK (factor > 0)', 'The restatement factor must be positive.'),
    ]

    @api.constrains('kind', 'factor')
    def _check_kind_factor(self):
        """A bonus issue or split increases the share count (factor above
        1); a consolidation reduces it (factor below 1). A factor on the
        wrong side of 1 restates every period's EPS in the wrong direction,
        so refuse it."""
        for ev in self:
            if ev.kind in ('bonus', 'split') and ev.factor <= 1.0:
                raise ValidationError(_(
                    "A bonus issue or share split increases the share "
                    "count: its restatement factor must be above 1 (a "
                    "1-for-4 bonus is 1.25, a 2-for-1 split is 2.0)."))
            if ev.kind == 'consolidation' and ev.factor >= 1.0:
                raise ValidationError(_(
                    "A share consolidation reduces the share count: its "
                    "restatement factor must be below 1 (a 1-for-5 "
                    "consolidation is 0.2)."))

    def _check_run_frozen(self):
        # A restatement event drives the retrospective weighted-average
        # restatement, so editing or deleting one after the run is computed
        # would silently move the disclosed EPS. A parent-only guard would
        # be bypassed by editing the child directly, so freeze here too.
        for ev in self:
            if ev.run_id.state == 'computed':
                raise UserError(_(
                    "Restatement events are frozen once run %s is computed. "
                    "Set the run back to draft to change them (IAS 33).",
                    ev.run_id.display_name))

    @api.model_create_multi
    def create(self, vals_list):
        # Appending an event to an already-computed run would silently swing
        # the weighted-average shares and EPS. Block create on a frozen run;
        # a write/unlink-only guard leaves this append hole open.
        Run = self.env['eh.eps.run']
        for vals in vals_list:
            run = Run.browse(vals['run_id']) if vals.get('run_id') else None
            if run and run.state == 'computed':
                raise UserError(_(
                    "Restatement events cannot be added to run %s once it "
                    "is computed. Set the run back to draft first "
                    "(IAS 33).", run.display_name))
        return super().create(vals_list)

    def write(self, vals):
        self._check_run_frozen()
        return super().write(vals)

    def unlink(self):
        self._check_run_frozen()
        return super().unlink()
