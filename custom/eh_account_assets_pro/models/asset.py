# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Fixed asset record.

State machine:

  draft -> running -> paused -> running -> ...
                              \\-> fully_depreciated
                              \\-> disposed

draft: schedule not yet generated and approved.
running: schedule exists, monthly cron auto posts due lines.
paused: schedule exists, cron skips this asset.
fully_depreciated: net book value reached salvage; cron skips.
disposed: terminated by disposal wizard; cron skips.
"""

import calendar
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhAsset(models.Model):
    _name = 'eh.asset'
    _description = "Fixed Asset"
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.cron.batch.mixin',
        'eh.workflow.guard',
    ]
    _order = 'in_service_date desc, id desc'

    # Lifecycle state may only change through this model's own actions
    # (action_activate / action_capitalise / action_pause / action_resume /
    # action_set_to_draft / _maybe_mark_fully_depreciated) and the disposal
    # wizard, never a direct RPC write that would skip the posting checks.
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    code = fields.Char(
        copy=False, tracking=True,
        help="Internal asset tag, e.g. ITHW-2026-0001.",
    )
    category_id = fields.Many2one(
        'eh.asset.category', string="Category", required=True,
        ondelete='restrict', tracking=True,
    )
    deferred_type = fields.Selection(
        [
            ('asset', "Fixed asset (depreciation)"),
            ('deferred_revenue', "Deferred revenue (recognition over time)"),
            ('deferred_expense', "Deferred expense (recognition over time)"),
        ],
        default='asset', required=True, tracking=True,
        help=(
            "Depreciation engine flavour. 'asset' is a regular fixed "
            "asset whose net book value declines as accumulated "
            "depreciation rises. 'deferred_revenue' recognises a "
            "pre-paid revenue balance into income over the schedule. "
            "'deferred_expense' recognises a pre-paid expense into the "
            "P&L over the schedule. The same schedule generator and "
            "lifecycle apply; only the journal entry posted on each "
            "line differs."
        ),
    )
    state = fields.Selection([
        ('draft', "Draft"),
        ('running', "Running"),
        ('paused', "Paused"),
        ('fully_depreciated', "Fully Depreciated"),
        ('disposed', "Disposed"),
    ], default='draft', required=True, tracking=True)

    # ---- acquisition ----
    partner_id = fields.Many2one('res.partner', string="Vendor", tracking=True)
    invoice_id = fields.Many2one(
        'account.move', string="Origin Bill",
        domain="[('move_type', '=', 'in_invoice')]",
    )
    acquisition_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
    )
    in_service_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Date the asset entered service. Drives the schedule start.",
    )
    acquisition_cost = fields.Monetary(required=True, tracking=True)
    salvage_value = fields.Monetary(default=0.0, tracking=True)

    # ---- depreciation parameters ----
    method = fields.Selection([
        ('straight_line', "Straight Line"),
        ('reducing_balance', "Reducing Balance"),
        ('units_of_production', "Units of Production (Preview)"),
        ('prime_cost', "Prime Cost (AU tax)"),
        ('diminishing_value', "Diminishing Value (AU tax)"),
        ('manual', "Manual"),
    ], required=True, default='straight_line', tracking=True,
        help=(
            "Primary depreciation method posted to the GL. Straight "
            "Line and Reducing Balance are the standard accounting "
            "methods. Prime Cost and Diminishing Value are AU tax-"
            "compliant variants (factor 2.0 by default for DV per the "
            "post-2006 AU tax ruling). Use additional books for "
            "parallel methods (statutory + tax + IFRS)."
        ),
    )

    is_instant_write_off = fields.Boolean(
        string="Instant write-off",
        default=False,
        tracking=True,
        help=(
            "When set, the schedule writes off the entire depreciable "
            "amount in the first period regardless of useful life. "
            "Used by the AU instant-asset-write-off (currently AUD "
            "20,000 threshold for small business; check ATO for the "
            "current cap and end date) and similar fast-deduction "
            "regimes. Salvage value is honoured."
        ),
    )

    # ---- asset under construction ----
    is_under_construction = fields.Boolean(
        string="Under construction",
        default=False, tracking=True,
        help=(
            "When set, the asset is treated as work in progress. No "
            "depreciation schedule is generated and no JE posts. "
            "Acquisition cost accumulates on the configured AUC "
            "account until action_capitalise transfers the balance "
            "to the asset account, sets the in-service date, and "
            "starts depreciation. Use for projects that capitalise "
            "over multiple periods (e.g. a building under "
            "construction, software in development)."
        ),
    )
    auc_account_id = fields.Many2one(
        'account.account',
        string="AUC Holding Account",
        help=(
            "Balance-sheet account that carries the asset's cost "
            "while it is under construction. On capitalisation, the "
            "JE debits asset_account_id and credits this account."
        ),
    )
    capitalised_at = fields.Datetime(
        readonly=True, copy=False, tracking=True,
        help="Timestamp when the AUC was capitalised.",
    )
    capitalised_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )
    useful_life_months = fields.Integer(
        string="Useful Life (months)", required=True, tracking=True,
        default=lambda self: (
            self.env.company.eh_asset_default_useful_life_months or 60
        ),
    )
    declining_factor = fields.Float(default=2.0, tracking=True)
    prorate_first_period = fields.Boolean(default=True, tracking=True)
    prorata_mode = fields.Selection(
        [
            ('none', "Full first period"),
            ('daily', "By days in service"),
            ('half', "Half first period (mid-period convention)"),
        ],
        tracking=True,
        help="Overrides the day-based first-period proration when set. "
             "'none' charges a full first period; 'half' charges half (the "
             "mid-period / half-year convention used by some tax regimes); "
             "'daily' prorates by days in service. When blank, the Prorate "
             "First Period switch applies.",
    )

    # units of production parameters
    total_units = fields.Float(
        help="Total units expected over the asset life "
             "(units of production method).",
    )
    units_used = fields.Float(
        help="Cumulative units consumed. Drives next depreciation under "
             "units of production method.",
    )

    # ---- IAS 38 intangible assets ----
    asset_class = fields.Selection(
        [
            ('tangible', "Tangible (IAS 16)"),
            ('intangible', "Intangible (IAS 38)"),
        ],
        default='tangible', required=True, tracking=True,
        help=(
            "Measurement standard the asset falls under. Tangible "
            "assets (property, plant and equipment) follow IAS 16; "
            "intangible assets (software, licences, brands, goodwill, "
            "capitalised development) follow IAS 38, which adds the "
            "indefinite-life regime (no amortisation, mandatory annual "
            "impairment testing) and the development-cost "
            "capitalisation gate (IAS 38.57)."
        ),
    )
    is_indefinite_life = fields.Boolean(
        string="Indefinite useful life",
        default=False, tracking=True,
        help=(
            "IAS 38.107-108: an intangible asset with an indefinite "
            "useful life is NOT amortised. Instead IAS 36.10 requires "
            "an impairment test annually, and whenever there is an "
            "indication of impairment. Setting this flag blocks "
            "schedule generation and amortisation posting, and places "
            "the asset in the annual impairment-test population "
            "enforced by the IAS 36 annual-test cron."
        ),
    )
    dev_cost_capitalisation = fields.Boolean(
        string="Capitalised development cost",
        default=False, tracking=True,
        help=(
            "Marks an intangible asset arising from development. IAS "
            "38.57 permits capitalisation only when ALL six criteria "
            "are demonstrated; until every checklist item below is "
            "ticked this asset cannot leave draft. If any criterion "
            "cannot be demonstrated, IAS 38 requires the expenditure "
            "to be recognised as an EXPENSE when incurred (research "
            "and non-qualifying development are never capitalised, "
            "IAS 38.54)."
        ),
    )
    dev_technical_feasibility = fields.Boolean(
        string="Technical feasibility demonstrated",
        help=(
            "IAS 38.57(a): the technical feasibility of completing the "
            "intangible asset so that it will be available for use or "
            "sale."
        ),
    )
    dev_intention_complete = fields.Boolean(
        string="Intention to complete",
        help=(
            "IAS 38.57(b): the intention to complete the intangible "
            "asset and use or sell it."
        ),
    )
    dev_ability_use_sell = fields.Boolean(
        string="Ability to use or sell",
        help="IAS 38.57(c): the ability to use or sell the intangible asset.",
    )
    dev_probable_benefits = fields.Boolean(
        string="Probable future economic benefits",
        help=(
            "IAS 38.57(d): how the intangible asset will generate "
            "probable future economic benefits (existence of a market "
            "or, if for internal use, its usefulness)."
        ),
    )
    dev_resources_available = fields.Boolean(
        string="Adequate resources available",
        help=(
            "IAS 38.57(e): the availability of adequate technical, "
            "financial and other resources to complete the development "
            "and to use or sell the intangible asset."
        ),
    )
    dev_reliable_measurement = fields.Boolean(
        string="Expenditure reliably measurable",
        help=(
            "IAS 38.57(f): the ability to measure reliably the "
            "expenditure attributable to the intangible asset during "
            "its development."
        ),
    )

    # ---- IAS 36 annual-test governance ----
    annual_test_overdue = fields.Boolean(
        string="Annual test overdue",
        default=False, copy=False, tracking=True,
        help=(
            "Set by the IAS 36 annual-test cron when this goodwill or "
            "indefinite-life intangible asset has no impairment test "
            "evidence (a CGU test run, or a posted impairment event) "
            "dated inside the current fiscal year once the company's "
            "annual test month has been reached. Cleared automatically "
            "when a test posts. Surfaces the IAS 36.10 exception list "
            "in the asset list and filters."
        ),
    )
    recoverable_amount_latest = fields.Monetary(
        string="Latest recoverable amount",
        readonly=True, copy=False, tracking=True,
        help=(
            "Most recent recoverable-amount measurement linked to this "
            "asset: stamped by a CGU impairment test (the member's "
            "post-test carrying amount when the unit was written down "
            "to its recoverable amount) or by a hand-keyed impairment "
            "or reversal that states its recoverable amount. IAS 36 "
            "does not permit a revaluation uplift to carry the asset "
            "above its recoverable amount, so the revaluation wizard "
            "caps uplifts against this measurement."
        ),
    )
    recoverable_amount_date = fields.Date(
        string="Recoverable amount date",
        readonly=True, copy=False, tracking=True,
        help="Measurement date of the latest recoverable amount.",
    )

    # ---- accounts and journal ----
    asset_account_id = fields.Many2one('account.account', string="Asset Account")
    depreciation_account_id = fields.Many2one(
        'account.account', string="Depreciation Expense Account",
    )
    accumulated_depreciation_account_id = fields.Many2one(
        'account.account', string="Accumulated Depreciation Account",
    )
    disposal_gain_account_id = fields.Many2one(
        'account.account', string="Disposal Gain Account",
    )
    disposal_loss_account_id = fields.Many2one(
        'account.account', string="Disposal Loss Account",
    )
    journal_id = fields.Many2one('account.journal', string="Depreciation Journal")

    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )

    # ---- schedule and totals ----
    depreciation_line_ids = fields.One2many(
        'eh.asset.depreciation.line', 'asset_id', copy=False,
    )
    book_ids = fields.One2many(
        'eh.asset.book', 'asset_id', copy=True,
        help=(
            "Parallel depreciation books on this asset (tax, IFRS, "
            "management). Each book has independent method, useful "
            "life, salvage, and schedule. By default they are "
            "reporting-only and do not post to the GL; tick "
            "posts_to_gl on a book to also post."
        ),
    )
    book_count = fields.Integer(
        compute='_compute_book_count', store=False,
        help="Number of additional depreciation books configured.",
    )

    # ---- IAS 16 component accounting ----
    parent_asset_id = fields.Many2one(
        'eh.asset', string="Parent Asset",
        ondelete='restrict',
        index=True,
        help=(
            "Parent asset this record is a component of. IAS 16 "
            "requires entities to depreciate significant components "
            "of an asset separately when their useful life or "
            "depreciation pattern differs from the parent (e.g. an "
            "aircraft engine vs the airframe, an HVAC system vs the "
            "building). The parent rolls up component NBV in display "
            "but each component carries its own schedule and posts "
            "its own depreciation."
        ),
    )
    component_ids = fields.One2many(
        'eh.asset', 'parent_asset_id',
        string="Components",
        help="Child components of this asset. Empty for leaf assets.",
    )
    component_count = fields.Integer(
        compute='_compute_component_totals', store=False,
        help="Number of direct child components.",
    )
    rolled_up_cost = fields.Monetary(
        compute='_compute_component_totals', store=False,
        currency_field='currency_id',
        help=(
            "This asset's acquisition_cost plus the sum of every "
            "component's acquisition_cost. Equals acquisition_cost "
            "for assets with no components."
        ),
    )
    rolled_up_nbv = fields.Monetary(
        compute='_compute_component_totals', store=False,
        currency_field='currency_id',
        help=(
            "This asset's net_book_value plus the sum of every "
            "component's net_book_value. Equals net_book_value "
            "for assets with no components."
        ),
    )

    # ---- IAS 36 impairment ----
    impairment_ids = fields.One2many(
        'eh.asset.impairment', 'asset_id', copy=False,
        help=(
            "History of impairment charges and reversals. Each row "
            "represents a separate impairment event with its own JE."
        ),
    )
    accumulated_impairment = fields.Monetary(
        compute='_compute_impairment_totals', store=False,
        currency_field='currency_id',
        help=(
            "Net total of impairment charges minus reversals on this "
            "asset. Reduces the carrying amount used for the NBV "
            "computation; cannot exceed the depreciable base."
        ),
    )

    # ---- IAS 36 cash-generating unit ----
    cgu_id = fields.Many2one(
        'eh.asset.cgu',
        string="Cash-Generating Unit",
        ondelete='set null',
        index=True,
        help=(
            "Optional cash-generating unit (CGU) this asset belongs to. "
            "IAS 36 tests recoverable amount at the level of the "
            "smallest group of assets that generates largely "
            "independent cash inflows when an individual asset cannot "
            "be tested on its own. When the CGU's impairment test "
            "recognises a loss, the shortfall is allocated pro-rata "
            "across the CGU's member assets (any goodwill first). Left "
            "blank the asset is tested and impaired individually as "
            "before; this grouping is opt-in."
        ),
    )
    is_goodwill = fields.Boolean(
        string="Goodwill",
        default=False,
        help=(
            "Marks this asset as goodwill allocated to a CGU. IAS "
            "36.104 requires an impairment loss on a CGU to be applied "
            "first to reduce the carrying amount of any goodwill, then "
            "pro-rata across the other assets of the unit. Off by "
            "default; setting it only affects the CGU allocation order."
        ),
    )

    # ---- AU low-value pool ----
    lvp_pool_id = fields.Many2one(
        'eh.asset.lvp.pool',
        string="Low-Value Pool",
        ondelete='restrict',
        help=(
            "When set, this asset has been transferred to a low-value "
            "pool and is depreciated as part of the pool rather than "
            "individually. The asset's own schedule is frozen; the "
            "pool's schedule drives all subsequent depreciation. "
            "AU sites use this for assets under the AUD 1,000 "
            "low-value threshold."
        ),
    )
    lvp_opening_value = fields.Monetary(
        string="Pool Opening Adjustable Value",
        readonly=True, copy=False, currency_field='currency_id',
        help=(
            "Net book value captured at the moment this asset was "
            "transferred into its low-value pool (the 'opening adjustable "
            "value' that was reclassified into the pool asset account). "
            "The pool depreciates and reports on this base, not the gross "
            "acquisition cost, so the subledger stays aligned with the GL "
            "for an asset transferred in already partly depreciated."
        ),
    )
    lvp_allocation_date = fields.Date(
        string="Pool Allocation Date",
        readonly=True, copy=False,
        help=(
            "Date this asset was allocated (transferred) into its low-"
            "value pool. Drives the ATO first-year vs subsequent-year "
            "rate: the year of allocation attracts the 18.75% first-year "
            "rate, later years the 37.5% rate. Recorded independently of "
            "the in-service date so an asset transferred in a later year "
            "is rated on the correct base year and is not depreciated in "
            "the pool before it was ever transferred in."
        ),
    )
    total_depreciated = fields.Monetary(
        compute='_compute_totals', store=True,
    )
    net_book_value = fields.Monetary(
        compute='_compute_totals', store=True,
    )
    revaluation_adjustment = fields.Monetary(
        readonly=True, tracking=True,
        help=(
            "Cumulative signed revaluation applied to the carrying amount "
            "(positive = uplift, negative = downward revaluation). Held "
            "separately from acquisition_cost so historical cost, and the "
            "IAS 36.117 depreciated-cost ceiling derived from it, stay "
            "intact. net_book_value includes this adjustment."
        ),
    )
    revaluation_surplus = fields.Monetary(
        readonly=True, tracking=True,
        help=(
            "Cumulative revaluation surplus held in equity (the credited "
            "revaluation reserve balance attributable to this asset, per "
            "IAS 16.39-41). An uplift increases it; a subsequent downward "
            "revaluation first reverses it (IAS 16.40) and only the excess "
            "hits P&L; on disposal any remaining balance is recycled "
            "directly to retained earnings (IAS 16.41), never through P&L. "
            "Never negative."
        ),
    )
    revaluation_pl_decrease = fields.Monetary(
        readonly=True, tracking=True,
        help=(
            "Cumulative revaluation decrease previously recognised in P&L "
            "(the excess of a downward revaluation that could not be absorbed "
            "by the revaluation surplus, per IAS 16.40). A subsequent upward "
            "revaluation must first reverse this in P&L (credit to income) up "
            "to this balance before any remainder is credited to the "
            "revaluation surplus (IAS 16.39). Never negative."
        ),
    )
    next_post_date = fields.Date(compute='_compute_next_post')

    # ---- audit ----
    activated_at = fields.Datetime(readonly=True, tracking=True)
    activated_by_id = fields.Many2one('res.users', readonly=True)
    disposed_at = fields.Datetime(readonly=True, tracking=True)
    disposed_by_id = fields.Many2one('res.users', readonly=True)
    disposal_date = fields.Date(readonly=True, tracking=True)
    disposal_proceeds = fields.Monetary(readonly=True, tracking=True)
    disposal_partner_id = fields.Many2one('res.partner', readonly=True)
    disposal_move_id = fields.Many2one(
        'account.move', readonly=True, ondelete='restrict')

    notes = fields.Text()

    _sql_constraints = [
        ('check_acquisition_cost', 'CHECK (acquisition_cost > 0)', 'Acquisition cost must be positive.'),
        ('check_salvage_le_cost', 'CHECK (salvage_value >= 0)', 'Salvage value cannot be negative.'),
        ('check_useful_life', 'CHECK (useful_life_months > 0)', 'Useful life must be greater than zero.'),
    ]

    # ---- compute ----

    @api.depends(
        'acquisition_cost', 'revaluation_adjustment',
        'depreciation_line_ids.amount', 'depreciation_line_ids.is_posted',
        'impairment_ids.amount', 'impairment_ids.is_reversal',
        'impairment_ids.state',
    )
    def _compute_totals(self):
        for asset in self:
            posted = asset.depreciation_line_ids.filtered(lambda l: l.is_posted)
            asset.total_depreciated = sum(posted.mapped('amount'))
            posted_impairments = asset.impairment_ids.filtered(
                lambda i: i.state == 'posted'
            )
            charges = sum(
                posted_impairments
                .filtered(lambda i: not i.is_reversal)
                .mapped('amount'),
            )
            reversals = sum(
                posted_impairments
                .filtered(lambda i: i.is_reversal)
                .mapped('amount'),
            )
            net_impairment = charges - reversals
            asset.net_book_value = (
                asset.acquisition_cost
                - asset.total_depreciated
                - net_impairment
                + asset.revaluation_adjustment
            )

    @api.depends('depreciation_line_ids.is_posted', 'depreciation_line_ids.depreciation_date')
    def _compute_next_post(self):
        for asset in self:
            unposted = asset.depreciation_line_ids.filtered(lambda l: not l.is_posted)
            asset.next_post_date = (
                min(unposted.mapped('depreciation_date')) if unposted else False
            )

    @api.depends('book_ids')
    def _compute_book_count(self):
        for asset in self:
            asset.book_count = len(asset.book_ids)

    @api.depends(
        'component_ids', 'component_ids.acquisition_cost',
        'component_ids.net_book_value',
        'acquisition_cost', 'net_book_value',
    )
    def _compute_component_totals(self):
        for asset in self:
            children = asset.component_ids
            asset.component_count = len(children)
            asset.rolled_up_cost = (
                (asset.acquisition_cost or 0.0)
                + sum(children.mapped('acquisition_cost'))
            )
            asset.rolled_up_nbv = (
                (asset.net_book_value or 0.0)
                + sum(children.mapped('net_book_value'))
            )

    @api.depends('impairment_ids', 'impairment_ids.amount',
                 'impairment_ids.is_reversal', 'impairment_ids.state')
    def _compute_impairment_totals(self):
        for asset in self:
            posted_impairments = asset.impairment_ids.filtered(
                lambda i: i.state == 'posted'
            )
            charges = sum(
                posted_impairments
                .filtered(lambda i: not i.is_reversal)
                .mapped('amount'),
            )
            reversals = sum(
                posted_impairments
                .filtered(lambda i: i.is_reversal)
                .mapped('amount'),
            )
            asset.accumulated_impairment = charges - reversals

    def action_view_components(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Components"),
            'res_model': 'eh.asset',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('parent_asset_id', '=', self.id)],
            'context': {'default_parent_asset_id': self.id},
        }

    def action_view_impairments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Impairment History"),
            'res_model': 'eh.asset.impairment',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('asset_id', '=', self.id)],
            'context': {'default_asset_id': self.id},
        }

    def action_open_impairment_wizard(self):
        self.ensure_one()
        if self.state not in ('running', 'paused'):
            raise UserError(_(
                "Impairment requires a running or paused asset.",
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Record Impairment"),
            'res_model': 'eh.asset.impairment',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_asset_id': self.id},
        }

    def action_view_books(self):
        """Open the book list filtered to this asset."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Depreciation Books"),
            'res_model': 'eh.asset.book',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('asset_id', '=', self.id)],
            'context': {'default_asset_id': self.id},
        }

    # ---- onchange ----

    @api.onchange('category_id')
    def _onchange_category(self):
        if not self.category_id:
            return
        cat = self.category_id
        self.method = cat.method
        self.useful_life_months = cat.useful_life_months
        self.salvage_value = cat.salvage_rate * (self.acquisition_cost or 0.0)
        self.declining_factor = cat.declining_factor
        self.prorate_first_period = cat.prorate_first_period
        self.prorata_mode = cat.prorata_mode
        self.asset_account_id = cat.asset_account_id
        self.depreciation_account_id = cat.depreciation_account_id
        self.accumulated_depreciation_account_id = cat.accumulated_depreciation_account_id
        self.disposal_gain_account_id = cat.disposal_gain_account_id
        self.disposal_loss_account_id = cat.disposal_loss_account_id
        self.journal_id = cat.journal_id

    @api.onchange('acquisition_date')
    def _onchange_acquisition_date_default_in_service(self):
        """Default in_service_date to acquisition_date when empty.

        Most assets enter service on the day they are acquired; the
        common path is "buy a laptop today, start using it today".
        Pre-fill saves the user a duplicate date entry. We only fill
        when in_service_date is empty so a deferred-deployment case
        (asset in storage for weeks) is preserved.
        """
        for rec in self:
            if rec.acquisition_date and not rec.in_service_date:
                rec.in_service_date = rec.acquisition_date

    @api.onchange('acquisition_cost', 'salvage_value')
    def _onchange_cost_warn_negative_depreciable(self):
        """Warn when salvage exceeds cost (would yield negative
        depreciable base). The constraint blocks the save anyway,
        but a live warning during edit is friendlier than a UserError
        on Save.
        """
        for rec in self:
            if (rec.acquisition_cost
                    and rec.salvage_value
                    and rec.salvage_value > rec.acquisition_cost):
                return {
                    'warning': {
                        'title': "Salvage exceeds cost",
                        'message': (
                            "Salvage value %.2f is greater than "
                            "acquisition cost %.2f. The depreciable "
                            "base would be negative; saving will be "
                            "blocked." % (
                                rec.salvage_value, rec.acquisition_cost,
                            )
                        ),
                    }
                }

    @api.constrains('salvage_value', 'acquisition_cost')
    def _check_salvage(self):
        for asset in self:
            if asset.salvage_value > asset.acquisition_cost:
                raise ValidationError(_(
                    "Salvage value cannot exceed acquisition cost.",
                ))

    @api.constrains('asset_class', 'is_indefinite_life',
                    'dev_cost_capitalisation')
    def _check_ias38_class_flags(self):
        """The IAS 38 regimes only exist for intangible assets."""
        for asset in self:
            if asset.is_indefinite_life and asset.asset_class != 'intangible':
                raise ValidationError(_(
                    "Only an intangible asset (IAS 38) can have an "
                    "indefinite useful life. Tangible assets under IAS 16 "
                    "are always depreciated over a finite useful life.",
                ))
            if (asset.dev_cost_capitalisation
                    and asset.asset_class != 'intangible'):
                raise ValidationError(_(
                    "The development-cost capitalisation checklist (IAS "
                    "38.57) applies to intangible assets only. Set the "
                    "asset class to Intangible (IAS 38) first.",
                ))

    @api.constrains('is_indefinite_life', 'depreciation_line_ids')
    def _check_indefinite_no_schedule(self):
        """IAS 38.107: an indefinite-life intangible is not amortised.

        Blocks flipping is_indefinite_life on an asset that already
        carries schedule lines; the mirror guard on the line model
        blocks creating lines under an indefinite-life asset.
        """
        for asset in self:
            if asset.is_indefinite_life and asset.depreciation_line_ids:
                raise ValidationError(_(
                    "%(asset)s has an indefinite useful life; IAS 38.107 "
                    "prohibits amortising it, so it cannot carry a "
                    "depreciation schedule. Remove the schedule lines "
                    "(or clear the indefinite-life flag after a "
                    "finite-life reassessment per IAS 38.109).",
                    asset=asset.display_name,
                ))

    def _eh_is_indefinite_intangible(self):
        self.ensure_one()
        return self.asset_class == 'intangible' and self.is_indefinite_life

    _IAS38_57_CRITERIA = (
        ('dev_technical_feasibility', "technical feasibility (IAS 38.57(a))"),
        ('dev_intention_complete', "intention to complete (IAS 38.57(b))"),
        ('dev_ability_use_sell', "ability to use or sell (IAS 38.57(c))"),
        ('dev_probable_benefits',
         "probable future economic benefits (IAS 38.57(d))"),
        ('dev_resources_available',
         "adequate resources available (IAS 38.57(e))"),
        ('dev_reliable_measurement',
         "reliable measurement of expenditure (IAS 38.57(f))"),
    )

    def _check_ias38_dev_gate(self):
        """Capitalisation gate for development costs (IAS 38.57).

        An intangible flagged dev_cost_capitalisation cannot leave
        draft until all six IAS 38.57 criteria are ticked. When any
        criterion cannot be demonstrated, IAS 38 requires the
        expenditure to be expensed as incurred, not capitalised.
        """
        for asset in self:
            if not asset.dev_cost_capitalisation:
                continue
            missing = [
                label for field_name, label in self._IAS38_57_CRITERIA
                if not asset[field_name]
            ]
            if missing:
                raise UserError(_(
                    "%(asset)s is a capitalised development cost but the "
                    "IAS 38.57 capitalisation criteria are not all "
                    "demonstrated. Missing: %(missing)s. IAS 38 permits "
                    "capitalising development expenditure only when every "
                    "criterion is met; otherwise recognise it as an "
                    "expense when incurred.",
                    asset=asset.display_name,
                    missing='; '.join(missing),
                ))

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code('eh.asset') or '/'
                vals['name'] = seq
        return super().create(vals_list)

    # Cost inputs that fix the depreciable base. Once any schedule line has
    # posted they are frozen: editing them would silently re-base the carrying
    # amount and every remaining charge away from what has already been booked
    # to the ledger. A change of cost must go through revaluation.
    _FROZEN_AFTER_POST = ('acquisition_cost', 'salvage_value')

    def write(self, vals):
        frozen = [f for f in self._FROZEN_AFTER_POST if f in vals]
        if frozen:
            posted = self.filtered(
                lambda a: any(a.depreciation_line_ids.mapped('is_posted')))
            if posted:
                raise UserError(_(
                    "Cost inputs (%(fields)s) are frozen once depreciation has "
                    "posted; the depreciable base cannot be re-based under the "
                    "ledger. Use the revaluation wizard to change the carrying "
                    "amount.",
                    fields=', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        # An asset that has posted depreciation, or a disposal move, carries
        # a posting-move link (its lines' JEs and the disposal_move_id);
        # deleting the master would orphan a posted GL entry. Block it. A
        # draft asset with no posted line and no disposal move stays deletable.
        posted = self.filtered(
            lambda a: any(a.depreciation_line_ids.mapped('is_posted'))
            or a.disposal_move_id)
        if posted:
            raise UserError(_(
                "An asset with posted depreciation or a disposal entry "
                "cannot be deleted; its journal entries would be orphaned. "
                "Dispose of it instead."))
        return super().unlink()

    # ---- transitions ----

    def action_compute_schedule(self):
        """Generate (or regenerate) the depreciation schedule.

        Only allowed in draft. Wipes any existing draft (unposted) lines.
        Posted lines are preserved and the schedule resumes from the
        last posted line.
        """
        for asset in self:
            if asset.state not in ('draft',):
                raise UserError(_(
                    "Schedule can only be (re)generated while the asset "
                    "is in draft state.",
                ))
            if asset.is_under_construction:
                raise UserError(_(
                    "Asset %s is under construction; capitalise it "
                    "before computing the schedule.",
                ) % asset.display_name)
            if asset._eh_is_indefinite_intangible():
                raise UserError(_(
                    "%s has an indefinite useful life; IAS 38.107 "
                    "prohibits amortisation, so no schedule is generated. "
                    "The asset is instead subject to a mandatory annual "
                    "impairment test (IAS 36.10).",
                ) % asset.display_name)
            asset._wipe_unposted_lines()
            asset._build_schedule()

    def action_capitalise(self, capitalisation_date=None):
        """Capitalise an asset under construction.

        Sets in_service_date to capitalisation_date (defaults to today),
        flips is_under_construction off, posts a balanced JE moving
        the carrying amount from auc_account_id to asset_account_id
        when both are configured, generates the schedule, and
        activates the asset.

        Refuses to run when:
          * is_under_construction is False (nothing to capitalise).
          * acquisition_cost is zero (no carrying amount to transfer).
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can capitalise an asset under "
                "construction. This posting is a segregation-of-duties "
                "control point.",
            ))
        self = self._eh_workflow_action()
        for asset in self:
            if not asset.is_under_construction:
                raise UserError(_(
                    "Asset %s is not under construction.",
                ) % asset.display_name)
            if not asset.acquisition_cost:
                raise UserError(_(
                    "Asset %s has no carrying amount to capitalise.",
                ) % asset.display_name)
            asset._check_ias38_dev_gate()
            cap_date = (
                capitalisation_date
                or fields.Date.context_today(self)
            )
            asset.in_service_date = cap_date
            asset.is_under_construction = False
            # Post the AUC -> asset transfer JE when both accounts and
            # a journal are configured. Sites that prefer to reclassify
            # the carrying amount manually can leave auc_account_id
            # blank; in that case we skip the JE and rely on the user
            # to make the reclassification entry by hand.
            if asset.auc_account_id and asset.asset_account_id and asset.journal_id:
                asset._eh_post_auc_capitalisation_move(cap_date)
            # Generate schedule + activate so the cron starts posting.
            # An indefinite-life intangible (IAS 38.107) carries no
            # amortisation schedule; it activates schedule-less and is
            # covered by the annual impairment-test cron instead.
            asset._wipe_unposted_lines()
            if not asset._eh_is_indefinite_intangible():
                asset._build_schedule()
                asset._validate_posting_setup()
            asset.write({
                'state': 'running',
                'activated_at': fields.Datetime.now(),
                'activated_by_id': self.env.user.id,
                'capitalised_at': fields.Datetime.now(),
                'capitalised_by_id': self.env.user.id,
            })
            asset.message_post(body=_(
                "Capitalised on %(date)s. Depreciation schedule "
                "generated; the cron will post due lines from the "
                "next pass.",
                date=cap_date,
            ))
        return True

    def _eh_post_auc_capitalisation_move(self, cap_date):
        """Move the carrying amount from AUC holding to the asset account."""
        self.ensure_one()
        label = _("Capitalisation %s") % self.display_name
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'eh_sealed': True,
            'journal_id': self.journal_id.id,
            'date': cap_date,
            'ref': self.name,
            'line_ids': [
                (0, 0, {
                    'name': label,
                    'account_id': self.asset_account_id.id,
                    'debit': self.acquisition_cost,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': label,
                    'account_id': self.auc_account_id.id,
                    'debit': 0.0,
                    'credit': self.acquisition_cost,
                }),
            ],
        })
        move.action_post()
        return move

    def action_activate(self):
        self = self._eh_workflow_action()
        for asset in self:
            if asset.state != 'draft':
                raise UserError(_(
                    "Only draft assets can be activated.",
                ))
            if asset.is_under_construction:
                raise UserError(_(
                    "Asset %s is under construction; capitalise it "
                    "first via action_capitalise.",
                ) % asset.display_name)
            asset._check_ias38_dev_gate()
            if asset._eh_is_indefinite_intangible():
                # IAS 38.107: no amortisation schedule; the asset runs
                # schedule-less under the annual impairment-test regime.
                pass
            else:
                if not asset.depreciation_line_ids:
                    asset._build_schedule()
                asset._validate_posting_setup()
            asset.write({
                'state': 'running',
                'activated_at': fields.Datetime.now(),
                'activated_by_id': self.env.user.id,
            })

    def action_pause(self):
        self = self._eh_workflow_action()
        for asset in self:
            if asset.state != 'running':
                raise UserError(_(
                    "Only running assets can be paused.",
                ))
            asset.state = 'paused'

    def action_resume(self):
        self = self._eh_workflow_action()
        for asset in self:
            if asset.state != 'paused':
                raise UserError(_(
                    "Only paused assets can be resumed.",
                ))
            asset.state = 'running'

    def action_set_to_draft(self):
        self = self._eh_workflow_action()
        for asset in self:
            if asset.state == 'disposed':
                raise UserError(_(
                    "Disposed assets cannot return to draft.",
                ))
            if any(asset.depreciation_line_ids.mapped('is_posted')):
                raise UserError(_(
                    "Cannot return to draft once any depreciation line "
                    "has been posted.",
                ))
            asset.state = 'draft'

    def action_open_revalue_wizard(self):
        self.ensure_one()
        if self.state not in ('running', 'paused'):
            raise UserError(_(
                "Revaluation requires a running or paused asset.",
            ))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.asset.revalue.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_asset_id': self.id},
        }

    def action_open_dispose_wizard(self):
        self.ensure_one()
        if self.state in ('disposed',):
            raise UserError(_(
                "Asset is already disposed.",
            ))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.asset.dispose.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_asset_id': self.id},
        }

    def action_post_due_lines(self):
        """Force post all depreciation lines whose date is today or earlier."""
        today = fields.Date.context_today(self)
        for asset in self:
            if asset.state not in ('running',):
                continue
            due = asset.depreciation_line_ids.filtered(
                lambda l: not l.is_posted and l.depreciation_date <= today,
            ).sorted('depreciation_date')
            for line in due:
                line.action_post()
            asset._maybe_mark_fully_depreciated()

    # ---- helpers ----

    def _wipe_unposted_lines(self):
        self.ensure_one()
        unposted = self.depreciation_line_ids.filtered(lambda l: not l.is_posted)
        unposted.unlink()

    def _validate_posting_setup(self):
        """Verify the accounts and journal needed to post this schedule.

        Deferred revenue and deferred expense assets require both an
        asset/liability holding account (asset_account_id) and a P/L
        recognition account (depreciation_account_id), but not an
        accumulated_depreciation_account_id since the balance sheet
        leg is the holding account itself, not a contra account.
        """
        self.ensure_one()
        missing = []
        if not self.journal_id:
            missing.append(_("Depreciation Journal"))
        if self.deferred_type == 'asset':
            if not self.depreciation_account_id:
                missing.append(_("Depreciation Expense Account"))
            if not self.accumulated_depreciation_account_id:
                missing.append(_("Accumulated Depreciation Account"))
        elif self.deferred_type == 'deferred_revenue':
            if not self.asset_account_id:
                missing.append(_("Deferred Revenue Liability Account"))
            if not self.depreciation_account_id:
                missing.append(_("Revenue Recognition Account"))
        elif self.deferred_type == 'deferred_expense':
            if not self.asset_account_id:
                missing.append(_("Prepaid Expense Asset Account"))
            if not self.depreciation_account_id:
                missing.append(_("Expense Recognition Account"))
        if missing:
            raise UserError(_(
                "Asset %(asset)s is missing posting setup: %(missing)s.",
                asset=self.display_name,
                missing=", ".join(missing),
            ))

    def _build_schedule(self):
        self.ensure_one()
        Line = self.env['eh.asset.depreciation.line']
        rows = self._generate_schedule_rows()
        for row in rows:
            Line.create({
                'asset_id': self.id,
                'sequence': row['sequence'],
                'depreciation_date': row['date'],
                'amount': row['amount'],
                'accumulated': row['accumulated'],
                'remaining_value': row['remaining'],
            })

    def _generate_schedule_rows(self):
        """Compute the depreciation schedule as a list of dicts.

        Output:
          [{sequence, date, amount, accumulated, remaining}, ...]
        Pure function: does not write to DB.

        is_instant_write_off short-circuits to a single line for the
        full depreciable amount on the in-service date's month-end,
        regardless of method or useful life.
        """
        self.ensure_one()
        if self.method == 'manual':
            return []
        depreciable = self.acquisition_cost - self.salvage_value
        if depreciable <= 0:
            return []
        if self.is_instant_write_off:
            return self._schedule_instant_write_off(depreciable)
        if self.method in ('straight_line', 'prime_cost'):
            return self._schedule_straight_line(depreciable)
        if self.method in ('reducing_balance', 'diminishing_value'):
            return self._schedule_reducing_balance(depreciable)
        if self.method == 'units_of_production':
            return self._schedule_uop(depreciable)
        return []

    def _schedule_instant_write_off(self, depreciable):
        """One-shot full write-off in the first period.

        AU instant-asset-write-off and similar fast-deduction regimes
        depreciate the entire eligible amount in the year of acquisition.
        We post a single line at the in-service month-end so the GL
        impact is immediate; the cron picks it up on its next pass.
        """
        amount = self.currency_id.round(depreciable)
        return [{
            'sequence': 1,
            'date': self._first_period_date(),
            'amount': amount,
            'accumulated': amount,
            'remaining': self.currency_id.round(
                self.acquisition_cost - amount,
            ),
        }]

    def _schedule_straight_line(self, depreciable):
        rows = []
        months = self.useful_life_months
        per_period = depreciable / months
        accumulated = 0.0
        period_date = self._first_period_date()
        for n in range(1, months + 1):
            if n == 1:
                amount = self._first_period_amount(per_period)
            elif n == months:
                amount = depreciable - accumulated
            else:
                amount = per_period
            amount = self.currency_id.round(amount)
            accumulated = self.currency_id.round(accumulated + amount)
            remaining = self.currency_id.round(
                self.acquisition_cost - accumulated,
            )
            rows.append({
                'sequence': n,
                'date': period_date,
                'amount': amount,
                'accumulated': accumulated,
                'remaining': remaining,
            })
            period_date = self._next_period_end(period_date)
        return rows

    def _schedule_reducing_balance(self, depreciable):
        rows = []
        months = self.useful_life_months
        years = max(1, months / 12.0)
        sl_rate_per_year = 1.0 / years
        rate_per_year = self.declining_factor * sl_rate_per_year
        rate_per_period = rate_per_year / 12.0
        # Floor switch: when straight line on remaining balance exceeds
        # reducing balance, switch to straight line for the rest.
        accumulated = 0.0
        nbv = self.acquisition_cost
        period_date = self._first_period_date()
        for n in range(1, months + 1):
            remaining_periods = months - n + 1
            sl_amount = max(0.0, (nbv - self.salvage_value) / remaining_periods)
            rb_amount = max(0.0, (nbv - self.salvage_value)) * rate_per_period
            amount = max(rb_amount, sl_amount)
            if n == 1:
                amount = self._first_period_amount(amount)
            if n == months:
                amount = depreciable - accumulated
            amount = max(0.0, self.currency_id.round(amount))
            if accumulated + amount > depreciable:
                amount = self.currency_id.round(depreciable - accumulated)
            accumulated = self.currency_id.round(accumulated + amount)
            nbv = self.acquisition_cost - accumulated
            rows.append({
                'sequence': n,
                'date': period_date,
                'amount': amount,
                'accumulated': accumulated,
                'remaining': self.currency_id.round(nbv),
            })
            period_date = self._next_period_end(period_date)
            if accumulated >= depreciable:
                break
        return rows

    def _schedule_uop(self, depreciable):
        # Units of production depreciation is a preview method: usage
        # recording is not yet implemented, so a schedule cannot be
        # generated. Block activation with a clear message rather than
        # emitting a zero amount placeholder schedule.
        raise UserError(_(
            "Units of Production depreciation is a preview method and is "
            "not yet available. Choose Straight Line or Reducing Balance "
            "for the primary GL book, or use an additional book for a "
            "parallel method."
        ))

    def _first_period_date(self):
        d = self.in_service_date
        return self._month_end(d)

    @staticmethod
    def _month_end(d):
        last = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, last)

    def _next_period_end(self, d):
        nxt = d + relativedelta(months=1)
        return self._month_end(nxt)

    def _first_period_prorated_amount(self, full_period_amount):
        """Compute the prorated first period amount based on days in service."""
        d = self.in_service_date
        last = calendar.monthrange(d.year, d.month)[1]
        days_in_service = last - d.day + 1
        if last <= 0:
            return full_period_amount
        return full_period_amount * (days_in_service / float(last))

    def _first_period_amount(self, base):
        """First-period charge for the effective prorata mode.

        Mode resolves from prorata_mode when set, otherwise from the
        legacy prorate_first_period switch (True -> daily, False -> none),
        so existing assets keep their schedule unchanged.
        """
        mode = self.prorata_mode or (
            'daily' if self.prorate_first_period else 'none')
        if mode == 'none':
            return base
        if mode == 'half':
            return base / 2.0
        return self._first_period_prorated_amount(base)

    def _build_remaining_schedule(self, periods):
        """Build a fresh straight line schedule over `periods` periods on
        the current net book value. Used by the revaluation wizard to
        re-amortise the post-revaluation NBV across the remaining
        useful life. Sequence numbers continue from the last posted line.
        """
        self.ensure_one()
        Line = self.env['eh.asset.depreciation.line']
        depreciable = self.net_book_value - self.salvage_value
        if depreciable <= 0 or periods <= 0:
            return
        per_period = depreciable / periods
        posted = self.depreciation_line_ids.filtered(lambda l: l.is_posted)
        last_seq = max(posted.mapped('sequence')) if posted else 0
        last_date = (
            max(posted.mapped('depreciation_date')) if posted
            else self.in_service_date
        )
        accumulated_after_posted = sum(posted.mapped('amount'))
        period_date = self._next_period_end(last_date)
        for i in range(1, periods + 1):
            if i == periods:
                amount = depreciable - per_period * (periods - 1)
            else:
                amount = per_period
            amount = self.currency_id.round(amount)
            accumulated_after_posted = self.currency_id.round(
                accumulated_after_posted + amount,
            )
            remaining = self.currency_id.round(
                self.acquisition_cost + self.revaluation_adjustment
                - accumulated_after_posted,
            )
            Line.create({
                'asset_id': self.id,
                'sequence': last_seq + i,
                'depreciation_date': period_date,
                'amount': amount,
                'accumulated': accumulated_after_posted,
                'remaining_value': remaining,
            })
            period_date = self._next_period_end(period_date)

    def _maybe_mark_fully_depreciated(self):
        self = self._eh_workflow_action()
        for asset in self:
            if asset.state != 'running':
                continue
            unposted = asset.depreciation_line_ids.filtered(
                lambda l: not l.is_posted,
            )
            if not unposted and asset.net_book_value <= asset.salvage_value:
                asset.state = 'fully_depreciated'

    # ---- IAS 36 impairment helpers ----

    def _eh_rebuild_after_impairment(self):
        """Re-amortise remaining depreciation on the post-event carrying
        amount, per IAS 36.63.

        After an impairment loss (or its reversal) is recognised, the
        depreciation charge must be adjusted in future periods to allocate
        the asset's revised carrying amount, less residual value, on a
        systematic basis over its remaining useful life. We wipe the
        unposted lines and rebuild them over the same number of remaining
        periods on the current (post-event) net book value, reusing the
        same re-amortisation the revaluation wizard uses.

        Applies to regular depreciating fixed assets only. Manual, units of
        production, instant write-off, pooled, and deferred items keep
        their schedule untouched.
        """
        self.ensure_one()
        if self.deferred_type != 'asset':
            return
        if self.method in ('manual', 'units_of_production'):
            return
        if self.is_instant_write_off or self.lvp_pool_id:
            return
        unposted = self.depreciation_line_ids.filtered(
            lambda l: not l.is_posted,
        )
        remaining_periods = len(unposted)
        if remaining_periods <= 0:
            return
        self.invalidate_recordset(['net_book_value'])
        self._wipe_unposted_lines()
        self._build_remaining_schedule(remaining_periods)

    def _ias36_depreciated_cost(self, as_of_date=None):
        """Carrying amount the asset would have had if no impairment had
        ever been recognised: original cost less the depreciation that
        would have accrued, over the same number of elapsed (posted)
        periods, on the original cost base. IAS 36.117 caps an impairment
        reversal at this depreciated historical cost.

        Aligned on the count of posted periods (not on the calendar) so an
        asset whose depreciation cron is behind is not falsely treated as
        more depreciated than it is.

        Manual and units-of-production assets have no engine-generated
        schedule to replay, so the ceiling falls back to a HYPOTHETICAL
        straight line over the asset's useful life (see
        _ias36_hypothetical_sl_cost); as_of_date anchors the elapsed-time
        measurement for that fallback (defaults to today).
        """
        self.ensure_one()
        posted = self.depreciation_line_ids.filtered(lambda l: l.is_posted)
        posted_total = sum(posted.mapped('amount'))
        if self.method in ('manual', 'units_of_production'):
            # _generate_schedule_rows returns nothing for manual and
            # raises for units of production; both route to the
            # hypothetical straight-line fallback.
            return self._ias36_hypothetical_sl_cost(as_of_date, posted_total)
        rows = self._generate_schedule_rows()
        if not rows:
            return self._ias36_hypothetical_sl_cost(as_of_date, posted_total)
        hypothetical_accum = sum(r['amount'] for r in rows[:len(posted)])
        return self.currency_id.round(
            self.acquisition_cost - hypothetical_accum,
        )

    def _ias36_hypothetical_sl_cost(self, as_of_date, posted_total):
        """IAS 36.117 ceiling for assets with no engine schedule.

        IAS 36.117 caps a reversal at the carrying amount that "would
        have been determined (net of amortisation or depreciation) had
        no impairment loss been recognised". A manual-method asset has
        no engine schedule from which to replay that hypothetical, and
        the naive fallback (raw cost less whatever happens to have been
        posted) lets an asset with little or no posted depreciation
        reverse all the way back to full cost, over-reversing relative
        to ANY systematic depreciation basis.

        The defensible proxy is a hypothetical straight line over the
        asset's stated useful life (the default systematic basis of
        IAS 16.62 / IAS 38.97): elapsed month-end periods since the
        in-service date, times (cost - salvage) / useful_life_months.
        The ceiling is the LOWER of that hypothetical depreciated cost
        and cost less actually posted depreciation (a reversal must
        never restore depreciation that has genuinely been charged),
        floored at salvage value.
        """
        self.ensure_one()
        as_of = as_of_date or fields.Date.context_today(self)
        months = self.useful_life_months or 0
        depreciable = self.acquisition_cost - self.salvage_value
        if months <= 0 or depreciable <= 0:
            hypothetical = self.acquisition_cost
        else:
            elapsed = max(0, min(months, self._eh_elapsed_month_ends(as_of)))
            hypothetical = (
                self.acquisition_cost - depreciable * (elapsed / float(months))
            )
        actual = self.acquisition_cost - posted_total
        return self.currency_id.round(
            max(self.salvage_value, min(hypothetical, actual)),
        )

    def _eh_elapsed_month_ends(self, as_of):
        """Number of monthly periods elapsed under the module's month-end
        schedule convention: counts month-end dates from the in-service
        month end through as_of, inclusive only when as_of itself is a
        month end (a mid-month date has not completed its period)."""
        self.ensure_one()
        start = self._month_end(self.in_service_date)
        if as_of < start:
            return 0
        elapsed = (as_of.year - start.year) * 12 + (as_of.month - start.month)
        if as_of == self._month_end(as_of):
            elapsed += 1
        return elapsed

    # ---- cron ----

    @api.model
    def _cron_post_due(self, batch_size=200):
        today = fields.Date.context_today(self)
        domain = [
            ('state', '=', 'running'),
        ]
        assets = self.search(domain, limit=batch_size)

        # Per-asset savepoint via the shared batch mixin so one bad asset
        # does not poison the cursor and abort the rest of the batch.
        def _post_asset(asset):
            due = asset.depreciation_line_ids.filtered(
                lambda l: not l.is_posted
                and l.depreciation_date <= today,
            ).sorted('depreciation_date')
            for line in due:
                line.action_post()
            asset._maybe_mark_fully_depreciated()

        self._eh_for_each_savepoint(
            assets, _post_asset, log_label="Auto post",
        )

    # ---- IAS 36 annual-test cron ----

    @api.model
    def _cron_ias36_annual_test(self, as_of=None, batch_size=500):
        """Enforce the IAS 36.10 annual impairment-test mandate.

        Population: running/paused goodwill assets and indefinite-life
        intangibles (the assets IAS 36.10 requires to be tested
        annually irrespective of indicators). Once the company's annual
        test month (Settings, default December) has been reached inside
        the current fiscal year, any population asset with no test
        evidence dated in that fiscal year is flagged
        annual_test_overdue and receives a to-do activity; the flag
        clears automatically when a CGU test runs or an impairment
        event posts (and on the next cron pass after either).

        ``as_of`` overrides "today" for deterministic testing.
        """
        today = fields.Date.to_date(as_of) if as_of else (
            fields.Date.context_today(self)
        )
        assets = self.search([
            ('state', 'in', ['running', 'paused']),
            '|',
            ('is_goodwill', '=', True),
            '&',
            ('asset_class', '=', 'intangible'),
            ('is_indefinite_life', '=', True),
        ], limit=batch_size)

        def _review(asset):
            fy = asset.company_id.compute_fiscalyear_dates(today)
            trigger = asset._eh_annual_test_trigger_date(fy)
            tested = asset._eh_ias36_tested_in(
                fy['date_from'], fy['date_to'],
            )
            overdue = today >= trigger and not tested
            if asset.annual_test_overdue != overdue:
                asset.annual_test_overdue = overdue
            if overdue:
                asset._eh_schedule_annual_test_activity(fy)

        self._eh_for_each_savepoint(
            assets, _review, log_label="IAS 36 annual test",
        )

    def _eh_annual_test_trigger_date(self, fy):
        """First day of the company's annual test month inside the
        fiscal year [fy['date_from'], fy['date_to']]."""
        self.ensure_one()
        month = self.company_id.eh_ias36_annual_test_month or 12
        candidate = date(fy['date_from'].year, month, 1)
        if candidate < fy['date_from']:
            candidate = date(fy['date_from'].year + 1, month, 1)
        return min(candidate, fy['date_to'])

    def _eh_ias36_tested_in(self, date_from, date_to):
        """Impairment-test evidence for this asset inside a date range:
        the asset's CGU ran a test (passed or impaired) in the range,
        or an impairment event (charge or reversal) posted in the
        range. Both funnel through the segregation-of-duties posting
        gates, so either is auditable evidence a test was performed."""
        self.ensure_one()
        cgu = self.cgu_id
        if (cgu and cgu.last_test_date
                and date_from <= cgu.last_test_date <= date_to):
            return True
        return bool(self.impairment_ids.filtered(
            lambda i: i.state == 'posted'
            and date_from <= i.impairment_date <= date_to,
        ))

    def _eh_schedule_annual_test_activity(self, fy):
        """One open to-do per asset (module idiom: activity, not email)
        prompting the annual test; deduped on the summary."""
        self.ensure_one()
        summary = _("IAS 36 annual impairment test overdue")
        existing = self.env['mail.activity'].search_count([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('summary', '=', summary),
        ])
        if existing:
            return
        self.activity_schedule(
            'mail.mail_activity_data_todo',
            summary=summary,
            note=_(
                "IAS 36.10 requires goodwill and indefinite-life "
                "intangible assets to be tested for impairment "
                "annually, irrespective of indicators. No impairment "
                "test dated in the fiscal year %(date_from)s to "
                "%(date_to)s has been recorded for this asset. Run the "
                "cash-generating unit test (or record a hand-keyed "
                "impairment assessment) to clear the flag.",
                date_from=fy['date_from'], date_to=fy['date_to'],
            ),
            user_id=(self.activated_by_id or self.env.user).id,
        )

    # ---- record helpers ----

    @api.depends('code', 'name')
    def _compute_display_name(self):
        for asset in self:
            if asset.code and asset.name and asset.name != asset.code:
                asset.display_name = "%s / %s" % (asset.code, asset.name)
            else:
                asset.display_name = asset.code or asset.name or ''
