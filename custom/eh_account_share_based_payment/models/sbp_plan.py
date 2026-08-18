# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sbp.plan: one IFRS 2 share-based payment plan and its grants.

Measurement mechanics implemented here (IFRS 2 as published):

* Equity-settled, service / non-market conditions (IFRS 2.19-20): the
  cumulative expense at any date is grant-date fair value x instruments
  expected to vest x vested fraction. The number expected to vest is the
  granted count net of the current forfeiture estimate and is trued up
  each period through the current period charge (change in estimate). A
  non-market performance condition that fails reverses the cumulative
  expense in full (IFRS 2.23 by contrast: only MARKET conditions stick).
* Market conditions (IFRS 2.21): the market condition is reflected in the
  grant-date fair value, so the expense is never trued up for the market
  outcome. Failure of the market condition with service completed leaves
  the cumulative expense standing; only service forfeitures adjust the
  count.
* Graded vesting (IFRS 2.IG11): each tranche is expensed over its OWN
  vesting period off its own grant-date fair value. Tranche instruments =
  expected-to-vest x tranche portion.
* Cash-settled (IFRS 2.30-33): the liability is remeasured to CURRENT
  fair value x vested fraction each period; the period charge is the
  liability movement. Settlement trues the liability to the settlement
  amount through expense, then pays it.
* Modifications (IFRS 2.27): only beneficial modifications add
  measurement: the incremental fair value granted is expensed over the
  remaining vesting period (immediately when modified after vesting). A
  modification that REDUCES fair value is ignored for measurement, which
  is why a negative incremental fair value is refused outright: the
  original grant-date expense continues unchanged.
* Cancellation (IFRS 2.28(a)): cancelling an equity plan accelerates the
  remaining unrecognised expense immediately.

Vested-fraction convention: whole calendar months elapsed from the grant
date (relativedelta floor) divided by the total vesting months; no
intra-month proration. Cumulative amounts are rounded once to company
currency; the period charge is the difference of rounded cumulatives.

Grantees are res.partner records: employees are partners in Odoo and this
keeps the module free of a hard hr dependency. Deployments with hr
installed simply pick the employee's partner (employee_id.work_contact_id
or address_home_id depending on series).
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare


def _months_between(start, end):
    """Whole calendar months between two dates (floor, never negative)."""
    if not start or not end or end <= start:
        return 0
    delta = relativedelta(end, start)
    return delta.years * 12 + delta.months


