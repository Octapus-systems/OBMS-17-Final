# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.deferred.tax.line: one temporary difference on a deferred tax run.

A temporary difference is the gap between the carrying amount of an asset or
liability in the financial statements and its tax base (IAS 12.5). The line
classifies the gap as taxable or deductible from the item's nature, applies
the enacted rate to give the closing deferred tax asset or liability, and
carries the opening position so the run can post only the period movement.

Sign convention (IAS 12.15-24):
* Asset, carrying > tax base  -> taxable temporary difference -> DTL.
* Asset, carrying < tax base  -> deductible temporary difference -> DTA.
* Liability is the mirror of an asset.
* Tax loss / credit carried forward is a deductible amount in its own right
  and produces a DTA directly, subject to the recoverability flag.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_FROZEN_RUN_STATES = frozenset({'posted', 'reversed'})
_LOCKED_LINE_FIELDS = frozenset({
    'carrying_amount', 'tax_base', 'tax_rate', 'nature', 'recoverable',
    'recoverable_amount', 'through_oci', 'opening_dta', 'opening_dtl',
    'run_id', 'jurisdiction_id', 'manual_rate', 'manual_rate_reason',
    'opening_rate', 'expiry_date',
})


class EhDeferredTaxLine(models.Model):
    _name = 'eh.deferred.tax.line'
    _description = "Deferred tax temporary difference"
    _order = 'run_id, sequence, id'

    run_id = fields.Many2one(
        'eh.deferred.tax.run', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True,
    )

    name = fields.Char(
        required=True,
        help="Description of the temporary difference, e.g. 'Accelerated tax "
             "depreciation on plant' or 'Warranty provision'.",
    )
    category = fields.Selection(
        [
            ('depreciation', "Depreciation / PPE"),
            ('provision', "Provision"),
            ('revenue', "Revenue timing"),
            ('receivable_ecl', "Receivable impairment / ECL"),
            ('inventory_nrv', "Inventory write-down"),
            ('tax_loss', "Tax loss / credit carried forward"),
            ('other', "Other"),
        ],
        default='other', required=True,
    )
    nature = fields.Selection(
        [
            ('asset', "Asset"),
            ('liability', "Liability"),
            ('tax_loss', "Tax loss / credit"),
        ],
        default='asset', required=True,
        help="Drives how the carrying-versus-tax-base gap is classified. A "
             "tax loss is a deductible amount in its own right.",
    )

    carrying_amount = fields.Monetary(
        currency_field='currency_id',
        help="Accounting carrying amount of the asset or liability. For a "
             "tax loss / credit, enter the amount of the loss available.",
    )
    tax_base = fields.Monetary(
        currency_field='currency_id',
        help="Amount attributed to the asset or liability for tax purposes "
             "(IAS 12.7-8). Ignored for a tax loss / credit.",
    )
    temp_diff = fields.Monetary(
        compute='_compute_amounts', store=True, currency_field='currency_id',
        help="Carrying amount less tax base (positive or negative).",
    )
    taxable_diff = fields.Monetary(
        compute='_compute_amounts', store=True, currency_field='currency_id',
    )
    deductible_diff = fields.Monetary(
        compute='_compute_amounts', store=True, currency_field='currency_id',
    )

    jurisdiction_id = fields.Many2one(
        'eh.tax.jurisdiction', string="Jurisdiction",
        ondelete='restrict', index=True,
        help="Taxation authority whose enacted-rate table measures this "
             "difference (IAS 12.47). Defaults to the company jurisdiction, "
             "auto-created on first use. Under the net offsetting policy the "
             "run nets DTA against DTL per jurisdiction (IAS 12.74).",
    )
    tax_rate = fields.Float(
        digits=(6, 3),
        help="Enacted / substantively enacted tax rate applied to this "
             "difference, as a percentage. Compute resolves it in order: "
             "the manual override, then the jurisdiction rate table at the "
             "run's reporting date, then (only when still empty) the run's "
             "statutory rate. A value keyed here directly is preserved when "
             "no override or table row applies.",
    )
    manual_rate = fields.Float(
        digits=(6, 3), string="Manual Rate Override",
        help="Overrides the jurisdiction rate table and the run statutory "
             "rate for this line on every compute, e.g. a difference taxed "
             "at a special-regime rate. Requires a reason. Zero means no "
             "override.",
    )
    manual_rate_reason = fields.Char(
        string="Override Reason",
        help="Why this line is measured at a rate other than the "
             "jurisdiction's enacted rate. Required with a manual override.",
    )
    recoverable = fields.Boolean(
        default=True,
        help="Whether the deferred tax asset is expected to be recovered "
             "(IAS 12.24/34). When unset, the DTA on this line is excluded "
             "from the closing position and the posting.",
    )
    recoverable_amount = fields.Monetary(
        currency_field='currency_id', default=0.0,
        help="Projected future taxable profit available to recover this "
             "deductible difference (IAS 12.24/34). The DTA is recognised "
             "only to that extent: the deductible difference is capped at "
             "this amount before applying the tax rate. Leave at zero to "
             "recognise the full DTA (no profit constraint).",
    )
    expiry_date = fields.Date(
        string="Carryforward Expiry",
        help="Date the tax loss / credit carryforward (or other deductible "
             "amount) expires under the tax law. A carryforward expired on "
             "or before the run's reporting date can no longer produce "
             "future deductions, so its deferred tax asset is derecognised "
             "and disclosed as unrecognised (IAS 12.36(a)). Empty means no "
             "expiry.",
    )
    through_oci = fields.Boolean(
        string="Recognised in OCI",
        help="Set when the underlying item is recognised in other "
             "comprehensive income (e.g. revaluation surplus, cash-flow "
             "hedge reserve). The deferred tax movement then routes to OCI "
             "rather than profit or loss (IAS 12.61A).",
    )
    eh_auto_gathered = fields.Boolean(
        string="Gathered from an IFRS engine", readonly=True, copy=False,
        help="Set when this line was pulled automatically from a producer "
             "engine (ECL, provisions, ...) by Gather from Engines. Re-running "
             "the gather replaces these lines; hand-keyed lines are untouched.",
    )

    closing_dta = fields.Monetary(
        compute='_compute_amounts', store=True, currency_field='currency_id',
        help="Closing deferred tax asset on this difference, after any "
             "recoverability cap.",
    )
    unrecognised_dta = fields.Monetary(
        compute='_compute_amounts', store=True, currency_field='currency_id',
        help="Deferred tax asset NOT recognised because the deductible "
             "difference exceeds the projected recoverable amount, or the "
             "line is flagged not recoverable (disclosure, IAS 12.81(e)).",
    )
    closing_dtl = fields.Monetary(
        compute='_compute_amounts', store=True, currency_field='currency_id',
        help="Closing deferred tax liability on this difference.",
    )
    opening_dta = fields.Monetary(
        currency_field='currency_id',
        help="Deferred tax asset recognised on this line at the start of the "
             "period. The run posts only the movement (closing less opening).",
    )
    opening_dtl = fields.Monetary(
        currency_field='currency_id',
        help="Deferred tax liability recognised on this line at the start of "
             "the period.",
    )
    movement_dta = fields.Monetary(
        compute='_compute_movement', store=True, currency_field='currency_id',
    )
    movement_dtl = fields.Monetary(
        compute='_compute_movement', store=True, currency_field='currency_id',
    )

    # ---- IAS 12.47/60 rate-change remeasurement disclosure ----
    opening_rate = fields.Float(
        digits=(6, 3),
        help="Rate at which the opening balance was measured, as a "
             "percentage. Rolled forward from the prior posted run's "
             "applied rate, or keyed with a manual opening. Zero means "
             "unknown: no rate-change component is computed and the whole "
             "movement is treated as origination / reversal, the "
             "pre-remeasurement behaviour.",
    )
    closing_rate = fields.Float(
        compute='_compute_rate_change', store=True, digits=(6, 3),
        help="Rate applied at the reporting date (the line's applied tax "
             "rate); disclosed against the opening rate.",
    )
    rate_change_effect = fields.Monetary(
        compute='_compute_rate_change', store=True,
        currency_field='currency_id',
        help="Portion of the period movement caused by remeasuring the "
             "OPENING balance at the closing rate (IAS 12.60(b)): opening "
             "temporary difference times (closing rate less opening rate). "
             "Signed on the net liability: positive increases the net DTL "
             "(a charge). Routed to profit or loss, or to OCI when the "
             "line is flagged Recognised in OCI (IAS 12.61A); disclosed "
             "separately from origination in the reconciliation.",
    )
    origination_effect = fields.Monetary(
        compute='_compute_rate_change', store=True,
        currency_field='currency_id',
        help="Remainder of the period movement after the rate-change "
             "component: origination and reversal of temporary differences "
             "at constant rates, signed on the net liability.",
    )

    expected_opening_dta = fields.Monetary(
        compute='_compute_expected_opening', currency_field='currency_id',
        help="Deferred tax asset carried forward from the prior posted run's "
             "closing position for the same difference. The opening should "
             "tie to this figure.",
    )
    expected_opening_dtl = fields.Monetary(
        compute='_compute_expected_opening', currency_field='currency_id',
        help="Deferred tax liability carried forward from the prior posted "
             "run's closing position for the same difference.",
    )
    opening_tie_out = fields.Boolean(
        compute='_compute_expected_opening',
        help="Set when a prior posted run exists for this difference and the "
             "entered opening does not tie to that run's closing position. A "
             "keying error in the opening silently mis-states the period "
             "movement, so investigate before posting.",
    )

    _sql_constraints = [
        ('check_carrying_non_negative', 'CHECK (carrying_amount >= 0)', 'Carrying amount cannot be negative.'),
    ]

    @api.depends(
        'nature', 'carrying_amount', 'tax_base', 'tax_rate', 'recoverable',
        'recoverable_amount', 'expiry_date', 'run_id.period_end',
    )
    def _compute_amounts(self):
        for line in self:
            rate = (line.tax_rate or 0.0) / 100.0
            if line.nature == 'tax_loss':
                line.temp_diff = 0.0
                line.taxable_diff = 0.0
                line.deductible_diff = line.carrying_amount
            else:
                diff = line.carrying_amount - line.tax_base
                line.temp_diff = diff
                # For an asset a positive gap is taxable; for a liability the
                # relationship is inverted.
                if line.nature == 'asset':
                    line.taxable_diff = max(diff, 0.0)
                    line.deductible_diff = max(-diff, 0.0)
                else:
                    line.taxable_diff = max(-diff, 0.0)
                    line.deductible_diff = max(diff, 0.0)
            line.closing_dtl = line.taxable_diff * rate
            # IAS 12.24/34: a DTA is recognised only to the extent that future
            # taxable profit is probably available. recoverable_amount caps the
            # deductible difference; zero means unconstrained (full DTA), so
            # existing runs that never set a cap are unaffected. The Boolean is
            # a hard off-switch layered on top.
            # A carryforward expired on or before the reporting date can no
            # longer be deducted against any future profit, so nothing is
            # recognised regardless of the other flags (IAS 12.36(a)).
            expired = bool(
                line.expiry_date and line.run_id.period_end
                and line.expiry_date <= line.run_id.period_end)
            if expired or not line.recoverable:
                recognised_diff = 0.0
            elif line.recoverable_amount > 0.0:
                recognised_diff = min(
                    line.deductible_diff, line.recoverable_amount)
            else:
                recognised_diff = line.deductible_diff
            line.closing_dta = recognised_diff * rate
            line.unrecognised_dta = (line.deductible_diff - recognised_diff) \
                * rate

    @api.depends(
        'closing_dta', 'closing_dtl', 'opening_dta', 'opening_dtl',
    )
    def _compute_movement(self):
        for line in self:
            line.movement_dta = line.closing_dta - (line.opening_dta or 0.0)
            line.movement_dtl = line.closing_dtl - (line.opening_dtl or 0.0)

    @api.depends(
        'opening_dta', 'opening_dtl', 'opening_rate', 'tax_rate',
        'movement_dta', 'movement_dtl',
    )
    def _compute_rate_change(self):
        """Split the period movement into rate-change vs origination.

        Rate-change component (IAS 12.60(b)): restate each opening balance
        at the closing rate. An opening DTL of B measured at rate r_o is
        the temporary difference B / r_o; at the closing rate r_c it
        becomes B x r_c / r_o, so the effect is B x (r_c / r_o - 1), which
        equals opening temporary difference x (r_c - r_o). The DTA side
        enters with the opposite sign so the effect is stated on the net
        liability (positive = charge). When either rate is unknown (zero)
        the component is nil and the whole movement is origination, which
        is the pre-remeasurement behaviour.
        """
        for line in self:
            r_open = line.opening_rate or 0.0
            r_close = line.tax_rate or 0.0
            effect = 0.0
            if r_open and r_close and r_open != r_close:
                factor = r_close / r_open - 1.0
                effect = ((line.opening_dtl or 0.0) * factor
                          - (line.opening_dta or 0.0) * factor)
            line.closing_rate = r_close
            line.rate_change_effect = effect
            line.origination_effect = (
                line.movement_dtl - line.movement_dta - effect)

    def _resolve_rate(self):
        """Applied rate for this line at the run's reporting date.

        Order: manual override (reasoned), then the jurisdiction enacted-
        rate table at the reporting date, then the run's statutory rate as
        the fallback (the pre-table behaviour when no rows resolve).
        """
        self.ensure_one()
        if self.manual_rate:
            return self.manual_rate
        if self.jurisdiction_id:
            table_rate = self.jurisdiction_id.rate_at(self.run_id.period_end)
            if table_rate is not None:
                return table_rate
        return self.run_id.statutory_rate

    @api.constrains('manual_rate', 'manual_rate_reason')
    def _check_manual_rate_reason(self):
        for line in self:
            if line.manual_rate and not (line.manual_rate_reason or '').strip():
                raise ValidationError(_(
                    "Line %s carries a manual rate override; record the "
                    "reason for departing from the jurisdiction's enacted "
                    "rate.", line.display_name))
            if line.manual_rate < 0.0 or line.manual_rate > 100.0:
                raise ValidationError(_(
                    "Manual rate override must be between 0 and 100."))

    @api.depends(
        'name', 'opening_dta', 'opening_dtl',
        'run_id.company_id', 'run_id.period_end', 'run_id.state',
    )
    def _compute_expected_opening(self):
        for line in self:
            prior = line._prior_closing()
            line.expected_opening_dta = prior['dta']
            line.expected_opening_dtl = prior['dtl']
            currency = line.currency_id
            if not prior['found'] or not currency:
                line.opening_tie_out = False
                continue
            line.opening_tie_out = not (
                currency.is_zero((line.opening_dta or 0.0) - prior['dta'])
                and currency.is_zero((line.opening_dtl or 0.0) - prior['dtl']))

    def _prior_closing(self):
        """Closing DTA/DTL of the same difference on the prior posted run.

        The natural key of a temporary difference across periods is its name
        within a company. The prior run is the most recent posted run for the
        company dated before this run's reporting date. Returns a dict with the
        carried-forward closing figures and whether such a prior line exists.
        """
        self.ensure_one()
        run = self.run_id
        empty = {'found': False, 'dta': 0.0, 'dtl': 0.0, 'rate': 0.0}
        if not run or not run.company_id or not run.period_end or not self.name:
            return empty
        prior_run = self.env['eh.deferred.tax.run'].search([
            ('company_id', '=', run.company_id.id),
            ('state', '=', 'posted'),
            ('period_end', '<', run.period_end),
        ], order='period_end desc, id desc', limit=1)
        if not prior_run:
            return empty
        prior_line = prior_run.line_ids.filtered(
            lambda l: l.name == self.name)[:1]
        if not prior_line:
            return empty
        return {
            'found': True,
            'dta': prior_line.closing_dta,
            'dtl': prior_line.closing_dtl,
            'rate': prior_line.tax_rate,
        }

    # -- Integrity: freeze the figures once the movement is posted ----------

    @api.model_create_multi
    def create(self, vals_list):
        runs = self.env['eh.deferred.tax.run'].browse(
            [v.get('run_id') for v in vals_list if v.get('run_id')])
        if any(r.state in _FROZEN_RUN_STATES for r in runs):
            raise UserError(_(
                "Cannot add lines to a deferred tax run that is already "
                "posted or reversed."))
        # Default jurisdiction: the company jurisdiction, auto-created on
        # first use (IAS 12.74 offsetting and the enacted-rate table group
        # by jurisdiction). Existing rows without one keep the statutory
        # fallback until recomputed.
        Jurisdiction = self.env['eh.tax.jurisdiction']
        defaults = {}
        for vals in vals_list:
            if vals.get('jurisdiction_id') or not vals.get('run_id'):
                continue
            company = self.env['eh.deferred.tax.run'].browse(
                vals['run_id']).company_id
            if company.id not in defaults:
                defaults[company.id] = Jurisdiction._get_company_default(
                    company).id
            vals['jurisdiction_id'] = defaults[company.id]
        return super().create(vals_list)

    def write(self, vals):
        if _LOCKED_LINE_FIELDS.intersection(vals) and any(
            r.state in _FROZEN_RUN_STATES for r in self.run_id
        ):
            raise UserError(_(
                "Deferred tax figures are locked once the run is posted or "
                "reversed; they are the basis of a posted movement. Reverse "
                "the run to reopen it."))
        # Block moving a line INTO a posted / reversed run. The lock above only
        # inspects the current (source) parent; without this a plain user could
        # create a line on a draft run then re-point run_id at a posted run,
        # re-triggering its stored computes and mutating a frozen figure.
        if vals.get('run_id'):
            target = self.env['eh.deferred.tax.run'].browse(vals['run_id'])
            if target.state in _FROZEN_RUN_STATES:
                raise UserError(_(
                    "Cannot move a line into a deferred tax run that is "
                    "already posted or reversed."))
        return super().write(vals)

    def unlink(self):
        if any(r.state in _FROZEN_RUN_STATES for r in self.run_id):
            raise UserError(_(
                "Cannot delete lines of a posted or reversed deferred tax "
                "run."))
        return super().unlink()
