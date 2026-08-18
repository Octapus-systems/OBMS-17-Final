# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 7.40 market-risk sensitivity analysis computed from the ledger.

Currency risk: a configurable percentage shock (default 10) is applied to
the net open monetary position in each foreign currency, read from posted
receivable, payable and cash move lines at the reporting date. Interest-rate
risk: a configurable basis-point shock (default 100) is applied to the
carrying amount of instruments flagged floating-rate, sourced from the
latest financial-risk register snapshot, the latest maturity run's
borrowing instruments, and (soft lookup) fair-value items flagged
floating. The results are computed
rows; the shock assumptions are disclosed alongside them (IFRS 7.40(b)).

Sign convention, stated once and used everywhere: every impact is the
effect of the UPWARD shock. For currency risk that is a strengthening of
the foreign currency against the functional currency, so a net foreign
currency asset produces a positive (gain) impact and a net liability a
negative one. For interest-rate risk it is a parallel upward shift in
rates, so a floating-rate asset gains interest income (positive) and a
floating-rate borrowing bears more interest cost (negative). The downward
shock is symmetric with the opposite sign.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Parent inputs frozen once the analysis is finalised.
_SENSITIVITY_FROZEN_FIELDS = frozenset({
    'name', 'company_id', 'reporting_date', 'fx_shock_pct', 'ir_shock_bp',
    'line_ids', 'notes',
})


