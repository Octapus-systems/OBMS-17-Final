# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.fx.hedge: IFRS 9 hedge designation and accounting.

Three hedge types per IFRS 9 / IAS 39:

* Cash flow hedge (CFH): hedges variability in cash flows of a
  forecast transaction or a recognised asset / liability with variable
  cash flows. Effective portion goes to OCI (other comprehensive
  income, an equity reserve); ineffective portion goes straight to
  P&L. When the forecast transaction occurs, the OCI reserve is
  reclassified to P&L.

* Fair value hedge (FVH): hedges exposure to changes in fair value of
  a recognised asset, liability, or unrecognised firm commitment. The
  hedging instrument's gain/loss is recognised, and the effective
  portion adjusts the hedged item's carrying amount for the hedged risk
  (booked to the hedged item account), so the net P&L effect in the
  period is the ineffective portion only.

* Net investment hedge (NIH): hedges the FX exposure of a net
  investment in a foreign operation (subsidiary). Effective portion
  goes to OCI under the foreign currency translation reserve (CTA);
  reclassified to P&L on disposal of the foreign operation.

Effectiveness testing per IFRS 9.B6.4:
* Economic relationship between hedged item and hedging instrument.
* Credit risk does not dominate value changes.
* Hedge ratio matches the actual quantities used.

This module ships two quantitative test methods:

* dollar_offset: ratio of cumulative gain on hedging instrument to
  cumulative loss on hedged item must fall within 80-125% per the
  legacy IAS 39 test (still permitted under IFRS 9 as one acceptable
  method).
* regression: linear regression of changes; r-squared must exceed a
  configurable threshold (default 0.80).

The model is designation-only: actual valuation of the hedging
instrument and the hedged item is supplied via the periodic
movement records. Sites using treasury management systems pump the
fair-value changes into eh.fx.hedge.movement records via API.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


_HEDGE_TYPES = [
    ('cash_flow', "Cash flow hedge (CFH)"),
    ('fair_value', "Fair value hedge (FVH)"),
    ('net_investment', "Net investment hedge (NIH)"),
]

_TEST_METHODS = [
    ('dollar_offset', "Dollar-offset (80-125%)"),
    ('regression', "Regression (R-squared threshold)"),
]


