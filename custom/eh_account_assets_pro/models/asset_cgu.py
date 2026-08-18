# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.asset.cgu: IAS 36 cash-generating unit and recoverable-amount engine.

IAS 36 requires an entity, at each reporting date, to assess whether
there is any indication that an asset may be impaired (and, for goodwill
and certain intangibles, to test annually irrespective of indicators).
Where an individual asset does not generate cash inflows that are largely
independent of those from other assets, the recoverable amount is
determined for the cash-generating unit (CGU) to which the asset belongs
(IAS 36.66).

The recoverable amount is the HIGHER of:
  (a) value in use (VIU) -- the present value of the future cash flows
      expected to be derived from the CGU, computed here by discounting a
      projected cash-flow schedule at a pre-tax discount rate; and
  (b) fair value less costs of disposal (FVLCD) -- an entity input.

When the carrying amount of the CGU exceeds its recoverable amount, the
difference is an impairment loss. IAS 36.104 allocates that loss FIRST to
any goodwill allocated to the unit, then pro-rata to the other assets of
the unit on the basis of their carrying amounts.

This engine is an ADDITIONAL, opt-in way to DERIVE the impairment number.
It reuses the existing eh.asset.impairment posting path: the test creates
one draft impairment per allocated asset and posts it (a
segregation-of-duties control point), so every downstream figure -- NBV,
the ledger, the reversal ceiling -- stays consistent with a hand-keyed
impairment. Nothing here changes an asset that is not assigned to a CGU.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError  # noqa: F401


