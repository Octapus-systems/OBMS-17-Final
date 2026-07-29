# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.fair.value.item: an item measured at fair value under IFRS 13.

The item is classified in the fair-value hierarchy by the observability of
its inputs (Level 1/2/3). Remeasuring posts the change from the current
carrying amount to fair value, to profit or loss or OCI, and then rolls the
carrying amount forward to the new fair value.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhFairValueItem(models.Model):
    _name = 'eh.fair.value.item'
    _description = "Fair value item (IFRS 13)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'measurement_date desc, id desc'
    _rec_name = 'name'

    # Workflow-critical fields that may only change through this model's own
    # actions (action_remeasure / action_cancel / action_reset_to_draft /
    # action_derecognise / action_recycle), never a direct RPC write: a plain
    # user could otherwise write state='measured' to skip the remeasurement
    # posting, or flip recycled to bypass the OCI-reserve settlement.
    _eh_guarded_fields = ('state', 'recycled')

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('measured', "Measured"),
         ('derecognised', "Derecognised"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    nature = fields.Selection(
        [('financial_asset', "Financial asset"),
         ('financial_liability', "Financial liability"),
         ('investment_property', "Investment property"),
         ('non_financial_asset', "Other non-financial asset")],
        default='financial_asset', required=True, tracking=True)

    # ---- IFRS 9 classification engine (IFRS 9.4.1) ----
    instrument_type = fields.Selection(
        [('debt', "Debt instrument"),
         ('equity', "Equity instrument"),
         ('derivative', "Derivative")],
        tracking=True,
        help="Contractual nature of the financial instrument, driving the "
             "IFRS 9 classification engine. Leave empty on non-financial "
             "items and on records created before the engine existed; those "
             "keep the manual routing behaviour.")
    sppi_fixed_dates = fields.Boolean(
        string="Cash Flows on Fixed Dates", tracking=True,
        help="IFRS 9.4.1.2(b): the contractual terms give rise on specified "
             "dates to cash flows.")
    sppi_interest_only = fields.Boolean(
        string="Solely Principal and Interest", tracking=True,
        help="IFRS 9.4.1.3: interest is consideration only for the time "
             "value of money and the credit risk on the principal amount "
             "outstanding.")
    sppi_no_leverage = fields.Boolean(
        string="No Leverage Features", tracking=True,
        help="IFRS 9.B4.1.9: no contractual leverage that amplifies the "
             "variability of the contractual cash flows.")
    sppi_no_contingent_returns = fields.Boolean(
        string="No Contingent Returns", tracking=True,
        help="No returns linked to equity prices, commodity prices or other "
             "variables unrelated to a basic lending arrangement.")
    sppi_pass = fields.Boolean(
        compute='_compute_sppi_pass', store=True, precompute=True,
        string="SPPI Test Passed",
        help="A debt instrument whose four questionnaire answers all hold "
             "passes the solely-payments-of-principal-and-interest test. "
             "Equity instruments and derivatives fail by nature.")
    business_model = fields.Selection(
        [('hold_to_collect', "Hold to collect"),
         ('hold_collect_sell', "Hold to collect and sell"),
         ('other', "Other / trading")],
        tracking=True,
        help="IFRS 9.4.1.1(a): the business model within which the "
             "instrument is held.")
    held_for_trading = fields.Boolean(
        tracking=True,
        help="Held for trading (IFRS 9 Appendix A). Blocks the FVOCI equity "
             "election and forces fair value through profit or loss.")
    floating_rate = fields.Boolean(
        string="Floating rate", tracking=True,
        help="The instrument bears a floating (variable) interest rate. "
             "Flagged items feed the computed IFRS 7.40 interest-rate "
             "sensitivity in the disclosures module at their fair value; "
             "the impact of a FVOCI-debt instrument routes to OCI, "
             "everything else to profit or loss. Purely informational "
             "here: it changes no measurement or posting.")
    fvoci_equity_election = fields.Boolean(
        string="FVOCI Equity Election", tracking=True,
        help="IFRS 9.5.7.5: irrevocable election at initial recognition to "
             "present fair-value changes of a non-trading equity instrument "
             "in OCI. Cannot be changed once a journal entry has been "
             "posted under the elected treatment.")
    ifrs9_classification = fields.Selection(
        [('amortised_cost', "Amortised cost"),
         ('fvoci_debt', "FVOCI - debt (recycles to P&L)"),
         ('fvoci_equity', "FVOCI - equity election (never recycles)"),
         ('fvtpl', "FVTPL - fair value through profit or loss")],
        compute='_compute_ifrs9_classification', store=True, precompute=True,
        tracking=True, string="IFRS 9 Classification (derived)",
        help="Derived by the IFRS 9.4.1 engine from the instrument type, "
             "the SPPI test and the business model; never set by hand. "
             "Empty when no instrument type is captured (legacy records and "
             "non-financial items).")

    level = fields.Selection(
        [('1', "Level 1 - quoted prices"),
         ('2', "Level 2 - observable inputs"),
         ('3', "Level 3 - unobservable inputs")],
        default='1', required=True, tracking=True,
        help="Fair-value hierarchy level by input observability "
             "(IFRS 13.72-90).")
    valuation_technique = fields.Selection(
        [('market', "Market approach"), ('income', "Income approach"),
         ('cost', "Cost approach")],
        default='market', required=True)
    unobservable_inputs = fields.Text(
        help="Significant unobservable inputs and key assumptions, for "
             "Level 3 disclosure (IFRS 13.91-93).")

    measurement_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    prior_carrying = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Current carrying amount before this remeasurement.")
    fair_value = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True,
        help="Fair value at the measurement date.")
    remeasurement = fields.Monetary(
        compute='_compute_remeasurement', store=True,
        currency_field='currency_id',
        help="Fair value less prior carrying amount.")
    routing = fields.Selection(
        [('pl', "Profit or loss"), ('oci', "OCI")],
        compute='_compute_routing', store=True, readonly=False,
        precompute=True, required=True,
        help="Where the fair-value change is recognised. Derived from the "
             "IFRS 9 classification whenever the engine is active (FVTPL to "
             "profit or loss, FVOCI to OCI); manual only for non-financial "
             "items and legacy records without an instrument type.")
    fvoci_classification = fields.Selection(
        [('fvtpl', "FVTPL - fair value through profit or loss"),
         ('fvoci_debt', "FVOCI - debt (recycles to P&L)"),
         ('fvoci_equity', "FVOCI - equity election (never recycles)")],
        string="IFRS 9 Classification (legacy)", tracking=True,
        help="Manual classification label kept for records created before "
             "the IFRS 9.4.1 engine. Ignored as soon as an instrument type "
             "is set: the derived classification then governs and a "
             "conflicting legacy label is refused.")
    recycled = fields.Boolean(
        readonly=True, copy=False,
        help="The accumulated OCI reserve has been recycled or transferred "
             "on derecognition.")
    disposal_proceeds = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Consideration received (asset) or paid (liability) on "
             "derecognition.")
    derecognition_date = fields.Date(readonly=True, copy=False, tracking=True)

    # ---- accounts ----
    balance_sheet_account_id = fields.Many2one(
        'account.account', string="Balance Sheet Account", tracking=True,
        domain="[('internal_group', 'in', "
               "['asset', 'liability', 'equity'])]",
        help="Statement-of-financial-position account carrying the item. "
             "Must be an asset, liability or equity account, never a "
             "profit-or-loss account.")
    gain_loss_account_id = fields.Many2one(
        'account.account', string="Gain / Loss Account", tracking=True,
        domain="[('account_type', 'in', ['income_other', 'expense'])]")
    oci_account_id = fields.Many2one(
        'account.account', string="OCI / Equity Account", tracking=True,
        domain="[('account_type', '=', 'equity')]")
    settlement_account_id = fields.Many2one(
        'account.account', string="Settlement Account", tracking=True,
        domain="[('internal_group', '=', 'asset')]",
        help="Cash or clearing account taking the proceeds leg of the "
             "derecognition entry.")
    retained_earnings_account_id = fields.Many2one(
        'account.account', string="Retained Earnings Account", tracking=True,
        domain="[('account_type', '=', 'equity')]",
        help="Equity destination for the FVOCI equity-election reserve "
             "transfer on derecognition. The transfer stays within equity "
             "and never passes through profit or loss (IFRS 9.B5.7.1).")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]")

    move_ids = fields.One2many('account.move', 'eh_fair_value_item_id')
    move_count = fields.Integer(compute='_compute_move_count')
    oci_reserve_balance = fields.Monetary(
        compute='_compute_oci_reserve_balance', currency_field='currency_id',
        help="Net movement posted to the OCI / equity account for this item "
             "(debit less credit), the reserve available to recycle on "
             "derecognition.")

    # ---- Level 3 reconciliation (IFRS 13.93(e)) ----
    rollforward_ids = fields.One2many(
        'eh.fair.value.rollforward', 'item_id',
        string="Level 3 Reconciliation")
    ties_to_fair_value = fields.Boolean(
        compute='_compute_ties_to_fair_value',
        help="The closing balance of the latest Level 3 reconciliation "
             "period equals the item's fair value (within rounding).")

    # ---- Level 3 sensitivity analysis (IFRS 13.93(h)) ----
    sensitivity_ids = fields.One2many(
        'eh.fair.value.sensitivity', 'item_id',
        string="Sensitivity Analysis")

    notes = fields.Text()

    @api.depends('fair_value', 'prior_carrying')
    def _compute_remeasurement(self):
        for item in self:
            item.remeasurement = item.fair_value - item.prior_carrying

    @api.depends('instrument_type', 'sppi_fixed_dates', 'sppi_interest_only',
                 'sppi_no_leverage', 'sppi_no_contingent_returns')
    def _compute_sppi_pass(self):
        # IFRS 9.4.1.2(b)/.4.1.3: only a plain debt instrument can have cash
        # flows that are solely payments of principal and interest. An equity
        # instrument or a derivative fails by nature, whatever the
        # questionnaire answers say.
        for item in self:
            item.sppi_pass = bool(
                item.instrument_type == 'debt'
                and item.sppi_fixed_dates
                and item.sppi_interest_only
                and item.sppi_no_leverage
                and item.sppi_no_contingent_returns)

    @api.depends('nature', 'instrument_type', 'sppi_pass', 'business_model',
                 'fvoci_equity_election', 'held_for_trading')
    def _compute_ifrs9_classification(self):
        # IFRS 9.4.1.1-4.1.4 decision tree; FVTPL is the residual category.
        # No classification is derived without an instrument type, so records
        # created before the engine existed keep their manual behaviour.
        for item in self:
            if (item.nature not in ('financial_asset', 'financial_liability')
                    or not item.instrument_type):
                item.ifrs9_classification = False
            elif item.instrument_type == 'derivative':
                item.ifrs9_classification = 'fvtpl'
            elif item.instrument_type == 'equity':
                item.ifrs9_classification = (
                    'fvoci_equity'
                    if item.fvoci_equity_election
                    and not item.held_for_trading else 'fvtpl')
            elif item.nature == 'financial_liability':
                # IFRS 9.4.2.1: a financial liability sits at amortised cost
                # unless held for trading (own-credit FVTPL designation is
                # out of scope for this model).
                item.ifrs9_classification = (
                    'fvtpl' if item.held_for_trading else 'amortised_cost')
            elif (item.sppi_pass and not item.held_for_trading
                    and item.business_model == 'hold_to_collect'):
                item.ifrs9_classification = 'amortised_cost'
            elif (item.sppi_pass and not item.held_for_trading
                    and item.business_model == 'hold_collect_sell'):
                item.ifrs9_classification = 'fvoci_debt'
            else:
                item.ifrs9_classification = 'fvtpl'

    @api.depends('ifrs9_classification')
    def _compute_routing(self):
        for item in self:
            if item.ifrs9_classification in ('fvoci_debt', 'fvoci_equity'):
                item.routing = 'oci'
            elif item.ifrs9_classification:
                item.routing = 'pl'
            else:
                # Engine off: keep the manually captured routing.
                item.routing = item.routing or 'pl'

    def _compute_move_count(self):
        for item in self:
            item.move_count = len(item.move_ids)

    @api.depends('move_ids.line_ids.debit', 'move_ids.line_ids.credit',
                 'move_ids.state', 'oci_account_id')
    def _compute_oci_reserve_balance(self):
        for item in self:
            balance = 0.0
            if item.oci_account_id:
                for line in item.move_ids.line_ids:
                    if (line.parent_state == 'posted'
                            and line.account_id == item.oci_account_id):
                        balance += line.debit - line.credit
            item.oci_reserve_balance = balance

    @api.depends('rollforward_ids.closing_balance', 'rollforward_ids.period_end',
                 'fair_value', 'currency_id')
    def _compute_ties_to_fair_value(self):
        for item in self:
            latest = item.rollforward_ids.sorted(
                lambda r: (r.period_end or fields.Date.today(), r.id))[-1:]
            if latest:
                item.ties_to_fair_value = item.currency_id.is_zero(
                    latest.closing_balance - item.fair_value)
            else:
                item.ties_to_fair_value = False

    @api.constrains('balance_sheet_account_id')
    def _check_balance_sheet_account(self):
        # IFRS 9/13: the position leg of a fair-value remeasurement must land
        # on a statement-of-financial-position account (asset, liability or
        # equity), never on a profit-or-loss account. A P&L balance-sheet leg
        # would double-count the remeasurement and misstate the carrying
        # amount, so reject it outright rather than silently posting it.
        for item in self:
            account = item.balance_sheet_account_id
            if account and account.internal_group in ('income', 'expense'):
                raise UserError(_(
                    "The balance sheet account on %s must be an asset, "
                    "liability or equity account. %s is a profit-or-loss "
                    "account and cannot carry a fair-value remeasurement.",
                    item.display_name, account.display_name))

    @api.constrains('nature', 'instrument_type', 'fvoci_equity_election',
                    'held_for_trading', 'fvoci_classification')
    def _check_ifrs9_combos(self):
        for item in self:
            if (item.instrument_type
                    and item.nature not in ('financial_asset',
                                            'financial_liability')):
                raise ValidationError(_(
                    "%s: an instrument type only applies to a financial "
                    "asset or financial liability; a non-financial item is "
                    "measured under its own standard, not IFRS 9.",
                    item.display_name))
            if (item.nature == 'financial_liability'
                    and item.instrument_type == 'equity'):
                raise ValidationError(_(
                    "%s: a holding of an equity instrument is a financial "
                    "asset; a financial liability cannot be classified as "
                    "an equity instrument (and can never take the FVOCI "
                    "equity election).", item.display_name))
            if item.fvoci_equity_election:
                if item.instrument_type != 'equity':
                    raise ValidationError(_(
                        "%s: the FVOCI election (IFRS 9.5.7.5) is available "
                        "only for an equity instrument, never for a debt "
                        "instrument or a derivative.", item.display_name))
                if item.held_for_trading:
                    raise ValidationError(_(
                        "%s: an equity instrument held for trading cannot "
                        "take the FVOCI election (IFRS 9.5.7.5).",
                        item.display_name))
            if (item.instrument_type == 'derivative'
                    and item.fvoci_classification
                    and item.fvoci_classification != 'fvtpl'):
                raise ValidationError(_(
                    "%s: a derivative is always at fair value through "
                    "profit or loss; it cannot carry an FVOCI label.",
                    item.display_name))

    @api.constrains('routing', 'ifrs9_classification')
    def _check_routing_derived(self):
        # The recognition routing is a consequence of the IFRS 9
        # classification, not a choice: FVTPL to profit or loss, FVOCI to
        # OCI. The compute derives it; this constraint catches any raw write
        # that tries to detach the two.
        mapping = {'amortised_cost': 'pl', 'fvtpl': 'pl',
                   'fvoci_debt': 'oci', 'fvoci_equity': 'oci'}
        for item in self:
            expected = mapping.get(item.ifrs9_classification)
            if expected and item.routing != expected:
                raise ValidationError(_(
                    "%s: routing is derived from the IFRS 9 classification "
                    "(%s requires %s) and cannot be overridden.",
                    item.display_name, item.ifrs9_classification, expected))

    @api.constrains('ifrs9_classification', 'fvoci_classification')
    def _check_legacy_label_consistent(self):
        for item in self:
            if (item.ifrs9_classification and item.fvoci_classification
                    and item.fvoci_classification
                    != item.ifrs9_classification):
                raise ValidationError(_(
                    "%s: the derived IFRS 9 classification is %s; the "
                    "manual legacy label %s conflicts with it. Clear the "
                    "legacy label or fix the questionnaire.",
                    item.display_name, item.ifrs9_classification,
                    item.fvoci_classification))

    @api.constrains('retained_earnings_account_id')
    def _check_retained_earnings_account(self):
        # The equity-election transfer must stay within equity; a P&L
        # destination would smuggle the reserve into profit or loss.
        for item in self:
            account = item.retained_earnings_account_id
            if account and account.internal_group in ('income', 'expense'):
                raise ValidationError(_(
                    "%s: the retained earnings account must be an equity "
                    "account; %s is a profit-or-loss account.",
                    item.display_name, account.display_name))

    # Fields locked once the item has been measured and posted, so a
    # recorded remeasurement cannot be retro-edited out from under its
    # journal entry. The classification inputs are included: flipping them
    # would silently re-derive the routing under a posted entry.
    _FROZEN_AFTER_MEASURED = (
        'fair_value', 'prior_carrying', 'routing',
        'balance_sheet_account_id', 'gain_loss_account_id',
        'oci_account_id', 'nature', 'instrument_type', 'business_model',
        'sppi_fixed_dates', 'sppi_interest_only', 'sppi_no_leverage',
        'sppi_no_contingent_returns', 'held_for_trading',
        'fvoci_equity_election', 'fvoci_classification',
    )
    # A derecognised item is a closed position: only narrative fields stay
    # writable (chatter plumbing is whitelisted by prefix in write()).
    _WRITABLE_AFTER_DERECOGNISED = ('notes', 'unobservable_inputs')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.fair.value.item') or '/'
        return super().create(vals_list)

    def write(self, vals):
        if 'fvoci_equity_election' in vals:
            # The FVOCI equity election is irrevocable at initial recognition
            # (IFRS 9.5.7.5): once any entry has been posted under the
            # elected treatment the flag can never flip, in any state.
            for item in self:
                if (bool(vals['fvoci_equity_election'])
                        != item.fvoci_equity_election
                        and any(m.state == 'posted' for m in item.move_ids)):
                    raise UserError(_(
                        "The FVOCI equity election on %s is irrevocable: a "
                        "journal entry has already been posted under the "
                        "elected treatment.", item.display_name))
        blocked = [
            f for f in vals
            if f not in self._WRITABLE_AFTER_DERECOGNISED
            and not f.startswith('message_') and not f.startswith('activity_')
        ]
        if blocked:
            for item in self:
                if item.state == 'derecognised':
                    raise UserError(_(
                        "Item %s is derecognised and closed; %s cannot be "
                        "changed.", item.display_name, ', '.join(blocked)))
        frozen = [f for f in self._FROZEN_AFTER_MEASURED if f in vals]
        if frozen:
            for item in self:
                # action_remeasure rolls prior_carrying forward as it sets the
                # state to measured in the same write; allow that transition
                # but block edits once the item is already measured.
                if item.state == 'measured' and vals.get('state') != 'measured':
                    raise UserError(_(
                        "Item %s is measured and posted; %s cannot be "
                        "changed. Cancel and create a new measurement "
                        "instead.",
                        item.display_name, ', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        # An item that has posted a remeasurement carries the posting-move
        # link; deleting the master would orphan a posted GL entry. Block it
        # once any move exists (measured, or later reset-to-draft / cancelled
        # after a measurement). Items that never posted have no move and stay
        # deletable.
        posted = self.filtered(lambda i: i.move_ids)
        if posted:
            raise UserError(_(
                "A fair-value item with a posted remeasurement cannot be "
                "deleted; its journal entry would be orphaned. Cancel it "
                "instead."))
        return super().unlink()

    # ---- actions ----

    def action_remeasure(self):
        self.ensure_one()
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post a fair-value "
                "remeasurement."))
        if self.state == 'cancelled':
            raise UserError(_("Cannot remeasure a cancelled item."))
        if self.state == 'derecognised':
            raise UserError(_(
                "Item %s is derecognised; a closed position cannot be "
                "remeasured.", self.display_name))
        if self._effective_classification() == 'amortised_cost':
            raise UserError(_(
                "%s is classified at amortised cost (SPPI passed, "
                "hold-to-collect business model); it is carried at "
                "amortised cost and is not remeasured to fair value "
                "(IFRS 9.4.1.2).", self.display_name))
        self._validate_accounts()
        currency = self.currency_id
        change = currency.round(self.remeasurement)
        if currency.is_zero(change):
            raise UserError(_(
                "Fair value equals the carrying amount; nothing to post."))
        counter = (self.oci_account_id if self.routing == 'oci'
                   else self.gain_loss_account_id)
        # The posting direction depends on the item's nature. For an asset a
        # rise in fair value is a gain and increases the balance-sheet asset
        # (Dr asset / Cr gain); a fall is a loss (Dr loss / Cr asset). For a
        # financial liability the mirror holds: a rise increases the liability
        # and is a loss (Dr loss / Cr liability); a fall decreases the
        # liability and is a gain (Dr liability / Cr gain).
        is_liability = self.nature == 'financial_liability'
        amount = abs(change)
        rise = change > 0
        if is_liability:
            # Balance-sheet credit increases the liability, debit decreases it.
            if rise:
                lines = [
                    (counter, amount, 0.0,
                     _("Fair value loss %s", self.name)),
                    (self.balance_sheet_account_id, 0.0, amount,
                     _("Fair value increase %s", self.name)),
                ]
            else:
                lines = [
                    (self.balance_sheet_account_id, amount, 0.0,
                     _("Fair value decrease %s", self.name)),
                    (counter, 0.0, amount,
                     _("Fair value gain %s", self.name)),
                ]
        else:
            # Balance-sheet debit increases the asset, credit decreases it.
            if rise:
                lines = [
                    (self.balance_sheet_account_id, amount, 0.0,
                     _("Fair value increase %s", self.name)),
                    (counter, 0.0, amount,
                     _("Fair value gain %s", self.name)),
                ]
            else:
                lines = [
                    (counter, amount, 0.0,
                     _("Fair value loss %s", self.name)),
                    (self.balance_sheet_account_id, 0.0, amount,
                     _("Fair value decrease %s", self.name)),
                ]
        self._post_move(lines)
        self.write({'state': 'measured', 'prior_carrying': self.fair_value})
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can cancel a fair-value "
                "item."))
        for item in self:
            if item.state == 'cancelled':
                raise UserError(_(
                    "Item %s is already cancelled.", item.display_name))
            if item.state == 'derecognised':
                raise UserError(_(
                    "Item %s is derecognised; a closed position cannot be "
                    "cancelled.", item.display_name))
            item.state = 'cancelled'

    def action_reset_to_draft(self):
        """Reopen a measured item so a new fair value can be recorded.

        The prior remeasurement stays posted; resetting only lifts the write
        freeze so the next fair value (and its incremental remeasurement) can
        be entered from the rolled-forward carrying amount.
        """
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can reopen a fair-value "
                "item."))
        self.filtered(lambda i: i.state == 'measured').write(
            {'state': 'draft'})
        return True

    def action_recycle(self):
        """Recycle the accumulated OCI reserve on derecognition (IFRS 9.5.7.5).

        For an FVOCI-debt instrument the reserve accumulated in OCI is
        reclassified to profit or loss. For an FVOCI equity election the
        reserve is transferred within equity and is never routed through
        profit or loss. FVTPL never accumulates an OCI reserve, so there is
        nothing to recycle. The classification must be set; when it is unset
        the plain remeasurement flow is unchanged and this action is a no-op
        guarded by an error.
        """
        self.ensure_one()
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can recycle a fair-value "
                "reserve."))
        if self.state == 'derecognised':
            raise UserError(_(
                "Item %s is derecognised; its reserve was settled by the "
                "derecognition action.", self.display_name))
        classification = self._effective_classification()
        if not classification:
            raise UserError(_(
                "Set the IFRS 9 classification on %s before recycling its "
                "reserve.", self.display_name))
        if classification in ('fvtpl', 'amortised_cost'):
            raise UserError(_(
                "A %s item never accumulates a fair-value OCI reserve; "
                "there is nothing to recycle.", classification))
        if self.recycled:
            raise UserError(_(
                "The reserve on %s has already been recycled.",
                self.display_name))
        if not self.oci_account_id:
            raise UserError(_(
                "Configure the OCI / equity account on %s first.",
                self.display_name))
        if not self.journal_id:
            raise UserError(_("Configure the journal on %s first.",
                              self.display_name))
        currency = self.currency_id
        reserve = currency.round(self.oci_reserve_balance)
        if currency.is_zero(reserve):
            raise UserError(_(
                "There is no accumulated OCI reserve on %s to recycle.",
                self.display_name))
        # The OCI account carries the reserve as a net credit for an
        # accumulated gain (balance negative in debit-less terms). Clear the
        # reserve back to zero on the OCI leg, and post the offsetting leg to
        # profit or loss (FVOCI-debt) or to the destination equity account
        # (FVOCI equity election). The move balances by construction.
        amount = abs(reserve)
        oci_is_debit = reserve < 0  # reserve sits as a credit -> debit to clear
        if classification == 'fvoci_equity':
            # The equity election transfers the reserve within equity, never
            # through profit or loss. The destination must itself be an equity
            # account; fall back to the OCI account (a no-op transfer) when no
            # separate equity destination is configured.
            destination = (self.retained_earnings_account_id
                           or self.gain_loss_account_id
                           or self.oci_account_id)
            if destination.internal_group in ('income', 'expense'):
                raise UserError(_(
                    "An FVOCI equity election transfers within equity and "
                    "cannot be reclassified to the profit-or-loss account %s "
                    "on %s.", destination.display_name, self.display_name))
            reclass_label = _("Transfer within equity %s", self.name)
        else:  # fvoci_debt
            destination = self.gain_loss_account_id
            if not destination:
                raise UserError(_(
                    "Configure the gain / loss account on %s to recycle the "
                    "reserve to profit or loss.", self.display_name))
            reclass_label = _("Recycle OCI to P&L %s", self.name)
        if oci_is_debit:
            legs = [
                (self.oci_account_id, amount, 0.0,
                 _("Clear OCI reserve %s", self.name)),
                (destination, 0.0, amount, reclass_label),
            ]
        else:
            legs = [
                (destination, amount, 0.0, reclass_label),
                (self.oci_account_id, 0.0, amount,
                 _("Clear OCI reserve %s", self.name)),
            ]
        self._post_move(legs)
        self.recycled = True
        return True

    def action_derecognise(self, proceeds=None):
        """Derecognise the item and settle its OCI reserve atomically.

        IFRS 9.3.2 (assets) / 9.3.3 (liabilities): posts the disposal entry
        for the proceeds against the carrying amount, then in the same
        action settles the accumulated OCI reserve by classification:

        * FVOCI-debt: the reserve is reclassified to profit or loss as a
          reclassification adjustment (IFRS 9.5.7.10).
        * FVOCI-equity election: the reserve, including the final change up
          to the disposal price, is transferred within equity to retained
          earnings and never touches profit or loss (IFRS 9.B5.7.1).
        * FVTPL / amortised cost / unclassified: nothing to recycle.

        The item is frozen afterwards (state ``derecognised``).
        """
        self.ensure_one()
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can derecognise a fair-value "
                "item."))
        if self.state == 'derecognised':
            raise UserError(_(
                "Item %s is already derecognised.", self.display_name))
        if self.state != 'measured':
            # The recycle amount is exact only when the carrying amount and
            # the OCI reserve are current, so a final remeasurement at the
            # disposal date must come first.
            raise UserError(_(
                "Remeasure %s to its fair value at the disposal date before "
                "derecognising it, so the carrying amount and the OCI "
                "reserve are current.", self.display_name))
        if proceeds is not None:
            self.disposal_proceeds = proceeds
        classification = self._effective_classification()
        if self.routing == 'oci' and not classification:
            raise UserError(_(
                "Item %s accumulated an OCI reserve but has no IFRS 9 "
                "classification; set the instrument type (or the legacy "
                "label) so the reserve settlement is determined.",
                self.display_name))
        currency = self.currency_id
        carrying = currency.round(self.prior_carrying)
        proceeds_amt = currency.round(self.disposal_proceeds)
        if proceeds_amt < 0:
            raise UserError(_(
                "Disposal proceeds on %s cannot be negative.",
                self.display_name))
        is_liability = self.nature == 'financial_liability'
        if is_liability:
            difference = currency.round(carrying - proceeds_amt)
        else:
            difference = currency.round(proceeds_amt - carrying)
        # For an FVOCI equity election the final change against carrying is
        # the last fair-value movement and belongs in OCI, never in profit or
        # loss; it is swept into the reserve transfer below. Every other
        # classification takes the difference to profit or loss
        # (IFRS 9.3.2.12).
        diff_to_oci = classification == 'fvoci_equity' and not is_liability
        diff_account = (self.oci_account_id if diff_to_oci
                        else self.gain_loss_account_id)
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        if not self.balance_sheet_account_id:
            missing.append(_("balance sheet account"))
        if proceeds_amt and not self.settlement_account_id:
            missing.append(_("settlement account"))
        if difference and not diff_account:
            missing.append(_("gain / loss account"))
        if classification == 'fvoci_debt' and not self.gain_loss_account_id:
            missing.append(_("gain / loss account"))
        if classification == 'fvoci_equity':
            if not self.retained_earnings_account_id:
                missing.append(_("retained earnings account"))
            if not self.oci_account_id:
                missing.append(_("OCI / equity account"))
        if missing:
            raise UserError(_(
                "Configure the %s on item %s first.",
                ', '.join(dict.fromkeys(missing)), self.display_name))
        day = self.derecognition_date or fields.Date.context_today(self)
        legs = []
        if is_liability:
            if carrying:
                legs.append((self.balance_sheet_account_id, carrying, 0.0,
                             _("Derecognise carrying %s", self.name)))
            if proceeds_amt:
                legs.append((self.settlement_account_id, 0.0, proceeds_amt,
                             _("Settlement %s", self.name)))
            if difference > 0:
                legs.append((diff_account, 0.0, difference,
                             _("Gain on derecognition %s", self.name)))
            elif difference < 0:
                legs.append((diff_account, -difference, 0.0,
                             _("Loss on derecognition %s", self.name)))
        else:
            if proceeds_amt:
                legs.append((self.settlement_account_id, proceeds_amt, 0.0,
                             _("Disposal proceeds %s", self.name)))
            if carrying:
                legs.append((self.balance_sheet_account_id, 0.0, carrying,
                             _("Derecognise carrying %s", self.name)))
            if difference > 0:
                legs.append((diff_account, 0.0, difference,
                             _("Gain on derecognition %s", self.name)))
            elif difference < 0:
                legs.append((diff_account, -difference, 0.0,
                             _("Loss on derecognition %s", self.name)))
        if legs:
            self._post_move(legs, date=day)
        if classification in ('fvoci_debt', 'fvoci_equity'):
            # Re-read after the disposal posting so a final OCI leg (equity
            # election) is included and the recycle amount is exact.
            reserve = currency.round(self.oci_reserve_balance)
            if not currency.is_zero(reserve):
                amount = abs(reserve)
                if classification == 'fvoci_equity':
                    destination = self.retained_earnings_account_id
                    reclass_label = _(
                        "Transfer reserve to retained earnings %s", self.name)
                else:
                    destination = self.gain_loss_account_id
                    reclass_label = _("Recycle OCI to P&L %s", self.name)
                clear_label = _("Clear OCI reserve %s", self.name)
                if reserve < 0:  # net credit (accumulated gain) -> debit out
                    recycle_legs = [
                        (self.oci_account_id, amount, 0.0, clear_label),
                        (destination, 0.0, amount, reclass_label),
                    ]
                else:
                    recycle_legs = [
                        (destination, amount, 0.0, reclass_label),
                        (self.oci_account_id, 0.0, amount, clear_label),
                    ]
                self._post_move(recycle_legs, date=day)
        self.write({
            'state': 'derecognised',
            'derecognition_date': day,
            'disposal_proceeds': proceeds_amt,
            'recycled': classification in ('fvoci_debt', 'fvoci_equity'),
        })
        return True

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_fair_value_item_id', '=', self.id)],
        }

    # ---- helpers ----

    def _effective_classification(self):
        # The engine-derived classification governs whenever the
        # questionnaire is in use; the manual legacy label only stands for
        # records without an instrument type.
        self.ensure_one()
        return self.ifrs9_classification or self.fvoci_classification

    def _validate_accounts(self):
        self.ensure_one()
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        if not self.balance_sheet_account_id:
            missing.append(_("balance sheet account"))
        if self.routing == 'oci' and not self.oci_account_id:
            missing.append(_("OCI / equity account"))
        if self.routing == 'pl' and not self.gain_loss_account_id:
            missing.append(_("gain / loss account"))
        if missing:
            raise UserError(_(
                "Configure the %s on item %s first.",
                ', '.join(missing), self.display_name))

    def _post_move(self, legs, date=None):
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': date or self.measurement_date,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_fair_value_item_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class EhFairValueRollforward(models.Model):
    _name = 'eh.fair.value.rollforward'
    _description = "Level 3 fair value reconciliation (IFRS 13.93(e))"
    _inherit = ['eh.workflow.guard']
    _order = 'period_end desc, id desc'

    # A reconciliation may only close (and reopen) through its own actions,
    # which enforce the tie-to-fair-value check; a direct write of
    # state='closed' would bypass that check and freeze an untied disclosure.
    _eh_guarded_fields = ('state',)

    item_id = fields.Many2one(
        'eh.fair.value.item', string="Fair Value Item", required=True,
        ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='item_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='item_id.currency_id', store=True, readonly=True)
    state = fields.Selection(
        [('draft', "Draft"), ('closed', "Closed")],
        default='draft', required=True, index=True,
        help="Closing requires the computed closing balance to tie exactly "
             "to the item's fair value (IFRS 13.93(e)); a closed "
             "reconciliation is frozen.")
    move_ids = fields.One2many(
        'account.move', 'eh_fair_value_rollforward_id',
        string="Linked Journal Entries",
        help="The item's posted entries attributed to this period (Pull "
             "Ledger); they feed the gains columns from the ledger instead "
             "of a typed figure.")

    period_start = fields.Date(
        required=True, default=fields.Date.context_today,
        help="First day of the reconciliation period.")
    period_end = fields.Date(
        required=True, default=fields.Date.context_today,
        help="Reporting date the reconciliation rolls forward to.")

    # ---- roll-forward movements (IFRS 13.93(e)) ----
    opening_balance = fields.Monetary(
        currency_field='currency_id',
        help="Fair value at the beginning of the period.")
    gains_losses_in_pl = fields.Monetary(
        currency_field='currency_id', string="Gains / Losses in P&L",
        compute='_compute_ledger_gains', store=True, readonly=False,
        help="Total gains or losses for the period recognised in profit or "
             "loss (a loss is negative). Ledger-fed from the linked journal "
             "entries when any are attached; manual otherwise.")
    gains_losses_in_oci = fields.Monetary(
        currency_field='currency_id', string="Gains / Losses in OCI",
        compute='_compute_ledger_gains', store=True, readonly=False,
        help="Total gains or losses for the period recognised in other "
             "comprehensive income (a loss is negative). Ledger-fed from "
             "the linked journal entries when any are attached; manual "
             "otherwise.")
    purchases = fields.Monetary(currency_field='currency_id')
    issues = fields.Monetary(currency_field='currency_id')
    sales = fields.Monetary(currency_field='currency_id')
    settlements = fields.Monetary(currency_field='currency_id')
    transfers_into_level3 = fields.Monetary(
        currency_field='currency_id', string="Transfers into Level 3")
    transfers_out_of_level3 = fields.Monetary(
        currency_field='currency_id', string="Transfers out of Level 3")

    closing_balance = fields.Monetary(
        compute='_compute_closing_balance', store=True,
        currency_field='currency_id',
        help="Opening plus gains, purchases, issues and transfers in, less "
             "sales, settlements and transfers out (IFRS 13.93(e)).")

    notes = fields.Text()

    _sql_constraints = [
        ('period_order', 'CHECK (period_end >= period_start)', "The reconciliation period end cannot be before its start."),
        ('unique_item_period', 'UNIQUE (item_id, period_start, period_end)', "A fair-value item can have only one reconciliation per period."),
    ]

    @api.depends('opening_balance', 'gains_losses_in_pl', 'gains_losses_in_oci',
                 'purchases', 'issues', 'sales', 'settlements',
                 'transfers_into_level3', 'transfers_out_of_level3')
    def _compute_closing_balance(self):
        for line in self:
            line.closing_balance = (
                line.opening_balance
                + line.gains_losses_in_pl
                + line.gains_losses_in_oci
                + line.purchases
                + line.issues
                - line.sales
                - line.settlements
                + line.transfers_into_level3
                - line.transfers_out_of_level3
            )

    @api.depends('move_ids.line_ids.debit', 'move_ids.line_ids.credit',
                 'move_ids.state', 'item_id.gain_loss_account_id',
                 'item_id.oci_account_id')
    def _compute_ledger_gains(self):
        # With journal entries linked, the gains columns are read from the
        # ledger, not typed: a credit on the item's P&L / OCI account is a
        # gain, a debit a loss. Without links the manually captured value
        # stands (legacy records and hand-built disclosures).
        for line in self:
            posted = line.move_ids.filtered(lambda m: m.state == 'posted')
            if not posted:
                line.gains_losses_in_pl = line.gains_losses_in_pl or 0.0
                line.gains_losses_in_oci = line.gains_losses_in_oci or 0.0
                continue
            pl = oci = 0.0
            for ml in posted.line_ids:
                if ml.account_id == line.item_id.gain_loss_account_id:
                    pl += ml.credit - ml.debit
                elif ml.account_id == line.item_id.oci_account_id:
                    oci += ml.credit - ml.debit
            line.gains_losses_in_pl = pl
            line.gains_losses_in_oci = oci

    # Movement figures locked once the reconciliation is closed, so a tied
    # disclosure cannot drift out from under its sign-off.
    _FROZEN_AFTER_CLOSED = (
        'opening_balance', 'gains_losses_in_pl', 'gains_losses_in_oci',
        'purchases', 'issues', 'sales', 'settlements',
        'transfers_into_level3', 'transfers_out_of_level3',
        'period_start', 'period_end', 'item_id',
    )

    def write(self, vals):
        frozen = [f for f in self._FROZEN_AFTER_CLOSED if f in vals]
        if frozen:
            for line in self:
                if line.state == 'closed':
                    raise UserError(_(
                        "The Level 3 reconciliation %s is closed; %s cannot "
                        "be changed. Reopen it first.",
                        line.display_name, ', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        if any(line.state == 'closed' for line in self):
            raise UserError(_(
                "A closed Level 3 reconciliation cannot be deleted; reopen "
                "it first."))
        return super().unlink()

    # ---- actions ----

    def action_pull_ledger(self):
        """Attribute the item's posted entries dated in the period to this
        reconciliation, so the gains columns are ledger-fed, not typed."""
        for line in self:
            if line.state == 'closed':
                raise UserError(_(
                    "The Level 3 reconciliation %s is closed; reopen it "
                    "before re-pulling the ledger.", line.display_name))
            moves = line.item_id.move_ids.filtered(
                lambda m, line=line: m.state == 'posted' and m.date
                and line.period_start <= m.date <= line.period_end)
            stale = line.move_ids - moves
            if stale:
                stale.write({'eh_fair_value_rollforward_id': False})
            if moves:
                moves.write({'eh_fair_value_rollforward_id': line.id})
        return True

    def action_close(self):
        """Close the reconciliation; refused unless it ties to fair value.

        IFRS 13.93(e) requires the roll-forward to reconcile opening to
        closing fair value; a reconciliation whose closing balance does not
        equal the item's fair value is not a reconciliation, so closing it
        is blocked rather than warned about.
        """
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can close a Level 3 "
                "reconciliation."))
        for line in self:
            if line.state == 'closed':
                raise UserError(_(
                    "The Level 3 reconciliation %s is already closed.",
                    line.display_name))
            delta = line.closing_balance - line.item_id.fair_value
            if not line.currency_id.is_zero(delta):
                raise UserError(_(
                    "Cannot close the Level 3 reconciliation for %(item)s: "
                    "the closing balance %(closing)s does not tie to the "
                    "item's fair value %(fv)s (difference %(delta)s). Fix "
                    "the movements or pull the ledger before closing.",
                    item=line.item_id.display_name,
                    closing=line.closing_balance,
                    fv=line.item_id.fair_value,
                    delta=delta))
            line.state = 'closed'
        return True

    def action_reopen(self):
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can reopen a Level 3 "
                "reconciliation."))
        self.filtered(lambda line: line.state == 'closed').write(
            {'state': 'draft'})
        return True