class EhFxHedge(models.Model):
    _name = 'eh.fx.hedge'
    _description = "Hedge designation"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'designation_date desc, id desc'

    # The hedge lifecycle is driven by action_designate / action_dedesignate /
    # action_terminate and by _eh_apply_effectiveness (test-driven qualify /
    # suspend). Block any direct write to state that does not originate from
    # one of those (each flags its write).
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
        help=(
            "Display name for the hedge relationship. Convention: "
            "'<Type>: <Hedged item> via <Instrument>'."
        ),
    )
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('designated', "Designated"),
            ('effective', "Effective"),
            ('dedesignated', "De-designated"),
            ('terminated', "Terminated"),
        ],
        default='draft', required=True, tracking=True, index=True,
        help=(
            "Hedge lifecycle. draft: parameters being entered. "
            "designated: formal IFRS 9 designation in place but no "
            "test performed yet. effective: most recent test passed. "
            "de-designated: management voluntarily ended the hedge. "
            "terminated: the underlying instrument expired or was "
            "sold."
        ),
    )
    hedge_type = fields.Selection(
        _HEDGE_TYPES, required=True, default='cash_flow',
        tracking=True,
        help=(
            "IFRS 9 hedge type. Determines whether the effective "
            "portion goes to OCI (CFH, NIH) or P&L (FVH)."
        ),
    )

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True,
    )

    designation_date = fields.Date(
        required=True, default=fields.Date.context_today,
        tracking=True,
        help=(
            "Formal designation date. IFRS 9 hedge accounting starts "
            "from this date forward; gains and losses before this "
            "date go to P&L unconditionally."
        ),
    )
    termination_date = fields.Date(
        tracking=True,
        help="Date the hedge relationship ended.",
    )

    # ---- hedged item / hedging instrument ----
    hedged_item_description = fields.Text(
        required=True,
        help=(
            "Free-form description of the hedged item: forecast "
            "USD revenue Q3 2026, EUR-denominated trade receivable "
            "from Acme GmbH, net investment in DE subsidiary, etc. "
            "IFRS 9 requires the hedged item to be reliably "
            "measurable; document the measurement basis here."
        ),
    )
    hedging_instrument_description = fields.Text(
        required=True,
        help=(
            "Free-form description of the hedging instrument: "
            "AUD/USD forward 2026-09-30 notional 1M, USD-denominated "
            "loan, etc."
        ),
    )
    notional_amount = fields.Monetary(
        currency_field='hedged_currency_id',
        help=(
            "Notional amount of the hedging instrument in the hedged "
            "item's currency. Drives the dollar-offset and regression "
            "calculations."
        ),
    )
    hedged_currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
        help=(
            "Currency of the hedged exposure. For CFH on a USD "
            "forecast sale this is USD; for NIH on a EUR subsidiary "
            "this is EUR."
        ),
    )

    # ---- effectiveness testing ----
    test_method = fields.Selection(
        _TEST_METHODS, required=True, default='dollar_offset',
        help=(
            "Quantitative effectiveness test. Dollar-offset checks "
            "the ratio of cumulative gain on the instrument to "
            "cumulative loss on the hedged item falls within "
            "80-125%. Regression performs a linear fit of period "
            "changes and requires R-squared above the threshold."
        ),
    )
    rsquared_threshold = fields.Float(
        default=0.80,
        help=(
            "Minimum R-squared for the regression test. Standard "
            "industry threshold is 0.80; some entities tighten to "
            "0.90 for material hedges."
        ),
    )
    test_ids = fields.One2many(
        'eh.fx.hedge.test', 'hedge_id', copy=False,
        help="Periodic effectiveness test results.",
    )
    last_test_date = fields.Date(
        compute='_compute_last_test', store=True,
        help="Date of the most recent effectiveness test.",
    )
    last_test_effective = fields.Boolean(
        compute='_compute_last_test', store=True,
        help=(
            "True when the most recent test passed. The state "
            "auto-flips to effective on a passing test and stays in "
            "designated otherwise."
        ),
    )

    # ---- accounts ----
    oci_account_id = fields.Many2one(
        'account.account',
        string="OCI / Equity Account",
        help=(
            "OCI reserve where the effective portion of CFH and NIH "
            "gains/losses accumulates. Typically an equity-class "
            "account named 'Cash Flow Hedge Reserve' or 'Foreign "
            "Currency Translation Reserve'. Required for CFH and "
            "NIH; ignored for FVH."
        ),
    )
    pl_account_id = fields.Many2one(
        'account.account',
        string="P&L Account",
        help=(
            "P&L account where the ineffective portion (and the "
            "entire FVH movement) is recognised. Typically the FX "
            "gain/loss account."
        ),
    )
    instrument_account_id = fields.Many2one(
        'account.account',
        string="Hedging Instrument Account",
        help=(
            "Balance-sheet account that carries the hedging "
            "instrument's fair value (e.g. derivative asset / "
            "liability for a forward, FX-loan account)."
        ),
    )
    hedged_item_account_id = fields.Many2one(
        'account.account',
        string="Hedged Item Account",
        help=(
            "Balance-sheet account carrying the hedged item (the "
            "recognised asset, liability, or firm commitment). For a "
            "fair-value hedge the effective portion adjusts this "
            "account's carrying amount for the hedged risk, so only "
            "the ineffective portion reaches P&L. Required for FVH; "
            "ignored for CFH and NIH."
        ),
    )
    cta_position_id = fields.Many2one(
        'eh.fx.cta.position', string="CTA Position",
        tracking=True,
        help=(
            "CTA reserve position of the hedged foreign operation "
            "(NIH only). When set, the effective portion posts to the "
            "position's CTA equity account and the entry is tagged to "
            "the position, so the position balance is ledger-fed and "
            "the parked amounts are recycled to P&L by disposing the "
            "position (IAS 21.48), not per movement."
        ),
    )
    journal_id = fields.Many2one(
        'account.journal', string="Journal",
        help="Journal used to post hedge movements.",
    )

    movement_ids = fields.One2many(
        'eh.fx.hedge.movement', 'hedge_id', copy=False,
        help="Periodic gain/loss recognition records.",
    )
    movement_count = fields.Integer(
        compute='_compute_counts', store=False,
    )
    test_count = fields.Integer(
        compute='_compute_counts', store=False,
    )

    notes = fields.Text(
        help=(
            "Hedge documentation: risk management objective, strategy, "
            "method of measuring effectiveness, sources of "
            "ineffectiveness. IFRS 9 requires this to be in place at "
            "designation."
        ),
    )

    @api.depends('test_ids.test_date', 'test_ids.is_effective')
    def _compute_last_test(self):
        for hedge in self:
            tests = hedge.test_ids.sorted('test_date', reverse=True)
            if tests:
                hedge.last_test_date = tests[0].test_date
                hedge.last_test_effective = tests[0].is_effective
            else:
                hedge.last_test_date = False
                hedge.last_test_effective = False

    @api.depends('movement_ids', 'test_ids')
    def _compute_counts(self):
        for hedge in self:
            hedge.movement_count = len(hedge.movement_ids)
            hedge.test_count = len(hedge.test_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code(
                    'eh.fx.hedge',
                ) or '/'
                vals['name'] = seq
        return super().create(vals_list)

    @api.constrains(
        'hedge_type', 'oci_account_id', 'pl_account_id',
        'hedged_item_account_id', 'cta_position_id',
    )
    def _check_account_setup(self):
        for hedge in self:
            if hedge.state == 'draft':
                continue
            if hedge.hedge_type in ('cash_flow', 'net_investment'):
                # NIH may source its OCI account from the linked CTA
                # position instead of a direct OCI account.
                has_reserve = hedge.oci_account_id or (
                    hedge.hedge_type == 'net_investment'
                    and hedge.cta_position_id
                )
                if not has_reserve:
                    raise ValidationError(_(
                        "Hedge %s requires an OCI / equity account "
                        "(or, for NIH, a CTA position) to receive the "
                        "effective portion of movements (CFH and NIH "
                        "route the effective portion through OCI per "
                        "IFRS 9).",
                    ) % hedge.display_name)
            if hedge.hedge_type == 'fair_value' \
                    and not hedge.hedged_item_account_id:
                raise ValidationError(_(
                    "Hedge %s is a fair-value hedge and requires a "
                    "Hedged Item Account: the effective portion adjusts "
                    "the hedged item's carrying amount, so only the "
                    "ineffective portion reaches P&L (IFRS 9 6.5.8).",
                ) % hedge.display_name)
            if not hedge.pl_account_id:
                raise ValidationError(_(
                    "Hedge %s requires a P&L account for the "
                    "ineffective portion (and for the full FVH "
                    "movement when applicable).",
                ) % hedge.display_name)

    @api.constrains('cta_position_id', 'company_id', 'hedge_type')
    def _check_cta_position(self):
        for hedge in self:
            if not hedge.cta_position_id:
                continue
            if hedge.hedge_type != 'net_investment':
                raise ValidationError(_(
                    "Hedge %s: only a net investment hedge can link a "
                    "CTA position. CFH and FVH effective portions do "
                    "not accumulate in the translation reserve.",
                ) % hedge.display_name)
            if hedge.cta_position_id.company_id != hedge.company_id:
                raise ValidationError(_(
                    "Hedge %(hedge)s and CTA position %(pos)s belong "
                    "to different companies.",
                    hedge=hedge.display_name,
                    pos=hedge.cta_position_id.display_name,
                ))

    def _eh_apply_effectiveness(self):
        """Sync the hedge state to its latest effectiveness test.

        A passing test on an active hedge qualifies it (state -> effective);
        a failing test suspends hedge accounting (effective -> designated).
        IFRS 9 permits the OCI / hedged-item deferral only while the hedge
        qualifies, so movement posting reads this state. Terminated and
        de-designated hedges are never auto-reactivated.
        """
        self = self._eh_workflow_action()
        for hedge in self:
            if hedge.state not in ('designated', 'effective'):
                continue
            if hedge.last_test_effective and hedge.state != 'effective':
                hedge.state = 'effective'
                hedge.message_post(body=_(
                    "Hedge qualifies: effectiveness test on %s passed.",
                ) % (hedge.last_test_date or ''))
            elif not hedge.last_test_effective and hedge.state == 'effective':
                hedge.state = 'designated'
                hedge.message_post(body=_(
                    "Hedge no longer qualifies: latest effectiveness test "
                    "failed. Hedge accounting is suspended until a passing "
                    "test is recorded; movements in the meantime recognise "
                    "the full change in P&L."
                ))

    # ---- actions ----

    def action_designate(self):
        self = self._eh_workflow_action()
        for hedge in self:
            if hedge.state != 'draft':
                raise UserError(_(
                    "Only draft hedges can be designated.",
                ))
            if not hedge.notes:
                raise UserError(_(
                    "Hedge %s needs documented risk management "
                    "objective and strategy in the Notes field "
                    "before designation. IFRS 9 paragraph 6.4.1(b) "
                    "requires this at inception.",
                ) % hedge.display_name)
            has_reserve = hedge.oci_account_id or (
                hedge.hedge_type == 'net_investment'
                and hedge.cta_position_id
            )
            if hedge.hedge_type in ('cash_flow', 'net_investment') \
                    and not has_reserve:
                raise ValidationError(_(
                    "Hedge %s requires an OCI / equity account (or, "
                    "for NIH, a CTA position) to receive the "
                    "effective portion of movements (CFH and NIH "
                    "route the effective portion through OCI per "
                    "IFRS 9).",
                ) % hedge.display_name)
            if hedge.hedge_type == 'fair_value' \
                    and not hedge.hedged_item_account_id:
                raise ValidationError(_(
                    "Hedge %s is a fair-value hedge and requires a "
                    "Hedged Item Account so the effective portion can "
                    "adjust the hedged item's carrying amount (IFRS 9 "
                    "6.5.8).",
                ) % hedge.display_name)
            if not hedge.pl_account_id:
                raise ValidationError(_(
                    "Hedge %s requires a P&L account for the "
                    "ineffective portion (and for the full FVH "
                    "movement when applicable).",
                ) % hedge.display_name)
            hedge.state = 'designated'
            hedge.message_post(body=_(
                "Hedge designated on %s.",
            ) % hedge.designation_date)

    def action_dedesignate(self):
        self = self._eh_workflow_action()
        for hedge in self:
            if hedge.state not in ('designated', 'effective'):
                raise UserError(_(
                    "Only active hedges can be de-designated.",
                ))
            hedge.write({
                'state': 'dedesignated',
                'termination_date': fields.Date.context_today(self),
            })
            hedge.message_post(body=_(
                "Hedge de-designated. Subsequent movements on the "
                "hedging instrument go straight to P&L; the OCI "
                "reserve stays until the original hedged transaction "
                "occurs (CFH) or the foreign operation is disposed "
                "(NIH)."
            ))

    def action_terminate(self):
        self = self._eh_workflow_action()
        for hedge in self:
            if hedge.state == 'terminated':
                continue
            hedge.write({
                'state': 'terminated',
                'termination_date': fields.Date.context_today(self),
            })

    def action_view_tests(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Effectiveness Tests"),
            'res_model': 'eh.fx.hedge.test',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('hedge_id', '=', self.id)],
            'context': {'default_hedge_id': self.id},
        }

    def action_view_movements(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Movements"),
            'res_model': 'eh.fx.hedge.movement',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('hedge_id', '=', self.id)],
            'context': {'default_hedge_id': self.id},
        }


