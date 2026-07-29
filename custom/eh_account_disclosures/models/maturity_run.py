# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 7.39 contractual-maturity analysis driven from the general ledger.

The register in eh.fin.risk is entered by hand. This model instead reads the
open (unreconciled, posted) move lines on a chosen set of financial-liability
accounts and buckets them into contractual-maturity bands relative to a
reporting date, so the liquidity maturity analysis ties back to the GL.
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Parent figures frozen once the run is finalised. Writing any of these on a
# finalised run is refused so a signed-off maturity analysis cannot be
# silently re-keyed or re-populated.
_MATURITY_FROZEN_FIELDS = frozenset({
    'reporting_date', 'annual_interest_rate', 'account_ids',
    'instrument_ids', 'line_ids', 'company_id', 'band_scheme',
    'include_open_items', 'include_leases',
})

# Ordered bands so the child lines read on-demand -> longest. The list is
# the union of both band schemes; each scheme populates only its own five
# bands (see _SCHEME_BANDS), and the ordering here drives band_sequence so
# mixed-scheme data still lists shortest-first.
MATURITY_BANDS = [
    ('on_demand', "On demand"),
    ('d0_30', "0-30 days"),
    ('lt_3m', "Under 3 months"),
    ('d31_90', "31-90 days"),
    ('3m_1y', "3 months to 1 year"),
    ('d91_365', "91-365 days"),
    ('1y_5y', "1 to 5 years"),
    ('gt_5y', "Over 5 years"),
]

# The five bands each scheme buckets into. 'contractual' is the original
# IFRS 7.39 presentation and stays the default so existing runs repopulate
# byte-identically; 'days' is the day-count presentation (overdue and
# on-demand items collapse into the first band).
_SCHEME_BANDS = {
    'contractual': ['on_demand', 'lt_3m', '3m_1y', '1y_5y', 'gt_5y'],
    'days': ['d0_30', 'd31_90', 'd91_365', '1y_5y', 'gt_5y'],
}

# Classes an extracted band row can belong to (IFRS 7.39 requires the
# analysis by class of financial instrument, IFRS 7.6).
MATURITY_ITEM_CLASSES = [
    ('liability', "Financial liabilities (selected accounts)"),
    ('instrument', "Interest-bearing instruments"),
    ('receivable', "Trade receivables (open items)"),
    ('payable', "Trade payables (open items)"),
    ('lease', "Lease liabilities (IFRS 16 schedules)"),
    ('manual', "Manual"),
]


