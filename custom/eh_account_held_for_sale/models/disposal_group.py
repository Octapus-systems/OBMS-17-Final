# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.disposal.group: a disposal group held for sale (IFRS 5).

A disposal group is a set of assets to be disposed of together in a single
transaction, plus the liabilities directly associated with those assets that
will be transferred with them (IFRS 5 Appendix A). Measurement happens at
GROUP level: on classification the group is remeasured to the lower of its
aggregate carrying amount and its fair value less costs to sell, and any
write-down is allocated to the members in the IFRS 5.23 order (which applies
IAS 36.104-105):

* first against any goodwill members;
* then pro rata, by carrying amount, over the members inside the IFRS 5
  measurement scope;
* never below a member's own fair-value floor when one is recorded (a
  member is not written below the highest of its fair value less costs of
  disposal and zero, IAS 36.105); the excess re-prorates over the
  remaining scope members;
* never to members OUTSIDE the measurement scope (financial assets,
  inventories carried at NRV, deferred tax assets, IFRS 5.5): flag those
  lines out of scope and they receive no allocation;
* never to liability members (they are derecognised on sale, not written
  down).

If every scope member reaches its floor before the shortfall is fully
allocated, the residual stays unallocated per IAS 36.105 (it is logged on
the record; no member is forced below its floor).

Members can reference a fixed asset (eh.asset). On classification such a
member seeds its carrying amount from the asset's ledger-derived net book
value, the asset is paused so depreciation ceases (IFRS 5.25), and the
member's share of any write-down is recorded against the asset through an
eh.asset.impairment row attached to the single group journal entry, so the
asset subledger and the group stay reconciled (IFRS 5.15).

A subsequent rise in fair value less costs to sell reverses previous
write-downs, capped at the cumulative write-down recognised on the group's
non-goodwill members (IFRS 5.22; goodwill write-downs are never reversed,
IAS 36.124).

12-month rule (IFRS 5.9): the sale is expected to complete within one year
of classification. This module is disclose-first by design: a held group
past its classification anniversary is flagged (overdue_12m) in the list
and the filter, but never auto-declassified; the extension flag records
the IFRS 5.9 exception (delay caused by events beyond the entity's
control while it remains committed to the plan).

Discontinued operations hook (IFRS 5.33): flagging a group (or a single
held-for-sale record) as discontinued and running "Tag Discontinued P&L"
applies a per-company account tag ("EH Discontinued Operations (<company>)")
to the P&L accounts selected on the member lines. The statement builder
integration lives in eh_account_statements; the contract exposed here is:

    env['eh.disposal.group'].eh_discontinued_pl_amount(
        period_from, period_to, company=company)

which returns the posted P&L total (credit minus debit, profit positive) of
the tagged accounts for the period. This module never edits
eh_account_statements.
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_FROZEN_STATES = frozenset({'held', 'sold'})

# Member measurement fields frozen once the group leaves draft: the group
# write-down was allocated over these values and posted, so re-shaping the
# membership afterwards would desync the lines from the ledger. The group's
# own actions write through the 'eh_dg_internal' context flag.
_LINE_MEASURE_FIELDS = frozenset({
    'carrying_amount', 'is_liability', 'is_goodwill', 'in_scope',
    'fair_value_floor', 'asset_id',
})
# Allocation stamps are engine output at every state, never user input.
_LINE_INTERNAL_FIELDS = frozenset({
    'allocated_writedown', 'cumulative_writedown',
})


