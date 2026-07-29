# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.eps.run: a basic and diluted earnings-per-share computation for a period.

Basic EPS is earnings attributable to ordinary holders over the weighted
average number of ordinary shares. Diluted EPS adds potential ordinary shares
most-dilutive first, including each only while it continues to reduce EPS, so
anti-dilutive instruments never lift diluted EPS above basic (IAS 33.44).
When earnings are split between continuing and discontinued operations, the
run also reports basic and diluted EPS from continuing operations (the
IAS 33.66 headline figures), the discontinued per-share amount is disclosed
as the difference (IAS 33.68), and profit from continuing operations is the
control number for the dilution test (IAS 33.42-43). Bonus issues, splits
and consolidations are recorded as dated restatement events applied
retrospectively to the weighted average (IAS 33.26-28, 64).
This is a disclosure computation and posts no journal entries.
"""

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhEpsRun(models.Model):
    _name = 'eh.eps.run'
    _description = "Earnings per share run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_end desc, id desc'
    _rec_name = 'name'

    # State moves only through action_compute / action_cancel /
    # action_set_to_draft (each of which flags its write); a direct
    # write({'state': 'computed'}) that would skip the dilution compute and
    # the freeze is refused (eh.workflow.guard).
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('computed', "Computed"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    period_start = fields.Date(required=True, tracking=True)
    period_end = fields.Date(required=True, tracking=True)

    net_profit = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Profit or loss attributable to the parent for the period, "
             "before deducting preference dividends.")
    preference_dividends = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="After-tax preference dividends deducted to arrive at earnings "
             "attributable to ordinary holders (IAS 33.12).")
    profit_continuing = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Profit - continuing operations",
        help="Profit or loss from continuing operations attributable to "
             "the parent (IAS 33.66). Together with the discontinued result "
             "it must total net profit; leave both at zero to skip the "
             "split. Preference dividends are deducted from the continuing "
             "figure (IAS 33.12).")
    profit_discontinued = fields.Monetary(
        currency_field='currency_id', tracking=True,
        string="Profit - discontinued operations",
        help="Profit or loss from discontinued operations (IFRS 5.33). The "
             "per-share amount disclosed for discontinued operations is the "
             "difference between total and continuing EPS (IAS 33.68). Use "
             "Prefill Discontinued to pull this from the discontinued-"
             "operations tagged ledger accounts when the held-for-sale "
             "module is installed.")
    restatement_factor = fields.Float(
        default=1.0, digits=(16, 6), tracking=True,
        compute='_compute_restatement_factor', store=True, readonly=False,
        help="Cumulative retrospective adjustment factor (IAS 33.64). With "
             "restatement events recorded this is the product of their "
             "factors - the factor that also restates the comparative "
             "period's weighted average and EPS. Without events it stays a "
             "manual scalar: a single 2-for-1 split uses 2.0, doubling the "
             "share count for every period presented so EPS is comparable. "
             "Leave at 1.0 for no adjustment.")

    movement_ids = fields.One2many(
        'eh.eps.share.movement', 'run_id', copy=True)
    potential_ids = fields.One2many(
        'eh.eps.potential', 'run_id', copy=True)
    restatement_event_ids = fields.One2many(
        'eh.eps.restatement.event', 'run_id', copy=True)

    weighted_avg_shares = fields.Float(
        compute='_compute_weighted', store=True, digits=(16, 2),
        help="Weighted average number of ordinary shares outstanding.")
    basic_earnings = fields.Monetary(
        compute='_compute_basic', store=True, currency_field='currency_id')
    basic_eps = fields.Float(
        compute='_compute_basic', store=True, digits=(16, 6))
    basic_eps_continuing = fields.Float(
        compute='_compute_basic', store=True, digits=(16, 6),
        help="Basic EPS from continuing operations (IAS 33.66): continuing "
             "profit less preference dividends over the weighted average "
             "shares. Zero while the continuing/discontinued split is not "
             "used.")
    basic_eps_discontinued = fields.Float(
        compute='_compute_basic', store=True, digits=(16, 6),
        help="Basic EPS from discontinued operations, disclosed as the "
             "difference between total and continuing basic EPS "
             "(IAS 33.68).")

    diluted_earnings = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')
    diluted_shares = fields.Float(readonly=True, copy=False, digits=(16, 2))
    diluted_eps = fields.Float(readonly=True, copy=False, digits=(16, 6))
    diluted_eps_continuing = fields.Float(
        readonly=True, copy=False, digits=(16, 6),
        help="Diluted EPS from continuing operations (IAS 33.66). Profit "
             "from continuing operations is the control number for the "
             "dilution test (IAS 33.42-43).")
    diluted_eps_discontinued = fields.Float(
        readonly=True, copy=False, digits=(16, 6),
        help="Diluted EPS from discontinued operations, the difference "
             "between total and continuing diluted EPS (IAS 33.68).")

    has_held_for_sale = fields.Boolean(
        compute='_compute_has_held_for_sale',
        help="True when the held-for-sale module's discontinued-operations "
             "ledger hook is installed, enabling Prefill Discontinued.")
    has_restatement_events = fields.Boolean(
        compute='_compute_has_restatement_events',
        help="True when dated restatement events drive the cumulative "
             "factor, making the scalar an alias rather than an input.")

    notes = fields.Text()

    _sql_constraints = [
        ('check_period', 'CHECK (period_start <= period_end)', 'Period start must be on or before period end.'),
    ]

    # Comparability inputs are frozen once the run is computed. Editing the
    # earnings or share figures in place after computation would silently move
    # the reported weighted shares, earnings and EPS away from the figures that
    # were computed and disclosed, with no re-run and no audit trail. To change
    # them, set the run back to draft (which discards the computed figures) and
    # recompute (IAS 33: EPS for every period presented is comparable).
    _FROZEN_FIELDS = (
        'net_profit', 'preference_dividends', 'restatement_factor',
        'profit_continuing', 'profit_discontinued',
        'period_start', 'period_end', 'company_id',
    )

    def _eh_has_split(self):
        """True when the continuing/discontinued earnings split is used."""
        self.ensure_one()
        return bool(self.profit_continuing or self.profit_discontinued)

    def _compute_has_held_for_sale(self):
        available = 'eh.disposal.group' in self.env
        for run in self:
            run.has_held_for_sale = available

    @api.depends('restatement_event_ids')
    def _compute_has_restatement_events(self):
        for run in self:
            run.has_restatement_events = bool(run.restatement_event_ids)

    @api.constrains('net_profit', 'profit_continuing', 'profit_discontinued')
    def _check_operations_split(self):
        """When the continuing/discontinued split is used it must decompose
        net profit exactly (IAS 33.66): continuing plus discontinued equals
        the total attributable profit, or the disclosed per-share amounts
        would not tie back to the statements."""
        for run in self:
            if not run._eh_has_split():
                continue
            currency = run.currency_id or run.company_id.currency_id
            if currency.compare_amounts(
                    run.profit_continuing + run.profit_discontinued,
                    run.net_profit):
                raise ValidationError(_(
                    "Continuing (%(cont)s) plus discontinued (%(disc)s) "
                    "profit must equal net profit (%(net)s) - IAS 33.66.",
                    cont=run.profit_continuing,
                    disc=run.profit_discontinued,
                    net=run.net_profit))

    @api.depends('restatement_event_ids.factor')
    def _compute_restatement_factor(self):
        """Cumulative restatement factor (IAS 33.64).

        With restatement events recorded, the factor is the product of the
        event factors - every event in the current period falls after the
        comparative period start, so this product is also the factor that
        restates the comparative period's weighted average and EPS. Without
        events the manually entered scalar is preserved (compatibility
        alias for pre-event-line runs)."""
        for run in self:
            if run.restatement_event_ids:
                product = 1.0
                for ev in run.restatement_event_ids:
                    product *= ev.factor or 1.0
                run.restatement_factor = product
            else:
                run.restatement_factor = run.restatement_factor or 1.0

    @api.depends('movement_ids.effective_date',
                 'movement_ids.shares_outstanding',
                 'period_start', 'period_end', 'restatement_factor',
                 'restatement_event_ids.date',
                 'restatement_event_ids.factor')
    def _compute_weighted(self):
        for run in self:
            if run.restatement_event_ids:
                # Per-movement retrospective factors are applied inside
                # _weighted_average; multiplying by the cumulative scalar as
                # well would double-restate.
                run.weighted_avg_shares = run._weighted_average()
            else:
                factor = run.restatement_factor or 1.0
                run.weighted_avg_shares = run._weighted_average() * factor

    @api.depends('net_profit', 'preference_dividends', 'weighted_avg_shares',
                 'profit_continuing', 'profit_discontinued')
    def _compute_basic(self):
        for run in self:
            run.basic_earnings = run.net_profit - run.preference_dividends
            wa = run.weighted_avg_shares
            run.basic_eps = run.basic_earnings / wa if wa else 0.0
            if wa and run._eh_has_split():
                # Preference dividends are deducted from continuing
                # operations (IAS 33.12); the discontinued per-share amount
                # is disclosed as the difference (IAS 33.68).
                run.basic_eps_continuing = (
                    run.profit_continuing - run.preference_dividends) / wa
                run.basic_eps_discontinued = (
                    run.basic_eps - run.basic_eps_continuing)
            else:
                run.basic_eps_continuing = 0.0
                run.basic_eps_discontinued = 0.0

    def _weighted_average(self):
        """Day-weighted average shares with retrospective restatement.

        Each movement's share count applies from its effective date to the
        day before the next movement. Restatement events (bonus, split,
        consolidation) apply retrospectively (IAS 33.64): a movement
        recorded BEFORE an event's date is multiplied by that event's
        factor, as if the event had always been in effect; a movement from
        the event date on is taken to already carry the post-event count.
        """
        self.ensure_one()
        if not self.period_start or not self.period_end:
            return 0.0
        total_days = (self.period_end - self.period_start).days + 1
        if total_days <= 0:
            return 0.0
        movements = self.movement_ids.sorted('effective_date')
        events = self.restatement_event_ids
        weighted = 0.0
        for idx, mv in enumerate(movements):
            start = max(mv.effective_date, self.period_start)
            if idx + 1 < len(movements):
                seg_end = movements[idx + 1].effective_date - timedelta(days=1)
            else:
                seg_end = self.period_end
            end = min(seg_end, self.period_end)
            if end < start:
                continue
            days = (end - start).days + 1
            shares = mv.shares_outstanding
            for ev in events:
                if ev.date and ev.date > mv.effective_date:
                    shares *= ev.factor or 1.0
            weighted += shares * days
        return weighted / total_days

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.eps.run') or '/'
        return super().create(vals_list)

    def write(self, vals):
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        if frozen:
            computed = self.filtered(lambda r: r.state == 'computed')
            if computed:
                raise UserError(_(
                    "Comparability inputs (%(fields)s) are frozen on a "
                    "computed run. Set it back to draft, then recompute "
                    "(IAS 33).",
                    fields=', '.join(frozen)))
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can compute an EPS run."))

    def action_compute(self):
        self._check_manager()
        self = self._eh_workflow_action()
        for run in self:
            if run.state == 'cancelled':
                raise UserError(_("Cannot compute a cancelled run."))
            run._compute_diluted()
            run.state = 'computed'
        return True

    def _compute_diluted(self):
        """Sequence the dilution test: add potential shares most-dilutive
        first, including each only while it reduces EPS (IAS 33.44).

        Control number (IAS 33.42-43): when earnings are split between
        continuing and discontinued operations, an instrument is dilutive
        only when it reduces EPS from CONTINUING operations; the same set
        of included instruments then feeds the total and discontinued
        diluted figures. Without a split, the control number is total basic
        earnings, which is identical behaviour.
        """
        self.ensure_one()
        num = self.basic_earnings
        den = self.weighted_avg_shares
        has_split = self._eh_has_split()
        # Earnings adjustments (interest on convertibles, preference
        # dividends avoided) belong to continuing operations, so both
        # numerators move by the same adjustment when a class is included.
        ctl = (self.profit_continuing - self.preference_dividends
               if has_split else num)
        # The dilution test flips is_dilutive on the child lines while the run
        # is (about to be) computed. Bypass the child freeze for this internal
        # write only; user edits to the child figures stay blocked.
        potentials = self.potential_ids.with_context(eh_eps_compute=True)
        potentials.write({'is_dilutive': False})
        if not den:
            self.diluted_earnings = num
            self.diluted_shares = den
            self.diluted_eps = self.basic_eps
            self.diluted_eps_continuing = self.basic_eps_continuing
            self.diluted_eps_discontinued = self.basic_eps_discontinued
            return
        current_ctl = ctl / den
        factor = self.restatement_factor or 1.0
        # Order by incremental EPS ascending (most dilutive first). Iterate the
        # bypass-context recordset so setting is_dilutive is not blocked by the
        # child freeze on an already-computed run being recomputed.
        for pot in potentials.sorted('incremental_eps'):
            # Treasury-stock net increment (equals gross shares when the
            # method does not apply), restated for any bonus/split so the
            # diluted denominator matches the retrospectively restated basic
            # denominator (IAS 33.64).
            increment = pot.net_incremental_shares * factor
            if not increment:
                continue
            test_ctl_num = ctl + pot.earnings_adjustment
            test_den = den + increment
            test_ctl = (test_ctl_num / test_den if test_den
                        else current_ctl)
            if test_ctl < current_ctl:
                ctl, den, current_ctl = test_ctl_num, test_den, test_ctl
                num += pot.earnings_adjustment
                pot.is_dilutive = True
            else:
                # Anti-dilutive: since ordered, all remaining are too.
                break
        self.diluted_earnings = num
        self.diluted_shares = den
        self.diluted_eps = num / den if den else self.basic_eps
        if has_split:
            self.diluted_eps_continuing = ctl / den if den else 0.0
            self.diluted_eps_discontinued = (
                self.diluted_eps - self.diluted_eps_continuing)
        else:
            self.diluted_eps_continuing = 0.0
            self.diluted_eps_discontinued = 0.0

    def action_prefill_discontinued(self):
        """Prefill the continuing/discontinued split from the ledger.

        Soft integration with the held-for-sale module (registry lookup, no
        hard dependency): the posted P&L total of the discontinued-
        operations tagged accounts over the run period (IFRS 5.33) becomes
        the discontinued result, and the remainder of net profit stays
        continuing. The figures remain editable in draft, so a manual
        override is always possible; without the module the split stays
        fully manual.
        """
        for run in self:
            if run.state != 'draft':
                raise UserError(_(
                    "Prefill the split while %s is in draft; a computed "
                    "run's inputs are frozen (IAS 33).", run.display_name))
            if 'eh.disposal.group' not in self.env:
                raise UserError(_(
                    "Install the held-for-sale module to prefill the "
                    "discontinued result from the ledger; without it, "
                    "enter the continuing/discontinued split manually."))
            currency = run.currency_id or run.company_id.currency_id
            amount = self.env['eh.disposal.group'].eh_discontinued_pl_amount(
                run.period_start, run.period_end, run.company_id)
            run.write({
                'profit_discontinued': amount,
                'profit_continuing': currency.round(
                    run.net_profit - amount),
            })
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        for run in self:
            run.state = 'cancelled'

    def action_set_to_draft(self):
        self = self._eh_workflow_action()
        for run in self:
            run.state = 'draft'