class EhSbpPlan(models.Model):
    _name = 'eh.sbp.plan'
    _description = "Share-based payment plan (IFRS 2)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'grant_date desc, id desc'
    _rec_name = 'name'

    # State moves only through Activate/Settle/Cancel (each posts a journal
    # entry), and recognised_cumulative is the cumulative-posted IFRS 2
    # expense / cash-settled liability anchor that the period-run engine trues
    # up against at post time. The mixin blocks any direct RPC write to either
    # field by a low-privilege user, closing both the
    # "write({'state': 'active'}) skips the action" bypass AND a silent
    # restatement of recognised_cumulative (which would double-charge the next
    # run's delta or suppress a legitimate true-up, and immediately corrupt the
    # IFRS 2.45 disclosure figure). The sanctioned actions and the run engine
    # write recognised_cumulative under su (self.sudo()).
    _eh_guarded_fields = ('state', 'recognised_cumulative')

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('active', "Active"),
         ('settled', "Settled"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    settlement = fields.Selection(
        [('equity', "Equity-settled"), ('cash', "Cash-settled")],
        default='equity', required=True, tracking=True,
        help="Equity-settled awards are measured once at grant-date fair "
             "value (IFRS 2.19); cash-settled awards are remeasured to "
             "current fair value every period until settled (IFRS 2.30).")
    condition_kind = fields.Selection(
        [('service', "Service condition"),
         ('non_market', "Non-market performance condition"),
         ('market', "Market condition")],
        default='service', required=True, tracking=True,
        help="Service and non-market conditions true up through the number "
             "of instruments expected to vest (IFRS 2.19-20); a failed "
             "non-market condition reverses the expense in full. A market "
             "condition is reflected in the grant-date fair value and its "
             "outcome is never trued up (IFRS 2.21): the expense stands "
             "once service is rendered.")
    instrument_desc = fields.Char(
        string="Instrument",
        help="Description of the instrument granted (e.g. options over "
             "ordinary shares, share appreciation rights).")

    grant_date = fields.Date(required=True, tracking=True,
                             default=fields.Date.context_today)
    vesting_years = fields.Integer(
        default=3, tracking=True,
        help="Vesting period in whole years (cliff plans). Ignored when "
             "graded vesting is on: each tranche carries its own end date.")
    vesting_months = fields.Integer(
        default=0, tracking=True,
        help="Additional vesting months on top of the vesting years.")
    graded_vesting = fields.Boolean(
        tracking=True,
        help="Each tranche vests on its own date and is expensed over its "
             "own vesting period off its own fair value (IFRS 2.IG11).")
    tranche_ids = fields.One2many(
        'eh.sbp.plan.tranche', 'plan_id', string="Vesting Tranches")
    vesting_end_date = fields.Date(
        compute='_compute_vesting_end_date', store=True,
        help="Final vesting date: grant date plus the vesting period, or "
             "the latest tranche end for graded plans.")

    grant_ids = fields.One2many('eh.sbp.grant', 'plan_id', string="Grants")
    modification_ids = fields.One2many(
        'eh.sbp.modification', 'plan_id', string="Modifications")
    run_ids = fields.One2many(
        'eh.sbp.period.run', 'plan_id', string="Period Runs")
    run_count = fields.Integer(compute='_compute_counts')

    recognised_cumulative = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        tracking=True,
        help="Cumulative expense recognised to date. For a cash-settled "
             "plan this is the liability carrying amount.")
    settlement_amount = fields.Monetary(
        currency_field='currency_id',
        help="Cash paid to settle a cash-settled plan. Settle trues the "
             "liability to this amount through expense, then pays it "
             "(IFRS 2.30: the liability is remeasured to fair value at "
             "settlement).")

    # ---- accounts ----
    expense_account_id = fields.Many2one(
        'account.account', string="Expense Account", tracking=True,
        domain="[('account_type', 'in', ['expense', 'expense_direct_cost'])]")
    equity_account_id = fields.Many2one(
        'account.account', string="Equity Reserve Account", tracking=True,
        domain="[('account_type', '=', 'equity')]",
        help="Share-based payment reserve credited as equity-settled "
             "expense accrues (IFRS 2.7).")
    liability_account_id = fields.Many2one(
        'account.account', string="Liability Account", tracking=True,
        domain="[('account_type', 'in', "
               "['liability_current', 'liability_non_current'])]",
        help="Liability remeasured to current fair value each period for "
             "cash-settled awards (IFRS 2.30).")
    settlement_account_id = fields.Many2one(
        'account.account', string="Settlement Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'liability_payable', 'asset_current'])]",
        help="Credited when a cash-settled plan pays out.")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]")

    move_ids = fields.One2many('account.move', 'eh_sbp_plan_id')
    move_count = fields.Integer(compute='_compute_counts')

    # ---- disclosure feed (IFRS 2.45) ----
    granted_total = fields.Integer(
        compute='_compute_disclosure', string="Granted")
    forfeited_total = fields.Integer(
        compute='_compute_disclosure', string="Forfeited")
    exercised_total = fields.Integer(
        compute='_compute_disclosure', string="Exercised")
    expired_total = fields.Integer(
        compute='_compute_disclosure', string="Expired")
    outstanding_total = fields.Integer(
        compute='_compute_disclosure', string="Outstanding")
    waep_outstanding = fields.Float(
        compute='_compute_disclosure', digits=(16, 4),
        string="WAEP (Outstanding)",
        help="Weighted average exercise price of the outstanding "
             "instruments that carry an exercise price (IFRS 2.45(b)).")

    notes = fields.Text()

    _sql_constraints = [
        ('check_vesting', 'CHECK (vesting_years >= 0 AND vesting_months >= 0)', 'The vesting period cannot be negative.'),  # noqa: E501
    ]

    # ------------------------------------------------------------------
    # computes
    # ------------------------------------------------------------------
    @api.depends('grant_date', 'vesting_years', 'vesting_months',
                 'graded_vesting', 'tranche_ids.vesting_end_date')
    def _compute_vesting_end_date(self):
        for plan in self:
            if plan.graded_vesting:
                ends = plan.tranche_ids.mapped('vesting_end_date')
                plan.vesting_end_date = max(ends) if ends else False
            elif plan.grant_date:
                plan.vesting_end_date = plan.grant_date + relativedelta(
                    years=plan.vesting_years or 0,
                    months=plan.vesting_months or 0)
            else:
                plan.vesting_end_date = False

    def _compute_counts(self):
        for plan in self:
            plan.move_count = len(plan.move_ids)
            plan.run_count = len(plan.run_ids)

    @api.depends('grant_ids.instruments_granted',
                 'grant_ids.forfeited_qty', 'grant_ids.exercised_qty',
                 'grant_ids.expired_qty', 'grant_ids.outstanding_qty',
                 'grant_ids.exercise_price')
    def _compute_disclosure(self):
        for plan in self:
            grants = plan.grant_ids
            plan.granted_total = sum(grants.mapped('instruments_granted'))
            plan.forfeited_total = sum(grants.mapped('forfeited_qty'))
            plan.exercised_total = sum(grants.mapped('exercised_qty'))
            plan.expired_total = sum(grants.mapped('expired_qty'))
            plan.outstanding_total = sum(grants.mapped('outstanding_qty'))
            priced = grants.filtered(
                lambda g: g.exercise_price > 0 and g.outstanding_qty > 0)
            weight = sum(priced.mapped('outstanding_qty'))
            plan.waep_outstanding = (
                sum(g.exercise_price * g.outstanding_qty for g in priced)
                / weight) if weight else 0.0

    # ------------------------------------------------------------------
    # measurement engine
    # ------------------------------------------------------------------
    def _total_vesting_months(self):
        self.ensure_one()
        return (self.vesting_years or 0) * 12 + (self.vesting_months or 0)

    def _service_fraction(self, on_date):
        """Straight-line vested fraction for a cliff (non-graded) plan.

        Whole months elapsed since the grant date over the total vesting
        months, capped at 1. No intra-month proration.
        """
        self.ensure_one()
        total = self._total_vesting_months()
        if total <= 0:
            return 1.0
        return min(_months_between(self.grant_date, on_date) / total, 1.0)

    def _expected_to_vest(self):
        """Instruments currently expected to vest across all grants."""
        self.ensure_one()
        return sum(g._expected_to_vest() for g in self.grant_ids)

    def _modification_amount(self, on_date, full=False):
        """Cumulative incremental-FV expense of modifications dated on or
        before ``on_date`` (IFRS 2.27): incremental fair value x instruments
        expected to vest x elapsed fraction of the REMAINING vesting period
        (from the modification date to the final vesting end). A
        modification after vesting expenses immediately (fraction 1)."""
        self.ensure_one()
        expected = self._expected_to_vest()
        total = 0.0
        for mod in self.modification_ids:
            if not mod.date or mod.date > on_date:
                continue
            if full:
                frac = 1.0
            else:
                remaining = _months_between(mod.date, self.vesting_end_date)
                if remaining <= 0:
                    frac = 1.0
                else:
                    frac = min(
                        _months_between(mod.date, on_date) / remaining, 1.0)
            total += expected * mod.incremental_fv * frac
        return total

    def _equity_cumulative_at(self, on_date, full=False):
        """Cumulative equity-settled expense at ``on_date`` (IFRS 2.19-20).

        base   = sum over grants of expected-to-vest x grant-date FV x
                 vested fraction; graded plans split the expected count by
                 tranche portion and expense each tranche over its own
                 period off its own FV (IFRS 2.IG11).
        mods   = incremental FV spread over the remaining vesting period.
        ``full=True`` measures at fraction 1 everywhere (cancellation
        acceleration, IFRS 2.28(a)).

        Rounded ONCE to company currency at the end; period charges are
        differences of rounded cumulatives.
        """
        self.ensure_one()
        base = 0.0
        for grant in self.grant_ids:
            n = grant._expected_to_vest()
            if not n:
                continue
            if self.graded_vesting:
                for tranche in self.tranche_ids:
                    frac = 1.0 if full else tranche._vested_fraction(on_date)
                    base += (n * (tranche.portion_pct / 100.0)
                             * tranche.fair_value * frac)
            else:
                frac = 1.0 if full else self._service_fraction(on_date)
                base += n * grant.grant_date_fair_value * frac
        base += self._modification_amount(on_date, full=full)
        return self.currency_id.round(base)

    def _cash_liability_at(self, on_date, fair_value):
        """Cash-settled liability at ``on_date`` (IFRS 2.30-33): instruments
        expected to vest x CURRENT fair value x vested fraction. Graded
        plans apply each tranche's own vested fraction to its portion."""
        self.ensure_one()
        total = 0.0
        for grant in self.grant_ids:
            n = grant._expected_to_vest()
            if not n:
                continue
            if self.graded_vesting:
                for tranche in self.tranche_ids:
                    total += (n * (tranche.portion_pct / 100.0)
                              * fair_value
                              * tranche._vested_fraction(on_date))
            else:
                total += n * fair_value * self._service_fraction(on_date)
        return self.currency_id.round(total)

    # ------------------------------------------------------------------
    # freeze / state guards
    # ------------------------------------------------------------------
    # Measurement inputs frozen once a period run has posted: editing them
    # in place would silently move the cumulative formula away from the
    # posted expense. Estimates (forfeiture, outcomes) stay updatable on
    # the grants: the true-up flows through the next period run.
    _FROZEN_FIELDS = (
        'settlement', 'condition_kind', 'grant_date', 'vesting_years',
        'vesting_months', 'graded_vesting', 'expense_account_id',
        'equity_account_id', 'liability_account_id',
        'settlement_account_id', 'journal_id', 'company_id',
    )

    def _has_posted_run(self):
        self.ensure_one()
        return bool(self.run_ids.filtered(lambda r: r.state == 'posted'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.sbp.plan') or '/'
        return super().create(vals_list)

    def write(self, vals):
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        if frozen:
            locked = self.filtered(lambda p: p._has_posted_run())
            if locked:
                raise UserError(_(
                    "Measurement inputs (%(fields)s) are frozen once a "
                    "period run has posted on %(plans)s. Estimates true up "
                    "through the next period run; terms change only through "
                    "a Modification (IFRS 2.27).",
                    fields=', '.join(frozen),
                    plans=', '.join(locked.mapped('display_name'))))
        # STATE transitions are enforced by the inherited eh.workflow.guard,
        # which blocks a non-superuser direct write; the sanctioned actions
        # (Activate, Settle, Cancel) run under sudo. No separate su-side arm is
        # needed - provenance is env.su, not a forgeable context key.
        return super().write(vals)

    def unlink(self):
        blocked = self.filtered(
            lambda p: p.move_ids or p.state in ('active', 'settled')
            or any(r.state == 'posted' for r in p.run_ids))
        if blocked:
            raise UserError(_(
                "A plan with posted entries or an active/settled plan "
                "cannot be deleted; cancel it instead."))
        return super().unlink()

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def action_activate(self):
        self.ensure_one()
        self._check_manager()
        if self.state != 'draft':
            raise UserError(_("Only a draft plan can be activated."))
        if not self.grant_ids:
            raise UserError(_(
                "Add at least one grant before activating %s.",
                self.display_name))
        if self.settlement == 'equity':
            self._validate_accounts(['expense', 'equity'])
        else:
            self._validate_accounts(['expense', 'liability', 'settlement'])
        if self.graded_vesting:
            if not self.tranche_ids:
                raise UserError(_(
                    "A graded-vesting plan needs its tranche lines "
                    "(portion, vesting end date and fair value)."))
            total = sum(self.tranche_ids.mapped('portion_pct'))
            if float_compare(total, 100.0, precision_digits=2) != 0:
                raise UserError(_(
                    "Tranche portions must sum to 100%% "
                    "(currently %(total).4f%%).", total=total))
            for tranche in self.tranche_ids:
                if not tranche.vesting_end_date \
                        or tranche.vesting_end_date <= self.grant_date:
                    raise UserError(_(
                        "Every tranche needs a vesting end date after the "
                        "grant date."))
                if self.settlement == 'equity' and tranche.fair_value <= 0:
                    raise UserError(_(
                        "Every tranche of an equity-settled graded plan "
                        "needs its own grant-date fair value "
                        "(IFRS 2.IG11)."))
        else:
            if self._total_vesting_months() <= 0:
                raise UserError(_(
                    "Set a vesting period (years/months) before "
                    "activating."))
            if self.settlement == 'equity' and any(
                    g.grant_date_fair_value <= 0 for g in self.grant_ids):
                raise UserError(_(
                    "Every grant of an equity-settled plan needs a "
                    "grant-date fair value (IFRS 2.19). Use the valuation "
                    "helper or key it manually."))
        self.sudo().state = 'active'
        return True

    def action_cancel(self):
        """IFRS 2.28(a): a cancellation during the vesting period is
        accounted for as an acceleration of vesting: the amount that would
        otherwise have been recognised over the remainder of the vesting
        period is recognised immediately. The full measure keeps the
        CURRENT forfeiture-adjusted expected count and all modification
        incremental fair value granted to date."""
        self.ensure_one()
        self._check_manager()
        if self.state == 'draft':
            self.sudo().state = 'cancelled'
            return True
        if self.state != 'active':
            raise UserError(_("Only a draft or active plan can be "
                              "cancelled."))
        if self.settlement == 'cash':
            if not self.currency_id.is_zero(self.recognised_cumulative):
                raise UserError(_(
                    "Settle the cash-settled liability before cancelling "
                    "%s.", self.display_name))
            self.sudo().state = 'cancelled'
            return True
        today = fields.Date.context_today(self)
        full = self._equity_cumulative_at(today, full=True)
        remaining = self.currency_id.round(
            full - self.recognised_cumulative)
        if self.currency_id.compare_amounts(remaining, 0.0) > 0:
            self._post_move([
                (self.expense_account_id, remaining, 0.0,
                 _("Cancellation acceleration %s", self.name)),
                (self.equity_account_id, 0.0, remaining,
                 _("SBP reserve on cancellation %s", self.name)),
            ], date=today)
            # recognised_cumulative is guarded (see _eh_guarded_fields); the
            # sanctioned action writes it under su.
            self.sudo().recognised_cumulative = full
        self.sudo().state = 'cancelled'
        return True

    def action_settle(self):
        self.ensure_one()
        self._check_manager()
        if self.state != 'active':
            raise UserError(_("Only an active plan can be settled."))
        currency = self.currency_id
        today = fields.Date.context_today(self)
        if self.settlement == 'equity':
            # Exercise/share-issue mechanics move amounts WITHIN equity and
            # are entity-specific policy; the reserve stays in equity
            # (IFRS 2.23 does not require a transfer). The plan closes once
            # vesting has run its course.
            if self.vesting_end_date and today < self.vesting_end_date:
                raise UserError(_(
                    "An equity-settled plan is settled after its vesting "
                    "end (%s). Cancel it to accelerate instead.",
                    self.vesting_end_date))
            self.sudo().state = 'settled'
            return True
        # Cash-settled: remeasure the liability to the settlement amount
        # through expense (IFRS 2.30), then pay it.
        self._validate_accounts(['expense', 'liability', 'settlement'])
        amount = currency.round(self.settlement_amount)
        if currency.compare_amounts(amount, 0.0) < 0:
            raise UserError(_("The settlement amount cannot be negative."))
        delta = currency.round(amount - self.recognised_cumulative)
        if not currency.is_zero(delta):
            if currency.compare_amounts(delta, 0.0) > 0:
                self._post_move([
                    (self.expense_account_id, delta, 0.0,
                     _("Settlement remeasurement %s", self.name)),
                    (self.liability_account_id, 0.0, delta,
                     _("SAR liability true-up %s", self.name)),
                ], date=today)
            else:
                self._post_move([
                    (self.liability_account_id, -delta, 0.0,
                     _("SAR liability true-down %s", self.name)),
                    (self.expense_account_id, 0.0, -delta,
                     _("Settlement remeasurement %s", self.name)),
                ], date=today)
        if currency.compare_amounts(amount, 0.0) > 0:
            self._post_move([
                (self.liability_account_id, amount, 0.0,
                 _("SAR liability settled %s", self.name)),
                (self.settlement_account_id, 0.0, amount,
                 _("Settlement paid %s", self.name)),
            ], date=today)
        # recognised_cumulative is guarded (see _eh_guarded_fields); the
        # sanctioned settle action writes it under su.
        self.sudo().recognised_cumulative = 0.0
        self.settlement_amount = 0.0
        self.sudo().state = 'settled'
        return True

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_sbp_plan_id', '=', self.id)],
        }

    def action_view_runs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Period Runs"),
            'res_model': 'eh.sbp.period.run',
            'view_mode': 'list,form',
            'domain': [('plan_id', '=', self.id)],
            'context': {'default_plan_id': self.id},
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post share-based "
                "payment entries."))

    def _validate_accounts(self, needed):
        self.ensure_one()
        field_map = {
            'expense': ('expense_account_id', _("expense account")),
            'equity': ('equity_account_id', _("equity reserve account")),
            'liability': ('liability_account_id', _("liability account")),
            'settlement': ('settlement_account_id',
                           _("settlement account")),
        }
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        for key in needed:
            fname, label = field_map[key]
            if not self[fname]:
                missing.append(label)
        if missing:
            raise UserError(_(
                "Configure the %s on plan %s first.",
                ', '.join(missing), self.display_name))

    def _post_move(self, legs, date=None, ref=None):
        self.ensure_one()
        lines = []
        for account, debit, credit, label in legs:
            lines.append((0, 0, {
                'name': label, 'account_id': account.id,
                'debit': debit, 'credit': credit,
            }))
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': date or fields.Date.context_today(self),
            'journal_id': self.journal_id.id,
            'ref': ref or self.name,
            'eh_sbp_plan_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class EhSbpPlanTranche(models.Model):
    _name = 'eh.sbp.plan.tranche'
    _description = "Graded-vesting tranche (IFRS 2.IG11)"
    _order = 'vesting_end_date, id'

    plan_id = fields.Many2one(
        'eh.sbp.plan', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='plan_id.company_id', store=True, index=True)
    name = fields.Char(required=True, string="Description",
                       default="Tranche")
    portion_pct = fields.Float(
        digits=(16, 6), required=True, string="Portion %",
        help="Share of each grant's instruments vesting in this tranche. "
             "Portions must sum to 100.")
    vesting_end_date = fields.Date(
        required=True,
        help="This tranche's own vesting end; it is expensed straight-line "
             "from the grant date to this date (IFRS 2.IG11).")
    fair_value = fields.Float(
        digits=(16, 4), string="Fair Value / Instrument",
        help="Grant-date fair value per instrument for this tranche. "
             "Longer-vesting tranches usually carry a different fair value "
             "(IFRS 2.IG11). Used for equity-settled plans; cash-settled "
             "plans remeasure at the run's current fair value.")

    _sql_constraints = [
        ('check_portion', 'CHECK (portion_pct > 0)', 'A tranche portion must be positive.'),
        ('check_fv', 'CHECK (fair_value >= 0)', 'A tranche fair value cannot be negative.'),
    ]

    def _vested_fraction(self, on_date):
        """Whole months elapsed from the PLAN grant date over this
        tranche's own vesting months, capped at 1."""
        self.ensure_one()
        total = _months_between(self.plan_id.grant_date,
                                self.vesting_end_date)
        if total <= 0:
            return 1.0
        return min(
            _months_between(self.plan_id.grant_date, on_date) / total, 1.0)

    # Tranches feed the posted cumulative formula, so they freeze once a
    # run posts: guard create, write AND unlink (a raw line edit would
    # silently desync the recognised expense).
    def _check_plan_open(self, plans=None):
        plans = plans if plans is not None else self.mapped('plan_id')
        locked = plans.filtered(lambda p: p._has_posted_run())
        if locked:
            raise UserError(_(
                "The tranches of %s are frozen once a period run has "
                "posted.", ', '.join(locked.mapped('display_name'))))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_plan_open(self.env['eh.sbp.plan'].browse(
            [v['plan_id'] for v in vals_list if v.get('plan_id')]))
        return super().create(vals_list)

    def write(self, vals):
        self._check_plan_open()
        if vals.get('plan_id'):
            self._check_plan_open(
                self.env['eh.sbp.plan'].browse(vals['plan_id']))
        return super().write(vals)

    def unlink(self):
        self._check_plan_open()
        return super().unlink()


