# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
IFRS 16 lease contract (lessee and basic lessor accounting).

Lessee default: recognises a Right Of Use (ROU) asset and a lease
liability at lease commencement, then runs the schedule:

* Each period: interest = liability_opening * periodic_rate; payment is
  split into interest and principal; liability balance decreases.
* ROU asset is depreciated straight line over the lease term - or over
  the underlying asset's useful life when a purchase option is
  reasonably certain (IFRS 16.32).

Recognition exemptions (IFRS 16.5-8): a short-term lease (term,
including reasonably-certain extensions, of 12 months or less and no
purchase option) or a low-value lease (underlying asset at or below the
company threshold when new) may elect out of ROU/liability recognition;
the schedule then recognises the lease payments as a straight-line
expense (equal fixed payments, so the per-period expense equals the
payment) and posts no opening entry.

Term options (IFRS 16.18-19/27): reasonably-certain extension options
extend the term used for the schedule; reasonably-certain termination
penalties and purchase prices are included in the liability and settle
with the final period's payment.

Lease / non-lease component split (IFRS 16.13-16): payment_service_pct
carves the service share out of each payment; only the lease share
builds the liability and ROU, the service share posts straight to
expense each period.

Basic lessor accounting (IFRS 16.67-77, 81): lessor_mode 'operating'
recognises rental income straight line with the underlying asset kept
on the books; 'finance' derecognises to a net investment (PV of the
payments at the rate implicit in the lease, entered in the rate field)
and splits each receipt into interest income and principal recovery.

Manufacturer / dealer finance lessor (IFRS 16.71-74): when
lessor_dealer is set on a finance lease the commencement entry also
recognises selling profit or loss. The net investment is the PV of the
lease payments PLUS the PV of the unguaranteed residual value, both at
the rate implicit in the lease; selling revenue is the lower of the
fair value of the asset and the PV of the lease payments at a market
rate; cost of sale is the carrying amount of the underlying asset less
the PV of the unguaranteed residual value; selling profit (revenue less
cost of sale) posts to P&L at commencement. Interest income then
accrues on the net investment (which amortises down to the unguaranteed
residual, recovered when the asset returns, not through the receipts).

State machine:

  draft -> active -> modified -> active -> ...
                              \\-> terminated
                              \\-> ended (term completed)
