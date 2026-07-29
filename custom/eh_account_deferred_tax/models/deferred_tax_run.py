# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.deferred.tax.run: a period-end deferred tax computation for one company.

Compute: resolve each line's rate (manual override, then the jurisdiction
enacted-rate table at the reporting date, then the run's statutory rate as
the fallback), roll opening balances and opening rates forward from the
prior posted run, then total the closing deferred tax asset and liability,
the movement from the opening position, and its split into rate-change
remeasurement (IAS 12.60(b)) versus origination. Compute also rebuilds the
auto rows of the IAS 12.81(c) effective-tax-rate reconciliation, keeping
manual rows.

Post: recognise only the period movement in one balanced journal. Movements
route to profit or loss, except lines flagged as OCI-related, whose movement
routes to the OCI reserve (IAS 12.61A). Under the gross offsetting policy
(the default and the historical behaviour) one DTA leg and one DTL leg are
posted; under the net-by-jurisdiction policy (IAS 12.74) the run posts one
net leg per jurisdiction per side while the lines keep the gross detail for
disclosure. The entry balances by construction: the deferred tax expense /
income leg is the balancing plug.

Reverse: post the symmetric inverse dated the day after the period end and
flip the run to Reversed, preserving both the entry and its reversal.
"""

import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Input / measurement fields on the run that become the basis of a posted
# movement. Once the run is posted or reversed they are frozen at the ORM
# write layer. 'state' is deliberately NOT listed: the action methods write
# only state + the audit stamps (posted_at/posted_by_id, ...), so a pure
# state-transition write carries no frozen field and always passes.
_FROZEN_AFTER_CONFIRM = (
    'line_ids', 'period_end', 'statutory_rate', 'company_id',
    'dta_account_id', 'dtl_account_id', 'deferred_tax_expense_account_id',
    'oci_account_id', 'journal_id',
    'accounting_profit', 'permanent_diff_tax', 'current_tax_expense',
    'offsetting_policy', 'recon_line_ids',
    'projected_taxable_profit', 'opening_unrecognised_dta',
)


class EhDeferredTaxRun(models.Model):
    _name = 'eh.deferred.tax.run'
    _description = "Deferred tax run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard', 'eh.gl.reversal']
    _order = 'period_end desc, id desc'
    _rec_name = 'name'

    # State is a posting state machine: it may only change through this
    # model's own action_* methods (which build/reverse the GL movement and
    # run the manager checks), never a direct RPC/ORM write. eh.workflow.guard
    # blocks any unflagged write to these fields for a non-superuser.
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('computed', "Computed"),
            ('posted', "Posted"),
            ('reversed', "Reversed"),
            ('cancelled', "Cancelled"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True,
    )
    period_end = fields.Date(
        required=True, tracking=True,
        default=fields.Date.context_today,
        help="Reporting date at which the deferred tax position is measured. "
             "The posting date of the movement entry.",
    )
    statutory_rate = fields.Float(
        digits=(6, 3), required=True, default=25.0, tracking=True,
        help="Enacted / substantively enacted tax rate for the period, as a "
             "percentage. Fallback: a line resolves its rate from its "
             "manual override, then its jurisdiction's enacted-rate table "
             "at the reporting date, and only then from this rate. Also "
             "drives the expected tax in the reconciliation and the "
             "recoverability ceiling.",
    )
    offsetting_policy = fields.Selection(
        [
            ('gross', "Gross (no offsetting)"),
            ('net_by_jurisdiction', "Net by jurisdiction (IAS 12.74)"),
        ],
        default='gross', required=True, tracking=True,
        help="IAS 12.74 permits offsetting a deferred tax asset against a "
             "deferred tax liability only when the entity has a legally "
             "enforceable right to set off current tax assets against "
             "current tax liabilities AND both relate to income taxes "
             "levied by the same taxation authority on the same taxable "
             "entity (or on entities intending to settle net). Choosing "
             "the net policy asserts those conditions hold within each "
             "jurisdiction of this company: posting then books one net leg "
             "per jurisdiction per side, while the lines keep the gross "
             "detail for disclosure. Gross is the default and the "
             "historical behaviour. Keep the policy consistent across "
             "periods: the net movement is based on the line openings, so "
             "switching policy mid-stream requires a reclassification of "
             "the prior gross balances.",
    )

    line_ids = fields.One2many(
        'eh.deferred.tax.line', 'run_id', copy=True,
    )
    recon_line_ids = fields.One2many(
        'eh.deferred.tax.recon.line', 'run_id', copy=False,
        string="Reconciliation Rows",
    )

    # ---- accounts ----
    dta_account_id = fields.Many2one(
        'account.account', string="Deferred Tax Asset Account",
        tracking=True,
        domain="[('account_type', 'in', "
               "['asset_non_current', 'asset_current'])]",
    )
    dtl_account_id = fields.Many2one(
        'account.account', string="Deferred Tax Liability Account",
        tracking=True,
        domain="[('account_type', 'in', "
               "['liability_non_current', 'liability_current'])]",
    )
    deferred_tax_expense_account_id = fields.Many2one(
        'account.account', string="Deferred Tax Expense Account",
        tracking=True,
        domain="[('account_type', 'in', ['expense', 'income_other'])]",
    )
    oci_account_id = fields.Many2one(
        'account.account', string="OCI / Equity Account", tracking=True,
        domain="[('account_type', '=', 'equity')]",
        help="Reserve that carries the deferred tax on OCI-related items. "
             "Required only when a line is flagged as recognised in OCI.",
    )
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]",
    )

    # ---- computed totals ----
    closing_dta = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    unrecognised_dta = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Total deferred tax asset not recognised because deductible "
             "differences exceed projected recoverable profit, or lines are "
             "flagged not recoverable (IAS 12.81(e) disclosure).")
    closing_dtl = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    net_deferred_tax = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Closing deferred tax liability less asset; positive = net "
             "liability.")
    opening_tie_out = fields.Boolean(
        compute='_compute_opening_tie_out',
        help="Set when at least one line's opening does not tie to the prior "
             "posted run's closing position. A keying error in the opening "
             "silently mis-states the period movement; investigate before "
             "posting.")
    pl_movement = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Movement recognised in profit or loss; positive = expense.")
    oci_movement = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Movement recognised in OCI; positive = charge to OCI.")
    rate_change_pl = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Rate-change remeasurement of opening balances routed to "
             "profit or loss (IAS 12.60(b)): sum of the rate-change effect "
             "of the non-OCI lines, positive = charge. Feeds the "
             "reconciliation's rate-change row.")
    rate_change_oci = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Rate-change remeasurement of opening balances routed to OCI "
             "(IAS 12.61A/63): sum of the rate-change effect of the "
             "OCI-flagged lines, positive = charge to OCI.")

    # ---- IAS 12.74 offsetting presentation ----
    net_dta_presented = fields.Monetary(
        compute='_compute_net_presentation', store=True,
        currency_field='currency_id',
        help="Deferred tax asset presented on the balance sheet under the "
             "run's offsetting policy: the gross closing DTA under the "
             "gross policy, or the sum of the per-jurisdiction net asset "
             "positions under the net policy (IAS 12.74).")
    net_dtl_presented = fields.Monetary(
        compute='_compute_net_presentation', store=True,
        currency_field='currency_id',
        help="Deferred tax liability presented on the balance sheet under "
             "the run's offsetting policy.")
    offsetting_note = fields.Text(
        compute='_compute_offsetting_note',
        help="Per-jurisdiction gross and presented positions for the "
             "balance sheet note.")

    # ---- IAS 12.24/34 run-level recoverability memo ----
    projected_taxable_profit = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Projected future taxable profit supporting recovery of the "
             "recognised deferred tax assets (IAS 12.24/34). At the "
             "statutory rate it gives the recognition ceiling; any closing "
             "DTA above the ceiling is disclosed as unrecognised and feeds "
             "the reconciliation's unrecognised row. Zero means no "
             "run-level constraint (line-level caps still apply).")
    dta_ceiling = fields.Monetary(
        compute='_compute_recoverability', store=True,
        currency_field='currency_id',
        help="Projected taxable profit at the statutory rate: the run-"
             "level ceiling on the recognised deferred tax asset.")
    run_level_unrecognised_dta = fields.Monetary(
        compute='_compute_recoverability', store=True,
        currency_field='currency_id',
        help="Closing DTA above the run-level ceiling: max(0, closing DTA "
             "less ceiling), disclosed per IAS 12.81(e) on top of the "
             "line-level unrecognised amounts.")
    opening_unrecognised_dta = fields.Monetary(
        currency_field='currency_id',
        help="Unrecognised deferred tax asset disclosed at the prior "
             "reporting date (line-level plus run-level). Rolled forward "
             "from the prior posted run when left at zero. The "
             "reconciliation's unrecognised row shows the period movement "
             "against this figure.")
    recoverability_memo = fields.Text(
        help="Evidence for the projected taxable profit: forecasts, "
             "reversal of existing taxable differences, tax-planning "
             "opportunities (IAS 12.28-31).")

    # ---- IAS 12.81(c) effective-tax-rate reconciliation ----
    accounting_profit = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Accounting profit before tax for the period. Drives the "
             "expected tax at the statutory rate in the reconciliation.")
    permanent_diff_tax = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Tax effect of permanent differences (non-deductible expenses, "
             "exempt income), entered as a signed amount added to expected "
             "tax in the reconciliation.")
    current_tax_expense = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Current tax expense for the period (from the tax return / BAS), "
             "entered so the reconciliation ties to the total tax charge.")
    expected_tax = fields.Monetary(
        compute='_compute_reconciliation', store=True,
        currency_field='currency_id',
        help="Accounting profit at the statutory rate.")
    total_tax_expense = fields.Monetary(
        compute='_compute_reconciliation', store=True,
        currency_field='currency_id',
        help="Current tax expense plus the deferred movement to profit or "
             "loss.")
    effective_rate = fields.Float(
        compute='_compute_reconciliation', store=True, digits=(6, 3),
        help="Total tax expense over accounting profit, as a percentage.")
    reconciliation_residual = fields.Monetary(
        compute='_compute_reconciliation', store=True,
        currency_field='currency_id',
        help="Total tax expense less (expected tax plus permanent-difference "
             "tax). A non-zero residual flags an unexplained reconciling "
             "item to investigate.")

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')

    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)
    reversed_at = fields.Datetime(readonly=True, tracking=True)
    reversed_by_id = fields.Many2one('res.users', readonly=True)

    notes = fields.Text()

    _sql_constraints = [
        ('unique_company_period', 'unique(company_id, period_end)', 'Only one deferred tax run per company per reporting date.'),
        ('check_rate', 'CHECK (statutory_rate >= 0 AND statutory_rate <= 100)', 'Statutory rate must be between 0 and 100.'),
    ]

    # ---- compute ----

    @api.depends(
        'line_ids.closing_dta', 'line_ids.closing_dtl',
        'line_ids.movement_dta', 'line_ids.movement_dtl',
        'line_ids.through_oci', 'line_ids.unrecognised_dta',
        'line_ids.rate_change_effect',
    )
    def _compute_totals(self):
        for run in self:
            run.closing_dta = sum(run.line_ids.mapped('closing_dta'))
            run.unrecognised_dta = sum(
                run.line_ids.mapped('unrecognised_dta'))
            run.closing_dtl = sum(run.line_ids.mapped('closing_dtl'))
            run.net_deferred_tax = run.closing_dtl - run.closing_dta
            pl = run.line_ids.filtered(lambda l: not l.through_oci)
            oci = run.line_ids.filtered(lambda l: l.through_oci)
            run.pl_movement = (
                sum(pl.mapped('movement_dtl')) - sum(pl.mapped('movement_dta')))
            run.oci_movement = (
                sum(oci.mapped('movement_dtl'))
                - sum(oci.mapped('movement_dta')))
            run.rate_change_pl = sum(pl.mapped('rate_change_effect'))
            run.rate_change_oci = sum(oci.mapped('rate_change_effect'))

    def _jurisdiction_groups(self):
        """Lines grouped by jurisdiction, deterministic order.

        Returns a list of (jurisdiction, lines) with the empty jurisdiction
        (legacy rows never recomputed) collected under a False key, ordered
        by jurisdiction name for stable posting and disclosure output.
        """
        self.ensure_one()
        groups = {}
        for line in self.line_ids:
            groups.setdefault(line.jurisdiction_id, []).append(line)
        ordered = sorted(
            groups.items(),
            key=lambda item: (item[0].name or '', item[0].id or 0))
        Line = self.env['eh.deferred.tax.line']
        return [
            (jur, Line.browse([l.id for l in lines]))
            for jur, lines in ordered
        ]

    @api.depends(
        'offsetting_policy', 'line_ids.closing_dta', 'line_ids.closing_dtl',
        'line_ids.jurisdiction_id',
    )
    def _compute_net_presentation(self):
        """Balance sheet presentation under the offsetting policy.

        Gross: the presented figures are the gross closing totals. Net by
        jurisdiction (IAS 12.74): within each jurisdiction DTA and DTL are
        offset and only the net asset or net liability is presented; the
        presented totals are the sums of those per-jurisdiction nets. The
        net position (DTL less DTA) is identical under both policies.
        """
        for run in self:
            if run.offsetting_policy != 'net_by_jurisdiction':
                run.net_dta_presented = sum(
                    run.line_ids.mapped('closing_dta'))
                run.net_dtl_presented = sum(
                    run.line_ids.mapped('closing_dtl'))
                continue
            net_dta = net_dtl = 0.0
            for _jur, lines in run._jurisdiction_groups():
                net = (sum(lines.mapped('closing_dtl'))
                       - sum(lines.mapped('closing_dta')))
                if net >= 0.0:
                    net_dtl += net
                else:
                    net_dta -= net
            run.net_dta_presented = net_dta
            run.net_dtl_presented = net_dtl

    @api.depends(
        'offsetting_policy', 'net_dta_presented', 'net_dtl_presented',
        'line_ids.closing_dta', 'line_ids.closing_dtl',
        'line_ids.jurisdiction_id',
    )
    def _compute_offsetting_note(self):
        for run in self:
            rows = []
            for jur, lines in run._jurisdiction_groups():
                gross_dta = sum(lines.mapped('closing_dta'))
                gross_dtl = sum(lines.mapped('closing_dtl'))
                if run.offsetting_policy == 'net_by_jurisdiction':
                    net = gross_dtl - gross_dta
                    shown_dta = -net if net < 0.0 else 0.0
                    shown_dtl = net if net >= 0.0 else 0.0
                else:
                    shown_dta, shown_dtl = gross_dta, gross_dtl
                rows.append(
                    "%s: gross DTA %.2f, gross DTL %.2f, "
                    "presented DTA %.2f, presented DTL %.2f" % (
                        jur.name if jur else _("(no jurisdiction)"),
                        gross_dta, gross_dtl, shown_dta, shown_dtl))
            if run.offsetting_policy == 'net_by_jurisdiction':
                rows.append(_(
                    "Offset per IAS 12.74: legally enforceable right of "
                    "set-off and same taxation authority assumed within "
                    "each jurisdiction."))
            run.offsetting_note = "\n".join(rows)

    @api.depends(
        'projected_taxable_profit', 'statutory_rate',
        'line_ids.closing_dta',
    )
    def _compute_recoverability(self):
        for run in self:
            projected = run.projected_taxable_profit
            if projected > 0.0:
                run.dta_ceiling = projected * run.statutory_rate / 100.0
                closing_dta = sum(run.line_ids.mapped('closing_dta'))
                run.run_level_unrecognised_dta = max(
                    0.0, closing_dta - run.dta_ceiling)
            else:
                run.dta_ceiling = 0.0
                run.run_level_unrecognised_dta = 0.0

    @api.depends('line_ids.opening_tie_out')
    def _compute_opening_tie_out(self):
        for run in self:
            run.opening_tie_out = any(run.line_ids.mapped('opening_tie_out'))

    @api.depends(
        'accounting_profit', 'statutory_rate', 'permanent_diff_tax',
        'current_tax_expense', 'pl_movement',
    )
    def _compute_reconciliation(self):
        for run in self:
            run.expected_tax = run.accounting_profit * run.statutory_rate / 100.0
            run.total_tax_expense = run.current_tax_expense + run.pl_movement
            run.effective_rate = (
                run.total_tax_expense / run.accounting_profit * 100.0
                if run.accounting_profit else 0.0)
            run.reconciliation_residual = (
                run.total_tax_expense
                - (run.expected_tax + run.permanent_diff_tax))

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.deferred.tax.run') or '/'
        return super().create(vals_list)

    # ---- integrity: freeze input once the movement is posted ----

    def write(self, vals):
        """Freeze the measurement / input fields once the run is posted or
        reversed; they are the basis of a posted GL movement.

        A pure state-transition write (the action methods write only
        {'state': ...} plus the audit stamps) carries no frozen field and
        passes. A write touching a frozen figure while any record is posted or
        reversed is always blocked. 'state' is never frozen, so recompute in
        draft / computed and the legitimate transitions keep working.
        """
        frozen = [f for f in _FROZEN_AFTER_CONFIRM if f in vals]
        confirmed = self.filtered(lambda r: r.state in ('posted', 'reversed'))
        if frozen and confirmed:
            raise UserError(_(
                "Figures on a posted deferred tax run are frozen (%(fields)s). "
                "Reverse it first (EH Accounting Manager only) to change it.",
                fields=', '.join(frozen)))
        # The state of a posted / reversed run is itself a control point:
        # resetting it to draft would silently lift the figure freeze above.
        # A raw ORM state write on such a run without the sanctioned-transition
        # context flag must be manager-gated, so a plain user cannot un-freeze
        # a GL-backed run. The action methods set the flag after their own
        # manager check and move handling.
        if 'state' in vals \
                and not self.env.context.get('eh_run_state_change'):
            crossing = confirmed.filtered(lambda r: r.state != vals['state'])
            if crossing:
                crossing._check_manager()
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager may change the state of a "
                "posted or reversed deferred tax run."))

    def unlink(self):
        posted = self.filtered(lambda r: r.state in ('posted', 'reversed'))
        if posted:
            raise UserError(_(
                "A posted or reversed deferred tax run cannot be deleted; it "
                "carries a posted GL movement. Reverse it first."))
        return super().unlink()

    @api.constrains('oci_account_id', 'line_ids', 'state')
    def _check_oci_account(self):
        for run in self:
            if run.state == 'draft':
                continue
            if run.line_ids.filtered(lambda l: l.through_oci) \
                    and not run.oci_account_id:
                raise ValidationError(_(
                    "Run %s has a line recognised in OCI but no OCI / equity "
                    "account is configured.", run.display_name))

    # ---- transitions ----

    def action_compute(self):
        self = self._eh_workflow_action()
        for run in self:
            if run.state not in ('draft', 'computed'):
                raise UserError(_(
                    "Compute is only available in draft or computed state."))
            run._resolve_line_rates()
            run._roll_forward_opening()
            run._rebuild_reconciliation()
            run.write({
                'state': 'computed',
                'computed_at': fields.Datetime.now(),
                'computed_by_id': self.env.user.id,
            })
        return True

    def _eh_deferred_tax_providers(self):
        """Models exposing eh_deferred_tax_temp_diffs(reporting_date), found by
        registry scan so producer modules (ECL, provisions, ...) stay
        independent SKUs with no hard dependency in either direction. Override
        to register a producer explicitly instead of by scan."""
        return [
            name for name, model in self.env.registry.models.items()
            if hasattr(model, 'eh_deferred_tax_temp_diffs')
        ]

    def action_gather_from_engines(self):
        """Pull temporary differences from the suite's IFRS engines (IFRS 9
        ECL loss allowance, IAS 37 provisions, and any module implementing the
        hook) into this run, so the category hooks are driven by a real
        producer rather than hand-keyed. Idempotent: previously auto-gathered
        lines are replaced; hand-keyed lines are left intact. Each provider is
        polled in its own try/except so one broken producer cannot sink the
        gather."""
        self.ensure_one()
        if self.state not in ('draft', 'computed'):
            raise UserError(_(
                "Gather is only available in draft or computed state."))
        Line = self.env['eh.deferred.tax.line']
        self.line_ids.filtered('eh_auto_gathered').sudo().unlink()
        seq = 1000
        created = 0
        for model_name in self._eh_deferred_tax_providers():
            model = self.env.get(model_name)
            if model is None:
                continue
            domain = ([('company_id', '=', self.company_id.id)]
                      if 'company_id' in model._fields else [])
            try:
                records = model.sudo().search(domain)
                diffs = (records.eh_deferred_tax_temp_diffs(self.period_end)
                         if records else [])
            except Exception:  # noqa: BLE001 - a provider must not sink gather
                _logger.warning(
                    "Deferred-tax provider %s failed on run %s; skipped.",
                    model_name, self.display_name)
                continue
            for d in (diffs or []):
                seq += 1
                Line.create({
                    'run_id': self.id,
                    'sequence': seq,
                    'eh_auto_gathered': True,
                    'name': d.get('name') or model_name,
                    'category': d.get('category', 'other'),
                    'nature': d.get('nature', 'asset'),
                    'carrying_amount': d.get('carrying_amount', 0.0),
                    'tax_base': d.get('tax_base', 0.0),
                    'through_oci': d.get('through_oci', False),
                })
                created += 1
        self.message_post(body=_(
            "Gathered %d temporary difference(s) from the IFRS engines.",
            created))
        return True

    def action_post(self):
        self = self._eh_workflow_action()
        for run in self:
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only an EH Accounting Manager can post a deferred tax "
                    "run."))
            # Serialise concurrent posts (a double click or a browser retry)
            # BEFORE reading state, so two transactions cannot both observe
            # 'computed', both build+post a move and both stamp 'posted' -
            # which would leave two posted deferred-tax entries and orphan the
            # first move. The loser re-reads the committed 'posted'/'reversed'
            # state and stops at the guard below.
            run._eh_lock_for_post()
            if run.state != 'computed':
                raise UserError(_("Run must be computed before posting."))
            run._validate_accounts()
            move = run._build_move()
            if not move:
                raise UserError(_(
                    "The deferred tax movement is nil; there is nothing to "
                    "post for %s.", run.display_name))
            run.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'move_id': move.id,
            })
        return True

    def action_reverse(self):
        self = self._eh_workflow_action()
        for run in self:
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only an EH Accounting Manager can reverse a deferred tax "
                    "run."))
            # Same double-submit guard as action_post: lock and re-read before
            # checking state / move_id so two concurrent reversals cannot both
            # build a reversal move for the one posted run.
            run._eh_lock_for_post()
            if run.state != 'posted':
                raise UserError(_("Only posted runs can be reversed."))
            if not run.move_id:
                raise UserError(_("Run has no posted move to reverse."))
            reversal = run.move_id._reverse_moves([{
                'date': run.period_end + timedelta(days=1),
                'journal_id': run.journal_id.id,
                'ref': _("Deferred tax reversal %s", run.name),
            }], cancel=False)
            reversal.action_post()
            run._eh_seal_reversal(reversal)
            run.with_context(eh_run_state_change=True).write({
                'state': 'reversed',
                'reversed_at': fields.Datetime.now(),
                'reversed_by_id': self.env.user.id,
                'reversal_move_id': reversal.id,
            })
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        for run in self:
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only an EH Accounting Manager can cancel a deferred tax "
                    "run."))
            if run.state in ('posted', 'reversed'):
                raise UserError(_(
                    "Cannot cancel a posted or reversed run."))
            run.state = 'cancelled'

    def action_set_to_draft(self):
        self = self._eh_workflow_action()
        for run in self:
            if run.state != 'cancelled':
                raise UserError(_(
                    "Only cancelled runs can return to draft."))
            run.state = 'draft'

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No movement entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    # ---- helpers ----

    def _eh_lock_for_post(self):
        """Take a row lock on this run and drop cached state so a serialised
        concurrent post/reverse re-reads the committed state rather than a
        stale pre-transition snapshot.

        Closes the double-submit race in which two transactions both read
        state=='computed', both build+post a deferred-tax move and both stamp
        'posted', producing two posted entries for one run and orphaning the
        first move. Mirrors eh_account_fx_revaluation's _eh_lock_for_post.
        """
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_deferred_tax_run WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        self.invalidate_recordset()

    def _resolve_line_rates(self):
        """Apply each line's resolved rate for this reporting date.

        A manual override or a jurisdiction table rate is re-applied on
        every compute (the table is authoritative; the override carries its
        reason). The statutory fallback only seeds a line whose rate is
        still empty, preserving the historical behaviour of keeping a rate
        keyed directly on the line. Lines without a jurisdiction (created
        before the rate table existed) are attached to the company default
        jurisdiction first, so offsetting and disclosure group correctly.
        """
        self.ensure_one()
        orphans = self.line_ids.filtered(lambda l: not l.jurisdiction_id)
        if orphans:
            default = self.env['eh.tax.jurisdiction']._get_company_default(
                self.company_id)
            orphans.write({'jurisdiction_id': default.id})
        for line in self.line_ids:
            if line.manual_rate:
                if line.tax_rate != line.manual_rate:
                    line.tax_rate = line.manual_rate
                continue
            table_rate = line.jurisdiction_id.rate_at(self.period_end)
            if table_rate is not None:
                if line.tax_rate != table_rate:
                    line.tax_rate = table_rate
            elif not line.tax_rate:
                # Statutory fallback seeds only an empty rate and never
                # overwrites a rate keyed directly on the line.
                line.tax_rate = self.statutory_rate

    def _roll_forward_opening(self):
        """Default each line's opening from the prior posted run's closing.

        Only lines whose opening is still nil (never keyed) are filled, so a
        manually entered opening is always preserved. Lines that already carry
        an opening keep it, and the per-line tie-out flag independently reports
        any disagreement with the prior closing for review. The opening RATE
        rolls forward with the balances so the rate-change remeasurement
        (IAS 12.60(b)) discloses against the rate actually applied last
        period; a still-empty opening rate on a manually keyed opening stays
        empty (no rate-change component, the historical behaviour). The
        run-level opening unrecognised-DTA figure rolls forward the same way
        for the reconciliation's unrecognised row.
        """
        self.ensure_one()
        currency = self.currency_id
        for line in self.line_ids:
            if not currency.is_zero(line.opening_dta or 0.0) \
                    or not currency.is_zero(line.opening_dtl or 0.0):
                continue
            prior = line._prior_closing()
            if not prior['found']:
                continue
            if not currency.is_zero(prior['dta']):
                line.opening_dta = prior['dta']
            if not currency.is_zero(prior['dtl']):
                line.opening_dtl = prior['dtl']
            if not line.opening_rate and prior['rate']:
                line.opening_rate = prior['rate']
        if currency.is_zero(self.opening_unrecognised_dta or 0.0):
            prior_run = self.env['eh.deferred.tax.run'].search([
                ('company_id', '=', self.company_id.id),
                ('state', '=', 'posted'),
                ('period_end', '<', self.period_end),
            ], order='period_end desc, id desc', limit=1)
            if prior_run:
                carried = (prior_run.unrecognised_dta
                           + prior_run.run_level_unrecognised_dta)
                if not currency.is_zero(carried):
                    self.opening_unrecognised_dta = carried

    def _rebuild_reconciliation(self):
        """Rebuild the auto rows of the IAS 12.81(c) reconciliation.

        Auto rows: expected tax, the permanent-difference header input,
        the rate-change remeasurement in profit or loss, the movement in
        unrecognised DTA (line-level plus run-level, against the opening
        figure), and a residual that balances the rows to the total tax
        expense. Manual rows (prior-year, credits, extra items) are
        preserved and enter the residual computation.
        """
        self.ensure_one()
        currency = self.currency_id
        self.recon_line_ids.filtered('is_auto').unlink()
        rows = [{
            'kind': 'expected',
            'name': _("Expected tax at statutory rate (%(rate)s%%)",
                      rate=self.statutory_rate),
            'amount': self.expected_tax,
            'sequence': 10,
            'is_auto': True,
        }]
        if not currency.is_zero(self.permanent_diff_tax):
            rows.append({
                'kind': 'permanent',
                'name': _("Permanent differences"),
                'amount': self.permanent_diff_tax,
                'sequence': 20,
                'is_auto': True,
            })
        if not currency.is_zero(self.rate_change_pl):
            rows.append({
                'kind': 'rate_change',
                'name': _("Remeasurement of opening balances at changed "
                          "rates"),
                'amount': self.rate_change_pl,
                'sequence': 30,
                'is_auto': True,
            })
        unrecognised_movement = (
            self.unrecognised_dta + self.run_level_unrecognised_dta
            - self.opening_unrecognised_dta)
        if not currency.is_zero(unrecognised_movement):
            rows.append({
                'kind': 'unrecognised',
                'name': _("Movement in unrecognised deferred tax assets"),
                'amount': unrecognised_movement,
                'sequence': 40,
                'is_auto': True,
            })
        explained = sum(r['amount'] for r in rows) \
            + sum(self.recon_line_ids.mapped('amount'))
        residual = self.total_tax_expense - explained
        if not currency.is_zero(residual):
            rows.append({
                'kind': 'other',
                'name': _("Other reconciling items (residual)"),
                'amount': residual,
                'sequence': 90,
                'is_auto': True,
            })
        self.write({'recon_line_ids': [(0, 0, vals) for vals in rows]})

    def _validate_accounts(self):
        self.ensure_one()
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        if not self.dta_account_id:
            missing.append(_("deferred tax asset account"))
        if not self.dtl_account_id:
            missing.append(_("deferred tax liability account"))
        if not self.deferred_tax_expense_account_id:
            missing.append(_("deferred tax expense account"))
        if self.line_ids.filtered(lambda l: l.through_oci) \
                and not self.oci_account_id:
            missing.append(_("OCI / equity account"))
        if missing:
            raise UserError(_(
                "Configure the %s on run %s before posting.",
                ', '.join(missing), self.display_name))

    def _build_move(self):
        """Post the period movement as one balanced entry.

        Deferred tax assets are debit-natured and liabilities credit-natured,
        so an increase in a DTA is a debit and an increase in a DTL a credit.
        The profit-or-loss / OCI counterpart is the balancing plug, which
        keeps the entry balanced to the cent regardless of rounding.

        Gross policy (default, historical behaviour): one aggregate DTA leg
        and one aggregate DTL leg. Net-by-jurisdiction policy (IAS 12.74):
        within each jurisdiction the closing and opening positions are
        netted (DTL less DTA) and the movement is posted as one net leg per
        jurisdiction per side, moving the balance between the DTA and DTL
        accounts when the net position flips sign. The per-jurisdiction net
        legs sum to the same total as the gross legs, so the OCI leg and
        the expense plug are identical under both policies and the entry
        still balances by construction.
        """
        self.ensure_one()
        currency = self.currency_id
        oci = self.line_ids.filtered(lambda l: l.through_oci)

        legs = []
        if self.offsetting_policy == 'net_by_jurisdiction':
            for jur, lines in self._jurisdiction_groups():
                net_closing = (sum(lines.mapped('closing_dtl'))
                               - sum(lines.mapped('closing_dta')))
                net_opening = (sum(lines.mapped('opening_dtl'))
                               - sum(lines.mapped('opening_dta')))
                dta_debit_j = currency.round(
                    max(-net_closing, 0.0) - max(-net_opening, 0.0))
                dtl_debit_j = currency.round(
                    -(max(net_closing, 0.0) - max(net_opening, 0.0)))
                label = jur.name if jur else _("(no jurisdiction)")
                legs.append((self.dta_account_id, dta_debit_j,
                             _("Deferred tax asset movement (%s)", label)))
                legs.append((self.dtl_account_id, dtl_debit_j,
                             _("Deferred tax liability movement (%s)",
                               label)))
        else:
            dta_debit = currency.round(
                sum(self.line_ids.mapped('movement_dta')))
            dtl_debit = currency.round(
                -sum(self.line_ids.mapped('movement_dtl')))
            legs.append((self.dta_account_id, dta_debit,
                         _("Deferred tax asset movement")))
            legs.append((self.dtl_account_id, dtl_debit,
                         _("Deferred tax liability movement")))

        oci_debit = currency.round(
            sum(oci.mapped('movement_dtl')) - sum(oci.mapped('movement_dta')))
        # Expense / income leg is the balancing plug for the P&L portion,
        # computed from the rounded balance-sheet legs so the entry always
        # balances to the cent.
        expense_debit = currency.round(
            -(sum(amount for _account, amount, _label in legs) + oci_debit))

        legs.append((self.oci_account_id, oci_debit,
                     _("Deferred tax in OCI")))
        legs.append((self.deferred_tax_expense_account_id, expense_debit,
                     _("Deferred tax expense / income")))
        lines = []
        for account, signed_debit, label in legs:
            if currency.is_zero(signed_debit) or not account:
                continue
            lines.append((0, 0, {
                'name': "%s %s" % (label, self.name),
                'account_id': account.id,
                'debit': signed_debit if signed_debit > 0 else 0.0,
                'credit': -signed_debit if signed_debit < 0 else 0.0,
            }))
        if not lines:
            return self.env['account.move']
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': self.period_end,
            'journal_id': self.journal_id.id,
            'ref': _("Deferred tax %s", self.name),
            'line_ids': lines,
            'eh_sealed': True,
        })
        move.action_post()
        return move