class EhFinMaturityRun(models.Model):
    _name = 'eh.fin.maturity.run'
    _description = "Contractual maturity analysis run (IFRS 7.39)"
    _inherit = ['mail.thread', 'eh.workflow.guard']
    _order = 'reporting_date desc, id desc'
    _rec_name = 'name'
    # State is a manager-gated machine (draft <-> finalised via the Finalise /
    # Reopen actions, which run under sudo). The inherited eh.workflow.guard
    # refuses any non-superuser direct write to it, so a plain user cannot
    # RPC-flip state past action_finalise and its lock.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('finalised', "Finalised")],
        default='draft', required=True, copy=False, tracking=True,
        help="A finalised run is locked: its inputs, bands and instruments "
             "cannot be edited, re-populated or appended. Only a manager can "
             "finalise or reopen it.")
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    reporting_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Bands are measured relative to this date.")

    band_scheme = fields.Selection(
        [('contractual', "On demand / <3m / 3m-1y / 1-5y / >5y"),
         ('days', "0-30 / 31-90 / 91-365 days / 1-5y / >5y")],
        default='contractual', required=True, tracking=True,
        help="Bucket boundaries used when populating (IFRS 7 does not "
             "prescribe the bands, B11: the entity uses its judgement). "
             "The contractual scheme is the original presentation and the "
             "default; the day-count scheme buckets by days from the "
             "reporting date to the contractual due date, with overdue and "
             "on-demand items in the first band.")
    include_open_items = fields.Boolean(
        string="Extract open receivables / payables", tracking=True,
        help="When populating, also build undiscounted contractual bucket "
             "rows from the due dates of ALL open (posted, unreconciled) "
             "trade receivable and trade payable items of the company, as "
             "their own classes. Off by default so existing runs keep "
             "analysing only the selected accounts / instruments.")
    include_leases = fields.Boolean(
        string="Extract lease schedules", tracking=True,
        help="When populating, also build bucket rows from the remaining "
             "contractual lease payments in the IFRS 16 lease schedules "
             "(eh_account_assets_pro), as the lease liability class "
             "(IFRS 16.58). Requires the assets and leases module; off by "
             "default.")
    annual_interest_rate = fields.Float(
        digits=(16, 6), default=0.0, tracking=True,
        string="Contractual interest rate (% p.a.)",
        help="Annual contractual interest rate applied to the open principal "
             "of each analysed liability line to estimate the UNDISCOUNTED "
             "contractual interest cash flows required by IFRS 7.39 / B11D. "
             "Interest accrues on a simple basis over the years from the "
             "reporting date to each line's contractual maturity and is added "
             "to the principal in that line's band. Leave at 0 when the "
             "instruments are non-interest-bearing or when only principal is "
             "available, in which case the analysis reports principal only.")

    account_ids = fields.Many2many(
        'account.account', 'eh_fin_maturity_run_account_rel',
        'run_id', 'account_id', string="Financial liability accounts",
        help="The borrowing / financial-liability accounts whose open move "
             "lines are analysed by contractual maturity.")

    instrument_ids = fields.One2many(
        'eh.fin.maturity.instrument', 'run_id', copy=True,
        string="Interest-bearing instruments",
        help="Per-instrument contractual terms (principal, coupon rate and "
             "frequency, maturity). When any instrument is listed, its "
             "projected contractual coupons and principal repayment are "
             "bucketed by band, so a coupon-bearing bond bands its interim "
             "coupons into the earlier bands and its principal plus final "
             "coupon into the maturity band. Leave empty to analyse the "
             "selected ledger accounts instead.")

    line_ids = fields.One2many(
        'eh.fin.maturity.line', 'run_id', copy=False,
        string="Maturity bands")

    total_undiscounted = fields.Monetary(
        compute='_compute_total', store=True, currency_field='currency_id',
        help="Sum of the undiscounted amounts across all bands; the total "
             "open balance analysed.")
    notes = fields.Text()

    @api.depends('line_ids.undiscounted_amount')
    def _compute_total(self):
        for run in self:
            run.total_undiscounted = sum(
                run.line_ids.mapped('undiscounted_amount'))

    @api.model_create_multi
    def create(self, vals_list):
        # Creating a run already finalised would skip the manager-gated
        # action_finalise; require a manager for that path.
        if any(v.get('state') == 'finalised' for v in vals_list):
            self._check_manager()
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.fin.maturity.run') or '/'
        return super().create(vals_list)

    def write(self, vals):
        # Freeze the run inputs, bands and instruments once finalised (a
        # signed-off analysis is frozen for everyone; restate via a
        # manager-gated reopen). The state field itself is owned by the
        # inherited eh.workflow.guard, which refuses any non-superuser direct
        # write; the sanctioned finalise / reopen actions run under sudo.
        if _MATURITY_FROZEN_FIELDS.intersection(vals):
            for run in self:
                if run.state == 'finalised':
                    raise UserError(_(
                        "Maturity run %s is finalised and cannot be edited. "
                        "Ask a manager to reopen it first.", run.name))
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can finalise or reopen a "
                "maturity run."))

    def unlink(self):
        for run in self:
            if run.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised and cannot be deleted. "
                    "Ask a manager to reopen it first.", run.name))
        return super().unlink()

    def action_finalise(self):
        """Lock the run: inputs, bands and instruments freeze. Manager only."""
        self._check_manager()
        for run in self:
            if run.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is already finalised.", run.name))
        self.sudo().write(
            {'state': 'finalised'})
        return True

    def action_reopen(self):
        """Return a finalised run to draft. Manager only."""
        self._check_manager()
        self.sudo().write(
            {'state': 'draft'})
        return True

    def _band_for(self, maturity_date, reporting_date):
        """Return the band key for a contractual maturity date relative to
        the reporting date, under the run's band scheme.

        Contractual scheme (default, original behaviour): a maturity on or
        before the reporting date (or with no maturity date) is on demand;
        then <3 months, 3 months to 1 year, 1 to 5 years, over 5 years.
        Day-count scheme: days from the reporting date to the due date,
        0-30 (including overdue and undated items), 31-90, 91-365, then 1
        to 5 years and over 5 years."""
        self.ensure_one()
        if self.band_scheme == 'days':
            if not maturity_date:
                return 'd0_30'
            days = (maturity_date - reporting_date).days
            if days <= 30:
                return 'd0_30'
            if days <= 90:
                return 'd31_90'
            if days <= 365:
                return 'd91_365'
            if maturity_date < reporting_date + relativedelta(years=5):
                return '1y_5y'
            return 'gt_5y'
        if not maturity_date or maturity_date <= reporting_date:
            return 'on_demand'
        if maturity_date < reporting_date + relativedelta(months=3):
            return 'lt_3m'
        if maturity_date < reporting_date + relativedelta(years=1):
            return '3m_1y'
        if maturity_date < reporting_date + relativedelta(years=5):
            return '1y_5y'
        return 'gt_5y'

    def _contractual_interest(self, principal, maturity_date, reporting_date,
                              annual_rate):
        """Undiscounted contractual interest cash flow on ``principal`` from
        the reporting date to ``maturity_date`` at ``annual_rate`` (% p.a.),
        on a simple-interest basis.

        IFRS 7.B11D requires the maturity analysis to include the contractual
        interest cash flows, not just the principal repayment. Where the
        instrument's rate is supplied on the run, the undiscounted interest is
        principal * rate% * years-to-maturity. A rate of zero (the default, and
        the case for a non-interest-bearing liability) yields no interest, so
        the analysis then reports principal only."""
        if not annual_rate or not maturity_date or \
                maturity_date <= reporting_date:
            return 0.0
        days = (maturity_date - reporting_date).days
        years = days / 365.0
        return principal * (annual_rate / 100.0) * years

    def action_populate(self):
        """Read open (unreconciled, posted) move lines on the selected
        accounts and rebuild the band lines.

        IFRS 7.39 / B11D requires the maturity analysis to show the
        UNDISCOUNTED contractual amounts of financial liabilities as positive
        outflows, which comprise both the principal repayment and the
        contractual interest cash flows. A financial-liability line carries a
        credit balance, so its ledger balance (debit - credit) is negative;
        taking that raw balance would mislabel a payable outflow as a negative
        amount. The contractual principal outflow is therefore the credit-side
        magnitude (credit - debit), i.e. the sign-flipped balance, which is
        positive for a net liability. On top of that principal, the
        undiscounted contractual interest to the line's maturity is added when
        an annual interest rate is supplied on the run, so the reported figure
        is the undiscounted contractual amount rather than the carrying
        principal alone.

        Only lines on or before the reporting date are analysed, so entries
        posted after the as-at date do not leak into the disclosure.

        Idempotent: extracted rows are wiped and rebuilt on every populate;
        manually keyed band rows (origin = manual) are preserved, the same
        pattern as the ECL populate."""
        Line = self.env['eh.fin.maturity.line']
        for run in self:
            if run.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; its bands cannot be "
                    "re-populated. Ask a manager to reopen it first.",
                    run.name))
            run.line_ids.filtered(lambda l: l.origin != 'manual').unlink()
            # Per-instrument projection takes precedence when instruments are
            # listed: each instrument's contractual coupons and principal
            # repayment are bucketed with its OWN rate, so a coupon bond bands
            # its interim coupons into the earlier bands (IFRS 7.39 / B11D).
            if run.instrument_ids:
                run._create_class_bands(
                    Line, 'instrument', run._instrument_flows())
            elif run.account_ids:
                run._create_class_bands(
                    Line, 'liability', run._ledger_account_flows())
            # Optional extraction sources, additive per class. Skip-zero so
            # a class with no open items adds no empty rows.
            if run.include_open_items:
                run._create_class_bands(
                    Line, 'receivable',
                    run._open_item_flows('asset_receivable'),
                    skip_zero=True)
                run._create_class_bands(
                    Line, 'payable',
                    run._open_item_flows('liability_payable'),
                    skip_zero=True)
            if run.include_leases:
                run._create_class_bands(
                    Line, 'lease', run._lease_flows(), skip_zero=True)
        return True

    def _create_class_bands(self, Line, item_class, flows, skip_zero=False):
        """Bucket (date, amount) contractual cash flows into the run's band
        scheme and create one extracted row per band for the class. The
        instrument / selected-account classes keep the original behaviour of
        one row per band including zero bands; the extraction classes skip
        zero bands (skip_zero)."""
        self.ensure_one()
        currency = self.currency_id
        buckets = {key: 0.0 for key in _SCHEME_BANDS[self.band_scheme]}
        for flow_date, amount in flows:
            band = self._band_for(flow_date, self.reporting_date)
            buckets[band] += amount
        for key in _SCHEME_BANDS[self.band_scheme]:
            amount = buckets[key]
            if currency:
                amount = currency.round(amount)
            if skip_zero and currency and currency.is_zero(amount):
                continue
            Line.create({
                'run_id': self.id,
                'band': key,
                'item_class': item_class,
                'origin': 'extracted',
                'undiscounted_amount': amount,
            })

    def _instrument_flows(self):
        """(date, amount) contractual cash flows of the listed instruments
        (IFRS 7.39 / B11D). Each instrument projects its own undiscounted
        contractual coupons at its own rate and frequency, plus its
        principal repayment at maturity, so a plain coupon bond bands its
        interim coupons into the earlier bands and its principal plus final
        coupon into the maturity band."""
        self.ensure_one()
        for instrument in self.instrument_ids:
            yield from instrument._contractual_cash_flows(
                self.reporting_date)

    def _ledger_account_flows(self):
        """(date, amount) undiscounted contractual outflows of the open
        (posted, unreconciled) move lines on the selected financial
        liability accounts: the credit-side principal magnitude plus the
        simple-interest add-on at the run's contractual rate (IFRS 7.B11D;
        zero rate reports principal only)."""
        self.ensure_one()
        move_lines = self.env['account.move.line'].search([
            ('account_id', 'in', self.account_ids.ids),
            ('company_id', '=', self.company_id.id),
            ('parent_state', '=', 'posted'),
            ('reconciled', '=', False),
            ('date', '<=', self.reporting_date),
        ])
        for ml in move_lines:
            maturity = ml.date_maturity or ml.date
            # Contractual principal outflow for a liability is the
            # credit-side magnitude (credit - debit == -balance), positive
            # for a net liability.
            principal = ml.credit - ml.debit
            # Undiscounted contractual interest to maturity (IFRS 7.B11D).
            # Zero for a non-interest-bearing liability (rate unset), so
            # the default behaviour is principal only and byte-identical.
            interest = self._contractual_interest(
                principal, maturity, self.reporting_date,
                self.annual_interest_rate)
            yield maturity, principal + interest

    def _open_item_flows(self, account_type):
        """(date, amount) open-item contractual cash flows for a receivable
        or payable class: every open (posted, unreconciled) item of the
        company on accounts of that type, at its residual amount and due
        date. Receivables report as positive inflows, payables as positive
        outflows (amount_residual is signed debit-positive, so the payable
        side is sign-flipped)."""
        self.ensure_one()
        currency = self.currency_id
        move_lines = self.env['account.move.line'].search([
            ('company_id', '=', self.company_id.id),
            ('parent_state', '=', 'posted'),
            ('reconciled', '=', False),
            ('account_id.account_type', '=', account_type),
            ('date', '<=', self.reporting_date),
        ])
        for ml in move_lines:
            residual = ml.amount_residual
            if currency and currency.is_zero(residual):
                continue
            amount = residual if account_type == 'asset_receivable' \
                else -residual
            yield (ml.date_maturity or ml.date), amount

    def _lease_flows(self):
        """(date, amount) remaining contractual lease payments from the
        IFRS 16 schedules (IFRS 16.58 requires the lease liability maturity
        analysis under IFRS 7.39). Soft lookup: raises a clear error when
        the assets and leases module is not installed. Only the lease
        component feeds the analysis (the service share is not a financial
        liability), and only payments contractually due after the reporting
        date on live (active or modified) leases."""
        self.ensure_one()
        if 'eh.lease.schedule.line' not in self.env:
            raise UserError(_(
                "Extracting lease schedules requires the Assets and Leases "
                "module (eh_account_assets_pro). Install it or untick "
                "'Extract lease schedules'."))
        schedule_lines = self.env['eh.lease.schedule.line'].search([
            ('company_id', '=', self.company_id.id),
            ('period_date', '>', self.reporting_date),
            ('lease_id.state', 'in', ('active', 'modified')),
        ])
        for line in schedule_lines:
            yield line.period_date, line.payment_amount