class EhFinSensitivity(models.Model):
    _name = 'eh.fin.sensitivity'
    _description = "Market-risk sensitivity analysis (IFRS 7.40)"
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
        help="A finalised analysis is locked: its shocks and rows cannot be "
             "edited or recomputed. Only a manager can finalise or reopen "
             "it.")
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    reporting_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)

    fx_shock_pct = fields.Float(
        digits=(7, 4), default=10.0, tracking=True,
        string="Currency shock (%)",
        help="Reasonably possible change in each exchange rate "
             "(IFRS 7.40(a)). The computed impact is the profit-or-loss "
             "effect of the foreign currency STRENGTHENING by this "
             "percentage against the functional currency, applied to the "
             "net open monetary position per currency; the weakening case "
             "is symmetric with the opposite sign.")
    ir_shock_bp = fields.Float(
        digits=(7, 2), default=100.0, tracking=True,
        string="Interest-rate shock (bp)",
        help="Parallel upward shift in interest rates in basis points "
             "(IFRS 7.40(a)), applied to the carrying amount of "
             "floating-rate instruments as the annual profit-or-loss "
             "effect; the downward shift is symmetric with the opposite "
             "sign.")

    line_ids = fields.One2many(
        'eh.fin.sensitivity.line', 'run_id', copy=False,
        string="Sensitivity rows")

    total_pnl_impact = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Sum of the profit-or-loss impacts of the upward shocks.")
    total_oci_impact = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Sum of the other-comprehensive-income impacts of the upward "
             "shocks (FVOCI-debt floating instruments).")
    assumption_note = fields.Text(
        compute='_compute_assumption_note',
        help="Methods and assumptions disclosed with the analysis "
             "(IFRS 7.40(b)).")
    notes = fields.Text()

    @api.depends('line_ids.pnl_impact', 'line_ids.oci_impact')
    def _compute_totals(self):
        for run in self:
            run.total_pnl_impact = sum(run.line_ids.mapped('pnl_impact'))
            run.total_oci_impact = sum(run.line_ids.mapped('oci_impact'))

    @api.depends('fx_shock_pct', 'ir_shock_bp')
    def _compute_assumption_note(self):
        for run in self:
            run.assumption_note = _(
                "Currency risk: each impact is the profit-or-loss effect of "
                "the foreign currency strengthening %(pct)s%% against the "
                "functional currency, applied to the net open monetary "
                "position (posted receivable, payable and cash items) in "
                "that currency at the reporting date; a weakening of the "
                "same size has the opposite effect. Interest-rate risk: "
                "each impact is the annual effect of a %(bp)s basis point "
                "parallel upward shift applied to the carrying amount of "
                "floating-rate instruments; a downward shift of the same "
                "size has the opposite effect. All other variables are "
                "held constant.",
                pct=run.fx_shock_pct, bp=run.ir_shock_bp)

    # --- compute ---------------------------------------------------------

    def action_compute(self):
        """Rebuild the computed sensitivity rows. Idempotent: computed rows
        are wiped and rebuilt; manually keyed rows are preserved."""
        Line = self.env['eh.fin.sensitivity.line']
        for run in self:
            if run.state == 'finalised':
                raise UserError(_(
                    "Sensitivity analysis %s is finalised; it cannot be "
                    "recomputed. Ask a manager to reopen it first.",
                    run.name))
            run.line_ids.filtered(
                lambda line_item: line_item.origin == 'computed').unlink()
            vals_list = run._fx_line_vals() + run._ir_line_vals()
            for vals in vals_list:
                vals['run_id'] = run.id
                vals['origin'] = 'computed'
            if vals_list:
                Line.create(vals_list)
        return True

    def _fx_open_positions(self):
        """Net open monetary position per foreign currency: the sum of the
        open (residual) foreign-currency amounts on posted receivable and
        payable lines plus the foreign-currency amounts on posted cash
        lines, up to the reporting date. Positive = net asset."""
        self.ensure_one()
        company_currency = self.company_id.currency_id
        move_lines = self.env['account.move.line'].search([
            ('company_id', '=', self.company_id.id),
            ('parent_state', '=', 'posted'),
            ('date', '<=', self.reporting_date),
            ('currency_id', '!=', False),
            ('currency_id', '!=', company_currency.id),
            ('account_id.account_type', 'in',
             ('asset_receivable', 'liability_payable', 'asset_cash')),
        ])
        positions = {}
        for ml in move_lines:
            if ml.account_id.account_type == 'asset_cash':
                # Cash lines are not reconcilable; the whole posted amount
                # remains a monetary position.
                amount = ml.amount_currency
            else:
                if ml.reconciled:
                    continue
                amount = ml.amount_residual_currency
            if not amount:
                continue
            positions[ml.currency_id] = \
                positions.get(ml.currency_id, 0.0) + amount
        return positions

    def _fx_line_vals(self):
        """One row per foreign currency with a non-zero net open monetary
        position. Impact convention: pnl = exposure in functional currency
        x shock%, i.e. the gain (positive exposure) or loss (negative
        exposure) if the foreign currency strengthens by the shock."""
        self.ensure_one()
        company_currency = self.company_id.currency_id
        vals_list = []
        for currency, amount_fc in sorted(
                self._fx_open_positions().items(), key=lambda i: i[0].name):
            if currency.is_zero(amount_fc):
                continue
            exposure = currency._convert(
                amount_fc, company_currency, self.company_id,
                self.reporting_date)
            impact = company_currency.round(
                exposure * self.fx_shock_pct / 100.0)
            vals_list.append({
                'kind': 'fx',
                'name': _("Net open monetary position - %s", currency.name),
                'shock_currency_id': currency.id,
                'exposure_foreign': amount_fc,
                'exposure': company_currency.round(exposure),
                'shock': '+%s%%' % (self.fx_shock_pct,),
                'pnl_impact': impact,
                'oci_impact': 0.0,
            })
        return vals_list

    def _ir_line_vals(self):
        """One row per floating-rate instrument. Sources:

        * the latest financial-risk register snapshot (eh.fin.risk flagged
          floating; the register is re-keyed each reporting period, so only
          exposures at the most recent register date on or before the
          reporting date are read and an exposure keyed in prior periods is
          never double-counted; the carrying amount is signed, positive for
          a net asset, so a floating liability keyed negative yields a
          negative impact);
        * the latest maturity run's instruments flagged floating (borrowing
          schedules; principal is a positive outflow, so the impact is
          negative: a rate rise costs interest);
        * fair-value items flagged floating (soft lookup; the impact of a
          FVOCI-debt instrument routes to OCI, everything else to P&L).
        """
        self.ensure_one()
        company_currency = self.company_id.currency_id
        factor = self.ir_shock_bp / 10000.0
        shock = '+%sbp' % (self.ir_shock_bp,)
        vals_list = []
        # The risk register is a snapshot per reporting period: exposures
        # are re-keyed each period end and freeze on finalise, so a plain
        # date-bounded search would count the same floating exposure once
        # per period it was keyed in. Mirror the maturity-run guard below:
        # only the most recent register snapshot on or before the reporting
        # date is read, so an exposure keyed in prior periods is never
        # double-counted.
        Risk = self.env['eh.fin.risk']
        latest_register = Risk.search([
            ('company_id', '=', self.company_id.id),
            ('reporting_date', '<=', self.reporting_date),
        ], order='reporting_date desc', limit=1)
        risks = Risk.search([
            ('company_id', '=', self.company_id.id),
            ('floating_rate', '=', True),
            ('reporting_date', '=', latest_register.reporting_date),
        ]) if latest_register else Risk
        for risk in risks:
            vals_list.append({
                'kind': 'interest',
                'name': _("Floating-rate exposure - %s", risk.name),
                'exposure': company_currency.round(risk.carrying_amount),
                'shock': shock,
                'pnl_impact': company_currency.round(
                    risk.carrying_amount * factor),
                'oci_impact': 0.0,
            })
        # Borrowing schedules: the latest maturity run's floating
        # instruments. Only the most recent run is read so the same
        # borrowing is never double-counted across periods.
        maturity_run = self.env['eh.fin.maturity.run'].search([
            ('company_id', '=', self.company_id.id),
            ('reporting_date', '<=', self.reporting_date),
        ], order='reporting_date desc, id desc', limit=1)
        for instrument in maturity_run.instrument_ids.filtered(
                'floating_rate'):
            vals_list.append({
                'kind': 'interest',
                'name': _("Floating-rate borrowing - %s", instrument.name),
                'exposure': company_currency.round(-instrument.principal),
                'shock': shock,
                'pnl_impact': company_currency.round(
                    -instrument.principal * factor),
                'oci_impact': 0.0,
            })
        # Fair-value items flagged floating (soft lookup: the fair value
        # module may not be installed, and older versions of it may not
        # carry the flag).
        if 'eh.fair.value.item' in self.env:
            Item = self.env['eh.fair.value.item']
            if 'floating_rate' in Item._fields:
                items = Item.search([
                    ('company_id', '=', self.company_id.id),
                    ('floating_rate', '=', True),
                    ('state', 'not in', ('derecognised', 'cancelled')),
                ])
                for item in items:
                    impact = company_currency.round(
                        item.fair_value * factor)
                    to_oci = item.ifrs9_classification == 'fvoci_debt'
                    vals_list.append({
                        'kind': 'interest',
                        'name': _("Floating-rate instrument - %s",
                                  item.name),
                        'exposure': company_currency.round(item.fair_value),
                        'shock': shock,
                        'pnl_impact': 0.0 if to_oci else impact,
                        'oci_impact': impact if to_oci else 0.0,
                    })
        return vals_list

    # --- draft / finalised lock -------------------------------------------

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can finalise or reopen a "
                "sensitivity analysis."))

    @api.model_create_multi
    def create(self, vals_list):
        if any(v.get('state') == 'finalised' for v in vals_list):
            self._check_manager()
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.fin.sensitivity') or '/'
        return super().create(vals_list)

    def write(self, vals):
        # Freeze the shocks and rows once finalised (a signed-off analysis is
        # frozen for everyone; restate via a manager-gated reopen). The state
        # field itself is owned by the inherited eh.workflow.guard, which
        # refuses any non-superuser direct write; the sanctioned finalise /
        # reopen actions run under sudo.
        if _SENSITIVITY_FROZEN_FIELDS.intersection(vals):
            for run in self:
                if run.state == 'finalised':
                    raise UserError(_(
                        "Sensitivity analysis %s is finalised and cannot "
                        "be edited. Ask a manager to reopen it first.",
                        run.name))
        return super().write(vals)

    def unlink(self):
        for run in self:
            if run.state == 'finalised':
                raise UserError(_(
                    "Sensitivity analysis %s is finalised and cannot be "
                    "deleted. Ask a manager to reopen it first.", run.name))
        return super().unlink()

    def action_finalise(self):
        """Lock the analysis: shocks and rows freeze. Manager only."""
        self._check_manager()
        for run in self:
            if run.state == 'finalised':
                raise UserError(_(
                    "Sensitivity analysis %s is already finalised.",
                    run.name))
        self.sudo().write(
            {'state': 'finalised'})
        return True

    def action_reopen(self):
        """Return a finalised analysis to draft. Manager only."""
        self._check_manager()
        self.sudo().write(
            {'state': 'draft'})
        return True