class EhAssetCgu(models.Model):
    _name = 'eh.asset.cgu'
    _description = "Asset cash-generating unit (IAS 36)"
    _order = 'name, id'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
        help="Measurement currency for the recoverable-amount test.",
    )

    member_ids = fields.One2many(
        'eh.asset', 'cgu_id',
        string="Member Assets",
        help=(
            "Assets that make up this cash-generating unit. Their "
            "carrying amounts are summed to the CGU carrying amount and "
            "any impairment loss is allocated across them (goodwill "
            "first)."
        ),
    )
    member_count = fields.Integer(
        compute='_compute_member_totals', store=False,
    )
    carrying_amount = fields.Monetary(
        compute='_compute_member_totals', store=False,
        currency_field='currency_id',
        help=(
            "Sum of the net book value of every member asset. The "
            "impairment test compares this to the recoverable amount."
        ),
    )

    # ---- value in use (discounted cash flow) ----
    discount_rate = fields.Float(
        string="Discount Rate (%)",
        digits=(6, 4),
        help=(
            "Pre-tax discount rate applied to the projected cash flows "
            "to derive value in use, expressed as a percent per period "
            "(e.g. 10 for 10%). IAS 36.55 requires a rate that reflects "
            "current market assessments of the time value of money and "
            "the risks specific to the asset."
        ),
    )
    cashflow_ids = fields.One2many(
        'eh.asset.cgu.cashflow', 'cgu_id',
        string="Projected Cash Flows",
        help=(
            "Projected future net cash inflows attributable to the CGU, "
            "one row per period. Discounted at the discount rate to a "
            "present value that is the value in use."
        ),
    )
    value_in_use = fields.Monetary(
        compute='_compute_recoverable', store=False,
        currency_field='currency_id',
        help=(
            "Present value of the projected cash flows discounted at "
            "the discount rate. Zero when no cash flows are projected."
        ),
    )

    # ---- fair value less costs of disposal ----
    fair_value = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Fair value of the CGU (an entity input, e.g. a market or "
            "appraised value), before deducting the costs of disposal."
        ),
    )
    costs_of_disposal = fields.Monetary(
        currency_field='currency_id',
        help="Incremental costs directly attributable to the disposal.",
    )
    fair_value_less_costs = fields.Monetary(
        compute='_compute_recoverable', store=False,
        currency_field='currency_id',
        help="fair_value less costs_of_disposal, floored at zero.",
    )

    recoverable_amount = fields.Monetary(
        compute='_compute_recoverable', store=False,
        currency_field='currency_id',
        help=(
            "Higher of value in use and fair value less costs of "
            "disposal (IAS 36.18)."
        ),
    )
    impairment_shortfall = fields.Monetary(
        compute='_compute_recoverable', store=False,
        currency_field='currency_id',
        help=(
            "carrying_amount less recoverable_amount when positive; "
            "zero otherwise. The amount an impairment test would "
            "allocate across the member assets."
        ),
    )

    # ---- test governance ----
    annual_test_required = fields.Boolean(
        default=False, tracking=True,
        help=(
            "Flags a CGU that must be tested for impairment annually "
            "irrespective of indicators (IAS 36.10: a CGU to which "
            "goodwill has been allocated, or that contains an "
            "indefinite-life intangible). Informational; drives review "
            "filters and reminders."
        ),
    )
    last_test_date = fields.Date(
        readonly=True, tracking=True,
        help="Date the most recent impairment test was run.",
    )
    last_test_result = fields.Selection(
        [
            ('passed', "No impairment"),
            ('impaired', "Impairment recognised"),
        ],
        readonly=True, tracking=True,
        help="Outcome of the most recent impairment test.",
    )
    impairment_ids = fields.One2many(
        'eh.asset.impairment', 'cgu_id',
        string="Allocated Impairments",
        help="Impairment events created by this CGU's tests.",
    )

    @api.depends(
        'member_ids', 'member_ids.net_book_value',
    )
    def _compute_member_totals(self):
        for cgu in self:
            cgu.member_count = len(cgu.member_ids)
            cgu.carrying_amount = sum(
                cgu.member_ids.mapped('net_book_value'),
            )

    @api.depends(
        'cashflow_ids.amount', 'cashflow_ids.period',
        'discount_rate', 'fair_value', 'costs_of_disposal',
        'member_ids', 'member_ids.net_book_value',
    )
    def _compute_recoverable(self):
        for cgu in self:
            viu = cgu._compute_value_in_use()
            fvlcd = max(
                0.0,
                (cgu.fair_value or 0.0) - (cgu.costs_of_disposal or 0.0),
            )
            fvlcd = cgu.currency_id.round(fvlcd)
            recoverable = max(viu, fvlcd)
            cgu.value_in_use = viu
            cgu.fair_value_less_costs = fvlcd
            cgu.recoverable_amount = recoverable
            shortfall = (cgu.carrying_amount or 0.0) - recoverable
            cgu.impairment_shortfall = cgu.currency_id.round(
                max(0.0, shortfall),
            )

    def _compute_value_in_use(self):
        """Present value of the projected cash flows.

        PV = sum over rows of amount / (1 + r) ** period, with r the
        per-period discount rate and period the (1-based) number of
        periods from the measurement date. Rounded in the CGU currency.
        """
        self.ensure_one()
        rate = (self.discount_rate or 0.0) / 100.0
        pv = 0.0
        for line in self.cashflow_ids:
            period = line.period or 0
            if period <= 0:
                # A period-0 (or unset) flow is undiscounted.
                pv += line.amount or 0.0
                continue
            pv += (line.amount or 0.0) / ((1.0 + rate) ** period)
        return self.currency_id.round(pv)

    # ---- allocation ----

    def _ias36_allocation(self):
        """Return a list of (asset, amount) tuples allocating the
        impairment shortfall across the CGU's member assets.

        IAS 36.104: reduce the carrying amount of any goodwill in the
        unit first; allocate the remainder pro-rata to the other assets
        on the basis of their carrying amount (net book value). No asset
        is written below zero. The allocated amounts sum EXACTLY to the
        shortfall (rounding true-up on the final line) so the resulting
        journal entries balance the derived number by construction.
        """
        self.ensure_one()
        rnd = self.currency_id.round
        shortfall = self.impairment_shortfall
        if shortfall <= 0:
            return []

        allocation = []
        remaining = shortfall

        # Stage 1: goodwill absorbs the loss first, capped at its NBV.
        goodwill = self.member_ids.filtered(
            lambda a: a.is_goodwill and a.net_book_value > 0,
        )
        for asset in goodwill:
            if remaining <= 0:
                break
            take = rnd(min(asset.net_book_value, remaining))
            if take > 0:
                allocation.append((asset, take))
                remaining = rnd(remaining - take)

        # Stage 2: pro-rata across the remaining (non-goodwill) assets on
        # the basis of their carrying amount.
        if remaining > 0:
            others = self.member_ids.filtered(
                lambda a: not a.is_goodwill and a.net_book_value > 0,
            )
            base = sum(others.mapped('net_book_value'))
            if base <= 0:
                raise UserError(_(
                    "The CGU %(name)s has an impairment shortfall of "
                    "%(amt).2f but no non-goodwill assets with carrying "
                    "amount to absorb it. IAS 36 does not permit "
                    "reducing an asset below zero; review the member "
                    "assets or the recoverable-amount inputs.",
                    name=self.display_name, amt=remaining,
                ))
            # Cap each pro-rata share at the asset's own NBV, then spread
            # any residue (from caps) again; finally true-up on the last
            # uncapped line so the total ties exactly to `remaining`.
            pro_rata = []  # noqa: F841
            allocated = 0.0
            uncapped = others  # noqa: F841
            target = remaining
            # Simple pro-rata with per-asset NBV cap.
            shares = []
            for asset in others:
                raw = target * (asset.net_book_value / base)
                capped = min(asset.net_book_value, rnd(raw))
                shares.append([asset, capped])
                allocated = rnd(allocated + capped)
            # Rounding / cap true-up: push the residual onto the asset
            # with the most remaining headroom.
            residual = rnd(remaining - allocated)
            if residual != 0:
                # Find an asset that can absorb the residual without
                # breaching its NBV cap.
                for pair in sorted(
                    shares,
                    key=lambda p: p[0].net_book_value - p[1],
                    reverse=True,
                ):
                    asset, amt = pair
                    headroom = rnd(asset.net_book_value - amt)
                    adjust = residual
                    if adjust > 0:
                        adjust = min(adjust, headroom)
                    else:
                        adjust = max(adjust, -amt)
                    if adjust != 0:
                        pair[1] = rnd(amt + adjust)
                        residual = rnd(residual - adjust)
                    if residual == 0:
                        break
            for asset, amt in shares:
                if amt > 0:
                    allocation.append((asset, amt))

        return allocation

    # ---- actions ----

    def action_test_now(self):
        """Indicator-driven impairment test: compare carrying amount to
        recoverable amount and, when carrying exceeds recoverable, derive
        and post the impairment across the member assets.

        This is the opt-in engine. It funnels through the existing
        eh.asset.impairment path (draft create then Post), so posting is
        gated to accounting managers and every allocated entry balances
        by construction. Returns silently (records last_test_result =
        'passed') when the CGU is not impaired.
        """
        if not self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        ):
            raise UserError(_(
                "Only an accounting manager can run a CGU impairment "
                "test that posts impairment charges to the general "
                "ledger. This is a segregation-of-duties control point.",
            ))
        Impairment = self.env['eh.asset.impairment']
        today = fields.Date.context_today(self)
        for cgu in self:
            cgu.member_ids.invalidate_recordset(['net_book_value'])
            cgu.invalidate_recordset([
                'carrying_amount', 'recoverable_amount',
                'impairment_shortfall', 'value_in_use',
                'fair_value_less_costs',
            ])
            if not cgu.member_ids:
                raise UserError(_(
                    "The CGU %s has no member assets to test.",
                ) % cgu.display_name)
            shortfall = cgu.impairment_shortfall
            if shortfall <= 0:
                cgu.write({
                    'last_test_date': today,
                    'last_test_result': 'passed',
                })
                # A completed test satisfies the IAS 36.10 annual
                # mandate for every member of the unit.
                cgu.member_ids.filtered('annual_test_overdue').write({
                    'annual_test_overdue': False,
                })
                cgu.message_post(body=_(
                    "IAS 36 test: carrying amount %(ca).2f does not "
                    "exceed recoverable amount %(ra).2f (VIU %(viu).2f, "
                    "FVLCD %(fv).2f). No impairment.",
                    ca=cgu.carrying_amount, ra=cgu.recoverable_amount,
                    viu=cgu.value_in_use, fv=cgu.fair_value_less_costs,
                ))
                continue
            reason = _(
                "IAS 36 CGU impairment test on %(name)s. Carrying "
                "amount %(ca).2f exceeds recoverable amount %(ra).2f "
                "(higher of value in use %(viu).2f and fair value less "
                "costs of disposal %(fv).2f). Shortfall %(sf).2f "
                "allocated across the unit (goodwill first, then "
                "pro-rata on carrying amount).",
                name=cgu.display_name, ca=cgu.carrying_amount,
                ra=cgu.recoverable_amount, viu=cgu.value_in_use,
                fv=cgu.fair_value_less_costs, sf=shortfall,
            )
            created = Impairment
            for asset, amount in cgu._ias36_allocation():
                imp = Impairment.create({
                    'asset_id': asset.id,
                    'cgu_id': cgu.id,
                    'impairment_date': today,
                    'amount': amount,
                    'is_reversal': False,
                    'reason': reason,
                })
                created |= imp
            created.action_post()
            cgu.write({
                'last_test_date': today,
                'last_test_result': 'impaired',
            })
            # The write-down brings the unit's carrying amount onto its
            # recoverable amount, so each member's post-test carrying
            # amount IS its allocated share of that recoverable amount
            # (IAS 36.104). Stamp it as the member's latest
            # recoverable-amount measurement (used by the revaluation
            # wizard's uplift cap), and clear the annual-test flag.
            for member in cgu.member_ids:
                member.invalidate_recordset(['net_book_value'])
                member.write({
                    'recoverable_amount_latest': member.net_book_value,
                    'recoverable_amount_date': today,
                    'annual_test_overdue': False,
                })
            cgu.message_post(body=reason)
        return True

    def action_view_impairments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Allocated impairments"),
            'res_model': 'eh.asset.impairment',
            'view_mode': 'list,form',
            'domain': [('cgu_id', '=', self.id)],
        }


class EhAssetCguCashflow(models.Model):
    _name = 'eh.asset.cgu.cashflow'
    _description = "CGU projected cash flow (IAS 36 value in use)"
    _order = 'cgu_id, period, id'

    cgu_id = fields.Many2one(
        'eh.asset.cgu', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        related='cgu_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='cgu_id.currency_id', store=True, readonly=True,
    )
    period = fields.Integer(
        required=True,
        help=(
            "Number of periods from the measurement date at which this "
            "cash flow occurs (1 = one period out). A period of 0 (or "
            "less) is treated as an undiscounted present-date flow."
        ),
    )
    amount = fields.Monetary(
        required=True, currency_field='currency_id',
        help=(
            "Projected net cash inflow for the period (positive) or "
            "outflow (negative), before discounting."
        ),
    )
    note = fields.Char(help="Optional label for this cash-flow row.")

    _sql_constraints = [
        ('check_period', 'CHECK (period >= 0)', 'Cash-flow period cannot be negative.'),
    ]