class EhFinMaturityLine(models.Model):
    _name = 'eh.fin.maturity.line'
    _description = "Contractual maturity analysis band"
    _order = 'run_id, band_sequence, id'

    run_id = fields.Many2one(
        'eh.fin.maturity.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    band = fields.Selection(
        MATURITY_BANDS, required=True,
        help="Contractual maturity band (IFRS 7.39).")
    item_class = fields.Selection(
        MATURITY_ITEM_CLASSES, required=True, default='manual',
        string="Class",
        help="Class of financial instrument the band row belongs to "
             "(IFRS 7.6): the analysed liability accounts, the projected "
             "instruments, or an extracted open-item / lease class.")
    origin = fields.Selection(
        [('extracted', "Extracted"), ('manual', "Manual")],
        required=True, default='manual',
        help="Extracted rows are wiped and rebuilt on every populate. A "
             "manually keyed row survives the repopulate (the same "
             "idempotent pattern as the ECL populate).")
    band_sequence = fields.Integer(
        compute='_compute_band_sequence', store=True)
    undiscounted_amount = fields.Monetary(
        currency_field='currency_id',
        help="Undiscounted contractual cash flow bucketed into this band, "
             "shown as a positive liability outflow. This is the open "
             "principal (credit-side magnitude of the ledger balance) plus "
             "the undiscounted contractual interest to maturity when an "
             "annual interest rate is supplied on the run (IFRS 7.B11D); "
             "with no rate it is the principal only.")

    @api.depends('band')
    def _compute_band_sequence(self):
        order = {key: i for i, (key, _label) in enumerate(MATURITY_BANDS)}
        for line in self:
            line.band_sequence = order.get(line.band, 99)

    @api.model_create_multi
    def create(self, vals_list):
        # A create-append hole silently moves the run total, so appending a
        # band to a finalised run is refused (create guard is required).
        runs = self.env['eh.fin.maturity.run'].browse([
            v.get('run_id') for v in vals_list if v.get('run_id')])
        for run in runs:
            if run.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; no band can be added. "
                    "Ask a manager to reopen it first.", run.name))
        return super().create(vals_list)

    def write(self, vals):
        for line in self:
            if line.run_id.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; its bands cannot be "
                    "edited. Ask a manager to reopen it first.",
                    line.run_id.name))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if line.run_id.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; its bands cannot be "
                    "removed. Ask a manager to reopen it first.",
                    line.run_id.name))
        return super().unlink()