class EhFinSensitivityLine(models.Model):
    _name = 'eh.fin.sensitivity.line'
    _description = "Market-risk sensitivity row (IFRS 7.40)"
    _order = 'run_id, kind, id'

    run_id = fields.Many2one(
        'eh.fin.sensitivity', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    kind = fields.Selection(
        [('fx', "Currency risk"), ('interest', "Interest-rate risk")],
        required=True, default='fx')
    name = fields.Char(required=True, help="Exposure description.")
    origin = fields.Selection(
        [('computed', "Computed"), ('manual', "Manual")],
        default='manual', required=True,
        help="Computed rows are wiped and rebuilt on every compute. A "
             "manual row (e.g. an other-price-risk exposure the ledger "
             "cannot see) survives the recompute.")
    shock_currency_id = fields.Many2one(
        'res.currency', string="Exposure currency",
        help="Foreign currency of a currency-risk row.")
    exposure_foreign = fields.Monetary(
        currency_field='shock_currency_id',
        help="Net open monetary position in the foreign currency.")
    exposure = fields.Monetary(
        currency_field='currency_id',
        help="Exposure in the functional currency the shock applies to "
             "(signed: positive = net asset).")
    shock = fields.Char(
        help="The shock applied, e.g. '+10.0%' or '+100.0bp'. The downward "
             "shock is symmetric with the opposite sign.")
    pnl_impact = fields.Monetary(
        currency_field='currency_id', string="P&L impact",
        help="Profit-or-loss effect of the upward shock (positive = gain).")
    oci_impact = fields.Monetary(
        currency_field='currency_id', string="OCI impact",
        help="Other-comprehensive-income effect of the upward shock "
             "(FVOCI-debt floating instruments).")

    @api.model_create_multi
    def create(self, vals_list):
        # Create guard on child lines feeding a frozen parent.
        runs = self.env['eh.fin.sensitivity'].browse([
            v.get('run_id') for v in vals_list if v.get('run_id')])
        for run in runs:
            if run.state == 'finalised':
                raise UserError(_(
                    "Sensitivity analysis %s is finalised; no row can be "
                    "added. Ask a manager to reopen it first.", run.name))
        return super().create(vals_list)

    def write(self, vals):
        for line in self:
            if line.run_id.state == 'finalised':
                raise UserError(_(
                    "Sensitivity analysis %s is finalised; its rows cannot "
                    "be edited. Ask a manager to reopen it first.",
                    line.run_id.name))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if line.run_id.state == 'finalised':
                raise UserError(_(
                    "Sensitivity analysis %s is finalised; its rows cannot "
                    "be removed. Ask a manager to reopen it first.",
                    line.run_id.name))
        return super().unlink()
