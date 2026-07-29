# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.provision: one IAS 37 provision or contingency.

A provision is recognised at the present value of the best estimate. The
discount unwinds each period as a finance cost that raises the carrying
amount towards the undiscounted estimate. Utilisation settles the obligation
against the provision. Contingent liabilities are disclosure-only and are
never posted.

Depth mechanics (IFRS 10/10 program, Phase 4):

* Onerous contracts (IAS 37.66-69, incl. the 2020 cost-of-fulfilment
  amendment): the provision is the lower of the net cost of fulfilling the
  contract (directly related fulfilment costs less the economic benefits
  expected under it, floored at zero) and the penalty of exiting. The best
  estimate derives from those inputs and is read-only for the onerous type
  unless a manual override with a documented reason is set.
* Restructuring (IAS 37.72, 37.80-81): recognition is gated on a detailed
  formal plan plus a valid expectation raised (announcement date and
  narrative), and the direct cost components (termination benefits,
  contract termination, other directly attributable) must sum exactly to
  the best estimate. Excluded costs (retraining, marketing, new systems)
  are registered but never enter the sum.
* Discount-rate governance: the pre-tax rate carries a basis attestation
  and a source note, both chatter-tracked; the rate and its basis are
  frozen once the provision posts and move only through Remeasure.
* Contingent assets (IAS 37.33-35): disclosure-only by default; when the
  inflow becomes virtually certain the related asset is recognised as an
  ASSET against income (never as a provision credit).
* Reimbursements (IAS 37.53): recognised as a separate asset, capped at
  the provision carrying amount, never netted against the provision
  liability. The credit is booked to the provision expense account, which
  implements the permitted NET presentation in profit or loss while the
  statement of financial position stays gross.
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# Direct restructuring cost kinds that enter the provision (IAS 37.80);
# every other kind is registered for completeness but excluded (IAS 37.81).
RESTRUCTURING_INCLUDED_KINDS = (
    'termination', 'contract_termination', 'other_direct')