# Coupon payments per year for each frequency.
COUPON_FREQUENCY = [
    ('annual', "Annual"),
    ('semiannual', "Semi-annual"),
    ('quarterly', "Quarterly"),
    ('monthly', "Monthly"),
    ('bullet', "At maturity only"),
]
_FREQ_PER_YEAR = {
    'annual': 1, 'semiannual': 2, 'quarterly': 4, 'monthly': 12, 'bullet': 0,
}


class EhFinMaturityInstrument(models.Model):
    _name = 'eh.fin.maturity.instrument'
    _description = "Interest-bearing instrument (IFRS 7.39)"
    _order = 'run_id, sequence, id'

    run_id = fields.Many2one(
        'eh.fin.maturity.run', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    name = fields.Char(required=True, help="Instrument description.")
    principal = fields.Monetary(
        currency_field='currency_id',
        help="Contractual principal (face amount) repaid at maturity, as a "
             "positive outflow.")
    annual_rate = fields.Float(
        digits=(16, 6), default=0.0,
        string="Coupon rate (% p.a.)",
        help="This instrument's own annual contractual coupon rate. Each "
             "coupon is principal * rate% / coupons-per-year. A rate of zero "
             "is a non-interest-bearing (zero-coupon) instrument, whose only "
             "cash flow is the principal at maturity.")
    coupon_frequency = fields.Selection(
        COUPON_FREQUENCY, default='annual', required=True,
        help="How often the contractual coupon is paid. 'At maturity only' "
             "pays a single coupon with the principal at maturity.")
    floating_rate = fields.Boolean(
        string="Floating rate",
        help="The borrowing bears a floating (variable) rate. Floating "
             "instruments on the company's latest maturity run feed the "
             "computed IFRS 7.40 interest-rate sensitivity: a rate rise "
             "increases their interest cost, so the impact is negative.")
    maturity_date = fields.Date(
        required=True,
        help="Contractual maturity date on which the principal is repaid.")

    @api.model_create_multi
    def create(self, vals_list):
        # Appending an instrument to a finalised run would change its projected
        # cash flows on the next populate, so the create guard is required.
        runs = self.env['eh.fin.maturity.run'].browse([
            v.get('run_id') for v in vals_list if v.get('run_id')])
        for run in runs:
            if run.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; no instrument can be "
                    "added. Ask a manager to reopen it first.", run.name))
        return super().create(vals_list)

    def write(self, vals):
        for instrument in self:
            if instrument.run_id.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; its instruments cannot be "
                    "edited. Ask a manager to reopen it first.",
                    instrument.run_id.name))
        return super().write(vals)

    def unlink(self):
        for instrument in self:
            if instrument.run_id.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; its instruments cannot be "
                    "removed. Ask a manager to reopen it first.",
                    instrument.run_id.name))
        return super().unlink()

    def _contractual_cash_flows(self, reporting_date):
        """Yield (payment_date, undiscounted_amount) tuples for this
        instrument's contractual cash flows strictly after the reporting
        date, on an undiscounted basis (IFRS 7.39 / B11D).

        Interim coupons fall on their own payment dates and are bucketed into
        the band of that date; the final cash flow at maturity is the last
        coupon plus the principal. A zero rate yields no coupons, so only the
        principal at maturity is returned (a zero-coupon instrument)."""
        self.ensure_one()
        if not self.maturity_date:
            return
        per_year = _FREQ_PER_YEAR.get(self.coupon_frequency, 0)
        rate = (self.annual_rate or 0.0) / 100.0
        # Coupon amount per period (0 when non-interest-bearing or bullet with
        # no rate). For 'bullet' there is a single coupon covering the whole
        # life, paid with the principal at maturity.
        if per_year and rate:
            coupon = self.principal * rate / per_year
            # Coupon dates counted back from maturity so the final coupon
            # lands exactly on the maturity date. months_step is the interval
            # between coupons.
            months_step = 12 // per_year
            n = 0
            # Emit interim coupons on their own dates (all before maturity),
            # newest-first walk back but yielded date-tagged so band bucketing
            # is order-independent.
            date = self.maturity_date
            while True:
                if date <= reporting_date:
                    break
                if date < self.maturity_date:
                    # Interim coupon on its own payment date.
                    yield date, coupon
                n += 1
                date = self.maturity_date - relativedelta(
                    months=months_step * n)
        # Final cash flow at maturity: principal + the coupon due at maturity.
        if self.maturity_date > reporting_date:
            final = self.principal
            if per_year and rate:
                final += self.principal * rate / per_year
            elif self.coupon_frequency == 'bullet' and rate:
                days = (self.maturity_date - reporting_date).days
                final += self.principal * rate * (days / 365.0)
            yield self.maturity_date, final
        elif self.maturity_date <= reporting_date:
            # Already matured / on demand: principal only, on demand band.
            if self.principal:
                yield self.maturity_date, self.principal
