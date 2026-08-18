# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.revenue.contract: an IFRS 15 customer contract.

Recognition posts the incremental revenue since the last run, crediting
revenue and debiting first any contract liability (billed ahead of
performance) then the contract asset. Billing debits the receivable and
credits first the contract asset then the contract liability. The two
mechanics keep the net contract position (cumulative recognised less
cumulative billed) correct on the balance sheet at all times.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError  # noqa: F401
from odoo.tools import float_compare


class EhRevenueContract(models.Model):
    _name = 'eh.revenue.contract'
    _description = "Revenue contract (IFRS 15)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'contract_date desc, id desc'
    _rec_name = 'name'

    # Workflow-critical field: only the contract's own transitions
    # (action_activate / action_close / action_cancel and the modification
    # path that activates a spun-off separate contract) may change state. A
    # direct RPC write to state, skipping those actions and their journal
    # entries, is refused by eh.workflow.guard.write().
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('active', "Active"),
         ('done', "Closed"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    partner_id = fields.Many2one(
        'res.partner', string="Customer", required=True, tracking=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    contract_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)

    transaction_price = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True,
        help="Total consideration the entity expects to be entitled to "
             "(IFRS 15.47).")
    obligation_ids = fields.One2many(
        'eh.revenue.obligation', 'contract_id', copy=True)
    total_ssp = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Sum of the obligations' standalone selling prices.")
    amount_recognised = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    amount_billed = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')
    contract_asset = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Revenue recognised but not yet billed (IFRS 15.107).")
    contract_liability = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Amounts billed but not yet recognised (IFRS 15.106).")

    bill_amount = fields.Monetary(
        currency_field='currency_id',
        help="Amount to bill on the next Record Billing action.")

    # ---- significant financing component (IFRS 15.60-65) ----
    # Opt-in per contract. When off, financing_pv equals the transaction price
    # and recognition posts exactly as before (byte-identical default).
    financing_component = fields.Boolean(
        tracking=True,
        help="Payment timing differs materially from the transfer of goods "
             "or services, so the promised consideration is discounted to "
             "present value and the difference recognised as interest over "
             "time (IFRS 15.60-65).")
    financing_direction = fields.Selection(
        [('advance', "Customer pays in advance"),
         ('arrears', "Customer pays in arrears")],
        default='arrears',
        help="Payment in arrears: the entity finances the customer and earns "
             "interest income. Payment in advance: the customer finances the "
             "entity and it incurs interest expense (IFRS 15.61).")
    financing_rate = fields.Float(
        digits=(7, 4), default=0.0,
        help="Annual discount rate used to reflect the financing, i.e. the "
             "rate in a separate financing transaction between the parties "
             "(IFRS 15.64).")
    financing_period_months = fields.Integer(
        default=0,
        help="Number of months between payment and transfer used to "
             "discount the consideration to present value.")
    financing_payment_date = fields.Date(
        tracking=True,
        help="Date the financed cash flow is due (the payment date in "
             "arrears, or the transfer date in advance). When set, the "
             "financing interest accretes on a time / effective-interest "
             "basis between the contract date and this date, independent of "
             "performance-obligation progress (IFRS 15.65), so a "
             "point-in-time transfer does not front-load the whole interest "
             "at recognition. Leave empty to keep the interest recognised in "
             "step with revenue progress.")
    financing_pv = fields.Monetary(
        compute='_compute_financing', store=True, currency_field='currency_id',
        help="Present value of the transaction price after removing the "
             "financing component; the amount recognised as revenue.")
    financing_component_amount = fields.Monetary(
        compute='_compute_financing', store=True, currency_field='currency_id',
        help="Total interest recognised over the financing period: the "
             "transaction price less its present value.")
    financing_interest_recognised = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Cumulative interest income or expense already posted.")
    financing_account_id = fields.Many2one(
        'account.account', string="Interest Account", tracking=True,
        help="Interest income (arrears) or interest expense (advance) "
             "account used for the financing component.")

    # ---- closure validation (IFRS 15) ----
    # action_close blocks while any obligation is unsatisfied. The manager
    # override releases the remaining contract liability to one of two
    # documented destinations before closing. All fields default off, so a
    # fully satisfied contract closes exactly as before.
    close_with_remainder = fields.Boolean(
        copy=False, tracking=True,
        help="Manager override: close the contract although performance "
             "obligations remain unsatisfied. Any remaining contract "
             "liability is released per the destination selection and the "
             "reason is recorded. A remaining contract asset is not "
             "touched; bill it before closing.")
    close_reason = fields.Text(
        copy=False,
        help="Why the contract is closed with unsatisfied obligations "
             "(required for the override close).")
    close_release_to = fields.Selection(
        [('income', "Release to profit or loss"),
         ('refund', "Reclassify to refund liability")],
        default='income',
        help="Destination of a remaining contract liability on an override "
             "close. Release to profit or loss when the entity expects to "
             "keep the billed-ahead consideration with no further "
             "performance and no refund due (the IFRS 15.B46 breakage "
             "pattern). Reclassify to a refund liability when the customer "
             "is owed the unperformed balance back (IFRS 15.55).")
    refund_liability_account_id = fields.Many2one(
        'account.account', string="Refund Liability Account", tracking=True,
        domain="[('account_type', 'in', ['liability_current'])]",
        help="Liability account that receives the unperformed balance when "
             "the override close reclassifies it as refundable.")
    close_released_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Contract liability released by an override close.")

    # ---- accounts ----
    revenue_account_id = fields.Many2one(
        'account.account', string="Revenue Account", tracking=True,
        domain="[('account_type', 'in', ['income', 'income_other'])]")
    contract_asset_account_id = fields.Many2one(
        'account.account', string="Contract Asset Account", tracking=True,
        domain="[('account_type', 'in', ['asset_current', 'asset_receivable'])]")
    contract_liability_account_id = fields.Many2one(
        'account.account', string="Contract Liability Account", tracking=True,
        domain="[('account_type', 'in', ['liability_current'])]")
    receivable_account_id = fields.Many2one(
        'account.account', string="Receivable Account", tracking=True,
        domain="[('account_type', '=', 'asset_receivable')]")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', 'in', ('sale', 'general'))]")

    move_ids = fields.One2many('account.move', 'eh_revenue_contract_id')
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    _sql_constraints = [
        ('check_price', 'CHECK (transaction_price >= 0)', 'Transaction price cannot be negative.'),
    ]

    @api.depends('transaction_price', 'obligation_ids.standalone_price',
                 'obligation_ids.recognised_amount', 'amount_billed',
                 'close_released_amount')
    def _compute_totals(self):
        for c in self:
            c.total_ssp = sum(c.obligation_ids.mapped('standalone_price'))
            c.amount_recognised = sum(
                c.obligation_ids.mapped('recognised_amount'))
            # A close-with-remainder release clears billed-ahead amounts out
            # of the contract liability (to P&L or a refund liability), so
            # it counts toward the net position exactly like recognition;
            # zero by default, leaving the original net untouched.
            net = (c.amount_recognised - c.amount_billed
                   + c.close_released_amount)
            c.contract_asset = max(net, 0.0)
            c.contract_liability = max(-net, 0.0)

    @api.depends('financing_component', 'financing_direction', 'financing_rate',
                 'financing_period_months', 'transaction_price')
    def _compute_financing(self):
        for c in self:
            currency = c.currency_id
            price = c.transaction_price
            if (c.financing_component and c.financing_rate
                    and c.financing_period_months):
                years = c.financing_period_months / 12.0
                factor = (1.0 + c.financing_rate) ** years
                if c.financing_direction == 'advance':
                    # Customer prepays cash now; revenue is that cash accreted
                    # forward to the transfer date, the excess is interest
                    # expense (IFRS 15.61, IE example 26).
                    revenue_base = price * factor
                else:
                    # Customer pays in arrears; revenue is the present value of
                    # the future payment, the shortfall is interest income
                    # (IFRS 15.64, IE example 29).
                    revenue_base = price / factor if factor else price
            else:
                revenue_base = price
            c.financing_pv = (
                currency.round(revenue_base) if currency else revenue_base)
            interest = c.financing_pv - price
            # Report the magnitude of the financing component; its sign is
            # implied by the direction (income in arrears, expense in advance).
            interest = interest if interest >= 0 else -interest
            c.financing_component_amount = (
                currency.round(interest) if currency else interest)

    def _financing_revenue_ratio(self):
        """Ratio of revenue to allocated (cash) amount for each recognised
        increment. Defaults to 1.0 so a contract without a financing component
        recognises exactly as before. Above 1.0 for advance (revenue exceeds
        cash, interest expense); below 1.0 for arrears (revenue is the present
        value, interest income)."""
        self.ensure_one()
        if (self.financing_component and self.transaction_price
                and self.financing_pv != self.transaction_price):
            return self.financing_pv / self.transaction_price
        return 1.0

    def _financing_is_time_based(self):
        """A financing component accretes on a time basis (IFRS 15.65) only
        when a payment date is set and the discount is genuinely in effect.
        Without a payment date the prior progress-driven behaviour is kept so
        existing contracts and tests are unaffected."""
        self.ensure_one()
        return bool(
            self.financing_payment_date
            and self.financing_component
            and self._financing_revenue_ratio() != 1.0)

    def _financing_signed_total_interest(self, currency):
        """Signed total interest over the whole financing period. Positive for
        advance (revenue exceeds cash, interest expense, a debit); negative for
        arrears (revenue below cash, interest income, a credit)."""
        self.ensure_one()
        return currency.round(self.financing_pv - self.transaction_price)

    def _financing_time_accreted_interest(self, currency, as_of=None):
        """Signed interest accreted from the contract date to as_of (today by
        default) on an effective-interest basis, capped at the payment date.

        The discount unwinds as the present value grows toward the nominal
        cash flow: at elapsed fraction t of the financing period the accreted
        interest is total_interest * ((1 + rate) ** (t * years) - 1) / factor,
        which is 0 at the contract date and the full interest at the payment
        date. This is driven purely by elapsed time, independent of
        performance-obligation progress, so a point-in-time transfer accretes
        the interest over the period rather than posting it all at t0."""
        self.ensure_one()
        total = self._financing_signed_total_interest(currency)
        if not total:
            return 0.0
        start = self.contract_date
        end = self.financing_payment_date
        if not end or not start or end <= start:
            return total
        as_of = as_of or fields.Date.context_today(self)
        if as_of <= start:
            return 0.0
        if as_of >= end:
            return total
        span = (end - start).days
        elapsed = (as_of - start).days
        fraction = elapsed / span if span else 1.0
        years = self.financing_period_months / 12.0
        factor = (1.0 + self.financing_rate) ** years
        if factor in (0.0, 1.0):
            # Degenerate rate/period: fall back to straight-line accretion.
            accreted = total * fraction
        else:
            grown = (1.0 + self.financing_rate) ** (fraction * years) - 1.0
            accreted = total * grown / (factor - 1.0)
        return currency.round(accreted)

    def _compute_move_count(self):
        for c in self:
            c.move_count = len(c.move_ids)

    # Account fields that must not move once revenue has posted: re-pointing
    # them would silently re-base already-posted entries.
    _POSTED_LOCKED = frozenset({
        'revenue_account_id', 'contract_asset_account_id',
        'contract_liability_account_id', 'receivable_account_id',
        'journal_id', 'transaction_price', 'company_id',
        # The financing parameters and interest account change the revenue /
        # interest split; re-basing them after posting would silently restate
        # already-recognised revenue and interest with no matching entry.
        'financing_component', 'financing_direction', 'financing_rate',
        'financing_period_months', 'financing_account_id',
        'financing_payment_date',
    })

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.revenue.contract') or '/'
        return super().create(vals_list)

    def write(self, vals):
        # Once any revenue has posted, freeze the accounts, journal, price and
        # company: changing them would re-base recognised revenue with no
        # matching journal entry, silently overstating (or understating) it.
        # A sanctioned contract modification (IFRS 15.18-21) is the one path
        # allowed to revise transaction_price after posting; it re-runs
        # recognition so a balanced catch-up posts. Everything else in
        # _POSTED_LOCKED stays frozen even under that context.
        locked = set(self._POSTED_LOCKED)
        if self.env.context.get('eh_revenue_modification'):
            locked.discard('transaction_price')
        if locked.intersection(vals):
            for c in self:
                if c._has_posted_revenue():
                    raise UserError(_(
                        "Contract %s has posted revenue; its accounts, "
                        "journal and transaction price are frozen. Post a "
                        "correction to adjust the recognised amount.",
                        c.display_name))
        return super().write(vals)

    def unlink(self):
        # Once revenue or billing has posted, the contract carries the
        # posting-move link; deleting the master would orphan posted GL
        # entries. Block it. A draft or unposted contract has no move and
        # stays deletable.
        posted = self.filtered(lambda c: c._has_posted_revenue())
        if posted:
            raise UserError(_(
                "A contract with posted revenue or billing cannot be "
                "deleted; its journal entries would be orphaned. Reverse "
                "the entries and cancel it instead."))
        return super().unlink()

    def _has_posted_revenue(self):
        self.ensure_one()
        return bool(self.amount_recognised) or bool(self.amount_billed)

    # ---- transitions ----

    def action_activate(self):
        self = self._eh_workflow_action()
        for c in self:
            if c.state != 'draft':
                raise UserError(_("Only draft contracts can be activated."))
            if not c.obligation_ids:
                raise UserError(_(
                    "Contract %s has no performance obligations.",
                    c.display_name))
            if not c.total_ssp:
                raise UserError(_(
                    "The obligations on %s have a zero total standalone "
                    "selling price; the transaction price cannot be "
                    "allocated.", c.display_name))
            c.state = 'active'

    def action_close(self):
        # Closing validates completion: every over-time obligation must be
        # at 100% and every point-in-time obligation satisfied. The one way
        # past an incomplete contract is the manager override
        # (close_with_remainder), which requires a reason and releases any
        # remaining contract liability to P&L or a refund liability before
        # the state flips.
        self = self._eh_workflow_action()
        for c in self:
            if c.state != 'active':
                raise UserError(_("Only active contracts can be closed."))
            open_obs = c.obligation_ids.filtered(lambda o: (
                (o.satisfaction == 'over_time'
                 and float_compare(o.percent_complete, 100.0,
                                   precision_digits=2) < 0)
                or (o.satisfaction == 'point_in_time' and not o.satisfied)))
            if open_obs and not c.close_with_remainder:
                raise UserError(_(
                    "Contract %(contract)s cannot be closed: %(names)s not "
                    "fully satisfied. Complete the obligations, or set "
                    "Close With Remainder with a manager reason to release "
                    "the remaining balance.",
                    contract=c.display_name,
                    names=', '.join(open_obs.mapped('name'))))
            if c.close_with_remainder:
                c._check_manager()
                if not (c.close_reason or '').strip():
                    raise UserError(_(
                        "Closing %s with a remainder requires a reason.",
                        c.display_name))
                c._close_release_remainder()
            c.state = 'done'

    def _close_release_remainder(self):
        """Release the remaining contract liability on an override close.

        Two documented destinations (close_release_to):

        * 'income': the entity keeps the billed-ahead consideration; no
          further performance is expected and no refund is due, so the
          liability is derecognised to profit or loss (the IFRS 15.B46
          breakage pattern). Entry: Dr contract liability / Cr revenue.
        * 'refund': the unperformed balance is owed back to the customer,
          so the contract liability is reclassified as a refund liability
          (IFRS 15.55). Entry: Dr contract liability / Cr refund liability.

        Balanced by construction; a zero remainder posts nothing. Returns
        True when an entry posted."""
        self.ensure_one()
        currency = self.currency_id
        remainder = currency.round(self.contract_liability)
        if currency.compare_amounts(remainder, 0.0) <= 0:
            return False
        if not self.journal_id or not self.contract_liability_account_id:
            raise UserError(_(
                "Configure the journal and contract liability account on "
                "%s before closing with a remainder.", self.display_name))
        if self.close_release_to == 'refund':
            if not self.refund_liability_account_id:
                raise UserError(_(
                    "Configure the refund liability account on %s to close "
                    "with a refundable remainder.", self.display_name))
            credit_account = self.refund_liability_account_id
            credit_label = _(
                "Unperformed balance reclassified to refund %s", self.name)
        else:
            if not self.revenue_account_id:
                raise UserError(_(
                    "Configure the revenue account on %s to release the "
                    "remainder to profit or loss.", self.display_name))
            credit_account = self.revenue_account_id
            credit_label = _(
                "Remaining contract liability released %s", self.name)
        self._post_move([
            (0, 0, {
                'name': _("Contract liability closed out %s", self.name),
                'account_id': self.contract_liability_account_id.id,
                'debit': remainder, 'credit': 0.0,
            }),
            (0, 0, {
                'name': credit_label,
                'account_id': credit_account.id,
                'debit': 0.0, 'credit': remainder,
            }),
        ])
        self.close_released_amount += remainder
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        for c in self:
            if c.amount_recognised or c.amount_billed:
                raise UserError(_(
                    "Cannot cancel %s: revenue or billing has been posted. "
                    "Reverse the entries first.", c.display_name))
            c.state = 'cancelled'

    def action_recognise(self):
        self.ensure_one()
        self._check_manager()
        if self.state != 'active':
            raise UserError(_("Revenue is recognised on active contracts."))
        self._validate_accounts(billing=False)
        currency = self.currency_id
        increment = currency.round(
            sum(self.obligation_ids.mapped('to_recognise')))
        cmp_zero = currency.compare_amounts(increment, 0.0)
        if cmp_zero == 0:
            raise UserError(_(
                "There is no revenue to recognise on %s.", self.display_name))
        ratio = self._financing_revenue_ratio()
        time_based = self._financing_is_time_based()
        if cmp_zero < 0:
            # Downward correction: the target recognised revenue has dropped
            # below what has already been posted (for example a reduced
            # percentage of completion or a de-satisfied obligation). Post a
            # balanced reversing entry that debits revenue and credits the
            # contract asset first, then the contract liability, mirroring the
            # forward recognition split. Manager-gated by _check_manager above.
            lines = self._recognition_reversal_lines(currency, -increment)
        else:
            lines = self._recognition_lines(currency, increment)
        self._post_move(lines)
        # recognised_amount is guarded on eh.revenue.obligation by the shared
        # eh.workflow.guard mixin: only a server-initiated (env.su) write may
        # advance the cumulative-posted anchor. This sanctioned run (after
        # _check_manager and the posted GL move above) elevates through
        # _eh_workflow_write, so the guarded write is proven server-side rather
        # than by a forgeable context flag.
        for ob in self.obligation_ids:
            ob._eh_workflow_write({'recognised_amount': ob.target_recognised})
        if ratio != 1.0 and not time_based:
            # Progress-driven mode (no payment date): track cumulative
            # financing interest (income positive, expense negative) alongside
            # the posted revenue. The reversal path passes a negative
            # increment, so use the signed increment here.
            self.financing_interest_recognised += currency.round(
                increment * ratio - increment)
        if time_based:
            # Time-based mode (IFRS 15.65): the recognition entry booked only
            # the present-value revenue and its asset leg; the discount unwinds
            # on its own time schedule, so post the interest accreted to date.
            self._post_financing_accretion()
        return True

    def action_accrue_financing(self):
        """Post the financing interest accreted to date on a time basis
        (IFRS 15.65), independent of performance-obligation progress. For a
        point-in-time obligation this lets the discount unwind over the
        financing period instead of front-loading the whole interest at the
        moment of transfer."""
        self.ensure_one()
        self._check_manager()
        if self.state != 'active':
            raise UserError(_("Interest is accrued on active contracts."))
        if not self._financing_is_time_based():
            raise UserError(_(
                "Contract %s has no time-based financing component to "
                "accrue. Set a financing payment date first.",
                self.display_name))
        self._validate_accounts(billing=False)
        if not self._post_financing_accretion():
            raise UserError(_(
                "There is no financing interest to accrue on %s yet.",
                self.display_name))
        return True

    def _post_financing_accretion(self):
        """Post the delta between the time-accreted interest to date and the
        interest already recognised. Returns True when an entry was posted.

        Arrears (interest income, signed total negative): the discount unwinds
        as a credit to interest income against a debit that grows the contract
        asset toward the nominal cash flow. Advance (interest expense, signed
        total positive): a debit to interest expense against a credit to the
        contract liability. Balanced by construction."""
        self.ensure_one()
        currency = self.currency_id
        accreted = self._financing_time_accreted_interest(currency)
        delta = currency.round(accreted - self.financing_interest_recognised)
        if currency.compare_amounts(delta, 0.0) == 0:
            return False
        if delta < 0:
            # Arrears: interest income earned this period (a credit); the
            # matching debit grows the contract asset (the gross receivable
            # accretes from present value toward the nominal amount).
            amount = -delta
            lines = [
                (0, 0, {
                    'name': _("Financing interest income %s", self.name),
                    'account_id': self.financing_account_id.id,
                    'debit': 0.0, 'credit': amount,
                }),
                (0, 0, {
                    'name': _("Financing interest accreted %s", self.name),
                    'account_id': self.contract_asset_account_id.id,
                    'debit': amount, 'credit': 0.0,
                }),
            ]
        else:
            # Advance: interest expense incurred this period (a debit); the
            # matching credit grows the contract liability (the deferred
            # revenue accretes toward the amount to be transferred).
            amount = delta
            lines = [
                (0, 0, {
                    'name': _("Financing interest expense %s", self.name),
                    'account_id': self.financing_account_id.id,
                    'debit': amount, 'credit': 0.0,
                }),
                (0, 0, {
                    'name': _("Financing interest accreted %s", self.name),
                    'account_id': self.contract_liability_account_id.id,
                    'debit': 0.0, 'credit': amount,
                }),
            ]
        self._post_move(lines)
        self.financing_interest_recognised += delta
        return True

    def _recognition_lines(self, currency, increment):
        """Forward recognition: credit revenue, debit contract liability
        (released) then contract asset. increment is positive and is the
        cash-basis amount that clears against billing.

        When a significant financing component is present (IFRS 15.60-65) the
        revenue credit is the present-value portion of the increment and the
        difference is posted to the interest account: interest income (a
        credit) for a customer paying in arrears, interest expense (a debit)
        for a customer paying in advance. The contract asset / liability leg
        still moves by the full cash-basis increment, so the entry stays
        balanced by construction and billing clears it exactly.

        In time-based mode (a financing payment date is set) the interest is
        no longer embedded here: it accretes on its own time schedule through
        _post_financing_accretion. This entry then books only the present-value
        revenue and moves the contract asset / liability by that same present
        value, so it stays balanced and the interest is not front-loaded at
        the moment of transfer (IFRS 15.65)."""
        ratio = self._financing_revenue_ratio()
        time_based = self._financing_is_time_based()
        # In time-based mode the asset / liability leg tracks the present-value
        # revenue (interest is accreted separately); otherwise it tracks the
        # full cash-basis increment and carries the interest split.
        moved = currency.round(increment * ratio) if time_based else increment
        liability_before = max(self.amount_billed - self.amount_recognised, 0.0)
        from_liability = currency.round(min(moved, liability_before))
        to_asset = currency.round(moved - from_liability)
        revenue = currency.round(increment * ratio)
        interest = 0.0 if time_based else currency.round(revenue - increment)
        lines = [(0, 0, {
            'name': _("Revenue recognised %s", self.name),
            'account_id': self.revenue_account_id.id,
            'debit': 0.0, 'credit': revenue,
        })]
        if interest > 0:
            # Advance: revenue exceeds the cash increment; the excess is
            # interest expense (a debit).
            lines.append((0, 0, {
                'name': _("Financing interest expense %s", self.name),
                'account_id': self.financing_account_id.id,
                'debit': interest, 'credit': 0.0,
            }))
        elif interest < 0:
            # Arrears: revenue is below the cash increment; the shortfall is
            # interest income (a credit).
            lines.append((0, 0, {
                'name': _("Financing interest income %s", self.name),
                'account_id': self.financing_account_id.id,
                'debit': 0.0, 'credit': -interest,
            }))
        if from_liability:
            lines.append((0, 0, {
                'name': _("Contract liability released %s", self.name),
                'account_id': self.contract_liability_account_id.id,
                'debit': from_liability, 'credit': 0.0,
            }))
        if to_asset:
            lines.append((0, 0, {
                'name': _("Contract asset %s", self.name),
                'account_id': self.contract_asset_account_id.id,
                'debit': to_asset, 'credit': 0.0,
            }))
        return lines

    def _recognition_reversal_lines(self, currency, amount):
        """Downward correction: debit revenue, credit the contract asset first
        (unwinding recognised-not-billed), then the contract liability. amount
        is the positive cash-basis magnitude of the reduction. Balanced by
        construction: the debits (revenue plus any interest expense reversed)
        equal the credits (asset plus liability, plus any interest income
        reversed).

        With a financing component the revenue debit is only the present-value
        portion of the reversal; the interest leg is reversed in the opposite
        direction to the forward entry. The asset / liability credit still
        moves by the full cash-basis amount so billing continues to clear.

        In time-based mode the interest is not embedded here (it accretes on
        its own time schedule); the revenue debit and the asset / liability
        credit both move by the present value so the entry stays balanced."""
        ratio = self._financing_revenue_ratio()
        time_based = self._financing_is_time_based()
        moved = currency.round(amount * ratio) if time_based else amount
        asset_before = max(self.amount_recognised - self.amount_billed, 0.0)
        cr_asset = currency.round(min(moved, asset_before))
        cr_liability = currency.round(moved - cr_asset)
        revenue = currency.round(amount * ratio)
        interest = 0.0 if time_based else currency.round(revenue - amount)
        lines = [(0, 0, {
            'name': _("Revenue correction %s", self.name),
            'account_id': self.revenue_account_id.id,
            'debit': revenue, 'credit': 0.0,
        })]
        if interest > 0:
            # Advance forward posted interest expense (a debit); reverse it as
            # a credit here.
            lines.append((0, 0, {
                'name': _("Financing interest reversed %s", self.name),
                'account_id': self.financing_account_id.id,
                'debit': 0.0, 'credit': interest,
            }))
        elif interest < 0:
            # Arrears forward posted interest income (a credit); reverse it as
            # a debit here.
            lines.append((0, 0, {
                'name': _("Financing interest reversed %s", self.name),
                'account_id': self.financing_account_id.id,
                'debit': -interest, 'credit': 0.0,
            }))
        if cr_asset:
            lines.append((0, 0, {
                'name': _("Contract asset reversed %s", self.name),
                'account_id': self.contract_asset_account_id.id,
                'debit': 0.0, 'credit': cr_asset,
            }))
        if cr_liability:
            lines.append((0, 0, {
                'name': _("Contract liability restored %s", self.name),
                'account_id': self.contract_liability_account_id.id,
                'debit': 0.0, 'credit': cr_liability,
            }))
        return lines

    def action_bill(self):
        self.ensure_one()
        self._check_manager()
        if self.state != 'active':
            raise UserError(_("Billing is recorded on active contracts."))
        self._validate_accounts(billing=True)
        currency = self.currency_id
        amount = currency.round(self.bill_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive amount to bill."))
        asset_before = max(self.amount_recognised - self.amount_billed, 0.0)
        cr_asset = currency.round(min(amount, asset_before))
        cr_liability = currency.round(amount - cr_asset)
        lines = [(0, 0, {
            'name': _("Billing %s", self.name),
            'account_id': self.receivable_account_id.id,
            'partner_id': self.partner_id.id,
            'debit': amount, 'credit': 0.0,
        })]
        if cr_asset:
            lines.append((0, 0, {
                'name': _("Contract asset billed %s", self.name),
                'account_id': self.contract_asset_account_id.id,
                'debit': 0.0, 'credit': cr_asset,
            }))
        if cr_liability:
            lines.append((0, 0, {
                'name': _("Contract liability %s", self.name),
                'account_id': self.contract_liability_account_id.id,
                'debit': 0.0, 'credit': cr_liability,
            }))
        self._post_move(lines)
        self.amount_billed += amount
        self.bill_amount = 0.0
        return True

    # ---- contract modifications (IFRS 15.18-21) ----

    modification_ids = fields.One2many(
        'eh.revenue.modification', 'contract_id', readonly=True, copy=False,
        help="History of contract modifications applied (IFRS 15.18-21).")
    modification_count = fields.Integer(compute='_compute_modification_count')

    def _compute_modification_count(self):
        for c in self:
            c.modification_count = len(c.modification_ids)

    def _apply_modification(self, method, added_obligations=None,
                            new_transaction_price=None, description=None):
        """Apply an IFRS 15.18-21 contract modification.

        method:
          'separate'    - IFRS 15.20: the added distinct goods/services are
                          priced at their standalone selling price, so the
                          modification is a new, separate contract. A fresh
                          eh.revenue.contract is created for them; this
                          contract is untouched.
          'prospective' - IFRS 15.21(a): the remaining goods/services are
                          distinct from those already transferred. The
                          not-yet-recognised transaction price plus any added
                          consideration is reallocated across the remaining
                          (unsatisfied) obligations and any added obligations,
                          effective going forward. Already-posted revenue is
                          not disturbed.
          'catch_up'    - IFRS 15.21(b): the remaining goods/services are not
                          distinct and form part of a single partially
                          satisfied obligation. The transaction price is
                          revised and revenue is trued up at the modification
                          date through the normal recognition run (a balanced
                          catch-up entry or reversal).

        Returns the created separate contract for 'separate', otherwise self.
        """
        self.ensure_one()
        # Flag the workflow write: the separate-contract branch spins off a new
        # contract via copy() and activates it (new_contract.state = 'active');
        # the copy inherits this context so that guarded state write is allowed.
        self = self._eh_workflow_action()
        self._check_manager()
        if self.state != 'active':
            raise UserError(_(
                "Only an active contract can be modified."))
        added_obligations = added_obligations or []
        currency = self.currency_id

        if method == 'separate':
            if not added_obligations:
                raise UserError(_(
                    "A separate-contract modification needs the added "
                    "distinct goods or services."))
            price = new_transaction_price
            if price is None:
                price = sum(
                    o.get('standalone_price', 0.0) for o in added_obligations)
            # Providing obligation_ids in the copy default overrides the
            # obligations copied from the source, so the separate contract
            # holds only the added distinct goods/services (IFRS 15.20).
            new_contract = self.copy({
                'name': '/',
                'state': 'draft',
                'transaction_price': currency.round(price),
                'amount_billed': 0.0,
                'financing_interest_recognised': 0.0,
                'obligation_ids': [(0, 0, dict(o)) for o in added_obligations],
            })
            new_contract.state = 'active'
            self.env['eh.revenue.modification'].create({
                'contract_id': self.id,
                'method': method,
                'description': description or '',
                'separate_contract_id': new_contract.id,
                'price_before': self.transaction_price,
                'price_after': self.transaction_price,
            })
            return new_contract

        if method not in ('prospective', 'catch_up'):
            raise UserError(_("Unknown modification method '%s'.", method))

        # prospective / catch_up both revise this contract in place. Run under
        # the modification context so the sanctioned reallocation may add
        # obligations and revise transaction_price on a posted contract; the
        # existing per-line basis freeze still protects posted revenue.
        price_before = self.transaction_price
        contract = self.with_context(eh_revenue_modification=True)

        if method == 'prospective':
            # IFRS 15.21(a): the remaining goods/services are distinct from
            # those transferred. Pin every obligation that has recognised
            # revenue to what it has already earned so it takes no share of the
            # remaining transaction price; that price is reallocated across the
            # still-open (and any added) obligations going forward, with no
            # catch-up on what is already posted.
            for ob in contract.obligation_ids:
                if ob.recognised_amount:
                    ob.write({
                        'allocation_frozen': True,
                        'frozen_allocation': ob.recognised_amount,
                    })

        if added_obligations:
            contract.obligation_ids = [(0, 0, o) for o in added_obligations]
        if new_transaction_price is not None:
            contract.write({
                'transaction_price': currency.round(new_transaction_price)})

        self.env['eh.revenue.modification'].create({
            'contract_id': self.id,
            'method': method,
            'description': description or '',
            'price_before': price_before,
            'price_after': self.transaction_price,
        })

        if method == 'catch_up':
            # True up revenue at the modification date if any progress is
            # already recorded. The recognition run posts the balanced
            # difference (or reversal) between the new blended target and what
            # is already posted. If nothing has posted yet there is nothing to
            # true up, so skip silently.
            if any(self.obligation_ids.mapped('recognised_amount')):
                if currency.compare_amounts(
                        sum(self.obligation_ids.mapped('to_recognise')),
                        0.0) != 0:
                    self.action_recognise()
            # Financing re-measurement (IFRS 15.60-65): with a time-based
            # financing component the interest accreted to date was computed
            # off the pre-modification transaction price. The revised price
            # rescales the total discount (present value and price move
            # together at the locked rate and period), so re-run the
            # accretion off the revised price and remaining schedule and
            # post the signed delta through the same interest accrual
            # accounts. In progress-driven mode the revenue-to-cash ratio
            # (financing_pv / transaction_price) is independent of the
            # price level, so the catch-up recognition entry above already
            # carries the revised interest split and no extra entry exists
            # to post.
            if (new_transaction_price is not None
                    and self._financing_is_time_based()):
                self._validate_accounts(billing=False)
                self._post_financing_accretion()
        return self

    def action_open_modification_wizard(self):
        self.ensure_one()
        self._check_manager()
        if self.state != 'active':
            raise UserError(_("Only an active contract can be modified."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Modify Contract"),
            'res_model': 'eh.revenue.modification.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_contract_id': self.id},
        }

    def action_view_modifications(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Modifications"),
            'res_model': 'eh.revenue.modification',
            'view_mode': 'list,form',
            'domain': [('contract_id', '=', self.id)],
        }

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_revenue_contract_id', '=', self.id)],
        }

    # ---- constraint reassessment (IFRS 15.56) ----

    review_ids = fields.One2many(
        'eh.revenue.constraint.review', 'contract_id', copy=False,
        help="Period-end reassessments of the variable consideration "
             "estimate and constraint (IFRS 15.56).")
    review_count = fields.Integer(compute='_compute_review_count')

    def _compute_review_count(self):
        for c in self:
            c.review_count = len(c.review_ids)

    def action_open_period_reviews(self):
        """Period-end helper (IFRS 15.56): open a draft constraint review
        for every obligation carrying variable consideration on the
        selected active contracts, skipping obligations that already have a
        draft review pending. The reviewer fills the revised amounts and
        rationale, then applies each review; the applied reviews are the
        period-by-period audit trail."""
        Review = self.env['eh.revenue.constraint.review']
        created = Review.browse()
        for c in self:
            if c.state != 'active':
                continue
            for ob in c.obligation_ids.filtered('variable_consideration'):
                if Review.search_count([
                        ('obligation_id', '=', ob.id),
                        ('state', '=', 'draft')]):
                    continue
                created |= Review.create({
                    'contract_id': c.id,
                    'obligation_id': ob.id,
                })
        if not created and not self.review_ids:
            raise UserError(_(
                "No performance obligation on %s carries variable "
                "consideration to review.",
                ', '.join(self.mapped('name'))))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Constraint Reviews"),
            'res_model': 'eh.revenue.constraint.review',
            'view_mode': 'list,form',
            'domain': [('contract_id', 'in', self.ids)],
        }

    def action_view_constraint_reviews(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Constraint Reviews"),
            'res_model': 'eh.revenue.constraint.review',
            'view_mode': 'list,form',
            'domain': [('contract_id', '=', self.id)],
            'context': {'default_contract_id': self.id},
        }

    # ---- helpers ----

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post revenue or billing."))

    def _validate_accounts(self, billing):
        self.ensure_one()
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        if not self.contract_asset_account_id:
            missing.append(_("contract asset account"))
        if not self.contract_liability_account_id:
            missing.append(_("contract liability account"))
        if billing and not self.receivable_account_id:
            missing.append(_("receivable account"))
        if not billing and not self.revenue_account_id:
            missing.append(_("revenue account"))
        # The interest account is only needed when a financing component is
        # actually in effect (a rate and a period discount the price); an
        # inactive toggle leaves recognition byte-identical and does not
        # require it.
        if (not billing and self.financing_component
                and self._financing_revenue_ratio() != 1.0
                and not self.financing_account_id):
            missing.append(_("interest account"))
        if missing:
            raise UserError(_(
                "Configure the %s on contract %s first.",
                ', '.join(missing), self.display_name))

    def _post_move(self, lines):
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': fields.Date.context_today(self),
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_revenue_contract_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_revenue_contract_id = fields.Many2one(
        'eh.revenue.contract', string="Revenue Contract",
        readonly=True, index=True, ondelete='restrict', copy=False)