class EhProvision(models.Model):
    _name = 'eh.provision'
    _description = "Provision / contingency (IAS 37)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'expected_settlement_date, id desc'
    _rec_name = 'name'

    # Workflow-critical fields. Two things may only move through the record's
    # own actions (recognise / recognise-asset / utilise / reverse / cancel /
    # unwind / remeasure / recognise-reimbursement), each of which posts the
    # required journal entry before writing the figure under sudo:
    #  * state - so a draft can never be relabelled 'recognised' straight past
    #    the posting; and
    #  * the posted subledger figures carrying_amount, utilised_amount and
    #    reimbursement_recognised - the IAS 37.84 carrying amount and the
    #    cumulative utilised / reimbursement-recognised anchors that Reverse
    #    and the deferred-tax temp-diff feed trust. A direct RPC
    #    write({'carrying_amount': ...}) by a low-privilege user (who holds
    #    perm_write on the model) would silently restate the booked figure and
    #    drive a later Reverse off a fabricated amount.
    # The shared eh.workflow.guard refuses every such write unless it
    # originates from an action (provenance proven by env.su, not a forgeable
    # context flag); the actions elevate through self._eh_workflow_action().
    _eh_guarded_fields = (
        'state', 'carrying_amount', 'utilised_amount',
        'reimbursement_recognised',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('recognised', "Recognised"),
         ('recognised_asset', "Asset Recognised"),
         ('settled', "Settled"), ('reversed', "Reversed"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    classification = fields.Selection(
        [('provision', "Provision"),
         ('contingent_liability', "Contingent liability (disclosure)"),
         ('contingent_asset', "Contingent asset (disclosure)")],
        default='provision', required=True, tracking=True,
        help="Only provisions are recognised; contingent items are disclosed "
             "and never posted (IAS 37.27-28, 86).")
    provision_type = fields.Selection(
        [('legal', "Legal"), ('constructive', "Constructive"),
         ('onerous', "Onerous contract"), ('restructuring', "Restructuring"),
         ('warranty', "Warranty"), ('other', "Other")],
        default='other', required=True)

    # ---- onerous contract measurement (IAS 37.66-69) ----
    unavoidable_cost_fulfil = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Cost of Fulfilling",
        help="Costs that relate directly to fulfilling the contract: the "
             "incremental costs plus an allocation of other costs that "
             "relate directly to it (IAS 37.68A, 2020 amendment).")
    contract_benefit_expected = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Benefits Expected",
        help="Economic benefits expected to be received under the contract; "
             "they reduce the net cost of fulfilling (IAS 37.68).")
    penalty_exit = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Penalty of Exiting",
        help="Compensation or penalty arising from failure to fulfil the "
             "contract. Zero means there is no exit option, so the net cost "
             "of fulfilling is not capped (IAS 37.68).")
    onerous_override = fields.Boolean(
        string="Manual Estimate Override", tracking=True,
        help="Tick to key the best estimate manually instead of deriving it "
             "from the onerous-contract inputs. Requires a documented "
             "reason.")
    onerous_override_reason = fields.Char(
        string="Override Reason", tracking=True,
        help="Why the derived onerous measure is overridden; kept for the "
             "audit trail.")

    # ---- restructuring recognition gate (IAS 37.72, 37.80-81) ----
    restructuring_plan = fields.Boolean(
        string="Detailed Formal Plan", tracking=True,
        help="A detailed formal plan identifying the business, locations, "
             "employees, expenditures and timing exists (IAS 37.72(a)).")
    restructuring_expectation = fields.Boolean(
        string="Valid Expectation Raised", tracking=True,
        help="Implementation has started or the main features have been "
             "announced to those affected (IAS 37.72(b)).")
    restructuring_announcement_date = fields.Date(
        string="Announcement Date", tracking=True,
        help="Date the plan was announced or implementation began.")
    restructuring_announcement = fields.Text(
        string="Announcement / Expectation Raised",
        help="How the valid expectation was raised: what was announced, to "
             "whom, and when implementation began.")
    restructuring_line_ids = fields.One2many(
        'eh.provision.restructuring.line', 'provision_id',
        string="Restructuring Cost Components")
    restructuring_component_total = fields.Monetary(
        compute='_compute_restructuring_totals',
        currency_field='currency_id', string="Direct Components Total",
        help="Sum of the direct cost components; must equal the best "
             "estimate before a restructuring provision can be recognised "
             "(IAS 37.80).")
    restructuring_excluded_total = fields.Monetary(
        compute='_compute_restructuring_totals',
        currency_field='currency_id', string="Excluded Costs Total",
        help="Costs registered for completeness but excluded from the "
             "provision: retraining, marketing, new systems (IAS 37.81).")

    best_estimate = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True,
        compute='_compute_best_estimate', store=True, readonly=False,
        precompute=True,
        help="Undiscounted best estimate of the expenditure required to "
             "settle the obligation (IAS 37.36). For an onerous contract it "
             "derives from the fulfilment/penalty inputs (lower of the net "
             "cost of fulfilling and the penalty of exiting, IAS 37.68) and "
             "is read-only unless the manual override is set.")
    discount_rate = fields.Float(
        digits=(6, 3), default=0.0, tracking=True,
        help="Pre-tax discount rate, as a percentage. Zero when the time "
             "value of money is not material.")
    rate_basis = fields.Selection(
        [('risk_free_govt', "Risk-free government rate"),
         ('entity_specific_pretax', "Entity-specific pre-tax rate"),
         ('other', "Other")],
        default='entity_specific_pretax', required=True, tracking=True,
        string="Rate Basis",
        help="Attestation of what the discount rate is: IAS 37.47 requires "
             "a pre-tax rate reflecting current market assessments of the "
             "time value of money and risks specific to the liability.")
    rate_source = fields.Char(
        string="Rate Source", tracking=True,
        help="Where the rate came from (curve, quote, internal memo "
             "reference); chatter-tracked for the audit trail.")
    periods_to_settlement = fields.Integer(
        default=0,
        help="Number of periods until settlement, used to discount the best "
             "estimate to present value.")
    expected_settlement_date = fields.Date(tracking=True)
    present_value = fields.Monetary(
        compute='_compute_present_value', store=True,
        currency_field='currency_id',
        help="Best estimate discounted to present value at the discount "
             "rate over the periods to settlement.")

    recognition_date = fields.Date(
        readonly=True, copy=False,
        help="Date the provision was recognised; the anchor for the discount "
             "unwind schedule.")
    last_unwind_date = fields.Date(
        readonly=True, copy=False,
        help="Date up to which the discount has been unwound. The next unwind "
             "accretes only the periods elapsed since this date.")
    unwound_periods = fields.Integer(
        readonly=True, copy=False, default=0,
        help="Number of whole periods already unwound, capped at the periods "
             "to settlement.")

    carrying_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Amount currently recognised as a liability.")
    utilised_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')
    utilise_amount = fields.Monetary(
        currency_field='currency_id',
        help="Amount to settle against the provision on the next Utilise "
             "action.")
    remeasure_estimate = fields.Monetary(
        currency_field='currency_id',
        help="Revised undiscounted best estimate of the obligation still "
             "outstanding. Enter it and use Remeasure to book the change in "
             "estimate to profit or loss (IAS 37.59). Ignored for an "
             "onerous contract without manual override: there the revision "
             "is staged through the revised fulfilment/penalty inputs.")
    remeasure_cost_fulfil = fields.Monetary(
        currency_field='currency_id', string="Revised Cost of Fulfilling",
        help="Stage the full revised set of onerous inputs, then Remeasure "
             "re-derives the estimate from them (IAS 37.59, 37.68).")
    remeasure_benefit_expected = fields.Monetary(
        currency_field='currency_id', string="Revised Benefits Expected")
    remeasure_penalty_exit = fields.Monetary(
        currency_field='currency_id', string="Revised Penalty of Exiting",
        help="Zero means no exit option (the net cost of fulfilling is not "
             "capped), consistent with the recognition inputs.")

    # ---- contingent asset recognition (IAS 37.33-35) ----
    virtually_certain = fields.Boolean(
        string="Inflow Virtually Certain", tracking=True,
        help="When the realisation of income is virtually certain the "
             "related asset is not a contingent asset any more and is "
             "recognised as an asset against income (IAS 37.33). Until "
             "then the item stays disclosure-only.")

    # ---- reimbursement asset (IAS 37.53) ----
    reimbursement_partner_id = fields.Many2one(
        'res.partner', string="Reimbursement Counterparty", tracking=True,
        help="Insurer, indemnifier or supplier warranty expected to "
             "reimburse part or all of the expenditure.")
    reimbursement_amount = fields.Monetary(
        currency_field='currency_id', string="Reimbursement to Recognise",
        help="Expected reimbursement that is virtually certain to be "
             "received. Recognised as a SEPARATE asset, never netted "
             "against the provision; capped at the provision carrying "
             "amount (IAS 37.53).")
    reimbursement_recognised = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        string="Reimbursement Recognised", tracking=True,
        help="Cumulative reimbursement asset recognised against this "
             "provision. The P&L credit goes to the provision expense "
             "account (permitted net presentation, IAS 37.54); the balance "
             "sheet keeps the asset and the liability gross.")

    # ---- accounts ----
    provision_account_id = fields.Many2one(
        'account.account', string="Provision Liability Account", tracking=True,
        domain="[('account_type', 'in', "
               "['liability_current', 'liability_non_current'])]")
    expense_account_id = fields.Many2one(
        'account.account', string="Expense Account", tracking=True,
        domain="[('account_type', 'in', ['expense', 'expense_direct_cost'])]")
    finance_cost_account_id = fields.Many2one(
        'account.account', string="Finance Cost Account", tracking=True,
        domain="[('account_type', '=', 'expense')]")
    settlement_account_id = fields.Many2one(
        'account.account', string="Settlement Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'liability_payable', 'asset_current'])]")
    asset_account_id = fields.Many2one(
        'account.account', string="Asset / Receivable Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_receivable', 'asset_current', 'asset_non_current'])]",
        help="Debited when a virtually certain contingent asset is "
             "recognised (IAS 37.33).")
    income_account_id = fields.Many2one(
        'account.account', string="Income Account", tracking=True,
        domain="[('account_type', 'in', ['income', 'income_other'])]",
        help="Credited when a virtually certain contingent asset is "
             "recognised.")
    reimbursement_account_id = fields.Many2one(
        'account.account', string="Reimbursement Asset Account",
        tracking=True,
        domain="[('account_type', 'in', "
               "['asset_receivable', 'asset_current', 'asset_non_current'])]",
        help="Debited when a reimbursement asset is recognised as a "
             "separate asset (IAS 37.53).")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]")

    move_ids = fields.One2many('account.move', 'eh_provision_id')
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    _sql_constraints = [
        ('check_estimate', 'CHECK (best_estimate >= 0)', 'Best estimate cannot be negative.'),
        ('check_rate', 'CHECK (discount_rate >= 0)', 'Discount rate cannot be negative.'),
        ('check_periods', 'CHECK (periods_to_settlement >= 0)', 'Periods to settlement cannot be negative.'),
        ('check_onerous_inputs', 'CHECK (unavoidable_cost_fulfil >= 0 AND penalty_exit >= 0 '
        'AND contract_benefit_expected >= 0)', 'Onerous contract inputs cannot be negative.'),
    ]

    # ---- onerous measurement (IAS 37.66-69) ----

    def _onerous_inputs_set(self):
        """Any onerous input keyed. All-zero inputs mean the engine is not
        in use (legacy onerous records keep their manual estimate)."""
        self.ensure_one()
        return bool(self.unavoidable_cost_fulfil or self.penalty_exit
                    or self.contract_benefit_expected)

    def _onerous_measure(self, fulfil=None, benefit=None, penalty=None):
        """Lower of the net cost of fulfilling and the penalty of exiting.

        net cost of fulfilling = max(fulfil costs - benefits expected, 0)
        measure = min(net cost, penalty) when an exit penalty exists,
        otherwise the net cost alone (no exit option, IAS 37.68).
        """
        self.ensure_one()
        fulfil = self.unavoidable_cost_fulfil if fulfil is None else fulfil
        benefit = (self.contract_benefit_expected if benefit is None
                   else benefit)
        penalty = self.penalty_exit if penalty is None else penalty
        net_fulfil = max(fulfil - benefit, 0.0)
        measure = min(net_fulfil, penalty) if penalty else net_fulfil
        return self.currency_id.round(measure) if self.currency_id \
            else measure

    @api.depends('provision_type', 'onerous_override',
                 'unavoidable_cost_fulfil', 'contract_benefit_expected',
                 'penalty_exit')
    def _compute_best_estimate(self):
        for p in self:
            if (p.provision_type == 'onerous' and not p.onerous_override
                    and p._onerous_inputs_set()):
                p.best_estimate = p._onerous_measure()
            else:
                # Editable-compute idiom: every other type keeps the keyed
                # estimate.
                p.best_estimate = p.best_estimate

    @api.constrains('best_estimate', 'provision_type', 'onerous_override',
                    'unavoidable_cost_fulfil', 'contract_benefit_expected',
                    'penalty_exit')
    def _check_onerous_best_estimate(self):
        # Guardrail, not honour-system: a hand-keyed best estimate on an
        # onerous contract without override cannot diverge from the derived
        # measure (a create() passing best_estimate bypasses the compute).
        for p in self:
            if (p.provision_type == 'onerous' and not p.onerous_override
                    and p._onerous_inputs_set()
                    and p.currency_id.compare_amounts(
                        p.best_estimate, p._onerous_measure()) != 0):
                raise ValidationError(_(
                    "The best estimate of an onerous contract derives from "
                    "its inputs: lower of the net cost of fulfilling and "
                    "the penalty of exiting (IAS 37.68). Set the manual "
                    "override (with a reason) to key it directly."))

    @api.constrains('onerous_override', 'onerous_override_reason')
    def _check_onerous_override_reason(self):
        for p in self:
            if p.onerous_override and not p.onerous_override_reason:
                raise ValidationError(_(
                    "Document why the derived onerous measure is being "
                    "overridden (audit trail)."))

    # ---- restructuring totals (IAS 37.80-81) ----

    @api.depends('restructuring_line_ids.amount',
                 'restructuring_line_ids.cost_kind')
    def _compute_restructuring_totals(self):
        for p in self:
            included = excluded = 0.0
            for line in p.restructuring_line_ids:
                if line.cost_kind in RESTRUCTURING_INCLUDED_KINDS:
                    included += line.amount
                else:
                    excluded += line.amount
            if p.currency_id:
                included = p.currency_id.round(included)
                excluded = p.currency_id.round(excluded)
            p.restructuring_component_total = included
            p.restructuring_excluded_total = excluded

    def _check_restructuring_gate(self):
        """IAS 37.72 recognition gate for a restructuring provision."""
        self.ensure_one()
        if not self.restructuring_plan or not self.restructuring_expectation:
            raise UserError(_(
                "A restructuring provision is recognised only with a "
                "detailed formal plan and a valid expectation in those "
                "affected that it will be carried out (IAS 37.72). Confirm "
                "both checklist items on %s.", self.display_name))
        if not self.restructuring_announcement_date \
                or not self.restructuring_announcement:
            raise UserError(_(
                "Record the announcement date and how the valid "
                "expectation was raised (announcement of the main features "
                "or start of implementation, IAS 37.72) on %s.",
                self.display_name))
        included = self.restructuring_line_ids.filtered(
            lambda l: l.cost_kind in RESTRUCTURING_INCLUDED_KINDS)
        if not included:
            raise UserError(_(
                "Break %s into its direct cost components (termination "
                "benefits, contract termination, other directly "
                "attributable costs) before recognition (IAS 37.80).",
                self.display_name))
        total = self.currency_id.round(sum(included.mapped('amount')))
        if self.currency_id.compare_amounts(
                total, self.best_estimate) != 0:
            raise UserError(_(
                "The direct restructuring components (%(total).2f) must sum "
                "to the best estimate (%(estimate).2f). Excluded costs such "
                "as retraining, marketing or investment in new systems "
                "never enter the provision (IAS 37.81).",
                total=total, estimate=self.best_estimate))

    @api.depends('best_estimate', 'discount_rate', 'periods_to_settlement')
    def _compute_present_value(self):
        for p in self:
            rate = (p.discount_rate or 0.0) / 100.0
            n = p.periods_to_settlement or 0
            pv = p.best_estimate / ((1.0 + rate) ** n) if rate and n else \
                p.best_estimate
            p.present_value = p.currency_id.round(pv) if p.currency_id else pv

    def _compute_move_count(self):
        for p in self:
            p.move_count = len(p.move_ids)

    def eh_deferred_tax_temp_diffs(self, reporting_date):
        """IAS 12 producer hook for the eh_account_deferred_tax seam.

        A recognised provision is a DEDUCTIBLE temporary difference: tax relief
        comes when the obligation is paid, not when it is provided, so the
        accounting carrying amount (present value) exceeds the tax base of nil.
        Emitted as a liability with carrying = present value and tax base 0,
        which the deferred-tax run turns into a DTA of carrying x rate. Only
        live (recognised) provisions are emitted.
        """
        out = []
        for p in self.filtered(lambda r: r.state == 'recognised'):
            pv = p.present_value or p.carrying_amount or 0.0
            if not pv:
                continue
            out.append({
                'name': _("Provision: %s", p.display_name),
                'category': 'provision',
                'nature': 'liability',
                'carrying_amount': pv,
                'tax_base': 0.0,
                'through_oci': False,
            })
        return out

    # Measurement inputs frozen once the provision is posted. Re-measuring a
    # recognised provision would silently move present_value away from the
    # posted carrying amount (IAS 37: a recognised provision is remeasured
    # only through the unwind/utilise/reversal postings, never by editing the
    # inputs in place).
    _FROZEN_FIELDS = (
        'best_estimate', 'discount_rate', 'periods_to_settlement',
        'expected_settlement_date', 'classification', 'provision_type',
        # onerous inputs feed the computed best estimate; a raw edit after
        # posting would silently move it through the compute (which runs
        # below the write() guard), so the inputs freeze with it.
        'unavoidable_cost_fulfil', 'contract_benefit_expected',
        'penalty_exit', 'onerous_override', 'onerous_override_reason',
        # discount-rate attestation and the gates that justified posting.
        'rate_basis', 'restructuring_plan', 'restructuring_expectation',
        'restructuring_announcement_date', 'restructuring_announcement',
        'virtually_certain',
    )

    # States in which the measurement is posted and therefore frozen.
    _FROZEN_STATES = ('recognised', 'recognised_asset', 'settled')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.provision') or '/'
        return super().create(vals_list)

    def write(self, vals):
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        # action_remeasure books a change in estimate through the ledger and
        # then updates best_estimate under this flag; a raw edit of the inputs
        # in place stays blocked.
        if frozen and not self.env.context.get('eh_provision_remeasure'):
            posted = self.filtered(
                lambda p: p.state in self._FROZEN_STATES)
            if posted:
                raise UserError(_(
                    "Measurement inputs (%(fields)s) are frozen on a posted "
                    "provision. Use Remeasure to book a change in estimate "
                    "through profit or loss, or reverse it (IAS 37.59).",
                    fields=', '.join(frozen)))
        # The state of a recognised provision is a control point: a raw ORM
        # write of {'state': 'reversed'/'settled'/'cancelled'} would relabel it
        # without the utilise / reverse / cancel journal entry the action
        # methods post, leaving the liability standing while the record reads
        # settled. Force the transition through the sanctioned actions, which
        # carry the flag after posting the GL movement.
        if 'state' in vals \
                and not self.env.context.get('eh_provision_state_change'):
            crossing = self.filtered(
                lambda p: p.state in self._FROZEN_STATES
                and p.state != vals['state'])
            if crossing:
                raise UserError(_(
                    "Change a recognised provision through its actions "
                    "(Utilise, Reverse or Cancel), which post the required "
                    "journal entry. Its state cannot be re-keyed directly."))
        return super().write(vals)

    def unlink(self):
        # A provision that has posted moves (recognised, then settled or
        # reversed) must not be deleted: the master carries the posting-move
        # link and deleting it would orphan a posted GL entry. Cancel it
        # instead. Draft and cancelled provisions have no posted move and
        # remain deletable.
        posted = self.filtered(
            lambda p: p.state in (
                'recognised', 'recognised_asset', 'settled', 'reversed'))
        if posted:
            raise UserError(_(
                "A recognised provision cannot be deleted; its journal "
                "entries would be orphaned. Reverse or utilise it and then "
                "cancel it instead."))
        return super().unlink()

    @api.constrains('classification', 'state', 'virtually_certain')
    def _check_contingent_not_recognised(self):
        for p in self:
            if p.classification != 'provision' \
                    and p.state in ('recognised', 'settled'):
                raise ValidationError(_(
                    "%s is a contingent item and cannot be recognised; it is "
                    "disclosure-only (IAS 37.27-28).", p.display_name))
            if p.state == 'recognised_asset' and (
                    p.classification != 'contingent_asset'
                    or not p.virtually_certain):
                raise ValidationError(_(
                    "%s: the Asset Recognised state is reserved for a "
                    "contingent asset whose inflow is virtually certain "
                    "(IAS 37.33).", p.display_name))

    # ---- actions ----

    def action_recognise(self):
        self = self._eh_workflow_action()
        self.ensure_one()
        self._check_manager()
        if self.classification != 'provision':
            raise UserError(_(
                "Contingent items are disclosed, not recognised."))
        if self.state != 'draft':
            raise UserError(_("Only draft provisions can be recognised."))
        if self.provision_type == 'onerous' and not self.onerous_override \
                and not self._onerous_inputs_set():
            raise UserError(_(
                "Measure the onerous contract first: enter the cost of "
                "fulfilling, the benefits expected and any exit penalty "
                "(IAS 37.66-68), or set the manual override with a "
                "documented reason."))
        if self.provision_type == 'restructuring':
            self._check_restructuring_gate()
        self._validate_accounts(['provision', 'expense'])
        amount = self.present_value
        if self.currency_id.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Nothing to recognise: present value is nil."))
        self._post_move([
            (self.expense_account_id, amount, 0.0,
             _("Provision expense %s", self.name)),
            (self.provision_account_id, 0.0, amount,
             _("Provision recognised %s", self.name)),
        ])
        today = fields.Date.context_today(self)
        self.write({
            'state': 'recognised', 'carrying_amount': amount,
            'recognition_date': today, 'last_unwind_date': today,
            'unwound_periods': 0,
        })
        return True

    def action_recognise_asset(self):
        """IAS 37.33-35: when the realisation of income is virtually
        certain, the related asset is not a contingent asset any more; it
        is recognised as an ASSET against income. The credit never touches
        a provision liability account. Without the flag the item stays
        disclosure-only and posting is refused."""
        self = self._eh_workflow_action()
        self.ensure_one()
        self._check_manager()
        if self.classification != 'contingent_asset':
            raise UserError(_(
                "Only a contingent asset can be recognised as an asset "
                "(IAS 37.33)."))
        if self.state != 'draft':
            raise UserError(_(
                "Only a draft contingent asset can be recognised."))
        if not self.virtually_certain:
            raise UserError(_(
                "A contingent asset is disclosure-only until the inflow is "
                "virtually certain (IAS 37.31-33). Attest 'Inflow Virtually "
                "Certain' on %s before recognising the asset.",
                self.display_name))
        self._validate_accounts(['asset', 'income'])
        amount = self.present_value
        if self.currency_id.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Nothing to recognise: present value is nil."))
        self._post_move([
            (self.asset_account_id, amount, 0.0,
             _("Asset recognised (inflow virtually certain) %s", self.name)),
            (self.income_account_id, 0.0, amount,
             _("Income on asset recognition %s", self.name)),
        ])
        self.write({
            'state': 'recognised_asset', 'carrying_amount': amount,
            'recognition_date': fields.Date.context_today(self),
        })
        return True

    def action_recognise_reimbursement(self):
        """IAS 37.53: a virtually certain reimbursement is recognised as a
        SEPARATE asset, capped at the provision carrying amount, and never
        netted against the provision liability. The credit goes to the
        provision expense account: that is the permitted NET presentation
        in profit or loss (IAS 37.54), while the statement of financial
        position keeps the asset and the liability gross."""
        # Elevate first: this action writes the guarded reimbursement_recognised
        # anchor, which the shared eh.workflow.guard only accepts under sudo.
        self = self._eh_workflow_action()
        self.ensure_one()
        self._check_manager()
        if self.classification != 'provision' or self.state != 'recognised':
            raise UserError(_(
                "A reimbursement asset is recognised against a recognised "
                "provision (IAS 37.53)."))
        self._validate_accounts(['reimbursement', 'expense'])
        currency = self.currency_id
        amount = currency.round(self.reimbursement_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_(
                "Enter a positive reimbursement amount to recognise."))
        if currency.compare_amounts(
                self.reimbursement_recognised + amount,
                self.carrying_amount) > 0:
            raise UserError(_(
                "The reimbursement recognised (%(total).2f) cannot exceed "
                "the provision carrying amount of %(cap).2f (IAS 37.53).",
                total=self.reimbursement_recognised + amount,
                cap=self.carrying_amount))
        self._post_move([
            (self.reimbursement_account_id, amount, 0.0,
             _("Reimbursement asset %s", self.name)),
            (self.expense_account_id, 0.0, amount,
             _("Reimbursement (net P&L presentation) %s", self.name)),
        ])
        self.reimbursement_recognised += amount
        self.reimbursement_amount = 0.0
        return True

    def _elapsed_unwind_periods(self):
        """New whole periods that have fallen due and are not yet unwound.

        The unwind schedule runs from ``recognition_date`` to
        ``expected_settlement_date`` in ``periods_to_settlement`` equal steps.
        The number of whole periods due by today is counted from the
        recognition date (drift-free), and the periods already recognised
        (``unwound_periods``) are subtracted, so a repeat click inside the
        same period returns zero and accretes nothing.

        Returns ``None`` when the elapsed-time basis is not derivable (no
        expected settlement date to fix the period length): the caller then
        falls back to the prior per-click, one-period behaviour so existing
        provisions keep working (opt-in-safe).
        """
        self.ensure_one()
        boundaries = self._unwind_boundaries()
        if boundaries is None:
            return None
        today = fields.Date.context_today(self)
        # Whole periods due = number of period boundaries that today has
        # reached or passed. Boundaries exclude the recognition date itself.
        due = sum(1 for b in boundaries if b <= today)
        remaining = (self.periods_to_settlement or 0) - self.unwound_periods
        return max(0, min(due - self.unwound_periods, remaining))

    def _unwind_boundaries(self):
        """Dates on which each unwind period falls due, or ``None``.

        Splits the span from ``recognition_date`` to
        ``expected_settlement_date`` into ``periods_to_settlement`` equal
        steps and returns the end date of each step. ``None`` when the horizon
        is not pinned to a real date, so time-based proration cannot apply.
        """
        self.ensure_one()
        n = self.periods_to_settlement or 0
        start = self.recognition_date
        end = self.expected_settlement_date
        if n <= 0 or not start or not end or end <= start:
            return None
        total_days = (end - start).days
        if total_days <= 0:
            return None
        # Boundary k (1..n) sits at start + round(k/n of the total span);
        # boundary n lands exactly on the settlement date.
        return [
            start + relativedelta(days=int(round(total_days * k / n)))
            for k in range(1, n + 1)
        ]

    def action_unwind(self):
        # Elevate first: this action writes the guarded carrying_amount anchor,
        # which the shared eh.workflow.guard only accepts under sudo.
        self = self._eh_workflow_action()
        self.ensure_one()
        self._check_manager()
        if self.state != 'recognised':
            raise UserError(_(
                "Unwinding applies to a recognised provision."))
        self._validate_accounts(['provision', 'finance_cost'])
        currency = self.currency_id
        rate = (self.discount_rate or 0.0) / 100.0
        if not rate:
            raise UserError(_(
                "No discount to unwind (the discount rate is nil)."))

        periods = self._elapsed_unwind_periods()
        if periods is not None:
            # Time-based path: accrete one compounded step per whole period
            # that has fallen due since the last unwind. A repeat click in the
            # same period yields zero elapsed periods and is refused, so the
            # same period can never be unwound twice.
            if periods <= 0:
                raise UserError(_(
                    "No new period has fallen due since the last unwind of "
                    "%s. The discount unwinds as time passes; it cannot be "
                    "unwound again for a period already recognised.",
                    self.display_name))
            interest = 0.0
            carrying = self.carrying_amount
            for _period in range(periods):
                interest += carrying * rate
                carrying += carrying * rate
            interest = currency.round(interest)
            # Advance the anchor to the last period boundary now recognised.
            boundaries = self._unwind_boundaries()
            new_anchor = boundaries[self.unwound_periods + periods - 1]
        else:
            # Opt-in-safe fallback: no settlement horizon to time the schedule,
            # so keep the prior per-click, one-period behaviour.
            periods = 1
            interest = currency.round(self.carrying_amount * rate)
            new_anchor = self.last_unwind_date

        if currency.compare_amounts(interest, 0.0) <= 0:
            raise UserError(_(
                "No discount to unwind (rate or carrying amount is nil)."))
        # Do not let unwinding push the carrying amount above the undiscounted
        # best estimate.
        cap = self.best_estimate - self.carrying_amount
        interest = min(interest, currency.round(cap))
        if currency.compare_amounts(interest, 0.0) <= 0:
            raise UserError(_(
                "The provision already sits at the undiscounted estimate."))
        self._post_move([
            (self.finance_cost_account_id, interest, 0.0,
             _("Unwinding of discount %s", self.name)),
            (self.provision_account_id, 0.0, interest,
             _("Provision accretion %s", self.name)),
        ])
        self.carrying_amount += interest
        self.unwound_periods += periods
        if new_anchor:
            self.last_unwind_date = new_anchor
        return True

    def action_remeasure(self):
        """IAS 37.59: at each reporting date a provision is reviewed and
        adjusted to the current best estimate. The change in the carrying
        amount is recognised in profit or loss (an increase against the
        original expense account, a decrease as a writeback), and the stored
        best estimate is updated so subsequent unwinding accretes toward the
        revised figure.
        """
        # Elevate first: this action writes the guarded carrying_amount anchor
        # (and best_estimate under the remeasure flag), which the shared
        # eh.workflow.guard only accepts under sudo.
        self = self._eh_workflow_action()
        self.ensure_one()
        self._check_manager()
        if self.state != 'recognised':
            raise UserError(_(
                "Only a recognised provision can be remeasured."))
        self._validate_accounts(['provision', 'expense'])
        currency = self.currency_id
        onerous_engine = (self.provision_type == 'onerous'
                          and not self.onerous_override)
        if onerous_engine:
            # The onerous measure re-derives from the revised inputs staged
            # in the remeasure_* fields (stage the FULL revised set); the
            # free-keyed estimate is ignored so the measurement stays
            # mechanical (IAS 37.59, 37.68).
            if not (self.remeasure_cost_fulfil or self.remeasure_penalty_exit
                    or self.remeasure_benefit_expected):
                raise UserError(_(
                    "Stage the revised onerous inputs (cost of fulfilling, "
                    "benefits expected, exit penalty); the onerous measure "
                    "re-derives from them (IAS 37.68)."))
            revised = self._onerous_measure(
                fulfil=self.remeasure_cost_fulfil,
                benefit=self.remeasure_benefit_expected,
                penalty=self.remeasure_penalty_exit)
        else:
            revised = currency.round(self.remeasure_estimate)
        if currency.compare_amounts(revised, 0.0) < 0:
            raise UserError(_("The revised estimate cannot be negative."))
        # Present value of the revised estimate over the periods still to run,
        # consistent with how the provision was recognised and unwound.
        rate = (self.discount_rate or 0.0) / 100.0
        remaining = max(
            (self.periods_to_settlement or 0) - self.unwound_periods, 0)
        target = revised / ((1.0 + rate) ** remaining) \
            if rate and remaining else revised
        target = currency.round(target)
        delta = currency.round(target - self.carrying_amount)
        if currency.is_zero(delta):
            raise UserError(_(
                "The revised estimate leaves the carrying amount unchanged."))
        if currency.compare_amounts(delta, 0.0) > 0:
            # Increase in the obligation: Dr expense / Cr provision.
            self._post_move([
                (self.expense_account_id, delta, 0.0,
                 _("Provision remeasurement (increase) %s", self.name)),
                (self.provision_account_id, 0.0, delta,
                 _("Provision remeasured up %s", self.name)),
            ])
        else:
            # Decrease in the obligation: writeback to the expense account.
            amount = -delta
            self._post_move([
                (self.provision_account_id, amount, 0.0,
                 _("Provision remeasured down %s", self.name)),
                (self.expense_account_id, 0.0, amount,
                 _("Provision remeasurement (decrease) %s", self.name)),
            ])
        self.carrying_amount = target
        vals = {'best_estimate': revised, 'remeasure_estimate': 0.0}
        if onerous_engine:
            # Adopt the staged inputs as the new measurement inputs so the
            # register and the posted figure stay in lock-step, then clear
            # the staging fields.
            vals.update({
                'unavoidable_cost_fulfil': self.remeasure_cost_fulfil,
                'contract_benefit_expected': self.remeasure_benefit_expected,
                'penalty_exit': self.remeasure_penalty_exit,
                'remeasure_cost_fulfil': 0.0,
                'remeasure_benefit_expected': 0.0,
                'remeasure_penalty_exit': 0.0,
            })
        self.with_context(eh_provision_remeasure=True).write(vals)
        return True

    def action_utilise(self):
        self = self._eh_workflow_action()
        self.ensure_one()
        self._check_manager()
        if self.state != 'recognised':
            raise UserError(_(
                "Only a recognised provision can be utilised."))
        self._validate_accounts(['provision', 'settlement'])
        currency = self.currency_id
        amount = currency.round(self.utilise_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive amount to utilise."))
        if currency.compare_amounts(amount, self.carrying_amount) > 0:
            raise UserError(_(
                "Cannot utilise %(amt).2f: it exceeds the provision carrying "
                "amount of %(bal).2f.",
                amt=amount, bal=self.carrying_amount))
        self._post_move([
            (self.provision_account_id, amount, 0.0,
             _("Provision utilised %s", self.name)),
            (self.settlement_account_id, 0.0, amount,
             _("Settlement %s", self.name)),
        ])
        self.carrying_amount -= amount
        self.utilised_amount += amount
        self.utilise_amount = 0.0
        if currency.is_zero(self.carrying_amount):
            self.with_context(eh_provision_state_change=True).state = 'settled'
        return True

    def action_reverse(self):
        self = self._eh_workflow_action()
        self.ensure_one()
        self._check_manager()
        if self.state == 'recognised_asset':
            # IAS 37.35: contingent assets are assessed continually; if the
            # inflow stops being virtually certain the recognised asset is
            # derecognised against the income that carried it.
            self._validate_accounts(['asset', 'income'])
            currency = self.currency_id
            amount = currency.round(self.carrying_amount)
            if currency.compare_amounts(amount, 0.0) <= 0:
                raise UserError(_(
                    "Nothing to reverse: the asset carrying amount is nil."))
            self._post_move([
                (self.income_account_id, amount, 0.0,
                 _("Asset recognition reversed %s", self.name)),
                (self.asset_account_id, 0.0, amount,
                 _("Asset derecognised %s", self.name)),
            ])
            self.carrying_amount -= amount
            self.with_context(
                eh_provision_state_change=True).state = 'reversed'
            return True
        if self.state != 'recognised':
            raise UserError(_(
                "Only a recognised provision can be reversed."))
        self._validate_accounts(['provision', 'expense'])
        currency = self.currency_id
        amount = currency.round(self.carrying_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_(
                "Nothing to reverse: the provision carrying amount is nil."))
        # A provision no longer required is credited back to profit or loss
        # against the original expense account (IAS 37.59). Dr provision
        # liability / Cr expense for the unused carrying amount.
        self._post_move([
            (self.provision_account_id, amount, 0.0,
             _("Provision reversed %s", self.name)),
            (self.expense_account_id, 0.0, amount,
             _("Provision writeback %s", self.name)),
        ])
        self.carrying_amount -= amount
        self.with_context(eh_provision_state_change=True).state = 'reversed'
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        for p in self:
            if p.state in ('recognised', 'recognised_asset') \
                    and not p.currency_id.is_zero(
                    p.carrying_amount):
                raise UserError(_(
                    "Reverse or utilise the carrying amount before "
                    "cancelling %s.", p.display_name))
            p.with_context(eh_provision_state_change=True).state = 'cancelled'

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_provision_id', '=', self.id)],
        }

    # ---- helpers ----

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post provision entries."))

    def _validate_accounts(self, needed):
        self.ensure_one()
        field_map = {
            'provision': ('provision_account_id', _("provision liability account")),
            'expense': ('expense_account_id', _("expense account")),
            'finance_cost': ('finance_cost_account_id', _("finance cost account")),
            'settlement': ('settlement_account_id', _("settlement account")),
            'asset': ('asset_account_id', _("asset / receivable account")),
            'income': ('income_account_id', _("income account")),
            'reimbursement': ('reimbursement_account_id',
                              _("reimbursement asset account")),
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
                "Configure the %s on provision %s first.",
                ', '.join(missing), self.display_name))

    def _post_move(self, legs):
        lines = []
        for account, debit, credit, label in legs:
            lines.append((0, 0, {
                'name': label, 'account_id': account.id,
                'debit': debit, 'credit': credit,
            }))
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': fields.Date.context_today(self),
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_provision_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class EhProvisionRestructuringLine(models.Model):
    _name = 'eh.provision.restructuring.line'
    _description = "Restructuring cost component (IAS 37.80-81)"
    _order = 'id'

    provision_id = fields.Many2one(
        'eh.provision', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='provision_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='provision_id.currency_id', store=True, readonly=True)
    name = fields.Char(required=True, string="Description")
    cost_kind = fields.Selection(
        [('termination', "Termination benefits (direct)"),
         ('contract_termination', "Contract termination (direct)"),
         ('other_direct', "Other directly attributable (direct)"),
         ('retraining', "Retraining / relocating staff (excluded)"),
         ('marketing', "Marketing (excluded)"),
         ('new_systems', "Investment in new systems (excluded)")],
        required=True, default='termination', string="Cost Kind",
        help="Direct kinds enter the provision and must sum to the best "
             "estimate (IAS 37.80). Excluded kinds relate to the future "
             "conduct of the business and never enter the sum (IAS 37.81); "
             "they are registered here for completeness only.")
    in_scope = fields.Boolean(
        compute='_compute_in_scope', string="In Provision",
        help="Whether this component enters the recognised provision.")
    amount = fields.Monetary(
        currency_field='currency_id', required=True)

    _sql_constraints = [
        ('check_amount', 'CHECK (amount >= 0)', 'A restructuring cost component cannot be negative.'),
    ]

    @api.depends('cost_kind')
    def _compute_in_scope(self):
        for line in self:
            line.in_scope = line.cost_kind in RESTRUCTURING_INCLUDED_KINDS

    # The parent's measurement freezes when it posts; its component lines
    # feed that measurement, so they freeze with it at create, write and
    # unlink (a raw line edit would silently desync the recognised sum).
    def _check_parent_open(self, provisions=None):
        if self.env.context.get('eh_provision_remeasure'):
            return
        provisions = (provisions if provisions is not None
                      else self.mapped('provision_id'))
        frozen = provisions.filtered(
            lambda p: p.state in p._FROZEN_STATES)
        if frozen:
            raise UserError(_(
                "The restructuring components of a posted provision are "
                "frozen (%s). Use Remeasure to book a change in estimate.",
                ', '.join(frozen.mapped('display_name'))))

    @api.model_create_multi
    def create(self, vals_list):
        # Guard BEFORE the insert: a component added to a posted provision
        # must never reach the table.
        self._check_parent_open(self.env['eh.provision'].browse(
            [v['provision_id'] for v in vals_list if v.get('provision_id')]))
        return super().create(vals_list)

    def write(self, vals):
        self._check_parent_open()
        if vals.get('provision_id'):
            # Reparenting a line onto a posted provision is the same hole.
            self._check_parent_open(
                self.env['eh.provision'].browse(vals['provision_id']))
        return super().write(vals)

    def unlink(self):
        self._check_parent_open()
        return super().unlink()


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_provision_id = fields.Many2one(
        'eh.provision', string="Provision", readonly=True, index=True,
        ondelete='restrict', copy=False)
