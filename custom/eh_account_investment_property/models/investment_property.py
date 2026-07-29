# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.investment.property: property held to earn rentals or for capital
appreciation under IAS 40.

Under the fair value model the property is remeasured to fair value and the
change is recognised in profit or loss (IAS 40.35); the carrying amount then
rolls forward to the new fair value.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero


class EhInvestmentProperty(models.Model):
    _name = 'eh.investment.property'
    _description = "Investment property (IAS 40)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'acquisition_date desc, id desc'
    _rec_name = 'name'

    # eh.workflow.guard: the state machine is enforced by the action methods
    # below (each carries the eh_workflow_action flag). A plain user cannot
    # RPC-write state to skip an action and its journal entry.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    description = fields.Char(help="Address or description of the property.")
    state = fields.Selection(
        [('draft', "Draft"), ('held', "Held"),
         ('disposed', "Disposed"), ('transferred', "Transferred"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    model_basis = fields.Selection(
        [('fair_value', "Fair value model"), ('cost', "Cost model")],
        default='fair_value', required=True, tracking=True)
    acquisition_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    initial_cost = fields.Monetary(
        currency_field='currency_id', tracking=True)
    carrying_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id', tracking=True)
    fair_value = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Fair value at the current reporting date.")
    remeasurement = fields.Monetary(
        compute='_compute_remeasurement', store=True,
        currency_field='currency_id')

    # --- Cost model (IAS 40.56): held at cost less accumulated depreciation ---
    useful_life_years = fields.Integer(
        string="Useful Life (years)", tracking=True,
        help="Straight-line depreciation period used under the cost model.")
    accumulated_depreciation = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id', tracking=True)
    depreciation_expense_account_id = fields.Many2one(
        'account.account', string="Depreciation Expense Account",
        tracking=True, domain="[('account_type', '=', 'expense')]")
    accumulated_depreciation_account_id = fields.Many2one(
        'account.account', string="Accumulated Depreciation Account",
        tracking=True,
        domain="[('account_type', 'in', "
               "['asset_non_current', 'asset_fixed', 'asset_current'])]")

    # --- IAS 40.57-62 transfers in and out of investment property ---
    transfer_target_account_id = fields.Many2one(
        'account.account', string="Transfer Target Account", tracking=True,
        help="PP&E or inventory account the property is reclassified into.")
    transfer_reason = fields.Char(string="Transfer Reason")
    transfer_date = fields.Date(
        string="Transfer Date", tracking=True, copy=False,
        help="Date of the change in use driving the transfer (IAS 40.57). "
             "Remeasurement and derecognition entries post on this date; "
             "left empty it defaults to today when the transfer runs.")
    transfer_fair_value = fields.Monetary(
        string="Fair Value at Transfer", currency_field='currency_id',
        tracking=True, copy=False,
        help="Fair value at the date of change in use. Under the fair "
             "value model the property is remeasured to this amount (gap "
             "to profit or loss, IAS 40.60-61) before derecognition and "
             "it becomes the deemed cost of the destination asset. Under "
             "the cost model a transfer never changes the carrying amount "
             "(IAS 40.59); an amount entered here is stored on the "
             "transfer audit trail for disclosure only. Leave at zero "
             "under the fair value model to transfer at the current "
             "carrying amount (already fair value, IAS 40.33).")
    revaluation_surplus_account_id = fields.Many2one(
        'account.account', string="Revaluation Surplus Account",
        tracking=True, domain="[('account_type', '=', 'equity')]",
        help="Equity (OCI) revaluation surplus account taking the uplift "
             "when an owner-occupied asset is transferred into investment "
             "property at a fair value above its carrying amount "
             "(IAS 40.61). The assets module takes its reserve account as "
             "a wizard parameter rather than storing one, so the account "
             "used for the transfer-in leg is configured here.")
    transfer_in_asset_id = fields.Integer(
        string="Source Fixed Asset ID", copy=False,
        help="Database id of the eh.asset (ERP Heritage assets module) "
             "being transferred into this property. This is a soft "
             "reference: the module does not depend on the assets module, "
             "so the link is resolved through the registry when the "
             "transfer-in action runs and the action refuses to run when "
             "the assets module is not installed.")
    transfer_in_asset_display = fields.Char(
        string="Source Fixed Asset", readonly=True, copy=False,
        help="Display name of the fixed asset this property was "
             "transferred in from (audit trail).")
    transfer_log_ids = fields.One2many(
        'eh.investment.property.transfer', 'property_id',
        string="Transfer Log", readonly=True, copy=False)

    # --- IAS 40.66-69 derecognition on disposal ---
    disposal_date = fields.Date(string="Disposal Date", tracking=True)
    disposal_proceeds = fields.Monetary(
        string="Disposal Proceeds", currency_field='currency_id',
        tracking=True,
        help="Net consideration received or receivable on disposal "
             "(IAS 40.69).")
    disposal_cash_account_id = fields.Many2one(
        'account.account', string="Proceeds / Cash Account", tracking=True,
        help="Cash or receivable account debited with the disposal "
             "proceeds.")
    disposal_gain_loss_account_id = fields.Many2one(
        'account.account', string="Disposal Gain / Loss Account",
        tracking=True,
        domain="[('account_type', 'in', ['income_other', 'expense'])]",
        help="Account taking the gain or loss on derecognition "
             "(IAS 40.69).")

    property_account_id = fields.Many2one(
        'account.account', string="Property Account", tracking=True)
    fv_gain_loss_account_id = fields.Many2one(
        'account.account', string="Fair Value Gain / Loss Account",
        tracking=True,
        domain="[('account_type', 'in', ['income_other', 'expense'])]")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]")

    move_ids = fields.One2many('account.move', 'eh_investment_property_id')
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    # A fair value model property is remeasured, never depreciated
    # (IAS 40.33-35), so it can never carry accumulated depreciation. The
    # write() hook below cancels any pending balance when the basis switches
    # to the fair value model; this constraint makes the invariant a hard
    # database rule rather than an honour-system convention.
    _sql_constraints = [
        ('check_fv_basis_no_accum_dep', "CHECK (model_basis != 'fair_value' "
        "OR COALESCE(accumulated_depreciation, 0) = 0)", 'A fair value model investment property cannot carry accumulated '
        'depreciation; it is remeasured, never depreciated (IAS 40.33-35).'),
    ]

    @api.depends('fair_value', 'carrying_amount')
    def _compute_remeasurement(self):
        for p in self:
            p.remeasurement = p.fair_value - p.carrying_amount

    def _compute_move_count(self):
        for p in self:
            p.move_count = len(p.move_ids)

    def _has_posted_move(self):
        """Return True once any journal entry has been posted for this record.

        Carrying amount and accumulated depreciation are ledger-controlled
        totals; once a move exists they must not be edited by hand, only
        rolled forward by a further posting (IAS 40).
        """
        self.ensure_one()
        return bool(self.move_ids.filtered(
            lambda m: m.state == 'posted'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.investment.property') or '/'
        return super().create(vals_list)

    # Ledger-controlled totals: only the posting actions below may roll them
    # forward. Once a move exists they are frozen against any other write so
    # the book value can never drift away from the posted ledger (IAS 40).
    # initial_cost, useful_life_years and model_basis are the measurement
    # inputs that fix the depreciable base and the valuation path; once a move
    # exists editing them would silently re-base every subsequent depreciation
    # charge (charge = initial_cost / useful_life_years) or switch the property
    # onto a valuation basis inconsistent with what has already been posted.
    _FROZEN_FIELDS = (
        'carrying_amount', 'accumulated_depreciation',
        'initial_cost', 'useful_life_years', 'model_basis',
    )

    def write(self, vals):
        # Depreciation halt on model switch (IAS 40.33-35): a property moved
        # onto the fair value model is remeasured, never depreciated, so any
        # pending accumulated depreciation captured while the record was on
        # the cost model is cancelled in the same write. The switch itself
        # is only reachable before a posted move exists (model_basis is
        # frozen after the first posting), at which point no depreciation
        # charge sits in the ledger and the reset is a pure state clean-up;
        # it also keeps the fair-value/no-accumulated-depreciation database
        # constraint satisfied by construction.
        if vals.get('model_basis') == 'fair_value' \
                and 'accumulated_depreciation' not in vals:
            vals = dict(vals, accumulated_depreciation=0.0)
        touched = [f for f in self._FROZEN_FIELDS if f in vals]
        if touched and not self.env.context.get('eh_ip_ledger_roll'):
            for p in self:
                if p._has_posted_move():
                    raise UserError(_(
                        "Carrying amount and accumulated depreciation are "
                        "controlled by the posted ledger for %s and cannot "
                        "be edited directly once entries exist.",
                        p.display_name))
        # The state of a property that carries a posted GL move is itself a
        # control point: resetting it back to draft (or otherwise out of the
        # held / disposed / transferred posted states) would silently lift the
        # ledger-controlled freeze above and orphan its entries. A raw ORM
        # state write without the sanctioned-transition context flag is
        # manager-gated so a plain user cannot un-freeze a GL-backed property.
        # The action methods that legitimately move state carry the flag (they
        # only move INTO a posted state, or cancel a record with no moves).
        if 'state' in vals \
                and not self.env.context.get('eh_ip_state_change'):
            for p in self:
                if p.state == vals['state']:
                    continue
                if p._has_posted_move() \
                        and vals['state'] not in self._POSTED_STATES:
                    if not self.env.user.has_group(
                            'eh_account_base.group_eh_manager'):
                        raise UserError(_(
                            "Only an EH Accounting Manager may change the "
                            "state of investment property %s once entries "
                            "have been posted for it.", p.display_name))
        return super().write(vals)

    # States in which the property carries a posted GL position: it has been
    # recognised (held) or derecognised (disposed / transferred). A move OUT of
    # these back to draft would strand the posted entries.
    _POSTED_STATES = ('held', 'disposed', 'transferred')

    def unlink(self):
        for p in self:
            if p._has_posted_move():
                raise UserError(_(
                    "Investment property %s cannot be deleted; it carries "
                    "posted journal entries that would be orphaned. Reverse "
                    "or cancel the entries first.", p.display_name))
        return super().unlink()

    def action_activate(self):
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can recognise investment "
                "property."))
        for p in self:
            if p.state != 'draft':
                raise UserError(_("Only a draft property can be recognised."))
            p.write({'state': 'held', 'carrying_amount': p.initial_cost})
        return True

    def action_remeasure(self):
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can remeasure investment "
                "property."))
        if self.state != 'held':
            raise UserError(_("Only a held property can be remeasured."))
        if self.model_basis != 'fair_value':
            raise UserError(_(
                "Remeasurement to fair value applies only under the fair "
                "value model."))
        if not self.journal_id or not self.property_account_id \
                or not self.fv_gain_loss_account_id:
            raise UserError(_(
                "Configure the journal, property account and fair value "
                "gain/loss account first."))
        currency = self.currency_id
        change = currency.round(self.remeasurement)
        if currency.is_zero(change):
            raise UserError(_(
                "Fair value equals the carrying amount; nothing to post."))
        if change > 0:
            lines = [
                (self.property_account_id, change, 0.0,
                 _("Fair value increase %s", self.name)),
                (self.fv_gain_loss_account_id, 0.0, change,
                 _("Fair value gain %s", self.name)),
            ]
        else:
            amount = -change
            lines = [
                (self.fv_gain_loss_account_id, amount, 0.0,
                 _("Fair value loss %s", self.name)),
                (self.property_account_id, 0.0, amount,
                 _("Fair value decrease %s", self.name)),
            ]
        self._post_move(lines)
        self.with_context(eh_ip_ledger_roll=True).write(
            {'carrying_amount': self.fair_value})
        return True

    def action_depreciate(self):
        """Post one straight-line depreciation charge under the cost model.

        Dr depreciation expense / Cr accumulated depreciation by
        round(initial_cost / useful_life_years). Increments accumulated
        depreciation and reduces the carrying amount (IAS 40.56).
        """
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can depreciate investment "
                "property."))
        if self.model_basis != 'cost':
            raise UserError(_(
                "Depreciation applies only under the cost model. Fair value "
                "model properties are remeasured, not depreciated."))
        if self.state != 'held':
            raise UserError(_("Only a held property can be depreciated."))
        if self.useful_life_years <= 0:
            raise UserError(_(
                "Set a positive useful life in years before depreciating."))
        if not self.journal_id or not self.depreciation_expense_account_id \
                or not self.accumulated_depreciation_account_id:
            raise UserError(_(
                "Configure the journal, depreciation expense account and "
                "accumulated depreciation account first."))
        currency = self.currency_id
        charge = currency.round(self.initial_cost / self.useful_life_years)
        if currency.is_zero(charge):
            raise UserError(_(
                "The period depreciation charge rounds to zero; nothing to "
                "post."))
        # Cap the charge at the remaining depreciable base so accumulated
        # depreciation can never exceed cost and the carrying amount can
        # never go negative past the end of the useful life (IAS 40.56).
        depreciable_base = currency.round(self.initial_cost)
        remaining = currency.round(
            depreciable_base - self.accumulated_depreciation)
        if float_compare(remaining, 0.0, precision_rounding=currency.rounding) \
                <= 0:
            raise UserError(_(
                "The property is already fully depreciated; the depreciable "
                "base is exhausted."))
        if float_compare(charge, remaining,
                         precision_rounding=currency.rounding) > 0:
            charge = remaining
        lines = [
            (self.depreciation_expense_account_id, charge, 0.0,
             _("Depreciation %s", self.name)),
            (self.accumulated_depreciation_account_id, 0.0, charge,
             _("Accumulated depreciation %s", self.name)),
        ]
        self._post_move(lines)
        self.with_context(eh_ip_ledger_roll=True).write({
            'accumulated_depreciation': self.accumulated_depreciation + charge,
            'carrying_amount': self.carrying_amount - charge,
        })
        return True

    def action_transfer_out(self):
        """Reclassify the property out of investment property (IAS 40.57-62).

        The measurement on the way out depends on the valuation basis:

        * Fair value model (IAS 40.60-61): the property is first remeasured
          to transfer_fair_value, the fair value at the date of change in
          use, with the gap recognised in profit or loss exactly like any
          other fair value change of the period (IAS 40.35); the property
          then leaves at that fair value, which becomes the deemed cost of
          the destination asset (IAS 40.60):

              remeasure (only when the gap is non-zero):
                Dr property account / Cr FV gain      (uplift), or
                Dr FV loss / Cr property account      (deficit)
              derecognise:
                Dr transfer target       fair value at transfer date
                  Cr property account    fair value at transfer date

          When transfer_fair_value is left at zero the current carrying
          amount (already fair value per IAS 40.33) is used and no
          remeasurement arises, preserving the previous behaviour.

        * Cost model (IAS 40.59): transfers between investment property
          carried under the cost model and owner-occupied property do not
          change the carrying amount of the property, so no remeasurement
          is posted. The net book value moves at gross cost with the
          accumulated depreciation reversed out rather than stranded:

              Dr transfer target             carrying amount
              Dr accumulated depreciation    accumulated_depreciation
                Cr property account          initial_cost

          A transfer_fair_value entered under the cost model is stored on
          the transfer audit trail for disclosure only and never posted.

        Every transfer writes an immutable eh.investment.property.transfer
        row (date, direction, basis, carrying amount before, fair value,
        delta posted with its routing, move links) so the transfer is
        reconstructable end to end.
        """
        self.ensure_one()
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can transfer investment "
                "property."))
        if self.state != 'held':
            raise UserError(_("Only a held property can be transferred out."))
        if not self.journal_id or not self.property_account_id \
                or not self.transfer_target_account_id:
            raise UserError(_(
                "Configure the journal, property account and transfer target "
                "account first."))
        currency = self.currency_id
        transfer_date = self.transfer_date or fields.Date.context_today(self)
        carrying_before = currency.round(self.carrying_amount)
        remeasure_move = False
        delta = 0.0
        routing = 'none'
        if self.model_basis == 'fair_value':
            # IAS 40.60-61: leave at the fair value at the date of change in
            # use; the gap to the last carrying amount is a fair value
            # change of the period and hits profit or loss before
            # derecognition. Zero means "not supplied": the carrying amount
            # is already fair value (IAS 40.33) and moves unchanged.
            fv = currency.round(self.transfer_fair_value)
            if float_compare(fv, 0.0,
                             precision_rounding=currency.rounding) < 0:
                raise UserError(_(
                    "The fair value at the transfer date cannot be "
                    "negative."))
            if currency.is_zero(fv):
                fv = carrying_before
            delta = currency.round(fv - carrying_before)
            if not currency.is_zero(delta):
                if not self.fv_gain_loss_account_id:
                    raise UserError(_(
                        "Configure the fair value gain/loss account: the "
                        "transfer fair value differs from the carrying "
                        "amount, so the gap must be recognised in profit "
                        "or loss before derecognition (IAS 40.60-61)."))
                if delta > 0:
                    lines = [
                        (self.property_account_id, delta, 0.0,
                         _("Fair value at transfer date %s", self.name)),
                        (self.fv_gain_loss_account_id, 0.0, delta,
                         _("Fair value gain on transfer %s", self.name)),
                    ]
                else:
                    lines = [
                        (self.fv_gain_loss_account_id, -delta, 0.0,
                         _("Fair value loss on transfer %s", self.name)),
                        (self.property_account_id, 0.0, -delta,
                         _("Fair value at transfer date %s", self.name)),
                    ]
                remeasure_move = self._post_move(lines, date=transfer_date)
                self.with_context(eh_ip_ledger_roll=True).write(
                    {'carrying_amount': fv, 'fair_value': fv})
                routing = 'pl'
            carrying = fv
            accumulated = 0.0
            gross = fv
            logged_fv = fv
        else:
            # IAS 40.59: no remeasurement; the fair value, when supplied, is
            # disclosed on the audit trail only.
            carrying = carrying_before
            accumulated = currency.round(self.accumulated_depreciation)
            gross = currency.round(self.initial_cost)
            logged_fv = currency.round(self.transfer_fair_value)
        if currency.is_zero(carrying):
            raise UserError(_(
                "The carrying amount is zero; nothing to reclassify."))
        lines = [(
            self.transfer_target_account_id, carrying, 0.0,
            _("Transfer out of investment property %s", self.name))]
        if not float_is_zero(accumulated,
                             precision_rounding=currency.rounding):
            lines.append((
                self.accumulated_depreciation_account_id, accumulated, 0.0,
                _("Derecognise accumulated depreciation %s", self.name)))
        lines.append((
            self.property_account_id, 0.0, gross,
            _("Derecognise investment property %s", self.name)))
        move = self._post_move(lines, date=transfer_date)
        self.transfer_date = transfer_date
        self.state = 'transferred'
        self._log_transfer(
            direction='out', date=transfer_date, basis=self.model_basis,
            carrying_before=carrying_before, fair_value=logged_fv,
            delta=delta, routing=routing, move=move,
            remeasure_move=remeasure_move,
            note=self.transfer_reason or False)
        return True

    def action_transfer_in(self):
        """Bring an owner-occupied fixed asset into investment property at
        fair value on the date of change in use (IAS 40.57(d)/.61-62).

        Applies to a DRAFT property on the fair value model, linked to an
        eh.asset from the ERP Heritage assets module through
        transfer_in_asset_id. The asset is revalued to transfer_fair_value
        and derecognised into this property in one balanced entry; the
        property opens at that fair value as its deemed cost:

            Dr investment property        fair value at transfer date
            Dr accumulated depreciation   gross - carrying (when any)
            Dr revaluation surplus        min(deficit, asset surplus)
            Dr fair value loss (P&L)      remaining deficit
              Cr asset account            gross cost (incl. revaluations)
              Cr revaluation surplus      uplift (OCI, IAS 40.61)

        IAS 40.61-62 treat the transfer-date revaluation exactly like an
        IAS 16 revaluation: an uplift is credited to the equity revaluation
        surplus (OCI); a deficit first consumes any revaluation surplus
        carried by that asset and only the excess is charged to profit or
        loss. An uplift on an asset carrying an unreversed impairment is
        refused: recovering an impairment must be routed through profit or
        loss as an impairment reversal in the assets module first, capped
        per IAS 36.117 (IAS 40.62(a)); this mirrors the assets module's own
        revaluation wizard gate.

        The source asset is paused (its schedule halts and the depreciation
        cron skips it) and keeps its full history; the stored link plus the
        transfer log row make the transfer reconstructable.

        Cost-model intake is deliberately not handled here: under IAS 40.59
        a transfer between owner-occupied property and investment property
        carried under the cost model does not change the carrying amount,
        so a cost-basis property is recognised at that carrying amount
        through the normal 'Recognise at Cost' flow with no remeasurement;
        this action refuses the cost basis and says so.

        Soft dependency: the module does not depend on the assets module,
        so the asset link is an integer id resolved through the registry at
        action time; when eh.asset is not installed the action refuses to
        run with an explicit message.
        """
        self.ensure_one()
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can transfer assets into "
                "investment property."))
        if self.state != 'draft':
            raise UserError(_(
                "Transfer in opens the property's ledger position, so it "
                "applies to a draft property only."))
        if self.model_basis != 'fair_value':
            raise UserError(_(
                "Transfer in at fair value applies to the fair value model "
                "only. Under the cost model IAS 40.59 carries the asset "
                "over unchanged: recognise the property at the asset's "
                "carrying amount with 'Recognise at Cost' instead; no "
                "remeasurement arises."))
        if 'eh.asset' not in self.env:
            raise UserError(_(
                "The ERP Heritage assets module is not installed; there is "
                "no fixed asset register to transfer from."))
        if not self.transfer_in_asset_id:
            raise UserError(_(
                "Set the source fixed asset id to transfer in."))
        asset = self.env['eh.asset'].browse(
            self.transfer_in_asset_id).exists()
        if not asset:
            raise UserError(_(
                "Fixed asset %s does not exist.", self.transfer_in_asset_id))
        if asset.company_id != self.company_id:
            raise UserError(_(
                "Fixed asset %s belongs to another company.",
                asset.display_name))
        if asset.state not in ('running', 'paused'):
            raise UserError(_(
                "Only a running or paused asset can be transferred into "
                "investment property; %(asset)s is %(state)s.",
                asset=asset.display_name, state=asset.state))
        if not self.journal_id or not self.property_account_id:
            raise UserError(_(
                "Configure the journal and property account first."))
        if not asset.asset_account_id:
            raise UserError(_(
                "Fixed asset %s is missing its asset account.",
                asset.display_name))
        currency = self.currency_id
        fv = currency.round(self.transfer_fair_value)
        if float_compare(fv, 0.0, precision_rounding=currency.rounding) <= 0:
            raise UserError(_(
                "Set the fair value at the transfer date: the property "
                "opens at fair value as its deemed cost (IAS 40.61)."))
        # Ledger decomposition of the asset (from the assets module design):
        # the asset account carries acquisition cost plus posted revaluation
        # adjustments; depreciation and impairments sit on the accumulated
        # depreciation contra. carrying = gross - contra by construction.
        carrying = currency.round(asset.net_book_value)
        gross = currency.round(
            asset.acquisition_cost + asset.revaluation_adjustment)
        contra = currency.round(gross - carrying)
        if float_compare(contra, 0.0,
                         precision_rounding=currency.rounding) < 0:
            raise UserError(_(
                "Fixed asset %s carries a negative depreciation/impairment "
                "contra balance; correct the asset before transferring.",
                asset.display_name))
        if not float_is_zero(contra, precision_rounding=currency.rounding) \
                and not asset.accumulated_depreciation_account_id:
            raise UserError(_(
                "Fixed asset %s is missing its accumulated depreciation "
                "account.", asset.display_name))
        delta = currency.round(fv - carrying)
        dr_to_surplus = 0.0
        dr_to_pl = 0.0
        routing = 'none'
        if float_compare(delta, 0.0,
                         precision_rounding=currency.rounding) > 0:
            if asset.accumulated_impairment > 0:
                raise UserError(_(
                    "%(asset)s carries an unreversed impairment of "
                    "%(imp).2f. Recovering it must be recognised as an "
                    "impairment reversal through profit or loss in the "
                    "assets module (capped per IAS 36.117) before any "
                    "remaining uplift can be credited to the revaluation "
                    "surplus on transfer (IAS 40.62(a)).",
                    asset=asset.display_name,
                    imp=asset.accumulated_impairment))
            if not self.revaluation_surplus_account_id:
                raise UserError(_(
                    "Configure the revaluation surplus account: the fair "
                    "value uplift on transfer is credited to equity (OCI) "
                    "per IAS 40.61."))
            routing = 'oci'
        elif float_compare(delta, 0.0,
                           precision_rounding=currency.rounding) < 0:
            # IAS 40.62(b): the deficit first reverses the revaluation
            # surplus carried by that asset; only the excess hits P&L.
            dr_to_surplus = currency.round(
                min(-delta, asset.revaluation_surplus or 0.0))
            dr_to_pl = currency.round(-delta - dr_to_surplus)
            if dr_to_surplus > 0 and not self.revaluation_surplus_account_id:
                raise UserError(_(
                    "Configure the revaluation surplus account: the fair "
                    "value deficit first reverses the asset's revaluation "
                    "surplus (IAS 40.62(b))."))
            if dr_to_pl > 0 and not self.fv_gain_loss_account_id:
                raise UserError(_(
                    "Configure the fair value gain/loss account: the fair "
                    "value deficit in excess of the asset's revaluation "
                    "surplus is charged to profit or loss (IAS 40.62(b))."))
            routing = 'pl' if dr_to_pl > 0 else 'oci'
        transfer_date = self.transfer_date or fields.Date.context_today(self)
        lines = [(
            self.property_account_id, fv, 0.0,
            _("Transfer into investment property %s", self.name))]
        if not float_is_zero(contra, precision_rounding=currency.rounding):
            lines.append((
                asset.accumulated_depreciation_account_id, contra, 0.0,
                _("Derecognise accumulated depreciation and impairment %s",
                  asset.display_name)))
        if dr_to_surplus > 0:
            lines.append((
                self.revaluation_surplus_account_id, dr_to_surplus, 0.0,
                _("Revaluation surplus reversal %s", asset.display_name)))
        if dr_to_pl > 0:
            lines.append((
                self.fv_gain_loss_account_id, dr_to_pl, 0.0,
                _("Fair value loss on transfer %s", self.name)))
        lines.append((
            asset.asset_account_id, 0.0, gross,
            _("Derecognise owner-occupied asset %s", asset.display_name)))
        if delta > 0:
            lines.append((
                self.revaluation_surplus_account_id, 0.0, delta,
                _("Revaluation surplus on transfer %s", self.name)))
        move = self._post_move(lines, date=transfer_date)
        if asset.state == 'running':
            asset.action_pause()
        if dr_to_surplus:
            asset.write({'revaluation_surplus': currency.round(
                (asset.revaluation_surplus or 0.0) - dr_to_surplus)})
        asset.message_post(body=_(
            "Transferred into investment property %(prop)s at fair value "
            "%(fv).2f on %(date)s (IAS 40.57(d)); the asset is paused and "
            "its ledger position was derecognised by entry %(move)s.",
            prop=self.display_name, fv=fv, date=transfer_date,
            move=move.display_name))
        self.with_context(
            eh_ip_ledger_roll=True, eh_ip_state_change=True,
        ).write({
            'state': 'held',
            'initial_cost': fv,
            'carrying_amount': fv,
            'fair_value': fv,
            'transfer_date': transfer_date,
            'transfer_in_asset_display': asset.display_name,
        })
        self._log_transfer(
            direction='in', date=transfer_date, basis='fair_value',
            carrying_before=carrying, fair_value=fv, delta=delta,
            routing=routing, move=move,
            source_document=asset.display_name)
        return True

    def _log_transfer(self, direction, date, basis, carrying_before,
                      fair_value, delta, routing, move,
                      remeasure_move=False, source_document=False,
                      note=False):
        """Write one immutable audit-trail row per transfer (IAS 40.57-62).

        The row stores everything needed to reconstruct the transfer: the
        measurement basis at the transfer date, the carrying amount
        immediately before, the fair value at the transfer date (posted
        under the fair value model, disclosed-only under the cost model),
        the remeasurement delta actually posted with its P&L/OCI routing,
        and the journal entries.
        """
        self.ensure_one()
        return self.env['eh.investment.property.transfer'].create({
            'property_id': self.id,
            'company_id': self.company_id.id,
            'currency_id': self.currency_id.id,
            'date': date,
            'direction': direction,
            'basis': basis,
            'carrying_before': carrying_before,
            'fair_value': fair_value,
            'delta_posted': delta,
            'delta_routing': routing,
            'move_id': move.id if move else False,
            'remeasure_move_id':
                remeasure_move.id if remeasure_move else False,
            'source_document': source_document or False,
            'note': note or False,
        })

    def action_dispose(self):
        """Derecognise the property on disposal (IAS 40.66-69).

        Post a balanced entry that removes the carrying amount from the
        balance sheet, recognises the proceeds and books the balancing gain
        or loss to profit or loss:

          Dr cash / receivable            disposal_proceeds
          Dr accumulated depreciation     accumulated_depreciation (cost model)
          Dr disposal loss  OR  Cr disposal gain   (balancing figure)
            Cr property account           initial_cost (cost model)
                                    or     carrying_amount (fair value model)

        The gain or loss is proceeds less carrying amount (IAS 40.69). The
        entry balances by construction: the gain/loss line is the residual
        that makes total debits equal total credits.
        """
        self.ensure_one()
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can dispose of investment "
                "property."))
        if self.state != 'held':
            raise UserError(_("Only a held property can be disposed."))
        if not self.journal_id or not self.property_account_id \
                or not self.disposal_cash_account_id \
                or not self.disposal_gain_loss_account_id:
            raise UserError(_(
                "Configure the journal, property account, proceeds/cash "
                "account and disposal gain/loss account first."))
        currency = self.currency_id
        proceeds = currency.round(self.disposal_proceeds)
        carrying = currency.round(self.carrying_amount)
        accumulated = currency.round(self.accumulated_depreciation)
        if self.model_basis == 'cost':
            gross = currency.round(self.initial_cost)
        else:
            gross = carrying
            accumulated = 0.0
        # Gain (positive) or loss (negative) = proceeds - carrying amount.
        gain = currency.round(proceeds - carrying)

        lines = []
        if not float_is_zero(proceeds, precision_rounding=currency.rounding):
            lines.append((
                self.disposal_cash_account_id, proceeds, 0.0,
                _("Disposal proceeds %s", self.name)))
        if not float_is_zero(accumulated,
                             precision_rounding=currency.rounding):
            lines.append((
                self.accumulated_depreciation_account_id, accumulated, 0.0,
                _("Derecognise accumulated depreciation %s", self.name)))
        lines.append((
            self.property_account_id, 0.0, gross,
            _("Derecognise investment property %s", self.name)))
        if float_compare(gain, 0.0,
                         precision_rounding=currency.rounding) > 0:
            lines.append((
                self.disposal_gain_loss_account_id, 0.0, gain,
                _("Gain on disposal %s", self.name)))
        elif float_compare(gain, 0.0,
                           precision_rounding=currency.rounding) < 0:
            lines.append((
                self.disposal_gain_loss_account_id, -gain, 0.0,
                _("Loss on disposal %s", self.name)))
        self._post_move(lines)
        self.with_context(eh_ip_ledger_roll=True).write({
            'state': 'disposed',
            'disposal_date': self.disposal_date
            or fields.Date.context_today(self),
            'carrying_amount': 0.0,
        })
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        for p in self:
            if p.move_ids:
                raise UserError(_(
                    "Reverse the posted entries before cancelling %s.",
                    p.display_name))
            p.state = 'cancelled'

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_investment_property_id', '=', self.id)],
        }

    def _post_move(self, legs, date=None):
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': date or fields.Date.context_today(self),
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_investment_property_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class EhInvestmentPropertyTransfer(models.Model):
    """Immutable audit trail of IAS 40.57-62 transfers.

    One row per transfer into or out of investment property, written by the
    transfer actions and never edited: it captures the measurement basis at
    the transfer date, the carrying amount immediately before, the fair
    value at the transfer date, the remeasurement delta actually posted with
    its P&L/OCI routing, and the journal entries, so any transfer can be
    reconstructed end to end from the log alone.
    """

    _name = 'eh.investment.property.transfer'
    _description = "Investment Property Transfer Log (IAS 40.57-62)"
    _order = 'date desc, id desc'

    property_id = fields.Many2one(
        'eh.investment.property', string="Investment Property",
        required=True, ondelete='cascade', index=True, readonly=True)
    company_id = fields.Many2one(
        'res.company', required=True, index=True, readonly=True)
    currency_id = fields.Many2one('res.currency', required=True,
                                  readonly=True)
    date = fields.Date(
        required=True, readonly=True,
        help="Date of the change in use (IAS 40.57).")
    direction = fields.Selection(
        [('in', "Into investment property"),
         ('out', "Out of investment property")],
        required=True, readonly=True)
    basis = fields.Selection(
        [('fair_value', "Fair value model"), ('cost', "Cost model")],
        string="Basis at Transfer", required=True, readonly=True)
    carrying_before = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="Carrying amount immediately before the transfer.")
    fair_value = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="Fair value at the transfer date. Posted under the fair value "
             "model; stored for disclosure only under the cost model, where "
             "IAS 40.59 keeps the carrying amount unchanged.")
    delta_posted = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="Signed remeasurement actually posted on transfer (fair value "
             "less carrying amount before). Zero when no remeasurement "
             "arises.")
    delta_routing = fields.Selection(
        [('none', "No remeasurement"), ('pl', "Profit or loss"),
         ('oci', "OCI revaluation surplus")],
        required=True, default='none', readonly=True,
        help="Where the remeasurement delta was recognised: profit or loss "
             "(IAS 40.35/.60-62) or the equity revaluation surplus "
             "(IAS 40.61).")
    remeasure_move_id = fields.Many2one(
        'account.move', string="Remeasurement Entry", readonly=True,
        ondelete='set null', copy=False)
    move_id = fields.Many2one(
        'account.move', string="Transfer Entry", readonly=True,
        ondelete='set null', copy=False)
    source_document = fields.Char(
        readonly=True,
        help="Source of an inbound transfer, e.g. the fixed asset "
             "derecognised into the property.")
    note = fields.Char(readonly=True)

    def write(self, vals):
        raise UserError(_(
            "Investment property transfer log rows are an immutable audit "
            "trail; they are written once by the transfer actions and never "
            "edited."))

    def unlink(self):
        raise UserError(_(
            "Investment property transfer log rows are an immutable audit "
            "trail and cannot be deleted."))


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_investment_property_id = fields.Many2one(
        'eh.investment.property', string="Investment Property", readonly=True,
        index=True, ondelete='restrict', copy=False)