"""

import calendar
import logging
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


CADENCE_MONTHS = {
    'monthly': 1,
    'quarterly': 3,
    'semi_annual': 6,
    'annual': 12,
}


class EhLeaseContract(models.Model):
    _name = 'eh.lease.contract'
    _description = "Lease Contract (IFRS 16)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'commencement_date desc, id desc'

    # State may only change through the lease's own actions (action_activate /
    # _maybe_mark_ended) and the modify / terminate wizards, never a direct
    # RPC write that would skip the opening entry and remeasurement postings.
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    reference = fields.Char(
        copy=False, tracking=True,
        help="External lease reference, e.g. landlord contract number.",
    )
    state = fields.Selection([
        ('draft', "Draft"),
        ('active', "Active"),
        ('modified', "Modified"),
        ('terminated', "Terminated"),
        ('ended', "Ended"),
    ], default='draft', required=True, tracking=True)

    lessor_id = fields.Many2one(
        'res.partner', string="Lessor", required=True, tracking=True,
    )
    commencement_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
    )
    term_months = fields.Integer(
        required=True, tracking=True,
        default=lambda self: (
            self.env.company.eh_lease_default_term_months or 36
        ),
        help="Total lease term in months.",
    )
    cadence = fields.Selection([
        ('monthly', "Monthly"),
        ('quarterly', "Quarterly"),
        ('semi_annual', "Semi Annual"),
        ('annual', "Annual"),
    ], required=True, default='monthly', tracking=True)
    payment_timing = fields.Selection([
        ('advance', "In Advance"),
        ('arrears', "In Arrears"),
    ], required=True, default='advance', tracking=True)

    payment_amount = fields.Monetary(required=True, tracking=True)
    incremental_borrowing_rate = fields.Float(
        string="IBR (annual %)", required=True, default=5.0, tracking=True,
        help=(
            "Annual discount rate as a percentage. Lessee: the rate "
            "implicit in the lease when readily determinable, else the "
            "incremental borrowing rate (IFRS 16.26). Finance lessor: "
            "the rate implicit in the lease (IFRS 16.68)."
        ),
    )
    initial_direct_costs = fields.Monetary(default=0.0, tracking=True)
    prepaid_lease_payments = fields.Monetary(default=0.0, tracking=True)

    # ---- IFRS 16.5-8 recognition exemptions ----
    exemption = fields.Selection(
        [
            ('none', "None (recognise ROU / liability)"),
            ('short_term', "Short-term lease (IFRS 16.6, term <= 12m)"),
            ('low_value', "Low-value asset (IFRS 16.6, B3-B8)"),
        ],
        required=True, default='none', tracking=True,
        help=(
            "Recognition exemption election. An exempt lease posts NO "
            "ROU asset and NO lease liability; its payments are "
            "recognised as an expense on a straight-line basis over "
            "the term (IFRS 16.6). Short-term requires a term of 12 "
            "months or less INCLUDING reasonably-certain extensions "
            "and no reasonably-certain purchase option (IFRS 16.5, "
            "18); low-value requires the underlying asset's value when "
            "new to be at or below the company threshold."
        ),
    )
    underlying_asset_value = fields.Monetary(
        tracking=True,
        help=(
            "Value of the underlying asset when new (IFRS 16.B3-B8: "
            "assessed on an absolute basis, regardless of the lessee's "
            "size). Required for the low-value exemption; compared "
            "against the company's low-value threshold."
        ),
    )
    exemption_election_note = fields.Text(
        string="Exemption election (per class)",
        help=(
            "IFRS 16.8: the short-term election is made by CLASS of "
            "underlying asset (the low-value election is lease-by-"
            "lease). Document here the class this lease belongs to and "
            "the election covering it, so the class-level policy is "
            "auditable from the contract."
        ),
    )

    # ---- IFRS 16.13-16 lease / non-lease component split ----
    payment_service_pct = fields.Float(
        string="Service (non-lease) share %",
        default=0.0, tracking=True, digits=(5, 2),
        help=(
            "Percentage of each payment that pays for non-lease "
            "components (services: maintenance, utilities, supplies). "
            "IFRS 16.13-16: consideration is allocated on relative "
            "stand-alone prices; only the lease component builds the "
            "liability and ROU, the service share posts straight to "
            "expense each period. 0 keeps the whole payment in the "
            "lease component (including under the IFRS 16.15 practical "
            "expedient of not separating)."
        ),
    )
    component_allocation_note = fields.Text(
        string="Component allocation basis",
        help=(
            "Stand-alone price evidence behind the service percentage "
            "(IFRS 16.14: relative stand-alone price allocation; "
            "observable prices, or estimates maximising observable "
            "inputs)."
        ),
    )

    # ---- IFRS 16.18-19 term options ----
    option_ids = fields.One2many(
        'eh.lease.option', 'lease_id', copy=True,
        string="Term Options",
        help=(
            "Extension, termination and purchase options. Only options "
            "flagged reasonably certain enter the term and liability "
            "measurement."
        ),
    )
    effective_term_months = fields.Integer(
        compute='_compute_effective_term_months',
        help=(
            "Lease term used for the schedule: the base term plus the "
            "months of every reasonably-certain extension option "
            "(IFRS 16.18)."
        ),
    )
    underlying_useful_life_months = fields.Integer(
        tracking=True,
        help=(
            "Useful life of the underlying asset in months. Required "
            "when a purchase option is reasonably certain: the ROU "
            "asset is then depreciated over this useful life instead "
            "of the lease term (IFRS 16.32)."
        ),
    )

    # ---- IFRS 16.67-77 lessor accounting ----
    lessor_mode = fields.Selection(
        [
            ('none', "Lessee (default)"),
            ('operating', "Lessor - operating lease"),
            ('finance', "Lessor - finance lease"),
        ],
        required=True, default='none', tracking=True,
        help=(
            "Accounting perspective for this contract. Lessee is the "
            "default ROU/liability model. Lessor - operating keeps the "
            "underlying asset on the books and recognises rental "
            "income straight line (IFRS 16.81). Lessor - finance "
            "derecognises the underlying asset into a net investment "
            "(the PV of the payments at the rate implicit in the "
            "lease, IFRS 16.67-68) and splits every receipt into "
            "interest income and principal recovery."
        ),
    )

    # ---- IFRS 16.71-74 manufacturer / dealer finance lessor ----
    lessor_dealer = fields.Boolean(
        string="Manufacturer / dealer lessor",
        default=False, tracking=True,
        help=(
            "IFRS 16.71-74: a manufacturer or dealer lessor recognises "
            "selling profit or loss at commencement of a finance lease. "
            "The net investment is the PV of the lease payments plus the "
            "PV of the unguaranteed residual value; selling revenue is "
            "the lower of the asset's fair value and the PV of the lease "
            "payments; cost of sale is the carrying amount less the PV of "
            "the unguaranteed residual; selling profit posts to P&L at "
            "commencement. Only available on a finance-lessor contract."
        ),
    )
    fair_value_of_asset = fields.Monetary(
        string="Fair value of underlying asset",
        default=0.0, tracking=True,
        help=(
            "IFRS 16.71: fair value of the underlying asset at "
            "commencement. Selling revenue is capped at the lower of "
            "this and the PV of the lease payments (a below-market rate "
            "restricts the revenue a dealer lessor may recognise)."
        ),
    )
    carrying_amount_of_asset = fields.Monetary(
        string="Carrying amount (cost) of asset",
        default=0.0, tracking=True,
        help=(
            "IFRS 16.71-72: carrying amount (cost) of the underlying "
            "asset. Cost of sale is this carrying amount less the PV of "
            "the unguaranteed residual value."
        ),
    )
    unguaranteed_residual_value = fields.Monetary(
        string="Unguaranteed residual value",
        default=0.0, tracking=True,
        help=(
            "IFRS 16.71-74: the portion of the residual value of the "
            "underlying asset the lessor is NOT guaranteed to recover "
            "(undiscounted amount at end of term). Its present value is "
            "included in the net investment (IFRS 16.70(b)) and excluded "
            "from cost of sale (IFRS 16.72). The net investment "
            "receivable amortises down to this residual, recovered when "
            "the asset returns, not through the lease receipts."
        ),
    )
    dealer_revenue_account_id = fields.Many2one(
        'account.account', string="Selling Revenue Account",
        domain="[('account_type', 'in', ['income', 'income_other'])]",
        help=(
            "IFRS 16.71: P&L account credited with the dealer lessor's "
            "selling revenue (lower of fair value and PV of the lease "
            "payments) at commencement."
        ),
    )
    dealer_cost_of_sale_account_id = fields.Many2one(
        'account.account', string="Cost of Sale Account",
        domain="[('account_type', 'in', "
               "['expense', 'expense_direct_cost'])]",
        help=(
            "IFRS 16.71-72: P&L account debited with the dealer lessor's "
            "cost of sale (carrying amount less PV of the unguaranteed "
            "residual value) at commencement."
        ),
    )

    # ---- accounts ----
    # The lessee ROU / liability accounts are enforced per mode at
    # activation (_validate_lease_setup), not with required=True: an
    # exempt lease posts only expense and cash, and a lessor contract
    # posts income / net-investment legs, so hard-requiring the ROU
    # block would force meaningless configuration on those contracts.
    rou_asset_account_id = fields.Many2one(
        'account.account', string="ROU Asset Account",
        domain="[('account_type', 'in', ['asset_fixed', 'asset_non_current'])]",
    )
    lease_liability_account_id = fields.Many2one(
        'account.account', string="Lease Liability Account",
        domain="[('account_type', 'in', ['liability_current', 'liability_non_current'])]",
    )
    interest_expense_account_id = fields.Many2one(
        'account.account', string="Interest Expense Account",
        domain="[('account_type', '=', 'expense')]",
    )
    rou_depreciation_account_id = fields.Many2one(
        'account.account', string="ROU Depreciation Account",
        domain="[('account_type', '=', 'expense_depreciation')]",
    )
    rou_accumulated_depreciation_account_id = fields.Many2one(
        'account.account', string="ROU Accumulated Depreciation",
        domain="[('account_type', 'in', ['asset_fixed', 'asset_non_current'])]",
    )
    cash_account_id = fields.Many2one(
        'account.account', string="Cash / Payables Account", required=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'liability_payable', 'liability_current'])]",
    )
    lease_expense_account_id = fields.Many2one(
        'account.account', string="Lease / Service Expense Account",
        domain="[('account_type', 'in', ['expense', 'expense_direct_cost'])]",
        help=(
            "P&L account for lease expense that bypasses the ROU model: "
            "the straight-line expense of an exempt (short-term / "
            "low-value) lease, and the service (non-lease component) "
            "share of each payment when a component split is set."
        ),
    )
    lessor_income_account_id = fields.Many2one(
        'account.account', string="Rental Income Account",
        domain="[('account_type', 'in', ['income', 'income_other'])]",
        help=(
            "Operating-lessor rental income account; credited straight "
            "line each period (IFRS 16.81)."
        ),
    )
    lessor_interest_income_account_id = fields.Many2one(
        'account.account', string="Interest Income Account",
        domain="[('account_type', 'in', ['income', 'income_other'])]",
        help=(
            "Finance-lessor interest income account; credited with the "
            "constant periodic return on the net investment "
            "(IFRS 16.75)."
        ),
    )
    net_investment_account_id = fields.Many2one(
        'account.account', string="Net Investment Account",
        domain="[('account_type', 'in', "
               "['asset_receivable', 'asset_current', 'asset_non_current', "
               "'asset_fixed'])]",
        help=(
            "Finance-lessor receivable carrying the net investment in "
            "the lease (IFRS 16.67). Debited at commencement with the "
            "PV of the payments; credited with the principal portion "
            "of every receipt."
        ),
    )
    lessor_counterpart_account_id = fields.Many2one(
        'account.account', string="Asset Derecognition Account",
        help=(
            "Counterpart credited when the net investment is "
            "recognised at commencement of a finance lease (the "
            "carrying amount of the underlying asset derecognised, or "
            "a clearing account when derecognition is posted "
            "separately)."
        ),
    )
    journal_id = fields.Many2one(
        'account.journal', string="Lease Journal", required=True,
        domain="[('type', '=', 'general')]",
    )

    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )

    # ---- computed at activation ----
    rou_initial_value = fields.Monetary(readonly=True, tracking=True)
    liability_initial_value = fields.Monetary(readonly=True, tracking=True)
    activated_at = fields.Datetime(readonly=True, tracking=True)
    activated_by_id = fields.Many2one('res.users', readonly=True)
    opening_move_id = fields.Many2one('account.move', readonly=True)

    # ---- termination ----
    terminated_at = fields.Datetime(readonly=True, tracking=True)
    terminated_by_id = fields.Many2one('res.users', readonly=True)
    termination_date = fields.Date(readonly=True, tracking=True)
    termination_move_id = fields.Many2one('account.move', readonly=True)

    # ---- modification audit ----
    modification_count = fields.Integer(readonly=True, default=0)
    last_modified_at = fields.Datetime(readonly=True, tracking=True)

    # ---- schedule ----
    schedule_line_ids = fields.One2many(
        'eh.lease.schedule.line', 'lease_id', copy=False,
    )

    # ---- totals ----
    total_paid = fields.Monetary(compute='_compute_totals', store=True)
    total_interest = fields.Monetary(compute='_compute_totals', store=True)
    total_principal = fields.Monetary(compute='_compute_totals', store=True)
    liability_balance = fields.Monetary(compute='_compute_totals', store=True)

    notes = fields.Text()

    _sql_constraints = [
        ('check_term_positive', 'CHECK (term_months > 0)', 'Lease term must be greater than zero.'),
        ('check_payment_positive', 'CHECK (payment_amount > 0)', 'Payment amount must be positive.'),
    ]

    # ---- compute ----

    @api.depends(
        'schedule_line_ids.is_posted',
        'schedule_line_ids.payment_amount',
        'schedule_line_ids.interest',
        'schedule_line_ids.principal',
        'schedule_line_ids.liability_close',
    )
    def _compute_totals(self):
        for lease in self:
            posted = lease.schedule_line_ids.filtered(lambda l: l.is_posted)
            lease.total_paid = sum(posted.mapped('payment_amount'))
            lease.total_interest = sum(posted.mapped('interest'))
            lease.total_principal = sum(posted.mapped('principal'))
            if posted:
                last = max(posted, key=lambda l: l.sequence)
                lease.liability_balance = last.liability_close
            else:
                lease.liability_balance = lease.liability_initial_value

    @api.depends('term_months', 'option_ids.option_type',
                 'option_ids.extension_months',
                 'option_ids.reasonably_certain')
    def _compute_effective_term_months(self):
        for lease in self:
            extensions = sum(
                lease.option_ids
                .filtered(lambda o: o.option_type == 'extension'
                          and o.reasonably_certain)
                .mapped('extension_months'),
            )
            lease.effective_term_months = lease.term_months + extensions

    # ---- constraints ----

    @api.constrains('exemption', 'term_months', 'underlying_asset_value',
                    'option_ids', 'lessor_mode',
                    'initial_direct_costs', 'prepaid_lease_payments')
    def _check_exemption(self):
        for lease in self:
            if lease.exemption == 'none':
                continue
            if lease.lessor_mode != 'none':
                raise ValidationError(_(
                    "The IFRS 16.5 recognition exemptions are LESSEE "
                    "elections; a lessor contract cannot be exempt.",
                ))
            certain_purchase = lease.option_ids.filtered(
                lambda o: o.option_type == 'purchase'
                and o.reasonably_certain,
            )
            if certain_purchase:
                raise ValidationError(_(
                    "A lease with a reasonably-certain purchase option "
                    "transfers the underlying asset and does not "
                    "qualify for a recognition exemption (IFRS 16.5, "
                    "Appendix A definition of a short-term lease).",
                ))
            if lease.exemption == 'short_term':
                if lease.effective_term_months > 12:
                    raise ValidationError(_(
                        "The short-term exemption requires a lease term "
                        "of 12 months or less INCLUDING reasonably-"
                        "certain extension options (IFRS 16.18); this "
                        "lease's effective term is %(term)s months.",
                        term=lease.effective_term_months,
                    ))
            if lease.exemption == 'low_value':
                threshold = (
                    lease.company_id.eh_lease_low_value_threshold or 5000.0
                )
                if lease.underlying_asset_value <= 0:
                    raise ValidationError(_(
                        "The low-value exemption requires the value of "
                        "the underlying asset when new (IFRS 16.B3-B8).",
                    ))
                if lease.underlying_asset_value > threshold:
                    raise ValidationError(_(
                        "The underlying asset's value when new "
                        "(%(value).2f) exceeds the company's low-value "
                        "threshold (%(threshold).2f); the low-value "
                        "exemption is not available.",
                        value=lease.underlying_asset_value,
                        threshold=threshold,
                    ))
            if (lease.initial_direct_costs or 0.0) or (
                    lease.prepaid_lease_payments or 0.0):
                raise ValidationError(_(
                    "An exempt lease recognises no ROU asset, so there "
                    "is nothing to capitalise initial direct costs or "
                    "prepayments into; expense them directly and leave "
                    "both fields at zero.",
                ))

    @api.constrains('payment_service_pct', 'lessor_mode')
    def _check_service_pct(self):
        for lease in self:
            if not (0.0 <= lease.payment_service_pct < 100.0):
                raise ValidationError(_(
                    "The service (non-lease) share must be at least 0 "
                    "and below 100 percent; at 100 percent there is no "
                    "lease component and the contract is a service "
                    "agreement, not a lease.",
                ))
            if lease.payment_service_pct and lease.lessor_mode != 'none':
                raise ValidationError(_(
                    "The lease / non-lease component split is a lessee "
                    "measurement feature; set the service share to zero "
                    "on a lessor contract.",
                ))

    @api.constrains('lessor_mode', 'initial_direct_costs',
                    'prepaid_lease_payments')
    def _check_lessor_mode(self):
        for lease in self:
            if lease.lessor_mode == 'none':
                continue
            if (lease.initial_direct_costs or 0.0) or (
                    lease.prepaid_lease_payments or 0.0):
                raise ValidationError(_(
                    "Initial direct costs and prepaid payments are "
                    "lessee ROU inputs; leave them at zero on a lessor "
                    "contract (lessor initial direct costs are outside "
                    "this basic lessor scope).",
                ))

    @api.constrains('lessor_dealer', 'lessor_mode', 'fair_value_of_asset',
                    'carrying_amount_of_asset')
    def _check_dealer(self):
        for lease in self:
            if not lease.lessor_dealer:
                continue
            if lease.lessor_mode != 'finance':
                raise ValidationError(_(
                    "The manufacturer / dealer selling-profit model "
                    "(IFRS 16.71-74) is a FINANCE-lessor feature; set the "
                    "accounting mode to Lessor - finance lease first.",
                ))
            if (lease.fair_value_of_asset or 0.0) <= 0:
                raise ValidationError(_(
                    "A manufacturer / dealer finance lease needs the fair "
                    "value of the underlying asset (IFRS 16.71).",
                ))
            if (lease.carrying_amount_of_asset or 0.0) <= 0:
                raise ValidationError(_(
                    "A manufacturer / dealer finance lease needs the "
                    "carrying amount (cost) of the underlying asset "
                    "(IFRS 16.71-72).",
                ))

    # ---- measurement helpers (components / options) ----

    def _lease_component_payment(self):
        """Lease-component share of each contractual payment: the full
        payment less the service (non-lease) share (IFRS 16.13-16)."""
        self.ensure_one()
        pct = (self.payment_service_pct or 0.0) / 100.0
        return self.currency_id.round(self.payment_amount * (1.0 - pct))

    def _service_component_payment(self):
        """Service (non-lease component) share of each payment; the
        residual of the rounded lease share so the two always sum back
        to the contractual payment exactly."""
        self.ensure_one()
        return self.currency_id.round(
            self.payment_amount - self._lease_component_payment(),
        )

    def _end_of_term_balloon(self):
        """Reasonably-certain purchase price plus reasonably-certain
        termination penalty, both settled with the final period's
        payment (IFRS 16.27(d)/(e))."""
        self.ensure_one()
        certain = self.option_ids.filtered('reasonably_certain')
        balloon = (
            sum(certain.filtered(lambda o: o.option_type == 'purchase')
                .mapped('purchase_price'))
            + sum(certain.filtered(lambda o: o.option_type == 'termination')
                  .mapped('termination_penalty'))
        )
        return self.currency_id.round(balloon)

    def _has_certain_purchase_option(self):
        self.ensure_one()
        return bool(self.option_ids.filtered(
            lambda o: o.option_type == 'purchase' and o.reasonably_certain,
        ))

    def _rou_depreciation_months(self):
        """Months over which the ROU asset depreciates: the underlying
        asset's useful life when a purchase option is reasonably
        certain (IFRS 16.32), else the effective lease term."""
        self.ensure_one()
        if self._has_certain_purchase_option():
            return self.underlying_useful_life_months
        return self.effective_term_months

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code('eh.lease.contract') or '/'
                vals['name'] = seq
        return super().create(vals_list)

    # ---- transitions ----

    def action_compute_schedule(self):
        for lease in self:
            if lease.state != 'draft':
                raise UserError(_(
                    "Schedule can only be computed in draft state.",
                ))
            lease._wipe_unposted_schedule()
            lease._build_schedule()

    def action_activate(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can activate a lease and post its "
                "opening entry. This posting is a segregation-of-duties "
                "control point.",
            ))
        self = self._eh_workflow_action()
        for lease in self:
            if lease.state != 'draft':
                raise UserError(_(
                    "Only draft leases can be activated.",
                ))
            lease._validate_lease_setup()
            if not lease.schedule_line_ids:
                lease._build_schedule()
            opening_move = lease._post_opening_entry()
            lease.write({
                'state': 'active',
                'activated_at': fields.Datetime.now(),
                'activated_by_id': self.env.user.id,
                'opening_move_id': opening_move.id if opening_move else False,
            })

    def _validate_lease_setup(self):
        """Mode-aware posting-setup validation, replacing blanket
        required=True on the lessee ROU block: each accounting mode
        needs a different set of accounts."""
        self.ensure_one()
        missing = []
        if self.exemption != 'none':
            if not self.lease_expense_account_id:
                missing.append(_("Lease / Service Expense Account"))
        elif self.lessor_mode == 'operating':
            if not self.lessor_income_account_id:
                missing.append(_("Rental Income Account"))
        elif self.lessor_mode == 'finance':
            if not self.net_investment_account_id:
                missing.append(_("Net Investment Account"))
            if not self.lessor_interest_income_account_id:
                missing.append(_("Interest Income Account"))
            if not self.lessor_counterpart_account_id:
                missing.append(_("Asset Derecognition Account"))
            if self.lessor_dealer:
                if not self.dealer_revenue_account_id:
                    missing.append(_("Selling Revenue Account"))
                if not self.dealer_cost_of_sale_account_id:
                    missing.append(_("Cost of Sale Account"))
        else:
            if not self.rou_asset_account_id:
                missing.append(_("ROU Asset Account"))
            if not self.lease_liability_account_id:
                missing.append(_("Lease Liability Account"))
            if not self.interest_expense_account_id:
                missing.append(_("Interest Expense Account"))
            if not self.rou_depreciation_account_id:
                missing.append(_("ROU Depreciation Account"))
            if not self.rou_accumulated_depreciation_account_id:
                missing.append(_("ROU Accumulated Depreciation"))
            if (self.payment_service_pct
                    and not self.lease_expense_account_id):
                missing.append(_("Lease / Service Expense Account"))
        if missing:
            raise UserError(_(
                "Lease %(lease)s is missing posting setup: %(missing)s.",
                lease=self.display_name,
                missing=", ".join(missing),
            ))

    def action_open_modify_wizard(self):
        self.ensure_one()
        if self.state not in ('active', 'modified'):
            raise UserError(_(
                "Only active leases can be modified.",
            ))
        self._check_remeasurement_supported(_("modified"))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.lease.modify.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_lease_id': self.id},
        }

    def action_open_terminate_wizard(self):
        self.ensure_one()
        if self.state not in ('active', 'modified'):
            raise UserError(_(
                "Only active leases can be terminated.",
            ))
        self._check_remeasurement_supported(_("terminated early"))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.lease.terminate.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_lease_id': self.id},
        }

    def action_post_due_lines(self):
        today = fields.Date.context_today(self)
        for lease in self:
            if lease.state not in ('active', 'modified'):
                continue
            due = lease.schedule_line_ids.filtered(
                lambda l: not l.is_posted and l.period_date <= today,
            ).sorted('sequence')
            for line in due:
                line.action_post()
            lease._maybe_mark_ended()

    # ---- helpers ----

    def _check_remeasurement_supported(self, verb):
        """The modification / early-termination wizards remeasure the
        lessee ROU-liability model. Exempt (expense-only) and lessor
        contracts, and leases whose measurement includes reasonably-
        certain options, are outside their arithmetic; block with a
        clear path instead of silently mis-measuring."""
        self.ensure_one()
        if self.exemption != 'none':
            raise UserError(_(
                "An exempt (short-term / low-value) lease has no ROU or "
                "liability to remeasure, so it cannot be %(verb)s through "
                "this wizard. Adjust the remaining expense rows in draft "
                "of a replacement contract, or end the schedule by "
                "posting its remaining rows.",
                verb=verb,
            ))
        if self.lessor_mode != 'none':
            raise UserError(_(
                "Lessor contracts cannot be %(verb)s through the lessee "
                "remeasurement wizard in this basic lessor scope.",
                verb=verb,
            ))
        certain = self.option_ids.filtered('reasonably_certain')
        if certain:
            raise UserError(_(
                "This lease's measurement includes reasonably-certain "
                "options (%(count)s). The remeasurement wizard rebuilds "
                "the schedule from plain term / payment / rate inputs "
                "and would drop the option amounts; reassess the "
                "options in a replacement contract instead.",
                count=len(certain),
            ))

    def _wipe_unposted_schedule(self):
        self.ensure_one()
        unposted = self.schedule_line_ids.filtered(lambda l: not l.is_posted)
        unposted.unlink()

    def _periodic_rate(self):
        self.ensure_one()
        annual = self.incremental_borrowing_rate / 100.0
        period_months = CADENCE_MONTHS[self.cadence]
        # Convert annual nominal rate compounded annually to per period
        # rate via (1+annual)^(period_months/12) - 1.
        return (1.0 + annual) ** (period_months / 12.0) - 1.0

    def _number_of_periods(self):
        self.ensure_one()
        period_months = CADENCE_MONTHS[self.cadence]
        term = self.effective_term_months
        if term % period_months:
            raise UserError(_(
                "Term (%(term)s months, including reasonably-certain "
                "extensions) must be a whole multiple of the cadence "
                "(%(months)s months).",
                term=term, months=period_months,
            ))
        return int(term // period_months)

    def _present_value_of_payments(self):
        """Present value of N equal lease-component payments at periodic
        rate r, plus the present value of any end-of-term balloon (a
        reasonably-certain purchase price or termination penalty,
        IFRS 16.27(d)/(e)) discounted over the full N periods."""
        self.ensure_one()
        n = self._number_of_periods()
        r = self._periodic_rate()
        pmt = self._lease_component_payment()
        if r == 0:
            pv = pmt * n
        else:
            pv = pmt * (1.0 - (1.0 + r) ** (-n)) / r
            if self.payment_timing == 'advance':
                pv = pv * (1.0 + r)
        balloon = self._end_of_term_balloon()
        if balloon:
            pv += balloon / (1.0 + r) ** n if r else balloon
        return pv

    # ---- IFRS 16.71-74 manufacturer / dealer lessor measurement ----

    def _pv_unguaranteed_residual(self):
        """Present value of the unguaranteed residual value at the rate
        implicit in the lease over the full term (IFRS 16.70(b))."""
        self.ensure_one()
        residual = self.unguaranteed_residual_value or 0.0
        if not residual:
            return 0.0
        n = self._number_of_periods()
        r = self._periodic_rate()
        return residual / (1.0 + r) ** n if r else residual

    def _dealer_measurement(self):
        """IFRS 16.71-74 commencement measurement for a manufacturer /
        dealer finance lessor. Returns a dict of the four rounded
        figures:

        * net_investment = PV(lease payments) + PV(unguaranteed residual)
        * revenue        = lower of fair value and PV(lease payments)
        * cost_of_sale   = carrying amount - PV(unguaranteed residual)
        * selling_profit = revenue - cost_of_sale

        The revenue is discounted at 'a market rate of interest'; this
        module uses the rate implicit already entered (the common case
        where that rate reflects the market), so PV(lease payments) is
        the annuity PV of the lease-component payments.
        """
        self.ensure_one()
        pv_payments = self._present_value_of_payments()
        pv_residual = self._pv_unguaranteed_residual()
        revenue = self.currency_id.round(
            min(self.fair_value_of_asset or 0.0, pv_payments),
        )
        cost_of_sale = self.currency_id.round(
            (self.carrying_amount_of_asset or 0.0) - pv_residual,
        )
        selling_profit = self.currency_id.round(revenue - cost_of_sale)
        # The payment component of the net investment equals the
        # recognised selling revenue: when the rate is at or above market
        # (fair value >= PV of payments) revenue IS the PV of payments; a
        # below-market rate restricts BOTH the revenue and the receivable
        # to the fair value (IFRS 16.71-72), which keeps the commencement
        # entry balanced.
        net_investment = self.currency_id.round(revenue + pv_residual)
        return {
            'pv_payments': self.currency_id.round(pv_payments),
            'pv_residual': self.currency_id.round(pv_residual),
            'net_investment': net_investment,
            'revenue': revenue,
            'cost_of_sale': cost_of_sale,
            'selling_profit': selling_profit,
        }

    def _validate_rou_depreciation_span(self):
        """A reasonably-certain purchase option depreciates the ROU over
        the underlying asset's useful life (IFRS 16.32); validate the
        inputs make a well-formed schedule."""
        self.ensure_one()
        if not self._has_certain_purchase_option():
            return
        period_months = CADENCE_MONTHS[self.cadence]
        useful = self.underlying_useful_life_months
        if useful <= 0:
            raise UserError(_(
                "A reasonably-certain purchase option requires the "
                "underlying asset's useful life (in months): the ROU "
                "asset depreciates over that life, not the lease term "
                "(IFRS 16.32).",
            ))
        if useful < self.effective_term_months:
            raise UserError(_(
                "The underlying asset's useful life (%(life)s months) "
                "cannot be shorter than the lease term (%(term)s "
                "months).",
                life=useful, term=self.effective_term_months,
            ))
        if (useful - self.effective_term_months) % period_months:
            raise UserError(_(
                "The useful life must exceed the term by a whole "
                "multiple of the cadence (%(months)s months) so the "
                "post-term depreciation rows align to periods.",
                months=period_months,
            ))

    def _build_schedule(self):
        self.ensure_one()
        if self.exemption != 'none':
            return self._build_exempt_schedule()
        if self.lessor_mode == 'operating':
            return self._build_operating_lessor_schedule()
        # Lessee ROU/liability model and finance-lessor net investment
        # share the same amortisation arithmetic; the finance lessor
        # simply carries no ROU (the liability fields carry the net
        # investment receivable) and posts income legs instead.
        is_finance_lessor = self.lessor_mode == 'finance'
        self._validate_rou_depreciation_span()
        Line = self.env['eh.lease.schedule.line']
        n = self._number_of_periods()
        r = self._periodic_rate()
        pmt = self._lease_component_payment()
        service_pmt = (
            0.0 if is_finance_lessor else self._service_component_payment()
        )

        # Manufacturer / dealer finance lessor: the net investment
        # includes the PV of the unguaranteed residual, and the
        # receivable amortises down to the UNDISCOUNTED residual
        # (recovered when the asset returns), not to zero
        # (IFRS 16.70(b)/.74).
        is_dealer = is_finance_lessor and self.lessor_dealer
        residual_terminal = (
            self.unguaranteed_residual_value or 0.0 if is_dealer else 0.0
        )
        if is_dealer:
            # Net investment = recognised selling revenue + PV of the
            # unguaranteed residual (IFRS 16.71-74); the receivable opens
            # here and amortises to the undiscounted residual.
            liability = self._dealer_measurement()['net_investment']
        else:
            liability = self._present_value_of_payments()
        rou_initial = 0.0 if is_finance_lessor else (
            liability
            + (self.initial_direct_costs or 0.0)
            + (self.prepaid_lease_payments or 0.0)
        )

        self.write({
            'liability_initial_value': self.currency_id.round(liability),
            'rou_initial_value': self.currency_id.round(rou_initial),
        })

        period_months = CADENCE_MONTHS[self.cadence]
        period_date = self._first_period_date()
        running = liability
        rou_accumulated = 0.0
        rou_months = self._rou_depreciation_months()
        rou_per_month = (
            rou_initial / rou_months if rou_months else 0.0
        )
        # Rows carrying ROU depreciation: the payment rows, plus - when
        # a reasonably-certain purchase option stretches depreciation
        # over the useful life (IFRS 16.32) - depreciation-only rows
        # after the payments end.
        extra_rou_rows = 0
        if rou_initial and rou_months > self.effective_term_months:
            extra_rou_rows = int(
                (rou_months - self.effective_term_months) // period_months,
            )
        total_rows = n + extra_rou_rows

        rows = self._compute_amortisation_rows(
            opening_liability=running, n=n, r=r, pmt=pmt,
            terminal_balance=self.currency_id.round(residual_terminal),
        )
        for n_idx, row in enumerate(rows, start=1):
            is_last_rou = (n_idx == total_rows)
            if is_last_rou:
                rou_amount = rou_initial - rou_accumulated
            else:
                rou_amount = rou_per_month * period_months
            rou_amount = self.currency_id.round(max(0.0, rou_amount))
            rou_accumulated = self.currency_id.round(
                rou_accumulated + rou_amount
            )

            Line.create({
                'lease_id': self.id,
                'sequence': n_idx,
                'period_date': period_date,
                'liability_open': row['liability_open'],
                'payment_amount': row['payment_amount'],
                'service_amount': service_pmt,
                'interest': row['interest'],
                'principal': row['principal'],
                'liability_close': row['liability_close'],
                'rou_amount': rou_amount,
                'rou_accumulated': rou_accumulated,
            })
            period_date = self._next_period_date(period_date, period_months)

        # IFRS 16.32 tail: depreciation-only rows after the last payment.
        for k_idx in range(n + 1, total_rows + 1):
            if k_idx == total_rows:
                rou_amount = rou_initial - rou_accumulated
            else:
                rou_amount = rou_per_month * period_months
            rou_amount = self.currency_id.round(max(0.0, rou_amount))
            rou_accumulated = self.currency_id.round(
                rou_accumulated + rou_amount
            )
            Line.create({
                'lease_id': self.id,
                'sequence': k_idx,
                'period_date': period_date,
                'liability_open': 0.0,
                'payment_amount': 0.0,
                'service_amount': 0.0,
                'interest': 0.0,
                'principal': 0.0,
                'liability_close': 0.0,
                'rou_amount': rou_amount,
                'rou_accumulated': rou_accumulated,
            })
            period_date = self._next_period_date(period_date, period_months)

    def _build_exempt_schedule(self):
        """IFRS 16.6: an exempt (short-term / low-value) lease recognises
        its payments as an expense on a straight-line basis over the
        term. The module supports equal fixed payments, so the
        straight-line per-period expense equals the payment; each row
        posts Dr Lease Expense / Cr Cash and carries no liability, no
        interest and no ROU."""
        self.ensure_one()
        Line = self.env['eh.lease.schedule.line']
        n = self._number_of_periods()
        pmt = self.payment_amount
        self.write({
            'liability_initial_value': 0.0,
            'rou_initial_value': 0.0,
        })
        period_months = CADENCE_MONTHS[self.cadence]
        period_date = self._first_period_date()
        for n_idx in range(1, n + 1):
            Line.create({
                'lease_id': self.id,
                'sequence': n_idx,
                'period_date': period_date,
                'liability_open': 0.0,
                'payment_amount': pmt,
                'service_amount': 0.0,
                'interest': 0.0,
                'principal': 0.0,
                'liability_close': 0.0,
                'rou_amount': 0.0,
                'rou_accumulated': 0.0,
            })
            period_date = self._next_period_date(period_date, period_months)

    def _build_operating_lessor_schedule(self):
        """IFRS 16.81: an operating lessor recognises lease payments as
        income on a straight-line basis; the underlying asset stays on
        the books (its depreciation continues in the asset register).
        Each row posts Dr Cash / Cr Rental Income."""
        self.ensure_one()
        Line = self.env['eh.lease.schedule.line']
        n = self._number_of_periods()
        pmt = self.payment_amount
        self.write({
            'liability_initial_value': 0.0,
            'rou_initial_value': 0.0,
        })
        period_months = CADENCE_MONTHS[self.cadence]
        period_date = self._first_period_date()
        for n_idx in range(1, n + 1):
            Line.create({
                'lease_id': self.id,
                'sequence': n_idx,
                'period_date': period_date,
                'liability_open': 0.0,
                'payment_amount': pmt,
                'service_amount': 0.0,
                'interest': 0.0,
                'principal': 0.0,
                'liability_close': 0.0,
                'rou_amount': 0.0,
                'rou_accumulated': 0.0,
            })
            period_date = self._next_period_date(period_date, period_months)

    def _compute_amortisation_rows(self, opening_liability, n, r, pmt,
                                   terminal_balance=0.0):
        """Return amortisation rows whose schedule and journal posting agree.

        Invariant: principal + interest == payment_amount on every row, and
        liability_close == liability_open - principal. The journal entry
        posts Dr Liability=principal, Dr Interest=interest, Cr Cash=pmt,
        which balances exactly. The last row trues up so that the closing
        liability lands on the terminal balance (zero by default; the
        unguaranteed residual value for a manufacturer / dealer finance
        lessor, whose net investment amortises down to the residual
        recovered when the asset returns, not through the receipts,
        IFRS 16.70(b)/.74) regardless of fixed payment rounding.
        """
        rows = []
        running = opening_liability
        for n_idx in range(1, n + 1):
            is_last = (n_idx == n)
            if self.payment_timing == 'advance':
                # In advance: payment first, interest accrues on the
                # post-payment balance over the period and is recapitalised.
                # principal[i] = pmt - interest[i]
                # interest[i] = (running - pmt) * r
                # liability_close[i] = running - principal[i]
                if is_last and not terminal_balance:
                    interest_raw = 0.0
                    principal_raw = running
                    period_pmt_raw = running
                elif is_last:
                    # Dealer residual: recover everything above the
                    # terminal balance, interest on the post-payment base.
                    interest_raw = max(0.0, (running - pmt) * r)
                    principal_raw = running - terminal_balance
                    period_pmt_raw = principal_raw + interest_raw
                else:
                    interest_raw = max(0.0, (running - pmt) * r)
                    principal_raw = pmt - interest_raw
                    period_pmt_raw = pmt
            else:
                # In arrears: interest accrues on the opening balance and
                # is settled at period end with the payment.
                if is_last:
                    interest_raw = running * r
                    principal_raw = running - terminal_balance
                    period_pmt_raw = principal_raw + interest_raw
                else:
                    interest_raw = max(0.0, running * r)
                    principal_raw = pmt - interest_raw
                    period_pmt_raw = pmt

            interest = self.currency_id.round(max(0.0, interest_raw))
            period_pmt = self.currency_id.round(max(0.0, period_pmt_raw))
            # Re-derive principal from rounded values so the journal entry
            # balances exactly: principal + interest == payment_amount.
            principal = self.currency_id.round(period_pmt - interest)
            liability_close = self.currency_id.round(
                max(0.0, running - principal)
            )
            rows.append({
                'liability_open': self.currency_id.round(running),
                'payment_amount': period_pmt,
                'interest': interest,
                'principal': principal,
                'liability_close': liability_close,
            })
            running = liability_close
        return rows

    def _first_period_date(self):
        self.ensure_one()
        period_months = CADENCE_MONTHS[self.cadence]
        if self.payment_timing == 'advance':
            return self._month_end(self.commencement_date)
        d = self.commencement_date + relativedelta(months=period_months)
        return self._month_end(d)

    @staticmethod
    def _month_end(d):
        last = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, last)

    def _next_period_date(self, current, months):
        nxt = current + relativedelta(months=months)
        return self._month_end(nxt)

    def _post_opening_entry(self):
        self.ensure_one()
        # IFRS 16.6: an exempt lease recognises no ROU asset and no
        # liability; there is no opening entry, expense posts per period.
        if self.exemption != 'none':
            return None
        # IFRS 16.81: an operating lessor keeps the underlying asset on
        # its books; income posts per period, no opening entry.
        if self.lessor_mode == 'operating':
            return None
        # IFRS 16.67: a finance lessor recognises the net investment in
        # the lease at commencement.
        if self.lessor_mode == 'finance':
            # IFRS 16.71-74: a manufacturer / dealer lessor recognises
            # selling profit or loss at commencement:
            #   Dr Net investment (PV payments + PV unguaranteed residual)
            #   Dr Cost of sale (carrying amount - PV unguaranteed residual)
            #     Cr Selling revenue (lower of fair value, PV of payments)
            #     Cr Asset derecognition (carrying amount of the asset)
            if self.lessor_dealer:
                m = self._dealer_measurement()
                move = self.env['account.move'].create({
                    'move_type': 'entry',
                    'eh_sealed': True,
                    'date': self.commencement_date,
                    'journal_id': self.journal_id.id,
                    'ref': _("Dealer finance lease %s", self.display_name),
                    'line_ids': [
                        (0, 0, {
                            'name': _("Net investment %s",
                                      self.display_name),
                            'account_id': self.net_investment_account_id.id,
                            'debit': m['net_investment'],
                            'credit': 0.0,
                        }),
                        (0, 0, {
                            'name': _("Cost of sale %s", self.display_name),
                            'account_id': (
                                self.dealer_cost_of_sale_account_id.id
                            ),
                            'debit': m['cost_of_sale'],
                            'credit': 0.0,
                        }),
                        (0, 0, {
                            'name': _("Selling revenue %s",
                                      self.display_name),
                            'account_id': self.dealer_revenue_account_id.id,
                            'debit': 0.0,
                            'credit': m['revenue'],
                        }),
                        (0, 0, {
                            'name': _("Underlying asset derecognition %s",
                                      self.display_name),
                            'account_id': (
                                self.lessor_counterpart_account_id.id
                            ),
                            'debit': 0.0,
                            'credit': self.currency_id.round(
                                self.carrying_amount_of_asset or 0.0,
                            ),
                        }),
                    ],
                })
                move.action_post()
                return move
            # Simple (non-dealer) finance lessor: Dr Net Investment (PV of
            # payments), Cr Asset Derecognition counterpart.
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'eh_sealed': True,
                'date': self.commencement_date,
                'journal_id': self.journal_id.id,
                'ref': _("Finance lease net investment %s", self.display_name),
                'line_ids': [
                    (0, 0, {
                        'name': _("Net investment %s", self.display_name),
                        'account_id': self.net_investment_account_id.id,
                        'debit': self.liability_initial_value,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': _("Underlying asset derecognition %s",
                                  self.display_name),
                        'account_id': self.lessor_counterpart_account_id.id,
                        'debit': 0.0,
                        'credit': self.liability_initial_value,
                    }),
                ],
            })
            move.action_post()
            return move
        # Lessee: Dr ROU Asset, Cr Lease Liability, plus initial direct
        # costs and prepaid lease payments (already capitalised into ROU).
        idc = self.initial_direct_costs or 0.0
        prepaid = self.prepaid_lease_payments or 0.0
        rou = self.rou_initial_value
        liab = self.liability_initial_value
        lines = [
            (0, 0, {
                'name': _("ROU asset opening %s", self.display_name),
                'account_id': self.rou_asset_account_id.id,
                'debit': rou,
                'credit': 0.0,
            }),
            (0, 0, {
                'name': _("Lease liability opening %s", self.display_name),
                'account_id': self.lease_liability_account_id.id,
                'debit': 0.0,
                'credit': liab,
            }),
        ]
        cash_credit = idc + prepaid
        if cash_credit > 0:
            lines.append((0, 0, {
                'name': _("Initial direct costs and prepayments %s", self.display_name),
                'account_id': self.cash_account_id.id,
                'debit': 0.0,
                'credit': self.currency_id.round(cash_credit),
            }))
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'eh_sealed': True,
            'date': self.commencement_date,
            'journal_id': self.journal_id.id,
            'ref': _("Lease opening %s", self.display_name),
            'line_ids': lines,
        })
        move.action_post()
        return move

    def _maybe_mark_ended(self):
        self = self._eh_workflow_action()
        for lease in self:
            if lease.state not in ('active', 'modified'):
                continue
            unposted = lease.schedule_line_ids.filtered(
                lambda l: not l.is_posted,
            )
            if not unposted:
                lease.state = 'ended'

    def _remaining_periods_from(self, anchor_date):
        """Count unposted schedule lines whose period_date >= anchor_date."""
        self.ensure_one()
        return len(self.schedule_line_ids.filtered(
            lambda l: not l.is_posted and l.period_date >= anchor_date,
        ))

    def _liability_balance_after_last_post(self):
        self.ensure_one()
        posted = self.schedule_line_ids.filtered(
            lambda l: l.is_posted,
        ).sorted('sequence')
        if posted:
            return posted[-1].liability_close
        return self.liability_initial_value

    def _rou_carrying_amount(self):
        self.ensure_one()
        posted = self.schedule_line_ids.filtered(lambda l: l.is_posted)
        accumulated = sum(posted.mapped('rou_amount'))
        return self.rou_initial_value - accumulated

    # ---- cron ----

    @api.model
    def _cron_post_due(self, batch_size=200):
        today = fields.Date.context_today(self)
        leases = self.search([
            ('state', 'in', ['active', 'modified']),
        ], limit=batch_size)
        for lease in leases:
            try:
                due = lease.schedule_line_ids.filtered(
                    lambda l: not l.is_posted and l.period_date <= today,
                ).sorted('sequence')
                for line in due:
                    line.action_post()
                lease._maybe_mark_ended()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Auto post failed for lease %s: %s",
                    lease.display_name, exc,
                )

    @api.depends('name', 'lessor_id', 'lessor_id.display_name')
    def _compute_display_name(self):
        for lease in self:
            if lease.lessor_id:
                lease.display_name = "%s / %s" % (
                    lease.name or '', lease.lessor_id.display_name,
                )
            else:
                lease.display_name = lease.name or ''
