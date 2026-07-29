# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.gov.grant: a government grant recognised and amortised under IAS 20.

On receipt an income-related grant, or an asset-related grant on the
deferred-income basis, is credited to deferred income and released to grant
income over the matching periods. An asset-related grant on the netting basis
is deducted from the asset on receipt. Repayment first reverses unamortised
deferred income, with any excess to profit or loss.

Non-monetary grants (IAS 20.23): a grant of a non-monetary asset is
recognised at the asset's fair value. Receipt debits the received asset at
fair value and credits deferred income (income approach) or the asset
contra (netting approach); every later release and clawback flows off the
fair-value base.

Conditions and clawback (IAS 20.7/8, 20.32): each grant carries a register
of attached conditions (open / fulfilled / breached). When the grant is
configured to defer income until the conditions are met, releases to income
are blocked while any condition is open or breached. A breach accrues the
repayment obligation before cash moves: the clawback first reverses any
unamortised deferred income and charges the excess to profit or loss,
crediting a clawback liability that the Repay action later settles against
cash.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhGovGrant(models.Model):
    _name = 'eh.gov.grant'
    _description = "Government grant (IAS 20)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'grant_date desc, id desc'
    _rec_name = 'name'

    # State machine guard: the lifecycle state may change only through the
    # record's own action_* methods (which flag the write), never a direct
    # RPC/ORM write, so a plain user cannot skip an action and its journal
    # entry by writing state straight to a posted value.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('received', "Received"),
         ('closed', "Closed"), ('repaid', "Repaid"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    grant_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)

    grant_type = fields.Selection(
        [('income_related', "Income related"),
         ('asset_related', "Asset related")],
        default='income_related', required=True, tracking=True)
    asset_approach = fields.Selection(
        [('deferred_income', "Deferred income"),
         ('deduct_asset', "Deduction from asset")],
        default='deferred_income',
        help="Presentation of an asset-related grant (IAS 20.24).")
    grant_kind = fields.Selection(
        [('monetary', "Monetary"),
         ('non_monetary', "Non-monetary asset")],
        default='monetary', required=True, tracking=True,
        help="A non-monetary grant transfers an asset (land, equipment) "
             "instead of cash. It is recognised at the fair value of the "
             "asset received (IAS 20.23): receipt debits the received asset "
             "at fair value, and every later release or clawback flows off "
             "that fair-value base.")

    amount = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True)
    asset_fair_value = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Fair value of the non-monetary asset received; the "
             "measurement base of a non-monetary grant (IAS 20.23).")
    recognised_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Grant income recognised to date.")
    remaining = fields.Monetary(
        compute='_compute_remaining', store=True, currency_field='currency_id',
        help="Deferred income not yet released to profit or loss.")
    amortise_amount = fields.Monetary(
        currency_field='currency_id',
        help="Amount to release to income on the next Amortise action.")
    amortise_date = fields.Date(
        help="Accounting date of the next Amortise release. IAS 20.12 requires "
             "the grant to be recognised over the periods that match the "
             "related costs, so each release posts in its earning period, not "
             "the grant's original period. Defaults to today when left blank.")
    repayment_amount = fields.Monetary(
        currency_field='currency_id',
        help="Amount repaid to the grantor on the Repay action (IAS 20.32).")

    # ---- conditions and clawback ----
    condition_ids = fields.One2many(
        'eh.gov.grant.condition', 'grant_id', copy=True)
    defer_until_conditions = fields.Boolean(
        string="Defer Income Until Conditions Met", tracking=True,
        help="IAS 20.7/8: a grant is recognised only when there is "
             "reasonable assurance the attached conditions will be complied "
             "with. When set, releases to income are blocked while any "
             "condition in the register is open or breached. Off by default "
             "so existing grants keep their behaviour.")
    open_condition_count = fields.Integer(
        compute='_compute_condition_stats')
    breached_condition_count = fields.Integer(
        compute='_compute_condition_stats')
    clawback_amount = fields.Monetary(
        currency_field='currency_id',
        help="Amount repayable to the grantor when a condition is breached "
             "(IAS 20.32). The breach accrues this as a clawback liability "
             "before cash moves; the Repay action settles it.")
    clawback_liability_account_id = fields.Many2one(
        'account.account', string="Clawback Liability Account", tracking=True,
        domain="[('account_type', 'in', "
               "['liability_current', 'liability_non_current'])]",
        help="Liability credited when a breach makes the grant repayable "
             "before the cash is paid (IAS 20.32); debited again when the "
             "Repay action settles the clawback.")
    clawback_accrued = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Clawback liability accrued on breach and not yet settled.")
    deferred_reversed = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Deferred income reversed by a breach clawback accrual; keeps "
             "the remaining release headroom honest after a breach.")

    # ---- accounts ----
    cash_account_id = fields.Many2one(
        'account.account', string="Cash / Receivable Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'asset_receivable', 'asset_current'])]")
    deferred_income_account_id = fields.Many2one(
        'account.account', string="Deferred Income Account", tracking=True,
        domain="[('account_type', 'in', "
               "['liability_current', 'liability_non_current'])]")
    grant_income_account_id = fields.Many2one(
        'account.account', string="Grant Income Account", tracking=True,
        domain="[('account_type', 'in', ['income', 'income_other'])]")
    asset_account_id = fields.Many2one(
        'account.account', string="Asset Account", tracking=True,
        help="For the netting approach: the asset whose carrying amount the "
             "grant reduces.")
    received_asset_account_id = fields.Many2one(
        'account.account', string="Received Asset Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_fixed', 'asset_non_current', 'asset_current'])]",
        help="Asset account debited at fair value on receipt of a "
             "non-monetary grant (IAS 20.23).")
    repayment_expense_account_id = fields.Many2one(
        'account.account', string="Repayment Expense Account", tracking=True,
        domain="[('account_type', 'in', ['expense', 'expense_other'])]",
        help="Where any excess of a repayment over the unamortised deferred "
             "income balance is charged to profit or loss (IAS 20.32).")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]")

    move_ids = fields.One2many('account.move', 'eh_gov_grant_id')
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    _sql_constraints = [
        ('check_amount', 'CHECK (amount >= 0)', 'Grant amount cannot be negative.'),
        ('check_fair_value', 'CHECK (asset_fair_value >= 0)', 'The asset fair value cannot be negative.'),
    ]

    def _measurement_base(self):
        """The grant's measurement base: the cash amount for a monetary
        grant, the fair value of the asset received for a non-monetary
        grant (IAS 20.23)."""
        self.ensure_one()
        return (self.asset_fair_value if self.grant_kind == 'non_monetary'
                else self.amount)

    @api.depends('amount', 'asset_fair_value', 'grant_kind',
                 'recognised_amount', 'deferred_reversed', 'grant_type',
                 'asset_approach')
    def _compute_remaining(self):
        for g in self:
            if g.grant_type == 'asset_related' \
                    and g.asset_approach == 'deduct_asset':
                g.remaining = 0.0
            else:
                # A breach clawback reverses deferred income without
                # releasing it to income, so both recognised and reversed
                # amounts consume the base.
                g.remaining = (g._measurement_base() - g.recognised_amount
                               - g.deferred_reversed)

    def _compute_move_count(self):
        for g in self:
            g.move_count = len(g.move_ids)

    @api.depends('condition_ids.state')
    def _compute_condition_stats(self):
        for g in self:
            states = g.condition_ids.mapped('state')
            g.open_condition_count = states.count('open')
            g.breached_condition_count = states.count('breached')

    # Measurement inputs that fix the size and accounting basis of the grant.
    # Once recognition has begun (state received or later) these must not
    # change, or deferred income could be re-based above what was ever credited
    # and released beyond it (IAS 20 integrity).
    _FROZEN_FIELDS = (
        'amount', 'grant_date', 'grant_type', 'asset_approach',
        'grant_kind', 'asset_fair_value', 'received_asset_account_id',
        'cash_account_id', 'deferred_income_account_id',
        'grant_income_account_id', 'asset_account_id',
        'repayment_expense_account_id', 'journal_id', 'company_id',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.gov.grant') or '/'
        return super().create(vals_list)

    # States in which the grant carries a posted GL move (receipt / release /
    # repayment). Its figures are frozen and its record cannot be deleted.
    _POSTED_STATES = ('received', 'closed', 'repaid')

    def write(self, vals):
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        # recognised_amount is a posted figure (grant income recognised to
        # date) driving the release schedule. action_amortise legitimately
        # increments it under the eh_grant_amortise flag; a raw ORM write of
        # it on a posted grant by anyone else is refused.
        if 'recognised_amount' in vals \
                and not self.env.context.get('eh_grant_amortise'):
            frozen.append('recognised_amount')
        # clawback_accrued and deferred_reversed are likewise posted figures
        # (the breach accrual and its deferred-income reversal). Only the
        # sanctioned clawback flows may move them.
        if not self.env.context.get('eh_grant_clawback'):
            for fname in ('clawback_accrued', 'deferred_reversed'):
                if fname in vals:
                    frozen.append(fname)
        # The settlement must debit the same account that carries the
        # accrual, so the liability account is locked while a clawback is
        # outstanding.
        if 'clawback_liability_account_id' in vals \
                and not self.env.context.get('eh_grant_clawback'):
            pending = self.filtered(lambda g: g.clawback_accrued)
            if pending:
                raise UserError(_(
                    "The clawback liability account on %(name)s cannot "
                    "change while an accrued clawback is outstanding; the "
                    "settlement must post to the account that carries the "
                    "accrual. Settle it with the Repay action first.",
                    name=', '.join(pending.mapped('display_name'))))
        if frozen:
            for g in self:
                if g.state in self._POSTED_STATES:
                    raise UserError(_(
                        "Grant %(name)s has been received: its amount, "
                        "accounts and measurement basis are frozen and cannot "
                        "be re-based after recognition begins (IAS 20). "
                        "Blocked field(s): %(fields)s.",
                        name=g.display_name, fields=', '.join(frozen)))
        # The state of a grant that carries a posted move is itself a control
        # point: resetting it back to draft would silently lift the figure
        # freeze above and orphan the posted GL entries. A raw ORM state write
        # that moves a posted grant OUT of the posted set (toward draft), made
        # without the sanctioned-transition context flag, must be
        # manager-gated so a plain user cannot un-freeze a GL-backed grant.
        # The action methods carry the flag on their own legitimate
        # transitions (which stay within the posted set) after their manager
        # check and move handling.
        if 'state' in vals \
                and not self.env.context.get('eh_grant_state_change'):
            leaving = self.filtered(
                lambda g: g.state in self._POSTED_STATES
                and vals['state'] not in self._POSTED_STATES)
            if leaving:
                leaving._check_manager()
        return super().write(vals)

    def unlink(self):
        posted = self.filtered(lambda g: g.state in self._POSTED_STATES)
        if posted:
            raise UserError(_(
                "Grant %s carries a posted GL entry (receipt / release / "
                "repayment) and cannot be deleted; its move would be "
                "orphaned. Cancel or reverse the entries first.",
                ', '.join(posted.mapped('display_name'))))
        return super().unlink()

    @property
    def _is_netting(self):
        self.ensure_one()
        return (self.grant_type == 'asset_related'
                and self.asset_approach == 'deduct_asset')

    # ---- actions ----

    def action_receive(self):
        self = self._eh_workflow_action()
        self.ensure_one()
        self._check_manager()
        if self.state != 'draft':
            raise UserError(_("Only a draft grant can be received."))
        if not self.journal_id:
            raise UserError(_("Configure the journal first."))
        currency = self.currency_id
        # IAS 20.23: a non-monetary grant is measured at the fair value of
        # the asset received; a monetary grant at the cash amount.
        amount = currency.round(self._measurement_base())
        if self.grant_kind == 'non_monetary':
            if currency.compare_amounts(amount, 0.0) <= 0:
                raise UserError(_(
                    "A non-monetary grant is recognised at the fair value "
                    "of the asset received (IAS 20.23); set a positive "
                    "asset fair value."))
            if not self.received_asset_account_id:
                raise UserError(_(
                    "A non-monetary grant needs the received asset "
                    "account: the asset is debited at its fair value on "
                    "receipt (IAS 20.23)."))
            debit_account = self.received_asset_account_id
            debit_label = _(
                "Non-monetary grant asset at fair value %s", self.name)
        else:
            if currency.compare_amounts(amount, 0.0) <= 0:
                raise UserError(_("The grant amount must be positive."))
            if not self.cash_account_id:
                raise UserError(_(
                    "Configure the cash / receivable account first."))
            debit_account = self.cash_account_id
            debit_label = _("Grant received %s", self.name)
        if self._is_netting:
            if not self.asset_account_id:
                raise UserError(_(
                    "The netting approach needs an asset account."))
            legs = [
                (debit_account, amount, 0.0, debit_label),
                (self.asset_account_id, 0.0, amount,
                 _("Grant deducted from asset %s", self.name)),
            ]
        else:
            if not self.deferred_income_account_id:
                raise UserError(_(
                    "Configure the deferred income account first."))
            legs = [
                (debit_account, amount, 0.0, debit_label),
                (self.deferred_income_account_id, 0.0, amount,
                 _("Deferred grant income %s", self.name)),
            ]
        self._post_move(legs)
        # A netting grant is recognised in full on receipt through the reduced
        # carrying amount of the asset. There is no deferred income to release
        # later, so its lifecycle completes here.
        self.state = 'closed' if self._is_netting else 'received'
        return True

    def action_amortise(self):
        self = self._eh_workflow_action()
        self.ensure_one()
        self._check_manager()
        if self.state != 'received':
            raise UserError(_(
                "Amortisation applies to a received grant."))
        if self._is_netting:
            raise UserError(_(
                "A netting-approach grant is deducted from the asset's "
                "carrying amount in full on receipt (IAS 20.27), so there is "
                "no deferred income for this action to release. The lower "
                "carrying amount reduces future depreciation in the asset "
                "register; this grant record posts no separate amortisation."))
        currency = self.currency_id
        if currency.compare_amounts(self.clawback_accrued, 0.0) > 0:
            raise UserError(_(
                "A breach clawback of %(amt).2f is outstanding on "
                "%(name)s; the grant is repayable (IAS 20.32) and no "
                "further income may be released. Settle the clawback with "
                "the Repay action.",
                amt=self.clawback_accrued, name=self.display_name))
        # IAS 20.7/8: a grant is recognised only when there is reasonable
        # assurance the conditions attaching to it will be complied with.
        # The gate is opt-in (defer_until_conditions) so existing grants
        # keep their behaviour.
        if self.defer_until_conditions:
            blocking = self.condition_ids.filtered(
                lambda c: c.state != 'fulfilled')
            if blocking:
                raise UserError(_(
                    "Grant income on %(name)s is deferred until its "
                    "conditions are met (IAS 20.7/8). Not yet fulfilled: "
                    "%(conditions)s.",
                    name=self.display_name,
                    conditions=', '.join(blocking.mapped('name'))))
        if not self.grant_income_account_id:
            raise UserError(_("Configure the grant income account first."))
        amount = currency.round(self.amortise_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive amount to amortise."))
        if currency.compare_amounts(amount, self.remaining) > 0:
            raise UserError(_(
                "Cannot amortise %(amt).2f: only %(rem).2f of deferred income "
                "remains.", amt=amount, rem=self.remaining))
        # IAS 20.12: the release is recognised in the period whose costs it
        # matches, so it posts on the amortisation date, not the grant date.
        release_date = self.amortise_date or fields.Date.context_today(self)
        self._post_move([
            (self.deferred_income_account_id, amount, 0.0,
             _("Release deferred grant %s", self.name)),
            (self.grant_income_account_id, 0.0, amount,
             _("Grant income %s", self.name)),
        ], move_date=release_date)
        self.with_context(eh_grant_amortise=True).write({
            'recognised_amount': self.recognised_amount + amount,
            'amortise_amount': 0.0,
            'amortise_date': False,
        })
        if currency.is_zero(self.remaining):
            self.state = 'closed'
        return True

    def action_accrue_clawback(self):
        """Accrue the repayment obligation arising from a breach (IAS 20.32).

        A grant that becomes repayable is accounted for prospectively: the
        repayment is applied first against any unamortised deferred credit,
        and the excess is recognised immediately in profit or loss. When
        the cash has not yet been paid, the obligation is carried as a
        clawback liability: Dr deferred income (up to the unamortised
        balance) + Dr repayment expense (excess) / Cr clawback liability
        (full clawback). The Repay action later settles the liability
        against cash. Normally triggered by breaching a condition in the
        register; also available directly once a condition is breached.
        """
        self.ensure_one()
        self._check_manager()
        if self.state != 'received':
            raise UserError(_(
                "A clawback accrues on a received grant on the "
                "deferred-income basis."))
        if self._is_netting:
            raise UserError(_(
                "A netting-approach grant was deducted from the asset's "
                "carrying amount on receipt (IAS 20.27); a repayment is "
                "recorded as an increase to the asset's carrying amount in "
                "the asset register, not through this clawback accrual."))
        currency = self.currency_id
        if currency.compare_amounts(self.clawback_accrued, 0.0) > 0:
            raise UserError(_(
                "A clawback of %(amt).2f is already accrued on %(name)s; "
                "settle it with the Repay action.",
                amt=self.clawback_accrued, name=self.display_name))
        amount = currency.round(self.clawback_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_(
                "Set the clawback amount on %(name)s: the amount repayable "
                "to the grantor under the breached condition (IAS 20.32).",
                name=self.display_name))
        if not self.clawback_liability_account_id:
            raise UserError(_(
                "Configure the clawback liability account on %(name)s so "
                "the repayment obligation can be accrued before the cash "
                "is paid.", name=self.display_name))
        if not self.deferred_income_account_id:
            raise UserError(_("Configure the deferred income account first."))
        if not self.journal_id:
            raise UserError(_("Configure the journal first."))
        deferred_balance = currency.round(self.remaining)
        # IAS 20.32 order: the clawback first reverses the unamortised
        # deferred income; only the excess hits profit or loss.
        reversed_deferred = min(amount, deferred_balance)
        excess = currency.round(amount - reversed_deferred)
        legs = []
        if currency.compare_amounts(reversed_deferred, 0.0) > 0:
            legs.append((
                self.deferred_income_account_id, reversed_deferred, 0.0,
                _("Reverse unamortised grant on breach %s", self.name)))
        if currency.compare_amounts(excess, 0.0) > 0:
            if not self.repayment_expense_account_id:
                raise UserError(_(
                    "The clawback exceeds the unamortised deferred income "
                    "by %(exc).2f. Configure the repayment expense account "
                    "so the excess can be charged to profit or loss "
                    "(IAS 20.32).", exc=excess))
            legs.append((
                self.repayment_expense_account_id, excess, 0.0,
                _("Grant clawback excess to profit or loss %s", self.name)))
        legs.append((
            self.clawback_liability_account_id, 0.0, amount,
            _("Grant clawback liability %s", self.name)))
        # A breach is a current-period event: the accrual posts on the
        # breach date, not in the grant's original period.
        self._post_move(legs, move_date=fields.Date.context_today(self))
        self.with_context(eh_grant_clawback=True).write({
            'clawback_accrued': amount,
            'deferred_reversed': self.deferred_reversed + reversed_deferred,
        })
        self.message_post(body=_(
            "Clawback accrued on breach: deferred income reversed "
            "%(rev).2f, excess to profit or loss %(exc).2f, clawback "
            "liability %(amt).2f (IAS 20.32).",
            rev=reversed_deferred, exc=excess, amt=amount))
        return True

    def action_repay(self):
        """Repay a grant that has become repayable (IAS 20.32).

        With a breach clawback outstanding, this settles the accrued
        liability against cash (the deferred-income reversal and any excess
        expense were already booked by the accrual). Otherwise the
        repayment first reverses any unamortised deferred income balance
        and any excess is recognised immediately in profit or loss as an
        expense. The resulting journal entry is balanced by construction
        and the grant moves to 'repaid'.
        """
        self = self._eh_workflow_action()
        self.ensure_one()
        self._check_manager()
        if self.state != 'received':
            raise UserError(_(
                "Only a received grant on the deferred-income basis can be "
                "repaid."))
        if self._is_netting:
            raise UserError(_(
                "A netting-approach grant is not repaid through this action. "
                "It was deducted from the asset's carrying amount on receipt "
                "(IAS 20.27), so a repayment is recorded as an increase to the "
                "asset's carrying amount in the asset register, not through "
                "this deferred-income repayment."))
        currency = self.currency_id
        if currency.compare_amounts(self.clawback_accrued, 0.0) > 0:
            if not self.journal_id or not self.cash_account_id:
                raise UserError(_(
                    "Configure the journal and cash / receivable account "
                    "first."))
            amount = currency.round(self.clawback_accrued)
            self._post_move([
                (self.clawback_liability_account_id, amount, 0.0,
                 _("Settle grant clawback liability %s", self.name)),
                (self.cash_account_id, 0.0, amount,
                 _("Grant clawback repaid %s", self.name)),
            ], move_date=fields.Date.context_today(self))
            self.with_context(eh_grant_clawback=True).write(
                {'clawback_accrued': 0.0})
            self.repayment_amount = 0.0
            self.state = 'repaid'
            return True
        if not self.deferred_income_account_id:
            raise UserError(_("Configure the deferred income account first."))
        currency = self.currency_id
        amount = currency.round(self.repayment_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive amount to repay."))
        if not self.journal_id or not self.cash_account_id:
            raise UserError(_(
                "Configure the journal and cash / receivable account first."))
        deferred_balance = currency.round(self.remaining)
        # The portion of the repayment reversing deferred income, capped at the
        # unamortised balance; any remainder is an expense (excess to P&L).
        reversed_deferred = min(amount, deferred_balance)
        excess = currency.round(amount - reversed_deferred)
        legs = []
        if currency.compare_amounts(reversed_deferred, 0.0) > 0:
            legs.append((
                self.deferred_income_account_id, reversed_deferred, 0.0,
                _("Reverse unamortised grant %s", self.name)))
        if currency.compare_amounts(excess, 0.0) > 0:
            if not self.repayment_expense_account_id:
                raise UserError(_(
                    "The repayment exceeds the unamortised deferred income by "
                    "%(exc).2f. Configure the repayment expense account so the "
                    "excess can be charged to profit or loss.", exc=excess))
            legs.append((
                self.repayment_expense_account_id, excess, 0.0,
                _("Grant repayment excess to profit or loss %s", self.name)))
        legs.append((
            self.cash_account_id, 0.0, amount,
            _("Grant repaid %s", self.name)))
        # A repayment (IAS 20.32) is a current-period event: it posts on the
        # repayment date, not the grant's original period.
        self._post_move(legs, move_date=fields.Date.context_today(self))
        self.repayment_amount = 0.0
        self.state = 'repaid'
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        for g in self:
            if g.move_ids:
                raise UserError(_(
                    "Reverse the posted entries before cancelling %s.",
                    g.display_name))
            g.state = 'cancelled'

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_gov_grant_id', '=', self.id)],
        }

    # ---- helpers ----

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post grant entries."))

    def _post_move(self, legs, move_date=None):
        # IAS 20.12 systematic matching and period cutoff: each move posts on
        # its own accounting date, not the grant's original period. The receipt
        # posts on the grant (receipt) date; each amortisation release posts in
        # its earning period; a repayment posts on the repayment date. Callers
        # pass move_date; it falls back to grant_date only when unset.
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': move_date or self.grant_date,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_gov_grant_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_gov_grant_id = fields.Many2one(
        'eh.gov.grant', string="Government Grant", readonly=True,
        index=True, ondelete='restrict', copy=False)
