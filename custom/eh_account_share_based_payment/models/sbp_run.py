# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sbp.period.run: the IFRS 2 period accounting engine.

One run per plan per period end date. The run computes the cumulative
measure at its period end (equity: grant-date FV x expected-to-vest x
vested fraction plus modification incremental FV; cash: current FV x
expected-to-vest x vested fraction) and posts the DELTA against what the
plan has already recognised:

* equity-settled: Dr expense / Cr SBP equity reserve (reversed legs for a
  true-down or a non-market failure reversal, IFRS 2.20/2.23 contrast);
* cash-settled:   Dr expense / Cr liability (reversed for a fair-value
  fall, IFRS 2.30-33).

Runs post in chronological order and are frozen once posted; the
generated entry is sealed. Estimate changes (forfeiture updates, failed
conditions, final vesting outcomes) flow through the NEXT run's delta:
that is the IFRS 2.20 true-up through the current period.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhSbpPeriodRun(models.Model):
    _name = 'eh.sbp.period.run'
    _description = "Share-based payment period run (IFRS 2)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_end desc, id desc'
    _rec_name = 'name'

    # State moves only through Compute/Post (Post books the sealed journal
    # entry); the mixin blocks any direct RPC write to it by a low-privilege
    # user, closing the "write({'state': 'posted'}) skips the entry" bypass.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/')
    state = fields.Selection(
        [('draft', "Draft"), ('posted', "Posted")],
        default='draft', required=True, tracking=True, index=True)
    plan_id = fields.Many2one(
        'eh.sbp.plan', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(
        related='plan_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='plan_id.currency_id', store=True, readonly=True)
    plan_settlement = fields.Selection(
        related='plan_id.settlement', string="Settlement")
    period_end = fields.Date(required=True, tracking=True)
    current_fair_value = fields.Float(
        digits=(16, 4), string="Current FV / Instrument", tracking=True,
        help="Fair value per instrument at the period end. Required for "
             "cash-settled plans: the liability remeasures to it every "
             "period (IFRS 2.30). Ignored for equity-settled plans, which "
             "stay at grant-date fair value (IFRS 2.19).")

    vested_fraction = fields.Float(
        digits=(16, 6), readonly=True, copy=False,
        help="Straight-line service fraction at the period end (whole "
             "months over total vesting months). Informational for graded "
             "plans, where each tranche applies its own fraction.")
    cumulative_target = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Cumulative expense (equity) or liability (cash) the plan "
             "should carry at this period end.")
    prior_recognised = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')
    period_charge = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        tracking=True,
        help="Cumulative target minus the amount already recognised; "
             "negative for a true-down or reversal.")
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        string="Journal Entry")
    notes = fields.Char()

    _sql_constraints = [
        ('unique_plan_period', 'UNIQUE (plan_id, period_end)', 'Only one period run per plan and period end date.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.sbp.period.run') or '/'
        return super().create(vals_list)

    # Posted runs are the ledger trail of the cumulative formula: freeze
    # every measurement input once posted.
    _FROZEN_FIELDS = ('plan_id', 'period_end', 'current_fair_value')

    def write(self, vals):
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        if frozen:
            posted = self.filtered(lambda r: r.state == 'posted')
            if posted:
                raise UserError(_(
                    "A posted period run is frozen (%(fields)s on "
                    "%(runs)s).",
                    fields=', '.join(frozen),
                    runs=', '.join(posted.mapped('display_name'))))
        # STATE transitions are enforced by the inherited eh.workflow.guard
        # (blocks a non-superuser direct write); the sanctioned Compute/Post
        # actions run under sudo. Provenance is env.su, not a context key.
        return super().write(vals)

    def unlink(self):
        posted = self.filtered(lambda r: r.state == 'posted')
        if posted:
            raise UserError(_(
                "A posted period run cannot be deleted; its journal entry "
                "would be orphaned."))
        return super().unlink()

    # ------------------------------------------------------------------
    # engine
    # ------------------------------------------------------------------
    def _validate_inputs(self):
        self.ensure_one()
        plan = self.plan_id
        if plan.state != 'active':
            raise UserError(_(
                "Activate plan %s before running the period engine.",
                plan.display_name))
        if self.period_end <= plan.grant_date:
            raise UserError(_(
                "The period end must fall after the grant date."))
        last = max(plan.run_ids.filtered(
            lambda r: r.state == 'posted' and r.id != self.id).mapped(
                'period_end'), default=False)
        if last and self.period_end <= last:
            raise UserError(_(
                "Runs post in chronological order: the last posted run of "
                "%(plan)s covers %(last)s.",
                plan=plan.display_name, last=last))
        if plan.settlement == 'cash' and self.current_fair_value <= 0.0:
            raise UserError(_(
                "A cash-settled run needs the current fair value per "
                "instrument (IFRS 2.30)."))

    def _measure(self):
        """(cumulative_target, prior, charge) at this run's period end."""
        self.ensure_one()
        plan = self.plan_id
        if plan.settlement == 'cash':
            target = plan._cash_liability_at(
                self.period_end, self.current_fair_value)
        else:
            target = plan._equity_cumulative_at(self.period_end)
        prior = plan.recognised_cumulative
        charge = plan.currency_id.round(target - prior)
        return target, prior, charge

    def action_compute(self):
        for run in self:
            if run.state != 'draft':
                raise UserError(_("Only a draft run can be recomputed."))
            run._validate_inputs()
            target, prior, charge = run._measure()
            run.write({
                'vested_fraction': run.plan_id._service_fraction(
                    run.period_end),
                'cumulative_target': target,
                'prior_recognised': prior,
                'period_charge': charge,
            })
        return True

    def action_post(self):
        self.ensure_one()
        plan = self.plan_id
        plan._check_manager()
        if self.state != 'draft':
            raise UserError(_("This period run has already posted."))
        self._validate_inputs()
        # Recompute at post time so a stale preview can never post: the
        # estimates may have moved since Compute was clicked.
        target, prior, charge = self._measure()
        currency = plan.currency_id
        if currency.is_zero(charge):
            raise UserError(_(
                "Nothing to post: the cumulative measure already equals "
                "the recognised amount."))
        if plan.settlement == 'equity':
            plan._validate_accounts(['expense', 'equity'])
            counter = plan.equity_account_id
            up_label = _("SBP expense %s", self.name)
            down_label = _("SBP true-up reversal %s", self.name)
        else:
            plan._validate_accounts(['expense', 'liability'])
            counter = plan.liability_account_id
            up_label = _("SAR expense %s", self.name)
            down_label = _("SAR remeasurement gain %s", self.name)
        if currency.compare_amounts(charge, 0.0) > 0:
            move = plan._post_move([
                (plan.expense_account_id, charge, 0.0, up_label),
                (counter, 0.0, charge, up_label),
            ], date=self.period_end, ref=self.name)
        else:
            move = plan._post_move([
                (counter, -charge, 0.0, down_label),
                (plan.expense_account_id, 0.0, -charge, down_label),
            ], date=self.period_end, ref=self.name)
        self.write({
            'vested_fraction': plan._service_fraction(self.period_end),
            'cumulative_target': target,
            'prior_recognised': prior,
            'period_charge': charge,
            'move_id': move.id,
        })
        # recognised_cumulative is a guarded anchor on the plan (see
        # eh.sbp.plan._eh_guarded_fields); the sanctioned post writes it as su
        # so a real (non-superuser) manager's post is not refused by the guard.
        plan.sudo().recognised_cumulative = target
        self.sudo().state = 'posted'
        return True