class EhDisposalGroup(models.Model):
    _name = 'eh.disposal.group'
    _description = "Disposal group held for sale (IFRS 5)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'classification_date desc, id desc'
    _rec_name = 'name'

    # State is a control point: without this guard a plain user could
    # RPC-write it straight to 'held'/'sold', skipping action_classify /
    # action_sell and the single allocated journal entry. Only the group's
    # own actions (which carry the eh_workflow_action flag) may change it.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    description = fields.Char(
        help="The disposal group, e.g. 'Retail division - Northgate "
             "cluster'.")
    state = fields.Selection(
        [('draft', "Draft"), ('held', "Held for sale"),
         ('sold', "Sold"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True,
        copy=False)

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
             "sell. Setting this suppresses the overdue flag; the group is "
             "never auto-declassified (disclose-first design).")
    overdue_12m = fields.Boolean(
        string="Overdue (12 Months)",
        compute='_compute_overdue_12m', search='_search_overdue_12m',
        help="The group has been held for sale for more than twelve months "
             "without the IFRS 5.9 extension. Flag only: review whether "
             "the held-for-sale criteria are still met and either record "
             "the extension or cease classification.")
    is_discontinued = fields.Boolean(
        string="Discontinued operation", tracking=True,
        help="Present separately in the statement of comprehensive income "
             "(IFRS 5.33). Use Tag Discontinued P&L to mark the member "
             "lines' P&L accounts with the company's discontinued-"
             "operations tag; the statements module reads that tag.")
    discontinued_tag_id = fields.Many2one(
        'account.account.tag', readonly=True, copy=False,
        string="Discontinued Operations Tag",
        help="Per-company account tag applied to the member lines' P&L "
             "accounts by Tag Discontinued P&L. The statement of "
             "comprehensive income segregates the tagged accounts into "
             "the single discontinued-operations line.")
    depreciation_ceased = fields.Boolean(
        readonly=True, copy=False,
        help="Depreciation stops on every member while the group is held "
             "for sale (IFRS 5.25).")

    line_ids = fields.One2many(
        'eh.disposal.group.line', 'group_id', string="Members", copy=True)

    carrying_amount = fields.Monetary(
        compute='_compute_carrying_amount', store=True,
        currency_field='currency_id',
        help="Aggregate carrying amount of the group: asset members less "
             "directly associated liability members. Derived from the "
             "member lines; asset-linked lines are seeded from the asset's "
             "net book value on classification.")
    fair_value_less_costs = fields.Monetary(
        string="Fair Value Less Costs to Sell",
        currency_field='currency_id', required=True, tracking=True,
        help="Fair value less costs to sell of the disposal group as a "
             "whole (IFRS 5.15 measures the group, not its members).")
    writedown = fields.Monetary(
        compute='_compute_writedown', store=True,
        currency_field='currency_id',
        help="Excess of the group carrying amount over its fair value "
             "less costs to sell.")
    cumulative_writedown = fields.Monetary(
        compute='_compute_cumulative_writedown',
        currency_field='currency_id',
        help="Net write-down currently recognised across the member lines "
             "(including goodwill). Reversals are capped at the "
             "non-goodwill portion (IFRS 5.22, IAS 36.124).")
    proceeds = fields.Monetary(
        currency_field='currency_id',
        help="Net proceeds received on sale of the group.")

    # ---- accounts ----
    asset_account_id = fields.Many2one(
        'account.account', string="Asset Account (Fallback)", tracking=True,
        help="Balance-sheet account used for a member line that does not "
             "set its own member account.")
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

    move_ids = fields.One2many('account.move', 'eh_disposal_group_id')
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    # ---- computes ----

    @api.depends('line_ids.carrying_amount', 'line_ids.is_liability')
    def _compute_carrying_amount(self):
        for group in self:
            group.carrying_amount = group.currency_id.round(sum(
                -line.carrying_amount if line.is_liability
                else line.carrying_amount
                for line in group.line_ids))

    @api.depends('carrying_amount', 'fair_value_less_costs')
    def _compute_writedown(self):
        for group in self:
            group.writedown = max(
                group.carrying_amount - group.fair_value_less_costs, 0.0)

    @api.depends('line_ids.cumulative_writedown')
    def _compute_cumulative_writedown(self):
        for group in self:
            group.cumulative_writedown = group.currency_id.round(
                sum(group.line_ids.mapped('cumulative_writedown')))

    def _compute_move_count(self):
        for group in self:
            group.move_count = len(group.move_ids)

    @api.depends('state', 'classification_date', 'extension_12m')
    def _compute_overdue_12m(self):
        today = fields.Date.context_today(self)
        for group in self:
            group.overdue_12m = bool(
                group.state == 'held'
                and not group.extension_12m
                and group.classification_date
                and group.classification_date + relativedelta(years=1)
                < today)

    def _search_overdue_12m(self, operator, value):
        # Overdue iff held, no extension, and the classification anniversary
        # has passed: classification_date + 1 year < today, i.e.
        # classification_date < today - 1 year.
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

    # ---- ORM guards (mirroring the single-asset flow) ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.disposal.group') or '/'
        return super().create(vals_list)

    def write(self, vals):
        # The state of a held / sold group is a control point: a raw ORM
        # state write moving OUT of a frozen state without the sanctioned-
        # transition flag must be manager-gated, so a plain user cannot
        # un-freeze a GL-backed group. The sanctioned actions (sell /
        # cancel) set the flag after their own manager check; classify
        # moves INTO 'held' and is unaffected.
        if 'state' in vals \
                and not self.env.context.get('eh_dg_state_change'):
            crossing = self.filtered(
                lambda group: group.state in _FROZEN_STATES
                and group.state != vals['state'])
            if crossing:
                crossing._check_manager()
        return super().write(vals)

    def unlink(self):
        posted = self.filtered(lambda group: group.state in _FROZEN_STATES)
        if posted:
            raise UserError(_(
                "A disposal group that has been classified or sold cannot "
                "be deleted; it carries a posted GL movement. Cancel it "
                "(EH Accounting Manager only) instead."))
        return super().unlink()

    # ---- actions ----

    def action_classify(self):
        """Classify the group as held for sale (IFRS 5.15, 5.23, 5.25).

        Seeds asset-linked members from their ledger net book value,
        pauses those assets (depreciation ceases on every member), then
        remeasures the group to the lower of carrying amount and fair
        value less costs to sell. A shortfall posts ONE journal entry with
        a per-member impairment leg pair, allocated goodwill-first then
        pro rata over the scope members, floor-capped (IFRS 5.23 applying
        IAS 36.104-105).
        """
        self.ensure_one()
        self = self._eh_workflow_action()
        self._check_manager()
        if self.state != 'draft':
            raise UserError(_(
                "Only a draft disposal group can be classified as held "
                "for sale."))
        if not self.line_ids:
            raise UserError(_(
                "Add at least one member line to %s before classifying "
                "it.", self.display_name))
        if not self.line_ids.filtered(lambda line: not line.is_liability):
            raise UserError(_(
                "A disposal group needs at least one asset member; %s "
                "only lists liabilities.", self.display_name))
        self._validate_accounts(['impairment'])
        self._validate_member_accounts()
        currency = self.currency_id
        for line in self.line_ids.filtered('asset_id'):
            line._eh_seed_from_asset()
            line._eh_cease_asset_depreciation()
        loss = currency.round(self.writedown)
        if loss > 0:
            plan, allocated, unallocated = self._eh_allocation_plan(loss)
            if plan:
                self._eh_post_writedown(plan, is_reversal=False)
            if unallocated > 0.005:
                # IAS 36.105: no member is written below its floor; the
                # residual shortfall stays unallocated. Disclose-first.
                self.message_post(body=_(
                    "Write-down of %(loss).2f could only be allocated for "
                    "%(alloc).2f: every in-scope member reached its "
                    "fair-value floor (IAS 36.105). The residual "
                    "%(residual).2f is not allocated and the group carries "
                    "above its fair value less costs to sell; review the "
                    "member floors and scope flags.",
                    loss=loss, alloc=allocated, residual=unallocated))
        self.write({'state': 'held', 'depreciation_ceased': True})
        return True

    def action_remeasure(self):
        """Subsequent group remeasurement (IFRS 5.15, 5.21-22).

        A fall in fair value less costs to sell posts a further allocated
        write-down. A rise reverses previous write-downs pro rata to the
        members' cumulative write-downs, capped at the cumulative amount
        recognised on non-goodwill members (IFRS 5.22; goodwill
        write-downs are never reversed, IAS 36.124).
        """
        self.ensure_one()
        # A subsequent remeasurement of an asset-linked member stamps the
        # asset's own eh.asset.impairment.state (a guarded field on
        # eh.workflow.guard) through _eh_attach_asset_impairment. Elevate the
        # action first so that sanctioned write runs under env.su and passes
        # the guard, mirroring action_classify / action_sell; without this a
        # real (non-superuser) manager hits AccessError and the whole
        # remeasurement rolls back (IFRS 5.21-22 cannot be booked).
        self = self._eh_workflow_action()
        self._check_manager()
        if self.state != 'held':
            raise UserError(_(
                "Only a disposal group held for sale can be remeasured."))
        self._validate_accounts(['impairment'])
        self._validate_member_accounts()
        currency = self.currency_id
        carrying = currency.round(self.carrying_amount)
        fvlcts = currency.round(self.fair_value_less_costs)
        delta = currency.round(fvlcts - carrying)
        if not delta:
            raise UserError(_(
                "The group carrying amount already equals fair value less "
                "costs to sell; there is nothing to remeasure on %s.",
                self.name))
        if delta < 0:
            plan, allocated, unallocated = self._eh_allocation_plan(-delta)
            if not plan:
                raise UserError(_(
                    "No further write-down can be allocated on %s: every "
                    "in-scope member is at its fair-value floor "
                    "(IAS 36.105).", self.name))
            self._eh_post_writedown(plan, is_reversal=False)
            if unallocated > 0.005:
                self.message_post(body=_(
                    "Remeasurement write-down allocated %(alloc).2f of "
                    "%(loss).2f; the residual %(residual).2f is blocked by "
                    "member fair-value floors (IAS 36.105).",
                    alloc=allocated, loss=-delta, residual=unallocated))
        else:
            plan, total = self._eh_reversal_plan(delta)
            if not plan:
                raise UserError(_(
                    "The reversal is fully limited on %s: a gain may not "
                    "exceed the cumulative write-down previously "
                    "recognised (IFRS 5.22), and goodwill write-downs are "
                    "never reversed (IAS 36.124).", self.name))
            self._eh_post_writedown(plan, is_reversal=True)
        return True

    def action_sell(self):
        self.ensure_one()
        self = self._eh_workflow_action()
        self._check_manager()
        if self.state != 'held':
            raise UserError(_(
                "Only a disposal group held for sale can be sold."))
        self._validate_accounts(['proceeds', 'gain_loss'])
        self._validate_member_accounts()
        currency = self.currency_id
        proceeds = currency.round(self.proceeds)
        # Asset-linked members are derecognised through the asset engine so the
        # underlying eh.asset is actually DISPOSED (cost + accumulated
        # depreciation + impairment reversed, state -> disposed), not left
        # paused with a live net book value. Proceeds are allocated pro-rata by
        # carrying across the non-liability members, so each disposed asset
        # carries its share and the residual funds the standalone move for the
        # remaining (non-asset) members and the liabilities.
        asset_members = self.line_ids.filtered(
            lambda line_item: line_item.asset_id and not line_item.is_liability)
        total_carry = sum(
            currency.round(line_item.carrying_amount)
            for line_item in self.line_ids.filtered(lambda line_item: not line_item.is_liability))
        remaining_proceeds = proceeds
        for line in asset_members:
            share = currency.round(
                proceeds * currency.round(line.carrying_amount) / total_carry
            ) if total_carry else 0.0
            line._eh_dispose_member_asset(share)
            remaining_proceeds = currency.round(remaining_proceeds - share)

        rest = self.line_ids - asset_members
        legs = []
        if remaining_proceeds:
            legs.append((self.proceeds_account_id, remaining_proceeds, 0.0,
                         _("Disposal proceeds %s", self.name)))
        rest_carry = 0.0
        for line in rest.sorted('id'):
            amount = currency.round(line.carrying_amount)
            if not amount:
                continue
            account = line._eh_member_account()
            if line.is_liability:
                # Derecognise the directly associated liability.
                legs.append((account, amount, 0.0,
                             _("Derecognise liability %s", line._eh_label())))
                rest_carry -= amount
            else:
                legs.append((account, 0.0, amount,
                             _("Derecognise asset %s", line._eh_label())))
                rest_carry += amount
        gain_loss = currency.round(remaining_proceeds - rest_carry)
        if gain_loss > 0:
            legs.append((self.gain_loss_account_id, 0.0, gain_loss,
                         _("Gain on disposal %s", self.name)))
        elif gain_loss < 0:
            legs.append((self.gain_loss_account_id, -gain_loss, 0.0,
                         _("Loss on disposal %s", self.name)))
        if legs:
            self._post_move(legs)
        for line in self.line_ids:
            line.with_context(eh_dg_internal=True).write(
                {'carrying_amount': 0.0})
        self.with_context(eh_dg_state_change=True).write({'state': 'sold'})
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        for group in self:
            if group.state == 'sold':
                raise UserError(_(
                    "A sold disposal group cannot be cancelled."))
            group._check_manager()
            # Cease-to-be-classified: members paused on classification
            # resume depreciation once the group no longer meets the
            # held-for-sale criteria (IFRS 5.26).
            for line in group.line_ids.filtered('asset_id'):
                line._eh_resume_asset()
            group.with_context(eh_dg_state_change=True).write(
                {'state': 'cancelled'})
        return True

    def action_tag_discontinued(self):
        """Apply the company's discontinued-operations tag (IFRS 5.33) to
        the P&L accounts selected on the member lines. Idempotent."""
        self.ensure_one()
        self._check_manager()
        if not self.is_discontinued:
            raise UserError(_(
                "Flag %s as a discontinued operation first.",
                self.display_name))
        accounts = self.line_ids.mapped('pl_account_id')
        if not accounts:
            raise UserError(_(
                "Select the P&L accounts of the discontinued operation on "
                "the member lines of %s first.", self.display_name))
        tag = self._eh_discontinued_tag(self.company_id, create=True)
        for account in accounts:
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
            'domain': [('eh_disposal_group_id', '=', self.id)],
        }

    # ---- discontinued operations hook (used by eh_account_statements) ----

    @api.model
    def _eh_discontinued_tag(self, company, create=False):
        """Find (or create) the company's discontinued-operations account
        tag. account.account.tag has no company field, so the tag is keyed
        per company by name; uniqueness is guaranteed by the core
        unique(name, applicability, country) constraint."""
        name = "EH Discontinued Operations (%s)" % company.name
        Tag = self.env['account.account.tag'].sudo()
        tag = Tag.search([
            ('name', '=', name), ('applicability', '=', 'accounts'),
            ('country_id', '=', False)], limit=1)
        if not tag and create:
            tag = Tag.create({'name': name, 'applicability': 'accounts'})
        return tag

    @api.model
    def eh_discontinued_pl_amount(self, period_from, period_to, company=None):
        """Posted P&L total of the discontinued-operations tagged accounts.

        THE statements hook: eh_account_statements calls this to build the
        single 'profit or loss from discontinued operations' line of the
        statement of comprehensive income (IFRS 5.33(a)); this module
        deliberately does not edit the statements module.

        :param period_from: first date included (inclusive).
        :param period_to: last date included (inclusive).
        :param company: res.company (defaults to the environment company).
        :return: company-currency amount, profit positive (credit minus
            debit over the tagged accounts' posted journal items).
        """
        company = company or self.env.company
        tag = self._eh_discontinued_tag(company)
        if not tag:
            return 0.0
        lines = self.env['account.move.line'].search([
            ('parent_state', '=', 'posted'),
            ('company_id', '=', company.id),
            ('date', '>=', period_from),
            ('date', '<=', period_to),
            ('account_id.tag_ids', 'in', tag.ids),
        ])
        return company.currency_id.round(
            sum(lines.mapped('credit')) - sum(lines.mapped('debit')))

    # ---- allocation engine (IFRS 5.23 / IAS 36.104-105) ----

    def _eh_prorate_capped(self, amount, members):
        """Allocate ``amount`` pro rata by weight over ``members``, capped.

        :param members: list of (line, weight, cap) tuples, in line-id
            order, weight > 0, cap the most that member may absorb.
        :return: ({line: allocated}, total_allocated). Deterministic:
            a member whose pro-rata share exceeds its cap takes the cap
            and drops out, and the excess re-prorates over the remaining
            members (IAS 36.105 reallocation); rounding residue (cents)
            lands on the first member with headroom.
        """
        currency = self.currency_id
        alloc = {}
        remaining = currency.round(amount)
        active = [(line, weight, currency.round(cap))
                  for (line, weight, cap) in members
                  if weight > 0 and currency.round(cap) > 0]
        # Cap-and-reallocate: whenever a share would breach a cap, that
        # member takes exactly its cap and the rest re-prorates.
        capped = True
        while active and remaining > 0.005 and capped:
            capped = False
            total_weight = sum(weight for _line, weight, _cap in active)
            for line, weight, cap in active:
                if remaining * weight / total_weight > cap + 0.005:
                    alloc[line] = cap
                    remaining = currency.round(remaining - cap)
                    active = [t for t in active if t[0] != line]
                    capped = True
                    break
        # Final pro-rata pass over the survivors, exact to the cent.
        if active and remaining > 0.005:
            total_weight = sum(weight for _line, weight, _cap in active)
            shares = {}
            for line, weight, cap in active:
                shares[line] = min(
                    currency.round(remaining * weight / total_weight), cap)
            residue = currency.round(remaining - sum(shares.values()))
            for line, _weight, cap in active:
                if abs(residue) < 0.005:
                    break
                if residue > 0:
                    room = currency.round(cap - shares[line])
                    step = min(residue, room)
                    if step > 0:
                        shares[line] = currency.round(shares[line] + step)
                        residue = currency.round(residue - step)
                else:
                    step = min(-residue, shares[line])
                    if step > 0:
                        shares[line] = currency.round(shares[line] - step)
                        residue = currency.round(residue + step)
            alloc.update({line: value
                          for line, value in shares.items() if value})
        return alloc, currency.round(sum(alloc.values()))

    def _eh_allocation_plan(self, loss):
        """IFRS 5.23 write-down allocation: goodwill first, then pro rata
        over in-scope non-goodwill members, floor-capped per member.

        :return: (plan {line: amount}, allocated, unallocated).
        """
        self.ensure_one()
        currency = self.currency_id
        scope = self.line_ids.filtered(
            lambda line: line.in_scope and not line.is_liability)
        goodwill = scope.filtered('is_goodwill').sorted('id')
        others = (scope - goodwill).sorted('id')

        def pool(lines):
            members = []
            for line in lines:
                cap = currency.round(
                    line.carrying_amount - max(line.fair_value_floor, 0.0))
                if line.carrying_amount > 0 and cap > 0:
                    members.append((line, line.carrying_amount, cap))
            return members

        plan = {}
        remaining = currency.round(loss)
        for members in (pool(goodwill), pool(others)):
            if remaining <= 0.005 or not members:
                continue
            alloc, total = self._eh_prorate_capped(remaining, members)
            for line, amount in alloc.items():
                plan[line] = currency.round(plan.get(line, 0.0) + amount)
            remaining = currency.round(remaining - total)
        allocated = currency.round(loss - remaining)
        return plan, allocated, remaining

    def _eh_reversal_plan(self, gain):
        """IFRS 5.22 reversal: pro rata to the members' cumulative
        write-downs, capped per member at that cumulative amount, and
        excluding goodwill entirely (IAS 36.124).

        :return: (plan {line: amount}, total).
        """
        self.ensure_one()
        currency = self.currency_id
        eligible = self.line_ids.filtered(
            lambda line: line.in_scope and not line.is_liability
            and not line.is_goodwill
            and line.cumulative_writedown > 0.005).sorted('id')
        members = [(line, line.cumulative_writedown,
                    line.cumulative_writedown) for line in eligible]
        capacity = currency.round(
            sum(line.cumulative_writedown for line in eligible))
        amount = min(currency.round(gain), capacity)
        if amount <= 0:
            return {}, 0.0
        return self._eh_prorate_capped(amount, members)

    # ---- posting helpers ----

    def _eh_post_writedown(self, plan, is_reversal):
        """Post ONE journal entry with a per-member leg pair for the
        allocated write-down (or its reversal), stamp the lines, and
        record the event on each linked asset's impairment subledger."""
        self.ensure_one()
        currency = self.currency_id
        legs = []
        for line in sorted(plan, key=lambda line_item: line_item.id):
            amount = plan[line]
            account = line._eh_member_account()
            if is_reversal:
                label = _("Held-for-sale write-down reversal %s",
                          line._eh_label())
                legs.append((account, amount, 0.0, label))
                legs.append((self.impairment_account_id, 0.0, amount, label))
            else:
                label = _("Held-for-sale write-down %s", line._eh_label())
                legs.append((self.impairment_account_id, amount, 0.0, label))
                legs.append((account, 0.0, amount, label))
        move = self._post_move(legs)
        sign = -1 if is_reversal else 1
        for line, amount in plan.items():
            line.with_context(eh_dg_internal=True).write({
                'carrying_amount': currency.round(
                    line.carrying_amount - sign * amount),
                'allocated_writedown': sign * amount,
                'cumulative_writedown': currency.round(
                    line.cumulative_writedown + sign * amount),
            })
            if line.asset_id:
                self._eh_attach_asset_impairment(
                    line, amount, is_reversal, move)
        return move

    def _eh_attach_asset_impairment(self, line, amount, is_reversal, move):
        """Record a member's allocated write-down (or reversal) on the
        linked asset's own impairment subledger, attached to the single
        group journal entry.

        The single-asset flow routes a write-down through
        eh.asset.impairment.action_post, which posts its own entry. A
        group posts ONE entry for all members, whose legs for this member
        (impairment expense against the member account) are exactly what
        the engine would have posted, so here the impairment row is
        created in draft (its IAS 36 validation constraints run) and then
        marked posted against the group move instead of producing a
        second, duplicate entry. The asset's net book value falls by the
        allocated amount and the subledgers stay reconciled (IFRS 5.15);
        the remaining schedule is re-amortised per IAS 36.63 like every
        other impairment event.
        """
        self.ensure_one()
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': line.asset_id.id,
            'impairment_date': self.classification_date,
            'amount': self.currency_id.round(amount),
            'is_reversal': is_reversal,
            'reason': _(
                "Held-for-sale group remeasurement (IFRS 5.15, 5.23) %s",
                self.name),
            'impairment_account_id': self.impairment_account_id.id,
            'accumulated_account_id': line._eh_member_account().id,
            'journal_id': self.journal_id.id,
        })
        impairment.write({
            'state': 'posted',
            'move_id': move.id,
            'posted_at': fields.Datetime.now(),
            'posted_by_id': self.env.user.id,
        })
        line.asset_id._eh_rebuild_after_impairment()
        return impairment

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
            'eh_disposal_group_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move

    # ---- validation helpers ----

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post disposal-group "
                "entries."))

    def _validate_accounts(self, needed):
        self.ensure_one()
        field_map = {
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

    def _validate_member_accounts(self):
        self.ensure_one()
        if self.asset_account_id:
            return
        unaccounted = self.line_ids.filtered(lambda line: not line.account_id)
        if unaccounted:
            raise UserError(_(
                "Set a member account on every line of %s (or a fallback "
                "Asset Account on the group). Missing on: %s.",
                self.display_name,
                ', '.join(line._eh_label() for line in unaccounted)))


class EhDisposalGroupLine(models.Model):
    _name = 'eh.disposal.group.line'
    _description = "Disposal group member (IFRS 5)"
    _order = 'group_id, id'
    _rec_name = 'name'

    group_id = fields.Many2one(
        'eh.disposal.group', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='group_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='group_id.currency_id', store=True, readonly=True)
    group_state = fields.Selection(
        related='group_id.state', readonly=True)

    name = fields.Char(
        help="Member description, e.g. 'Northgate store fit-out'. "
             "Defaults from the linked asset.")
    asset_id = fields.Many2one(
        'eh.asset', string="Linked Asset", ondelete='restrict', copy=False,
        domain="[('state', 'in', ['running', 'paused'])]",
        help="Fixed asset this member covers. On classification the "
             "carrying amount is seeded from the asset's net book value, "
             "the asset is paused so depreciation ceases (IFRS 5.25), and "
             "any allocated write-down is recorded on the asset's own "
             "impairment subledger so the two stay reconciled (IFRS "
             "5.15). Leave blank for a free carrying entry.")
    is_liability = fields.Boolean(
        string="Liability",
        help="A liability directly associated with the assets of the "
             "group, transferred in the same transaction (IFRS 5 "
             "Appendix A). Enter the liability's carrying amount as a "
             "positive number; it reduces the group carrying amount and "
             "never receives any write-down allocation.")
    is_goodwill = fields.Boolean(
        string="Goodwill",
        help="Goodwill member: an IFRS 5.23 write-down is applied first "
             "to goodwill (IAS 36.104) and a goodwill write-down is never "
             "reversed (IAS 36.124).")
    in_scope = fields.Boolean(
        string="In Measurement Scope", default=True,
        help="Member is inside the IFRS 5 measurement scope and absorbs "
             "its pro-rata share of a group write-down. Untick for "
             "members measured under their own standard (financial "
             "assets, inventories at net realisable value, deferred tax "
             "assets, IFRS 5.5): they stay in the group's carrying amount "
             "but receive no allocation.")
    carrying_amount = fields.Monetary(
        currency_field='currency_id', required=True,
        help="Member carrying amount (always entered positive; liability "
             "members reduce the group total). Asset-linked lines are "
             "re-seeded from the asset's ledger net book value on "
             "classification.")
    fair_value_floor = fields.Monetary(
        currency_field='currency_id',
        help="Optional floor: the member is never written below the "
             "higher of this amount and zero (IAS 36.105, applied by "
             "IFRS 5.23). Any blocked excess re-prorates over the "
             "remaining scope members. Leave at zero for no floor beyond "
             "zero itself.")
    allocated_writedown = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Signed allocation from the most recent group measurement "
             "event (negative for a reversal). Engine output.")
    cumulative_writedown = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Net write-down currently recognised on this member; the "
             "member's reversal cap (IFRS 5.22). Engine output.")
    account_id = fields.Many2one(
        'account.account', string="Member Account",
        help="Balance-sheet account carrying this member (the write-down "
             "credit leg for assets; the derecognition leg on sale). "
             "Falls back to the group's fallback asset account.")
    pl_account_id = fields.Many2one(
        'account.account', string="Discontinued P&L Account",
        domain="[('account_type', 'in', ['income', 'income_other', "
               "'expense', 'expense_depreciation', 'expense_direct_cost'])]",
        help="P&L account of this member's operations, tagged by Tag "
             "Discontinued P&L for separate presentation (IFRS 5.33).")
    asset_paused_by_group = fields.Boolean(
        readonly=True, copy=False,
        help="Set when the group's classification paused the linked "
             "asset, so cancellation only resumes an asset the group "
             "paused itself.")

    _sql_constraints = [
        ('check_carrying', 'CHECK (carrying_amount >= 0)', 'Member carrying amounts are entered positive; flag liabilities '  # noqa: E501
        'with the Liability toggle instead of a negative amount.'),  # noqa: E128
        ('check_floor', 'CHECK (fair_value_floor >= 0)', 'The fair-value floor cannot be negative.'),
        ('unique_asset_per_group', 'UNIQUE (group_id, asset_id)', 'The same asset cannot appear twice in one disposal group.'),  # noqa: E501
    ]

    @api.constrains('is_liability', 'is_goodwill')
    def _check_liability_not_goodwill(self):
        for line in self:
            if line.is_liability and line.is_goodwill:
                raise ValidationError(_(
                    "A member cannot be both a liability and goodwill."))

    @api.constrains('asset_id', 'group_id')
    def _check_asset_company(self):
        for line in self:
            if line.asset_id and line.group_id \
                    and line.asset_id.company_id != line.group_id.company_id:
                raise ValidationError(_(
                    "Asset %s belongs to another company than disposal "
                    "group %s.", line.asset_id.display_name,
                    line.group_id.display_name))

    @api.onchange('asset_id')
    def _onchange_asset_id(self):
        for line in self:
            if line.asset_id:
                line.carrying_amount = line.currency_id.round(
                    line.asset_id.net_book_value)
                line.is_goodwill = line.asset_id.is_goodwill
                if not line.name:
                    line.name = line.asset_id.display_name

    # ---- ORM guards ----
    # The membership of a classified group backed a posted, allocated
    # write-down: adding, removing or re-measuring members afterwards would
    # desync the lines from the ledger, so the member set is frozen once
    # the group leaves draft. The group's own actions write through the
    # 'eh_dg_internal' context flag.

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get('eh_dg_internal'):
            for vals in vals_list:
                group = self.env['eh.disposal.group'].browse(
                    vals.get('group_id'))
                if group and group.state != 'draft':
                    raise UserError(_(
                        "Members cannot be added to %s once it has left "
                        "draft; the posted group measurement covered the "
                        "membership as classified.", group.display_name))
                if set(vals) & _LINE_INTERNAL_FIELDS:
                    raise UserError(_(
                        "The allocation fields are engine output and "
                        "cannot be hand-seeded."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.context.get('eh_dg_internal'):
            if set(vals) & _LINE_INTERNAL_FIELDS:
                raise UserError(_(
                    "The allocation fields are engine output; they must "
                    "equal the posted group measurement and cannot be "
                    "edited."))
            if set(vals) & _LINE_MEASURE_FIELDS:
                frozen = self.filtered(
                    lambda line: line.group_id.state != 'draft')
                if frozen:
                    raise UserError(_(
                        "Member measurement fields are locked once the "
                        "group is classified; they must reconcile to the "
                        "posted group measurement. Use Remeasure on the "
                        "group to record a subsequent change (IFRS 5.15)."))
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('eh_dg_internal'):
            frozen = self.filtered(
                lambda line: line.group_id.state != 'draft')
            if frozen:
                raise UserError(_(
                    "Members cannot be removed from a classified disposal "
                    "group; the posted group measurement covered them."))
        return super().unlink()

    # ---- helpers ----

    def _eh_label(self):
        self.ensure_one()
        return (self.asset_id.display_name or self.name
                or _("member %s", self.id))

    def _eh_member_account(self):
        self.ensure_one()
        return self.account_id or self.group_id.asset_account_id

    def _eh_seed_from_asset(self):
        """Seed the member carrying amount from the linked asset's
        ledger-derived net book value (IFRS 5.15)."""
        self.ensure_one()
        nbv = self.currency_id.round(self.asset_id.net_book_value)
        self.with_context(eh_dg_internal=True).write(
            {'carrying_amount': nbv})

    def _eh_dispose_member_asset(self, proceeds):
        """Dispose this member's linked eh.asset through the asset engine on
        group sale, so its cost, accumulated depreciation and accumulated
        impairment are reversed and it moves to 'disposed' instead of staying
        paused. Proceeds is this member's pro-rata share of the group's sale
        proceeds. Gain/loss books on the asset's own disposal accounts.
        """
        self.ensure_one()
        if not self.asset_id:
            return
        vals = {
            'asset_id': self.asset_id.id,
            'disposal_date': fields.Date.context_today(self),
            'proceeds': proceeds,
        }
        if self.group_id.proceeds_account_id:
            vals['cash_account_id'] = self.group_id.proceeds_account_id.id
        self.env['eh.asset.dispose.wizard'].sudo().create(
            vals).action_dispose()
        self.with_context(eh_dg_internal=True).write(
            {'asset_paused_by_group': False})

    def _eh_cease_asset_depreciation(self):
        """Pause the linked asset so the depreciation cron skips it while
        the group is held for sale (IFRS 5.25). Mirrors the single-asset
        flow: only a running asset is paused and stamped; an asset the
        user had already paused is left alone."""
        self.ensure_one()
        asset = self.asset_id
        if asset.state == 'running':
            asset.action_pause()
            self.with_context(eh_dg_internal=True).write(
                {'asset_paused_by_group': True})
        elif asset.state == 'paused':
            self.with_context(eh_dg_internal=True).write(
                {'asset_paused_by_group': False})
        else:
            raise UserError(_(
                "Asset %s cannot join a held-for-sale group from state "
                "'%s'; only a running or paused asset can be held for "
                "sale.", asset.display_name, asset.state))

    def _eh_resume_asset(self):
        """Resume an asset this group paused on classification (IFRS
        5.26), and only such an asset."""
        self.ensure_one()
        if self.asset_paused_by_group and self.asset_id.state == 'paused':
            self.asset_id.action_resume()
        self.with_context(eh_dg_internal=True).write(
            {'asset_paused_by_group': False})


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_disposal_group_id = fields.Many2one(
        'eh.disposal.group', string="Disposal Group", readonly=True,
        index=True, ondelete='restrict', copy=False)


class EhAssetImpairment(models.Model):
    _inherit = 'eh.asset.impairment'

    def action_cancel(self):
        # An impairment row attached to a disposal group's single journal
        # entry shares that entry with the other members; cancelling it
        # standalone would draft the whole group move. The group flow owns
        # the correction path (Remeasure posts a capped reversal).
        grouped = self.filtered(
            lambda rec: rec.move_id and rec.move_id.eh_disposal_group_id)
        if grouped:
            raise UserError(_(
                "This impairment was posted by disposal group %s and "
                "shares its journal entry with the other members; it "
                "cannot be cancelled standalone. Remeasure the group "
                "instead (IFRS 5.15).",
                ', '.join(grouped.move_id.eh_disposal_group_id
                          .mapped('display_name'))))
        return super().action_cancel()
