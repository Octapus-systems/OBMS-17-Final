# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.equity.investment: an investment in an associate or joint venture carried
under the equity method (IAS 28).

The carrying amount starts at cost and rolls forward for the investor's share
of the investee's profit (or loss), less dividends received and any
impairment (IAS 28.10-11). Each pick-up, dividend and impairment posts a
balanced entry.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhEquityInvestment(models.Model):
    _name = 'eh.equity.investment'
    _description = "Equity-method investment (IAS 28)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'id desc'
    _rec_name = 'name'

    # State advances only through this record's own actions (run under sudo),
    # never a direct RPC write that would skip an action and its posted entry.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    investee_name = fields.Char(required=True, tracking=True)
    kind = fields.Selection(
        [('associate', "Associate"), ('joint_venture', "Joint venture")],
        default='associate', required=True)
    state = fields.Selection(
        [('draft', "Draft"), ('active', "Active"),
         ('disposed', "Disposed"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    ownership_pct = fields.Float(
        digits=(7, 4), required=True, default=25.0,
        help="Investor's ownership / share of the investee (percentage).")
    cost_of_investment = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Initial cost recognised on acquisition.")
    carrying_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id', tracking=True,
        help="Equity-method carrying amount, rolled forward from cost.")
    cumulative_share_of_profit = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')
    cumulative_dividends = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')
    cumulative_impairment = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')

    # ---- period inputs ----
    investee_profit = fields.Monetary(
        currency_field='currency_id',
        help="Investee's profit or loss for the period; the share picked up "
             "is this at the ownership percentage.")
    dividend_received = fields.Monetary(currency_field='currency_id')
    impairment_amount = fields.Monetary(currency_field='currency_id')
    disposal_proceeds = fields.Monetary(
        currency_field='currency_id',
        help="Fair value of consideration received on disposing of the "
             "investment (IAS 28.22).")

    # ---- accounts ----
    investment_account_id = fields.Many2one(
        'account.account', string="Investment Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_non_current', 'asset_fixed'])]")
    share_of_profit_account_id = fields.Many2one(
        'account.account', string="Share of Profit Account", tracking=True,
        domain="[('account_type', 'in', ['income_other', 'expense'])]")
    cash_account_id = fields.Many2one(
        'account.account', string="Cash / Receivable Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'asset_receivable', 'asset_current'])]")
    impairment_account_id = fields.Many2one(
        'account.account', string="Impairment Account", tracking=True,
        domain="[('account_type', '=', 'expense')]")
    disposal_gain_loss_account_id = fields.Many2one(
        'account.account', string="Disposal Gain / Loss Account",
        tracking=True,
        domain="[('account_type', 'in', ['income_other', 'expense'])]",
        help="Profit or loss account for the gain or loss on disposal "
             "(IAS 28.22).")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]")

    move_ids = fields.One2many('account.move', 'eh_equity_investment_id')
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    _sql_constraints = [
        ('check_ownership', 'CHECK (ownership_pct >= 0 AND ownership_pct <= 100)', 'Ownership percentage must be between 0 and 100.'),
    ]

    def _compute_move_count(self):
        for inv in self:
            inv.move_count = len(inv.move_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.equity.investment') or '/'
        return super().create(vals_list)

    # ---- actions ----

    def action_activate(self):
        self = self._eh_workflow_action()
        for inv in self:
            if inv.state != 'draft':
                raise UserError(_("Only draft investments can be activated."))
            inv._check_manager()
            currency = inv.currency_id
            cost = currency.round(inv.cost_of_investment)
            # Recognise the investment at cost on the ledger (IAS 28.10).
            # Nothing else feeds the investment account (unlike a fixed asset
            # created from a vendor bill), so if the acquisition is not posted
            # here the investment never reaches the balance sheet and a later
            # disposal derecognises a carrying amount that was never
            # recognised, leaving a phantom credit on the investment account.
            if not currency.is_zero(cost):
                inv._validate_accounts(['investment', 'cash'])
                inv._post_move([
                    (inv.investment_account_id, cost, 0.0,
                     _("Investment in associate at cost %s", inv.name)),
                    (inv.cash_account_id, 0.0, cost,
                     _("Consideration paid %s", inv.name)),
                ])
            inv.write({
                'state': 'active',
                'carrying_amount': cost,
            })
        return True

    def action_pickup_profit(self):
        self.ensure_one()
        self._check_manager()
        self._require_active()
        self._validate_accounts(['investment', 'share_of_profit'])
        currency = self.currency_id
        share = currency.round(self.investee_profit * self.ownership_pct / 100.0)
        if currency.is_zero(share):
            raise UserError(_(
                "The share of profit is nil; nothing to pick up."))
        if share > 0:
            legs = [
                (self.investment_account_id, share, 0.0,
                 _("Share of profit %s", self.name)),
                (self.share_of_profit_account_id, 0.0, share,
                 _("Share of profit %s", self.name)),
            ]
        else:
            # IAS 28.38: discontinue recognising the share of loss once the
            # investment reaches nil. Recognise only the loss that brings the
            # carrying amount to zero; floor it there.
            recognised_loss = min(-share, max(self.carrying_amount, 0.0))
            recognised_loss = currency.round(recognised_loss)
            if currency.is_zero(recognised_loss):
                raise UserError(_(
                    "The investment is already reduced to nil; no further "
                    "share of loss can be recognised under IAS 28.38."))
            # The amount actually recognised may be less than the full share of
            # loss; keep the roll-forward and the entry consistent.
            share = -recognised_loss
            legs = [
                (self.share_of_profit_account_id, recognised_loss, 0.0,
                 _("Share of loss %s", self.name)),
                (self.investment_account_id, 0.0, recognised_loss,
                 _("Share of loss %s", self.name)),
            ]
        self._post_move(legs)
        self.carrying_amount += share
        self.cumulative_share_of_profit += share
        self.investee_profit = 0.0
        return True

    def action_record_dividend(self):
        self.ensure_one()
        self._check_manager()
        self._require_active()
        self._validate_accounts(['investment', 'cash'])
        currency = self.currency_id
        amount = currency.round(self.dividend_received)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive dividend received."))
        # Dividends reduce the carrying amount, they are not income.
        self._post_move([
            (self.cash_account_id, amount, 0.0,
             _("Dividend received %s", self.name)),
            (self.investment_account_id, 0.0, amount,
             _("Dividend reduces investment %s", self.name)),
        ])
        self.carrying_amount -= amount
        self.cumulative_dividends += amount
        self.dividend_received = 0.0
        return True

    def action_impair(self):
        self.ensure_one()
        self._check_manager()
        self._require_active()
        self._validate_accounts(['investment', 'impairment'])
        currency = self.currency_id
        amount = currency.round(self.impairment_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive impairment amount."))
        self._post_move([
            (self.impairment_account_id, amount, 0.0,
             _("Impairment of associate %s", self.name)),
            (self.investment_account_id, 0.0, amount,
             _("Impairment reduces investment %s", self.name)),
        ])
        self.carrying_amount -= amount
        self.cumulative_impairment += amount
        self.impairment_amount = 0.0
        return True

    def action_dispose(self):
        """Derecognise the investment on disposal (IAS 28.22).

        On losing significant influence the equity-method carrying amount is
        derecognised. The difference between the proceeds received and the
        carrying amount is a gain or loss recognised in profit or loss. The
        entry balances by construction.
        """
        self.ensure_one()
        self._check_manager()
        # Run the state transition as su so the guarded 'state' write is
        # accepted; env.user (checked above) is preserved for audit stamps.
        self = self._eh_workflow_action()
        self._require_active()
        self._validate_accounts(['investment', 'cash', 'disposal'])
        currency = self.currency_id
        proceeds = currency.round(self.disposal_proceeds)
        if currency.compare_amounts(proceeds, 0.0) < 0:
            raise UserError(_("Disposal proceeds cannot be negative."))
        carrying = currency.round(self.carrying_amount)
        gain_loss = currency.round(proceeds - carrying)

        legs = []
        # Dr cash / receivable for the proceeds received.
        if not currency.is_zero(proceeds):
            legs.append((self.cash_account_id, proceeds, 0.0,
                         _("Disposal proceeds %s", self.name)))
        # Cr investment to derecognise the carrying amount (Dr if the carrying
        # amount is negative, though it normally floors at zero).
        if currency.compare_amounts(carrying, 0.0) > 0:
            legs.append((self.investment_account_id, 0.0, carrying,
                         _("Derecognise investment %s", self.name)))
        elif currency.compare_amounts(carrying, 0.0) < 0:
            legs.append((self.investment_account_id, -carrying, 0.0,
                         _("Derecognise investment %s", self.name)))
        # Balancing gain (Cr income) or loss (Dr expense) to profit or loss.
        if currency.compare_amounts(gain_loss, 0.0) > 0:
            legs.append((self.disposal_gain_loss_account_id, 0.0, gain_loss,
                         _("Gain on disposal %s", self.name)))
        elif currency.compare_amounts(gain_loss, 0.0) < 0:
            legs.append((self.disposal_gain_loss_account_id, -gain_loss, 0.0,
                         _("Loss on disposal %s", self.name)))
        if legs:
            self._post_move(legs)
        self.carrying_amount = 0.0
        self.disposal_proceeds = 0.0
        self.state = 'disposed'
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        for inv in self:
            if inv.move_ids:
                raise UserError(_(
                    "Reverse the posted entries before cancelling %s.",
                    inv.display_name))
            inv.state = 'cancelled'

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_equity_investment_id', '=', self.id)],
        }

    # ---- helpers ----

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post equity-method "
                "entries."))

    def _require_active(self):
        if self.state != 'active':
            raise UserError(_(
                "The investment must be active. Activate it to recognise it "
                "at cost first."))

    def _validate_accounts(self, needed):
        self.ensure_one()
        field_map = {
            'investment': ('investment_account_id', _("investment account")),
            'share_of_profit': ('share_of_profit_account_id',
                                 _("share of profit account")),
            'cash': ('cash_account_id', _("cash / receivable account")),
            'impairment': ('impairment_account_id', _("impairment account")),
            'disposal': ('disposal_gain_loss_account_id',
                         _("disposal gain / loss account")),
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
                "Configure the %s on %s first.",
                ', '.join(missing), self.display_name))

    def _post_move(self, legs):
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': fields.Date.context_today(self),
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_equity_investment_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_equity_investment_id = fields.Many2one(
        'eh.equity.investment', string="Equity Investment", readonly=True,
        index=True, ondelete='set null', copy=False)
