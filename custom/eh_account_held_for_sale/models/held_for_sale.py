# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.held.for.sale: a non-current asset or disposal group held for sale.

On classification the item is remeasured to the lower of its carrying amount
and fair value less costs to sell, posting any write-down; depreciation then
ceases (IFRS 5.15, 25). On sale the disposal posts proceeds against the
carrying amount with the resulting gain or loss.
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# carrying_amount is the ledger-derived figure: it must equal the posted
# remeasurement and so cannot be edited by hand once the item leaves draft.
# The model's own actions set it through the 'eh_hfs_internal' context flag.
# fair_value_less_costs stays editable while held: it is the manager's input
# to the sanctioned Remeasure action, not a posted figure.
_LOCKED_MEASURE_FIELDS = frozenset({'carrying_amount'})
_FROZEN_STATES = frozenset({'held', 'sold'})


class EhHeldForSale(models.Model):
    _name = 'eh.held.for.sale'
    _description = "Asset held for sale (IFRS 5)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'classification_date desc, id desc'
    _rec_name = 'name'

    # State is a control point: without this guard a plain user could
    # RPC-write it straight to 'held'/'sold', skipping action_classify /
    # action_sell and their journal entries. Only the record's own actions
    # (which carry the eh_workflow_action flag) may change it.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    description = fields.Char(
        help="The asset or disposal group, e.g. 'Retail division - "
             "Northgate store'.")
    state = fields.Selection(
        [('draft', "Draft"), ('held', "Held for sale"),
         ('sold', "Sold"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    classification_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    extension_12m = fields.Boolean(
        string="12-Month Extension (IFRS 5.9)", tracking=True,
        help="The sale has not completed within one year of classification "
             "because of events or circumstances beyond the entity's "
             "control, and the entity remains committed to its plan to "
             "sell. Setting this suppresses the overdue flag; the item is "
             "never auto-declassified (disclose-first design).")
    overdue_12m = fields.Boolean(
        string="Overdue (12 Months)",
        compute='_compute_overdue_12m', search='_search_overdue_12m',
        help="The item has been held for sale for more than twelve months "
             "without the IFRS 5.9 extension. Flag only: review whether "
             "the held-for-sale criteria are still met and either record "
             "the extension or cease classification.")
    is_discontinued = fields.Boolean(
        string="Discontinued operation",
        help="Present separately in the statement of comprehensive income "
             "(IFRS 5.33). Pick the operation's P&L accounts below and "
             "use Tag Discontinued P&L; the statements module reads the "
             "resulting account tag.")
    discontinued_tag_id = fields.Many2one(
        'account.account.tag', readonly=True, copy=False,
        string="Discontinued Operations Tag",
        help="Per-company account tag applied to the selected P&L "
             "accounts by Tag Discontinued P&L (shared with disposal "
             "groups of the same company).")
    discontinued_pl_account_ids = fields.Many2many(
        'account.account', 'eh_held_for_sale_disc_account_rel',
        'item_id', 'account_id', string="Discontinued P&L Accounts",
        domain="[('account_type', 'in', ['income', 'income_other', "
               "'expense', 'expense_depreciation', 'expense_direct_cost'])]",
        help="P&L accounts of the discontinued operation this item "
             "represents; Tag Discontinued P&L stamps them with the "
             "company's discontinued-operations tag (IFRS 5.33).")
    # ---- optional disposal-group membership ----
    # A single-asset record can join an eh.disposal.group. IFRS 5.15
    # measures a disposal group as a whole, so while a record is grouped
    # its standalone Remeasure is blocked: the group's classify/remeasure
    # actions are the sanctioned measurement path. The record's own
    # posted history (its original classification) stays valid.
    group_id = fields.Many2one(
        'eh.disposal.group', string="Disposal Group", tracking=True,
        ondelete='set null', copy=False,
        domain="[]",
        help="Disposal group this item belongs to. While grouped (and the "
             "group is not cancelled) the item is measured at group "
             "level and its standalone Remeasure is blocked (IFRS 5.15).")
    depreciation_ceased = fields.Boolean(
        readonly=True, copy=False,
        help="Depreciation stops while the asset is held for sale "
             "(IFRS 5.25).")

    # ---- optional link to the fixed-asset engine ----
    # When an eh.asset is linked, classification drives its carrying amount
    # from the asset's ledger-derived net book value (not a hand-keyed
    # figure) and pauses the asset so the depreciation cron ceases charging
    # it (IFRS 5.25). Leaving this blank keeps the standalone, hand-keyed
    # behaviour unchanged.
    asset_id = fields.Many2one(
        'eh.asset', string="Linked Asset", tracking=True,
        ondelete='restrict', copy=False,
        domain="[('company_id', '=', company_id), "
               "('state', 'in', ['running', 'paused'])]",
        help="Fixed asset this classification covers. On classification the "
             "carrying amount is seeded from the asset's net book value and "
             "the asset's depreciation is paused so it is not charged twice "
             "(IFRS 5.15, 5.25). Leave blank for a standalone, hand-keyed "
             "item.")
    asset_paused_by_hfs = fields.Boolean(
        readonly=True, copy=False,
        help="Set when this classification paused the linked asset, so a "
             "reversal or cancellation only resumes an asset it paused "
             "itself (not one the user had already paused).")

    carrying_amount = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True,
        help="Carrying amount immediately before classification.")
    fair_value_less_costs = fields.Monetary(
        string="Fair Value Less Costs to Sell",
        currency_field='currency_id', required=True, tracking=True)
    writedown = fields.Monetary(
        compute='_compute_writedown', store=True, currency_field='currency_id',
        help="Excess of carrying amount over fair value less costs to sell.")
    proceeds = fields.Monetary(
        currency_field='currency_id',
        help="Net proceeds received on sale.")

    # ---- accounts ----
    asset_account_id = fields.Many2one(
        'account.account', string="Asset Account", tracking=True)
    impairment_account_id = fields.Many2one(
        'account.account', string="Impairment Expense Account", tracking=True,
        domain="[('account_type', 'in', ['expense', 'expense_depreciation'])]")
    proceeds_account_id = fields.Many2one(
        'account.account', string="Proceeds Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'asset_receivable', 'asset_current'])]")
    gain_loss_account_id = fields.Many2one(
        'account.account', string="Gain / Loss on Disposal Account",
        tracking=True,
        domain="[('account_type', 'in', ['income_other', 'expense'])]")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]")

    move_ids = fields.One2many('account.move', 'eh_held_for_sale_id')
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    _sql_constraints = [
        ('check_carrying', 'CHECK (carrying_amount >= 0)', 'Carrying amount cannot be negative.'),
    ]

    @api.depends('carrying_amount', 'fair_value_less_costs')
    def _compute_writedown(self):
        for item in self:
            item.writedown = max(
                item.carrying_amount - item.fair_value_less_costs, 0.0)

    def _compute_move_count(self):
        for item in self:
            item.move_count = len(item.move_ids)

    @api.depends('state', 'classification_date', 'extension_12m')
    def _compute_overdue_12m(self):
        today = fields.Date.context_today(self)
        for item in self:
            item.overdue_12m = bool(
                item.state == 'held'
                and not item.extension_12m
                and item.classification_date
                and item.classification_date + relativedelta(years=1)
                < today)

    def _search_overdue_12m(self, operator, value):
        # Overdue iff held, no extension, and the classification
        # anniversary has passed (classification_date < today - 1 year).
        if isinstance(value, (list, tuple)):
            value = True in value
        if operator in ('!=', 'not in'):
            value = not value
        elif operator not in ('=', 'in'):
            raise NotImplementedError(
                "Unsupported operator %r on overdue_12m" % operator)
        cutoff = fields.Date.context_today(self) - relativedelta(years=1)
        if value:
            return [('state', '=', 'held'), ('extension_12m', '=', False),
                    ('classification_date', '<', cutoff)]
        return ['|', '|', ('state', '!=', 'held'),
                ('extension_12m', '=', True),
                ('classification_date', '>=', cutoff)]

    @api.onchange('asset_id')
    def _onchange_asset_id(self):
        """Pre-fill the carrying amount from the linked asset's net book
        value so the user does not hand-key a figure that would double the
        carrying value. Only pre-fills in draft; the authoritative seed
        still happens in action_classify off the ledger at posting time.
        """
        for item in self:
            if item.asset_id and item.state == 'draft':
                item.carrying_amount = item.currency_id.round(
                    item.asset_id.net_book_value)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.held.for.sale') or '/'
        return super().create(vals_list)

    def write(self, vals):
        if (
            _LOCKED_MEASURE_FIELDS.intersection(vals)
            and not self.env.context.get('eh_hfs_internal')
            and any(item.state in _FROZEN_STATES for item in self)
        ):
            raise UserError(_(
                "The carrying amount is locked once the item is classified "
                "as held for sale; it must equal the posted remeasurement. "
                "Set a new fair value less costs to sell and use Remeasure to "
                "record a subsequent change (IFRS 5.15)."))
        # The state of a held / sold item is itself a control point: resetting
        # it to draft would silently lift the carrying-amount freeze above. A
        # raw ORM state write moving OUT of a frozen state without the
        # sanctioned-transition context flag must be manager-gated, so a plain
        # user cannot un-freeze a GL-backed item. The sanctioned actions
        # (sell / cancel) set the flag after their own manager check and move
        # handling; classify moves INTO 'held' and is unaffected.
        if 'state' in vals \
                and not self.env.context.get('eh_hfs_state_change'):
            crossing = self.filtered(
                lambda item: item.state in _FROZEN_STATES
                and item.state != vals['state'])
            if crossing:
                crossing._check_manager()
        return super().write(vals)

    def unlink(self):
        posted = self.filtered(lambda item: item.state in _FROZEN_STATES)
        if posted:
            raise UserError(_(
                "A held-for-sale item that has been classified or sold cannot "
                "be deleted; it carries a posted GL movement. Cancel it "
                "(EH Accounting Manager only) instead."))
        return super().unlink()

    # ---- actions ----

    def action_classify(self):
        self.ensure_one()
        self = self._eh_workflow_action()
        self._check_manager()
        if self.state != 'draft':
            raise UserError(_(
                "Only a draft item can be classified as held for sale."))
        self._validate_accounts(['asset', 'impairment'])
        currency = self.currency_id
        # When linked to a fixed asset, seed the carrying amount from the
        # asset's ledger-derived net book value and cease its depreciation
        # so the two records cannot double-count the carrying value
        # (IFRS 5.15, 5.25). The write-down below then measures off that
        # ledger figure rather than a hand-keyed one.
        if self.asset_id:
            self._eh_seed_from_asset()
            self._eh_cease_asset_depreciation()
        writedown = currency.round(self.writedown)
        if writedown > 0:
            if self.asset_id:
                # Linked asset: route the write-down through the asset's own
                # impairment engine so the eh.asset net book value falls by the
                # same amount and the two subledgers reconcile (IFRS 5.15). One
                # journal entry, sharing the held-for-sale accounts, keeps the
                # asset ledger and the carrying amount from diverging.
                self._eh_post_asset_impairment(writedown, is_reversal=False)
            else:
                self._post_move([
                    (self.impairment_account_id, writedown, 0.0,
                     _("Held-for-sale write-down %s", self.name)),
                    (self.asset_account_id, 0.0, writedown,
                     _("Remeasure to FVLCTS %s", self.name)),
                ])
            self.with_context(eh_hfs_internal=True).write(
                {'carrying_amount': self.carrying_amount - writedown})
        self.write({'state': 'held', 'depreciation_ceased': True})
        return True

    def action_remeasure(self):
        """Subsequent remeasurement of a held-for-sale item (IFRS 5.15).

        On each reporting date remeasure to the lower of the current carrying
        amount and fair value less costs to sell. A fall posts a further
        write-down; a subsequent rise reverses previously recognised
        write-downs, but not above the carrying amount before classification
        (IFRS 5.21-22). This is the only sanctioned path to move the carrying
        amount once the item is held for sale.
        """
        self.ensure_one()
        self._check_manager()
        if self.state != 'held':
            raise UserError(_(
                "Only an item held for sale can be remeasured."))
        if self.group_id and self.group_id.state != 'cancelled':
            raise UserError(_(
                "%s belongs to disposal group %s: IFRS 5.15 measures a "
                "disposal group as a whole, so remeasure the group "
                "instead. Remove the item from the group to measure it "
                "standalone again.", self.name, self.group_id.display_name))
        self._validate_accounts(['asset', 'impairment'])
        currency = self.currency_id
        carrying = currency.round(self.carrying_amount)
        fvlcts = currency.round(self.fair_value_less_costs)
        delta = currency.round(fvlcts - carrying)
        if not delta:
            raise UserError(_(
                "The carrying amount already equals fair value less costs to "
                "sell; there is nothing to remeasure on %s.", self.name))
        if delta < 0:
            # Further write-down: DR impairment, CR asset.
            amount = -delta
            if self.asset_id:
                self._eh_post_asset_impairment(amount, is_reversal=False)
            else:
                self._post_move([
                    (self.impairment_account_id, amount, 0.0,
                     _("Held-for-sale remeasurement write-down %s", self.name)),
                    (self.asset_account_id, 0.0, amount,
                     _("Remeasure to FVLCTS %s", self.name)),
                ])
        else:
            # Reversal of a prior write-down: DR asset, CR impairment.
            # IFRS 5.22 caps the gain so the carrying amount is not raised
            # above the amount that would have been carried had the item never
            # been written down, i.e. the reversal cannot exceed the cumulative
            # write-downs previously recognised. Anything above that cap is not
            # posted and the carrying amount is held at the ceiling.
            cap = self._cumulative_writedown()
            amount = currency.round(min(delta, cap))
            if amount <= 0:
                raise UserError(_(
                    "The reversal is fully limited by IFRS 5.22: fair value "
                    "less costs to sell may not lift the carrying amount above "
                    "its cumulative write-downs on %s.", self.name))
            if self.asset_id:
                self._eh_post_asset_impairment(amount, is_reversal=True)
            else:
                self._post_move([
                    (self.asset_account_id, amount, 0.0,
                     _("Held-for-sale write-down reversal %s", self.name)),
                    (self.impairment_account_id, 0.0, amount,
                     _("Reverse impairment %s", self.name)),
                ])
            capped_carrying = currency.round(carrying + amount)
            self.with_context(eh_hfs_internal=True).write(
                {'carrying_amount': capped_carrying})
            return True
        self.with_context(eh_hfs_internal=True).write(
            {'carrying_amount': fvlcts})
        return True

    def _cumulative_writedown(self):
        """Net write-down still recognised on this item, in company currency.

        Every classification/remeasurement write-down debits the impairment
        account and every reversal credits it, all on moves linked to this
        item. The net debit on that account across the item's own posted moves
        is therefore the cumulative write-down available to reverse under
        IFRS 5.22.
        """
        self.ensure_one()
        lines = self.move_ids.filtered(
            lambda m: m.state == 'posted'
        ).line_ids.filtered(
            lambda line: line.account_id == self.impairment_account_id)
        return self.currency_id.round(
            sum(lines.mapped('debit')) - sum(lines.mapped('credit')))

    def action_sell(self):
        self.ensure_one()
        self = self._eh_workflow_action()
        self._check_manager()
        if self.state != 'held':
            raise UserError(_(
                "Only an item held for sale can be sold."))
        currency = self.currency_id
        proceeds = currency.round(self.proceeds)
        if self.asset_id:
            # Linked fixed asset: derecognise it through the asset engine so
            # its cost, accumulated depreciation and accumulated impairment
            # (including the held-for-sale write-downs, routed through the
            # asset's own impairment engine) are all reversed, gain/loss is
            # booked, and the eh.asset moves to 'disposed'. The standalone
            # carrying-derecognition below would leave the asset paused with a
            # live net book value and its gross cost / accumulated GL balances
            # never reversed.
            self._eh_dispose_linked_asset(proceeds)
            self.asset_paused_by_hfs = False
            self.with_context(
                eh_hfs_internal=True, eh_hfs_state_change=True).write(
                {'state': 'sold', 'carrying_amount': 0.0})
            return True
        self._validate_accounts(['asset', 'proceeds', 'gain_loss'])
        carrying = currency.round(self.carrying_amount)
        gain_loss = currency.round(proceeds - carrying)
        lines = []
        if proceeds:
            lines.append((self.proceeds_account_id, proceeds, 0.0,
                          _("Disposal proceeds %s", self.name)))
        if carrying:
            lines.append((self.asset_account_id, 0.0, carrying,
                          _("Derecognise asset %s", self.name)))
        if gain_loss > 0:
            lines.append((self.gain_loss_account_id, 0.0, gain_loss,
                          _("Gain on disposal %s", self.name)))
        elif gain_loss < 0:
            lines.append((self.gain_loss_account_id, -gain_loss, 0.0,
                          _("Loss on disposal %s", self.name)))
        if not lines:
            raise UserError(_("Nothing to post on sale of %s.", self.name))
        self._post_move(lines)
        self.with_context(
            eh_hfs_internal=True, eh_hfs_state_change=True).write(
            {'state': 'sold', 'carrying_amount': 0.0})
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        for item in self:
            if item.state == 'sold':
                raise UserError(_("A sold item cannot be cancelled."))
            self._check_manager()
            # Cease-to-be-classified: an asset paused on classification
            # resumes depreciation once it no longer meets the held-for-sale
            # criteria (IFRS 5.26).
            if item.asset_id:
                item._eh_resume_asset()
            # Sanctioned exit out of 'held': flag the state write so the
            # state-reset gate in write() lets this manager-driven transition
            # through (a plain raw reset to draft stays blocked).
            item.with_context(eh_hfs_state_change=True).write(
                {'state': 'cancelled'})

    def action_tag_discontinued(self):
        """Apply the company's discontinued-operations tag (IFRS 5.33) to
        the P&L accounts picked on this record. Shares the per-company
        tag (and the statements hook) with eh.disposal.group. Idempotent.
        """
        self.ensure_one()
        self._check_manager()
        if not self.is_discontinued:
            raise UserError(_(
                "Flag %s as a discontinued operation first.",
                self.display_name))
        if not self.discontinued_pl_account_ids:
            raise UserError(_(
                "Pick the P&L accounts of the discontinued operation on "
                "%s first.", self.display_name))
        tag = self.env['eh.disposal.group']._eh_discontinued_tag(
            self.company_id, create=True)
        for account in self.discontinued_pl_account_ids:
            if tag not in account.tag_ids:
                account.write({'tag_ids': [(4, tag.id)]})
        self.discontinued_tag_id = tag.id
        return True

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_held_for_sale_id', '=', self.id)],
        }

    # ---- helpers ----

    def _eh_seed_from_asset(self):
        """Set the carrying amount to the linked asset's net book value.

        The asset's net_book_value is the ledger-derived carrying amount
        (cost less accumulated depreciation, impairment and revaluation),
        so classification measures off it rather than a hand-keyed figure
        (IFRS 5.15).
        """
        self.ensure_one()
        nbv = self.currency_id.round(self.asset_id.net_book_value)
        self.with_context(eh_hfs_internal=True).write(
            {'carrying_amount': nbv})

    def _eh_cease_asset_depreciation(self):
        """Pause the linked asset via its own API so the monthly cron
        skips it while it is held for sale (IFRS 5.25).

        Only a running asset is paused; an asset already paused is left as
        is but not stamped, so a later resume does not reactivate an asset
        the user had deliberately paused. A disposed or fully depreciated
        asset cannot be classified.
        """
        self.ensure_one()
        asset = self.asset_id
        if asset.state == 'running':
            asset.action_pause()
            self.asset_paused_by_hfs = True
        elif asset.state == 'paused':
            self.asset_paused_by_hfs = False
        else:
            raise UserError(_(
                "Asset %s cannot be classified as held for sale from state "
                "'%s'; only a running or paused asset can be held for sale.",
                asset.display_name, asset.state))

    def _eh_post_asset_impairment(self, amount, is_reversal):
        """Post a write-down (or reversal) on the linked asset through its
        own eh.asset.impairment engine, sharing the held-for-sale accounts.

        Routing the write-down through the asset engine reduces the eh.asset
        net book value by the same amount, so the asset subledger and the
        held-for-sale carrying amount stay reconciled after classification and
        remeasurement (IFRS 5.15). The impairment record is fed the
        held-for-sale accounts (impairment expense as the P&L leg, the
        held-for-sale asset account as the contra) so the journal entry legs
        are the same accounts the standalone path would have used. The move it
        posts is tagged back to this record so the cumulative-write-down cap
        (IFRS 5.22), the move count and the entries view all still see it.
        """
        self.ensure_one()
        amount = self.currency_id.round(amount)
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': self.asset_id.id,
            'impairment_date': self.classification_date,
            'amount': amount,
            'is_reversal': is_reversal,
            'reason': _("Held-for-sale remeasurement (IFRS 5.15) %s",
                        self.name),
            'impairment_account_id': self.impairment_account_id.id,
            'accumulated_account_id': self.asset_account_id.id,
            'journal_id': self.journal_id.id,
        })
        impairment.action_post()
        impairment.move_id.eh_held_for_sale_id = self.id
        return impairment

    def _eh_dispose_linked_asset(self, proceeds):
        """Dispose the linked eh.asset through its own dispose engine.

        Reverses the asset's gross cost, accumulated depreciation and
        accumulated impairment, books the proceeds and the gain/loss on the
        asset's disposal accounts, and moves the asset to 'disposed'. The eh
        manager group implies account.group_account_manager, so the wizard's
        own manager gate is satisfied; sudo() lets it run in one call.
        """
        self.ensure_one()
        if proceeds > 0 and not self.proceeds_account_id:
            raise UserError(_(
                "Configure a proceeds account on %s before selling a linked "
                "asset.", self.display_name))
        vals = {
            'asset_id': self.asset_id.id,
            'disposal_date': fields.Date.context_today(self),
            'proceeds': proceeds,
        }
        if self.proceeds_account_id:
            vals['cash_account_id'] = self.proceeds_account_id.id
        self.env['eh.asset.dispose.wizard'].sudo().create(vals).action_dispose()

    def _eh_resume_asset(self):
        """Resume a linked asset that this record paused on classification.

        Guarded by asset_paused_by_hfs so a cease-to-be-classified event or
        cancellation only reactivates an asset we paused ourselves, and only
        while it is still paused (IFRS 5.26 resumes depreciation once the
        asset no longer meets the held-for-sale criteria).
        """
        self.ensure_one()
        if self.asset_paused_by_hfs and self.asset_id.state == 'paused':
            self.asset_id.action_resume()
        self.asset_paused_by_hfs = False

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post held-for-sale "
                "entries."))

    def _validate_accounts(self, needed):
        self.ensure_one()
        field_map = {
            'asset': ('asset_account_id', _("asset account")),
            'impairment': ('impairment_account_id', _("impairment account")),
            'proceeds': ('proceeds_account_id', _("proceeds account")),
            'gain_loss': ('gain_loss_account_id', _("gain / loss account")),
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
            'date': self.classification_date,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_held_for_sale_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_held_for_sale_id = fields.Many2one(
        'eh.held.for.sale', string="Held for Sale", readonly=True,
        index=True, ondelete='restrict', copy=False)