class EhFxHedgeTest(models.Model):
    _name = 'eh.fx.hedge.test'
    _description = "Hedge effectiveness test"
    _order = 'hedge_id, test_date desc'

    hedge_id = fields.Many2one(
        'eh.fx.hedge', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        related='hedge_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='hedge_id.currency_id', store=True, readonly=True,
    )

    test_date = fields.Date(
        required=True, default=fields.Date.context_today,
    )
    method = fields.Selection(
        _TEST_METHODS, required=True, default='dollar_offset',
    )

    # Dollar-offset inputs
    cumulative_instrument_change = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Cumulative fair-value change of the hedging instrument "
            "since designation. Positive = gain on instrument."
        ),
    )
    cumulative_hedged_change = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Cumulative fair-value change of the hedged item since "
            "designation. Positive = gain on hedged item. For CFH "
            "this is the variability in expected future cash flows."
        ),
    )

    offset_ratio = fields.Float(
        compute='_compute_results', store=True, digits=(7, 4),
        help=(
            "Absolute ratio of |instrument change| to |hedged "
            "change|. Must fall within 0.80-1.25 to pass the "
            "dollar-offset test."
        ),
    )

    # Regression inputs (delimited list of paired changes)
    regression_pairs = fields.Text(
        help=(
            "Newline-delimited 'instrument_change,hedged_change' "
            "pairs across testing periods. Used by the regression "
            "method to compute R-squared."
        ),
    )
    rsquared = fields.Float(
        compute='_compute_results', store=True, digits=(7, 4),
        help="R-squared of the linear regression on regression_pairs.",
    )
    slope = fields.Float(
        compute='_compute_results', store=True, digits=(7, 4),
        help=(
            "Slope of the regression of hedged-item change on hedging-"
            "instrument change. An effective hedge offsets the hedged "
            "item, so the slope must be negative and close to -1 (its "
            "absolute value within 0.80 to 1.25). R-squared alone is "
            "sign-blind, so the slope guards against a positively "
            "correlated, non-hedging relationship."
        ),
    )

    is_effective = fields.Boolean(
        compute='_compute_results', store=True,
        help=(
            "True when the test passes. Dollar-offset: ratio in "
            "[0.80, 1.25]. Regression: R-squared >= threshold on "
            "the parent hedge."
        ),
    )
    notes = fields.Text()

    @api.depends(
        'method', 'cumulative_instrument_change',
        'cumulative_hedged_change', 'regression_pairs',
        'hedge_id.rsquared_threshold',
    )
    def _compute_results(self):
        for test in self:
            if test.method == 'dollar_offset':
                hedged = abs(test.cumulative_hedged_change or 0.0)
                inst = abs(test.cumulative_instrument_change or 0.0)
                test.slope = 0.0
                if hedged == 0:
                    test.offset_ratio = 0.0
                    test.rsquared = 0.0
                    test.is_effective = False
                    continue
                ratio = inst / hedged
                test.offset_ratio = round(ratio, 4)
                test.rsquared = 0.0
                test.is_effective = (0.80 <= ratio <= 1.25)
            else:
                pairs = test._parse_regression_pairs()
                test.offset_ratio = 0.0
                if len(pairs) < 2:
                    test.rsquared = 0.0
                    test.slope = 0.0
                    test.is_effective = False
                    continue
                rsq = test._compute_rsquared(pairs)
                slope = test._regression_slope(pairs)
                test.rsquared = round(rsq, 4)
                test.slope = round(slope, 4)
                threshold = (
                    test.hedge_id.rsquared_threshold or 0.80
                )
                # IFRS 9 regression effectiveness needs more than a high
                # R-squared: the slope must be negative and close to -1,
                # because the hedging instrument has to move opposite to
                # the hedged item. R-squared is sign-blind, so a positively
                # correlated (non-hedging) pair would otherwise pass at
                # R-squared near 1. The 0.80 to 1.25 band on the absolute
                # slope mirrors the dollar-offset band.
                test.is_effective = (
                    rsq >= threshold
                    and slope < 0.0
                    and 0.80 <= abs(slope) <= 1.25
                )

    @api.model_create_multi
    def create(self, vals_list):
        tests = super().create(vals_list)
        # Recording a test drives the parent hedge's qualification state so
        # movement posting can enforce that OCI / hedged-item deferral only
        # happens while a passing test is in force.
        tests.hedge_id._eh_apply_effectiveness()
        return tests

    def write(self, vals):
        res = super().write(vals)
        self.hedge_id._eh_apply_effectiveness()
        return res

    def _parse_regression_pairs(self):
        """Parse regression_pairs into a list of (x, y) tuples.

        Lines that do not parse cleanly are skipped silently so a
        stray blank line does not break the test.
        """
        self.ensure_one()
        out = []
        for line in (self.regression_pairs or '').splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(',')
            if len(parts) != 2:
                continue
            try:
                x = float(parts[0].strip())
                y = float(parts[1].strip())
            except ValueError:
                continue
            out.append((x, y))
        return out

    @staticmethod
    def _compute_rsquared(pairs):
        """Linear regression coefficient of determination.

        Pure Python (no numpy dep). Returns 0.0 when the variance of
        either series is zero so the test fails closed.
        """
        n = len(pairs)
        if n < 2:
            return 0.0
        sum_x = sum(x for x, _ in pairs)
        sum_y = sum(y for _, y in pairs)
        mean_x = sum_x / n
        mean_y = sum_y / n
        ss_xx = sum((x - mean_x) ** 2 for x, _ in pairs)
        ss_yy = sum((y - mean_y) ** 2 for _, y in pairs)
        ss_xy = sum(
            (x - mean_x) * (y - mean_y) for x, y in pairs
        )
        if ss_xx == 0 or ss_yy == 0:
            return 0.0
        r = ss_xy / ((ss_xx * ss_yy) ** 0.5)
        return r * r

    @staticmethod
    def _regression_slope(pairs):
        """Slope of the linear regression of hedged-item change (y) on
        hedging-instrument change (x).

        For an effective hedge the instrument offsets the hedged item, so
        the slope must be negative. Pure Python (no numpy). Returns 0.0
        when the instrument series has zero variance.
        """
        n = len(pairs)
        if n < 2:
            return 0.0
        mean_x = sum(x for x, _ in pairs) / n
        mean_y = sum(y for _, y in pairs) / n
        ss_xx = sum((x - mean_x) ** 2 for x, _ in pairs)
        ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
        if ss_xx == 0:
            return 0.0
        return ss_xy / ss_xx