class EhFairValueSensitivity(models.Model):
    _name = 'eh.fair.value.sensitivity'
    _description = "Level 3 sensitivity analysis (IFRS 13.93(h))"
    _order = 'item_id, input_name, shock_pct, id'
    _rec_name = 'input_name'

    item_id = fields.Many2one(
        'eh.fair.value.item', string="Fair Value Item", required=True,
        ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='item_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='item_id.currency_id', store=True, readonly=True)

    input_name = fields.Char(
        required=True, string="Unobservable Input",
        help="Significant unobservable input being shocked, e.g. discount "
             "rate, terminal growth rate, capitalisation rate.")
    shock_pct = fields.Float(
        string="Shock (%)", required=True,
        help="Relative shock applied to the unobservable input, in percent; "
             "negative for a downward shock.")
    sensitivity_factor = fields.Float(
        default=1.0, required=True,
        help="Elasticity of the fair value to the shocked input: the "
             "fraction of the relative input shock that flows through to "
             "the measurement. 1.0 is a one-for-one linear response.")
    value_delta = fields.Monetary(
        compute='_compute_value_delta', store=True,
        currency_field='currency_id', string="Value Delta",
        help="fair value x shock% x factor: the change in the measurement "
             "if the input moved by the shock, the quantitative sensitivity "
             "IFRS 13.93(h) asks for.")
    notes = fields.Char()

    _sql_constraints = [
        ('shock_not_zero', 'CHECK (shock_pct <> 0)', "A sensitivity line needs a non-zero shock."),
    ]

    @api.depends('item_id.fair_value', 'shock_pct', 'sensitivity_factor')
    def _compute_value_delta(self):
        for line in self:
            delta = (line.item_id.fair_value * line.shock_pct / 100.0
                     * line.sensitivity_factor)
            currency = line.currency_id or line.item_id.currency_id
            line.value_delta = currency.round(delta) if currency else delta


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_fair_value_item_id = fields.Many2one(
        'eh.fair.value.item', string="Fair Value Item", readonly=True,
        index=True, ondelete='restrict', copy=False)
    eh_fair_value_rollforward_id = fields.Many2one(
        'eh.fair.value.rollforward', string="Level 3 Reconciliation",
        readonly=True, index=True, ondelete='set null', copy=False,
        help="Reconciliation period this entry is attributed to, so the "
             "roll-forward gains columns are ledger-fed.")