class EhSbpGrant(models.Model):
    _name = 'eh.sbp.grant'
    _description = "Share-based payment grant (IFRS 2)"
    _order = 'id'
    _rec_name = 'partner_id'

    plan_id = fields.Many2one(
        'eh.sbp.plan', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='plan_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='plan_id.currency_id', store=True, readonly=True)
    partner_id = fields.Many2one(
        'res.partner', required=True, string="Grantee",
        help="The grantee as a partner. Employees are partners in Odoo, so "
             "this avoids a hard hr dependency; with hr installed, pick "
             "the employee's related partner.")
    instruments_granted = fields.Integer(required=True)
    grant_date_fair_value = fields.Float(
        digits=(16, 4), string="Grant-date FV / Instrument",
        help="Fair value per instrument at grant date (IFRS 2.19). Market "
             "conditions are baked into this value (IFRS 2.21). Fill from "
             "a valuation record or key it manually. Ignored for graded "
             "plans (each tranche carries its own FV) and for cash-settled "
             "plans (remeasured at each run).")
    exercise_price = fields.Float(
        digits=(16, 4), string="Exercise Price",
        help="Strike per instrument, used for the WAEP disclosure "
             "(IFRS 2.45(b)). Zero for free shares / SARs.")
    valuation_id = fields.Many2one(
        'eh.sbp.valuation', string="Valuation",
        help="Option valuation whose result can be pulled into the "
             "grant-date fair value.")

    expected_forfeiture_pct = fields.Float(
        digits=(6, 3), string="Expected Forfeiture %",
        help="Current estimate of the portion NOT expected to vest for "
             "service/non-market reasons. Updatable while vesting runs; "
             "the change flows through the next period run (IFRS 2.20).")
    forfeited = fields.Boolean(
        help="The grantee left before vesting (service condition failed): "
             "the grant's cumulative expense reverses through the next "
             "run.")
    condition_failed = fields.Boolean(
        string="Performance Condition Failed",
        help="A failed NON-MARKET condition reverses the expense in full. "
             "A failed MARKET condition changes nothing: the expense "
             "stands once service is rendered (IFRS 2.21 vs 2.23 - the "
             "market outcome was priced into the grant-date fair value).")
    vesting_finalised = fields.Boolean(
        help="Vesting has completed and the actual outcome below replaces "
             "the estimate (final true-up). For market-condition plans the "
             "actual count is the instruments whose SERVICE condition was "
             "met, whatever the market outcome (IFRS 2.21).")
    actual_vested_qty = fields.Integer(
        string="Actual Vested",
        help="Instruments that actually vested (service rendered). Used "
             "once vesting is finalised.")
    exercised_qty = fields.Integer(
        string="Exercised",
        help="Instruments exercised / settled to date (disclosure "
             "rollforward, IFRS 2.45).")
    expired_qty = fields.Integer(
        string="Expired",
        help="Vested instruments that lapsed unexercised (disclosure "
             "rollforward, IFRS 2.45).")

    forfeited_qty = fields.Integer(
        compute='_compute_rollforward', string="Forfeited (actual)")
    outstanding_qty = fields.Integer(
        compute='_compute_rollforward', string="Outstanding")

    notes = fields.Char()

    _sql_constraints = [
        ('check_granted', 'CHECK (instruments_granted > 0)', 'A grant must carry a positive number of instruments.'),
        ('check_fv', 'CHECK (grant_date_fair_value >= 0 AND exercise_price >= 0)', 'Fair value and exercise price cannot be negative.'),  # noqa: E501
        ('check_quantities', 'CHECK (actual_vested_qty >= 0 AND exercised_qty >= 0 '
        'AND expired_qty >= 0)', 'Grant quantities cannot be negative.'),  # noqa: E128
    ]

    @api.constrains('expected_forfeiture_pct')
    def _check_forfeiture_range(self):
        for grant in self:
            if not 0.0 <= grant.expected_forfeiture_pct <= 100.0:
                raise ValidationError(_(
                    "The expected forfeiture must lie between 0 and 100%%."))

    @api.constrains('actual_vested_qty', 'instruments_granted',
                    'exercised_qty', 'expired_qty')
    def _check_quantity_caps(self):
        for grant in self:
            if grant.actual_vested_qty > grant.instruments_granted:
                raise ValidationError(_(
                    "Actual vested instruments cannot exceed the granted "
                    "count."))
            if (grant.exercised_qty + grant.expired_qty
                    > grant.instruments_granted):
                raise ValidationError(_(
                    "Exercised plus expired instruments cannot exceed the "
                    "granted count."))

    def _expected_to_vest(self):
        """Instruments expected to vest for measurement (IFRS 2.19-21).

        * service forfeiture (grantee left): zero, always trued up;
        * failed non-market condition: zero (full reversal, the IFRS 2.23
          contrast case);
        * failed MARKET condition: NO adjustment (IFRS 2.21) - the flag is
          recorded for the register but the count ignores it;
        * finalised: the actual vested count (for market plans: the count
          that completed service);
        * otherwise: granted x (1 - current forfeiture estimate).
        """
        self.ensure_one()
        plan = self.plan_id
        if self.forfeited:
            return 0.0
        if self.condition_failed and plan.condition_kind == 'non_market':
            return 0.0
        if self.vesting_finalised:
            return float(self.actual_vested_qty)
        return self.instruments_granted * (
            1.0 - (self.expected_forfeiture_pct or 0.0) / 100.0)

    @api.depends('instruments_granted', 'forfeited', 'condition_failed',
                 'vesting_finalised', 'actual_vested_qty', 'exercised_qty',
                 'expired_qty', 'plan_id.condition_kind')
    def _compute_rollforward(self):
        for grant in self:
            if grant.forfeited or (
                    grant.condition_failed
                    and grant.plan_id.condition_kind == 'non_market'):
                forfeited = grant.instruments_granted
            elif grant.vesting_finalised:
                forfeited = max(
                    grant.instruments_granted - grant.actual_vested_qty, 0)
            else:
                forfeited = 0
            grant.forfeited_qty = forfeited
            grant.outstanding_qty = max(
                grant.instruments_granted - forfeited
                - grant.exercised_qty - grant.expired_qty, 0)

    def action_use_valuation(self):
        for grant in self:
            if not grant.valuation_id:
                raise UserError(_(
                    "Link a valuation record first, then pull its result."))
            if not grant.valuation_id.result_value:
                raise UserError(_(
                    "Compute the valuation %s first.",
                    grant.valuation_id.display_name))
            grant.grant_date_fair_value = grant.valuation_id.result_value
        return True

    # Measurement fields freeze once a run posts; estimate/outcome fields
    # stay open because IFRS 2.20 trues them up through the next period.
    _POST_EDITABLE = (
        'expected_forfeiture_pct', 'forfeited', 'condition_failed',
        'vesting_finalised', 'actual_vested_qty', 'exercised_qty',
        'expired_qty', 'notes',
    )

    def _check_plan_open(self, plans=None):
        plans = plans if plans is not None else self.mapped('plan_id')
        locked = plans.filtered(lambda p: p._has_posted_run())
        if locked:
            raise UserError(_(
                "Grants of %s are frozen once a period run has posted: "
                "only the vesting estimates and outcomes stay updatable "
                "(IFRS 2.20). A new award is a new plan.",
                ', '.join(locked.mapped('display_name'))))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_plan_open(self.env['eh.sbp.plan'].browse(
            [v['plan_id'] for v in vals_list if v.get('plan_id')]))
        return super().create(vals_list)

    def write(self, vals):
        if any(f not in self._POST_EDITABLE for f in vals):
            self._check_plan_open()
            if vals.get('plan_id'):
                self._check_plan_open(
                    self.env['eh.sbp.plan'].browse(vals['plan_id']))
        return super().write(vals)

    def unlink(self):
        self._check_plan_open()
        return super().unlink()


class EhSbpModification(models.Model):
    _name = 'eh.sbp.modification'
    _description = "Share-based payment modification (IFRS 2.27)"
    _order = 'date, id'

    plan_id = fields.Many2one(
        'eh.sbp.plan', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='plan_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='plan_id.currency_id', store=True, readonly=True)
    name = fields.Char(required=True, string="Description")
    date = fields.Date(required=True,
                       default=fields.Date.context_today)
    incremental_fv = fields.Float(
        digits=(16, 4), required=True,
        string="Incremental FV / Instrument",
        help="Fair value granted by the modification, per instrument: "
             "modified-award FV minus original-award FV, both at the "
             "modification date. Expensed over the remaining vesting "
             "period (IFRS 2.27). A modification that REDUCES fair value "
             "is ignored for measurement - the original grant-date expense "
             "continues as if nothing happened - so a negative amount is "
             "refused here rather than silently booked.")

    @api.constrains('incremental_fv')
    def _check_incremental_fv(self):
        for mod in self:
            if mod.incremental_fv < 0.0:
                raise ValidationError(_(
                    "Incremental fair value cannot be negative: a "
                    "modification that reduces fair value is IGNORED for "
                    "measurement and the original expense continues "
                    "(IFRS 2.27). Record it at zero if you want it on "
                    "file."))

    @api.constrains('date', 'plan_id')
    def _check_date(self):
        for mod in self:
            if mod.plan_id.grant_date and mod.date \
                    and mod.date < mod.plan_id.grant_date:
                raise ValidationError(_(
                    "A modification cannot predate the grant."))

    @api.constrains('plan_id')
    def _check_plan_settlement(self):
        for mod in self:
            if mod.plan_id.settlement != 'equity':
                raise ValidationError(_(
                    "Modifications apply to equity-settled plans; a "
                    "cash-settled award is remeasured to current fair "
                    "value every period anyway (IFRS 2.30)."))

    # A modification already consumed by a posted run (run period end on or
    # after its date) is part of the posted cumulative and freezes.
    def _check_not_consumed(self):
        for mod in self:
            consumed = mod.plan_id.run_ids.filtered(
                lambda r: r.state == 'posted'
                and r.period_end >= mod.date)
            if consumed:
                raise UserError(_(
                    "Modification %s already feeds a posted period run and "
                    "is frozen.", mod.display_name))

    def write(self, vals):
        self._check_not_consumed()
        return super().write(vals)

    def unlink(self):
        self._check_not_consumed()
        return super().unlink()


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_sbp_plan_id = fields.Many2one(
        'eh.sbp.plan', string="SBP Plan", readonly=True, index=True,
        ondelete='restrict', copy=False)