class EhFxHedgeMovement(models.Model):
    _name = 'eh.fx.hedge.movement'
    _description = "Hedge movement (period gain/loss recognition)"
    _order = 'hedge_id, movement_date desc, id desc'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']

    # A movement reaches 'posted' / 'reclassified' only through action_post /
    # action_reclassify_to_pl, which attach its journal entry. Block any direct
    # write to state that does not originate from those (which flag the write).
    _eh_guarded_fields = ('state',)

    hedge_id = fields.Many2one(
        'eh.fx.hedge', required=True, ondelete='cascade', index=True,
    )
    # Surfaced as a direct related field so view modifiers can
    # reference it without a dotted path (Odoo 16 rejects composed
    # fields in attrs).
    hedge_type = fields.Selection(
        related='hedge_id.hedge_type', store=True, readonly=True,
    )
    company_id = fields.Many2one(
        related='hedge_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='hedge_id.currency_id', store=True, readonly=True,
    )

    movement_date = fields.Date(
        required=True, default=fields.Date.context_today,
        tracking=True,
    )
    total_change = fields.Monetary(
        required=True, currency_field='currency_id', tracking=True,
        help=(
            "Total fair-value change of the hedging instrument for "
            "this period (positive = gain on instrument)."
        ),
    )
    effective_portion = fields.Monetary(
        required=True, currency_field='currency_id', tracking=True,
        help=(
            "Portion considered effective per the most recent "
            "effectiveness test. Goes to OCI for CFH and NIH; for FVH "
            "it adjusts the hedged item's carrying amount (the hedged "
            "item account), leaving only the ineffective portion in "
            "P&L. Deferring a non-zero effective portion requires a "
            "passing effectiveness test in force for the period."
        ),
    )
    ineffective_portion = fields.Monetary(
        compute='_compute_ineffective', store=True,
        currency_field='currency_id',
        help="total_change minus effective_portion. Always to P&L.",
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('posted', "Posted"),
            ('reclassified', "Reclassified to P&L"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        help="JE posted for the period gain/loss.",
    )
    reclassification_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        help=(
            "JE that reclassifies the OCI portion to P&L when the "
            "hedged transaction occurs (CFH) or the foreign "
            "operation is disposed (NIH)."
        ),
    )

    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)

    notes = fields.Text()

    @api.depends('total_change', 'effective_portion')
    def _compute_ineffective(self):
        for mvt in self:
            mvt.ineffective_portion = (
                (mvt.total_change or 0.0)
                - (mvt.effective_portion or 0.0)
            )

    @api.constrains('effective_portion', 'total_change')
    def _check_effective_le_total(self):
        for mvt in self:
            currency = mvt.currency_id or mvt.company_id.currency_id
            effective = abs(mvt.effective_portion or 0.0)
            total = abs(mvt.total_change or 0.0)
            exceeds = (
                currency.compare_amounts(effective, total) > 0
                if currency else effective > total + 0.01
            )
            if exceeds:
                raise ValidationError(_(
                    "Effective portion %.2f cannot exceed total "
                    "change %.2f in absolute value.",
                ) % (mvt.effective_portion, mvt.total_change))

    @api.constrains('effective_portion', 'total_change')
    def _check_effective_sign(self):
        for mvt in self:
            eff = mvt.effective_portion or 0.0
            tot = mvt.total_change or 0.0
            if eff and tot and (eff > 0) != (tot > 0):
                raise ValidationError(_(
                    "The effective portion must have the same sign as the "
                    "total change: the effective part of an instrument gain "
                    "is a gain, of a loss a loss. Got effective %.2f against "
                    "total %.2f.",
                ) % (eff, tot))

    # ---- post ----

    def action_post(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only accounting managers can post hedge movements. "
                "Posting recognises OCI / P&L hedge accounting entries "
                "and is a segregation-of-duties control.",
            ))
        self = self._eh_workflow_action()
        for mvt in self:
            if mvt.state != 'draft':
                raise UserError(_(
                    "Only draft movements can be posted.",
                ))
            hedge = mvt.hedge_id
            if not hedge.journal_id or not hedge.instrument_account_id:
                raise UserError(_(
                    "Hedge %s needs journal and instrument account "
                    "configured before posting movements.",
                ) % hedge.display_name)
            if hedge.hedge_type == 'fair_value' \
                    and not hedge.hedged_item_account_id:
                raise UserError(_(
                    "Fair-value hedge %s needs a Hedged Item Account before "
                    "posting: the effective portion adjusts the hedged "
                    "item's carrying amount.",
                ) % hedge.display_name)
            if hedge.hedge_type == 'net_investment' \
                    and hedge.cta_position_id \
                    and hedge.cta_position_id.state == 'disposed':
                raise UserError(_(
                    "NIH %(hedge)s points at CTA position %(pos)s, "
                    "which is disposed: its reserve was already "
                    "reclassified to P&L in full (IAS 21.48), so no "
                    "further effective portion can be parked there.",
                    hedge=hedge.display_name,
                    pos=hedge.cta_position_id.display_name,
                ))
            # IFRS 9: the OCI (CFH/NIH) or hedged-item (FVH) deferral of the
            # effective portion is only permitted while the hedge qualifies.
            # A non-zero effective portion therefore requires a passing
            # effectiveness test dated on or before this movement. Otherwise
            # the movement must recognise the full change in P&L (effective
            # portion 0), which is exactly the de-designated treatment.
            if mvt.effective_portion:
                qualifying = hedge.test_ids.filtered(
                    lambda t: t.is_effective
                    and t.test_date <= mvt.movement_date
                )
                if hedge.state != 'effective' or not qualifying:
                    raise UserError(_(
                        "Hedge %(name)s does not currently qualify for hedge "
                        "accounting, so the effective portion cannot be "
                        "deferred to OCI or the hedged item. Record a passing "
                        "effectiveness test dated on or before %(date)s, or "
                        "set the effective portion to 0 to recognise the full "
                        "change in P&L.",
                        name=hedge.display_name, date=mvt.movement_date,
                    ))
            move = mvt._build_move()
            mvt.write({
                'state': 'posted',
                'move_id': move.id,
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
            })
        return True

    def _build_move(self):
        """Post the period gain/loss with the effective / ineffective split.

        For CFH and NIH:
          Dr/Cr instrument (total_change)
          Cr/Dr OCI account (effective_portion)
          Cr/Dr P&L account (ineffective_portion)

        For FVH the effective portion adjusts the HEDGED ITEM's carrying
        amount for the hedged risk, not P&L:
          Dr/Cr instrument (total_change)
          Cr/Dr hedged item account (effective_portion)
          Cr/Dr P&L account (ineffective_portion)
        so the net P&L effect is the ineffective portion only, which is
        the whole point of a fair-value hedge (IFRS 9 6.5.8). Routing the
        effective portion to the hedged item is what stops an FVH being
        booked as if the exposure were unhedged.
        """
        self.ensure_one()
        hedge = self.hedge_id
        # Sign convention: total_change > 0 means we have a gain on
        # the hedging instrument. Debit the instrument balance-sheet
        # account; credit the gain across OCI + P&L per the type.
        amount = self.total_change
        is_gain = amount >= 0
        absamount = abs(amount)
        eff_abs = abs(self.effective_portion or 0.0)
        ineff_abs = abs(self.ineffective_portion or 0.0)
        label = _("Hedge movement %s on %s") % (
            hedge.name, self.movement_date,
        )
        lines = []
        # Hedging instrument leg.
        if is_gain:
            lines.append((0, 0, {
                'name': label,
                'account_id': hedge.instrument_account_id.id,
                'debit': absamount,
                'credit': 0.0,
            }))
        else:
            lines.append((0, 0, {
                'name': label,
                'account_id': hedge.instrument_account_id.id,
                'debit': 0.0,
                'credit': absamount,
            }))
        # Effective portion: OCI for CFH/NIH, the hedged item's carrying
        # amount for FVH. Only the ineffective portion reaches P&L. A
        # position-linked NIH books its OCI leg on the position's CTA
        # equity account so the reserve balance is ledger-fed.
        cta_position = (
            hedge.cta_position_id
            if hedge.hedge_type == 'net_investment' else
            self.env['eh.fx.cta.position']
        )
        if cta_position:
            eff_account = cta_position.cta_account_id
        elif hedge.hedge_type in ('cash_flow', 'net_investment'):
            eff_account = hedge.oci_account_id
        else:
            eff_account = hedge.hedged_item_account_id
        if eff_abs > 0:
            if is_gain:
                lines.append((0, 0, {
                    'name': label,
                    'account_id': eff_account.id,
                    'debit': 0.0,
                    'credit': eff_abs,
                }))
            else:
                lines.append((0, 0, {
                    'name': label,
                    'account_id': eff_account.id,
                    'debit': eff_abs,
                    'credit': 0.0,
                }))
        # Ineffective portion: always P&L.
        if ineff_abs > 0:
            if is_gain:
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.pl_account_id.id,
                    'debit': 0.0,
                    'credit': ineff_abs,
                }))
            else:
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.pl_account_id.id,
                    'debit': ineff_abs,
                    'credit': 0.0,
                }))
        move_vals = {
            'move_type': 'entry',
            'journal_id': hedge.journal_id.id,
            'date': self.movement_date,
            'ref': "%s / %s" % (hedge.name, self.id),
            'line_ids': lines,
            'eh_sealed': True,
        }
        if cta_position:
            move_vals['eh_cta_position_id'] = cta_position.id
        move = self.env['account.move'].create(move_vals)
        move.action_post()
        if cta_position:
            # The position balance is a stored-free ledger compute; drop
            # the cached value so the freshly posted OCI leg is visible.
            cta_position.invalidate_recordset(['balance'])
        return move

    def action_reclassify_to_pl(self):
        """Reclassify the effective portion from OCI to P&L.

        Used when:
        * CFH: the forecast transaction occurs (sale recognised,
          purchase recognised). The OCI reserve flips to revenue or
          COGS.
        * NIH: the foreign operation is disposed of. The CTA reserve
          flips to gain/loss on disposal.

        Only applies to movements that originally posted to OCI.
        FVH movements never went to OCI so this action is a no-op.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only accounting managers can reclassify hedge OCI to "
                "P&L. Reclassification recognises deferred amounts into "
                "profit or loss and is a segregation-of-duties control.",
            ))
        self = self._eh_workflow_action()
        for mvt in self:
            if mvt.state != 'posted':
                raise UserError(_(
                    "Only posted movements can be reclassified.",
                ))
            hedge = mvt.hedge_id
            if hedge.hedge_type == 'fair_value':
                raise UserError(_(
                    "Fair-value hedge movements never post to OCI; "
                    "no reclassification needed.",
                ))
            if hedge.hedge_type == 'net_investment' \
                    and hedge.cta_position_id:
                raise UserError(_(
                    "NIH %(hedge)s parks its effective portion in CTA "
                    "position %(pos)s. IAS 21.48 recycles the FULL "
                    "accumulated reserve (including these amounts) on "
                    "disposal of the foreign operation: use the "
                    "Dispose action on the position instead of "
                    "reclassifying per movement.",
                    hedge=hedge.display_name,
                    pos=hedge.cta_position_id.display_name,
                ))
            eff_abs = abs(mvt.effective_portion or 0.0)
            if eff_abs == 0:
                raise UserError(_(
                    "Movement %s has zero effective portion; "
                    "nothing to reclassify.",
                ) % mvt.id)
            label = _("OCI reclassification %s on %s") % (
                hedge.name, fields.Date.context_today(self),
            )
            # Reverse the OCI leg, recognise into P&L.
            is_gain = (mvt.total_change or 0.0) >= 0
            lines = []
            if is_gain:
                # Original posted: Cr OCI. Reclassification: Dr OCI,
                # Cr P&L (recognise as income).
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.oci_account_id.id,
                    'debit': eff_abs,
                    'credit': 0.0,
                }))
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.pl_account_id.id,
                    'debit': 0.0,
                    'credit': eff_abs,
                }))
            else:
                # Original posted: Dr OCI. Reclassification: Cr OCI,
                # Dr P&L (recognise as expense).
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.oci_account_id.id,
                    'debit': 0.0,
                    'credit': eff_abs,
                }))
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.pl_account_id.id,
                    'debit': eff_abs,
                    'credit': 0.0,
                }))
            recl_move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': hedge.journal_id.id,
                'date': fields.Date.context_today(self),
                'ref': "Reclass %s / %s" % (hedge.name, mvt.id),
                'line_ids': lines,
                'eh_sealed': True,
            })
            recl_move.action_post()
            mvt.with_context(eh_hedge_state_change=True).write({
                'state': 'reclassified',
                'reclassification_move_id': recl_move.id,
            })
        return True

    # ---- freeze-after-post ----

    # Measurement inputs that determine the posted journal entry and the
    # audit figures. Once a movement has posted, its journal entry is live
    # and these figures are the recognised gain/loss split; a direct write
    # to any of them would silently desynchronise the audit record from the
    # posted JE. They are frozen from that point on.
    _EH_FROZEN_MOVEMENT_FIELDS = (
        'total_change',
        'effective_portion',
        'movement_date',
        'hedge_id',
    )

    @api.model_create_multi
    def create(self, vals_list):
        # A movement is always born in draft and only reaches posted /
        # reclassified through action_post / action_reclassify_to_pl, which
        # attach its journal entry. Creating one already in a posted state
        # would fabricate a finalised figure with no journal entry behind it,
        # so refuse it at the ORM layer regardless of ACL create rights.
        if not self.env.context.get('eh_hedge_state_change'):
            for v in vals_list:
                if v.get('state') and v['state'] != 'draft':
                    raise UserError(_(
                        "A hedge movement is created in draft and reaches a "
                        "posted state only through Post / Reclassify, which "
                        "attach its journal entry. It cannot be created "
                        "directly in the %s state.", v['state']))
        return super().create(vals_list)

    def write(self, vals):
        # Block edits to posted measurement inputs on any movement that has
        # already left draft. The state-transition writes issued by
        # action_post / action_reclassify_to_pl move a still-draft record
        # forward and set only bookkeeping fields, so they are unaffected.
        touched = [f for f in self._EH_FROZEN_MOVEMENT_FIELDS if f in vals]
        if touched:
            for mvt in self:
                if mvt.state in ('posted', 'reclassified'):
                    raise UserError(_(
                        "Movement %(name)s is %(state)s: its measurement "
                        "inputs are frozen because they drive the posted "
                        "journal entry and the audit figures. Reverse the "
                        "posted entry and recreate the movement to correct "
                        "%(fields)s.",
                        name=mvt.hedge_id.name or mvt.id,
                        state=mvt.state,
                        fields=", ".join(touched),
                    ))
        # The state of a posted / reclassified movement is itself a control
        # point: resetting it to draft would silently lift the figure freeze
        # above and orphan the posted journal entry. A raw ORM 'state' write
        # that moves such a movement OUT of its posted / reclassified state,
        # without the sanctioned-transition context flag, is manager-gated so a
        # plain user cannot un-freeze a GL-backed movement. The legitimate
        # transitions (action_post moves a draft record; action_reclassify_to_pl
        # sets the flag) are unaffected.
        if 'state' in vals \
                and not self.env.context.get('eh_hedge_state_change'):
            confirmed = self.filtered(
                lambda m: m.state in ('posted', 'reclassified'))
            crossing = confirmed.filtered(lambda m: m.state != vals['state'])
            if crossing:
                crossing._check_manager()
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager may change the state of a "
                "posted or reclassified hedge movement."))

    def unlink(self):
        posted = self.filtered(
            lambda m: m.state in ('posted', 'reclassified'))
        if posted:
            raise UserError(_(
                "A posted or reclassified hedge movement cannot be deleted; "
                "it carries a posted GL entry (the period gain/loss and, once "
                "reclassified, the OCI-to-P&L transfer). Reverse the entry "
                "instead."))
        return super().unlink()
