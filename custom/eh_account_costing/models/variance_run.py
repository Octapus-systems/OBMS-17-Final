# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.cost.variance.run: the two-way variance decomposition of a period.

Sign convention (documented once, used everywhere): ADVERSE POSITIVE,
FAVOURABLE NEGATIVE. Every variance is "actual cost above standard", so a
positive amount is an adverse variance (a debit when posted) and a negative
amount is favourable (a credit when posted).

Formulas, per actual and element, with units = actual units produced and
each amount rounded to company currency (2dp) at the step shown:

  variable elements (material / labour / variable overhead)
    flexible  = round2(std_price x actual_qty)
    allowed   = std_qty_per_unit x units          (std qty allowed)
    absorbed  = round2(std_price x allowed)
    price-type variance = actual_cost - flexible
        material -> PRICE   = (actual price - std price) x actual qty
        labour   -> RATE    = (actual rate  - std rate ) x actual hours
        var. OH  -> SPEND   = actual VOH - std rate x actual driver qty
    quantity-type variance = flexible - absorbed
        material -> USAGE      = (actual qty - allowed) x std price
        labour   -> EFFICIENCY = (actual hrs - allowed) x std rate
        var. OH  -> EFFICIENCY = (driver qty - allowed) x std rate

  fixed overhead
    budget    = round2(fixed rate per unit x normal capacity)
    absorbed  = round2(fixed rate per unit x units)
    SPEND     = actual fixed OH - budget
    VOLUME    = budget - absorbed

Reconciliation identity (enforced by constraint and asserted after every
compute): because each element's two variances telescope
(price + quantity = actual - absorbed), the sum of ALL variance lines
equals total actual cost minus total standard cost absorbed, exactly, at
2dp. Total absorbed is the sum of the PER-ELEMENT rounded absorbed amounts
(the same figures inside the variance formulas), so the identity holds to
the cent by construction.

Posting is OPTIONAL and OFF by default (analysis-only mode): nothing
touches the ledger unless "Post Variances" is enabled and per-kind variance
accounts plus an absorption account are configured. The posted entry
aggregates lines per variance kind (net adverse = debit, net favourable =
credit) with the absorption account carrying the balancing leg; it is
sealed against edit and the run freezes with it.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .cost_card import COST_ELEMENTS, ELEMENT_ORDER

VARIANCE_KINDS = [
    ('price', "Price"),
    ('usage', "Usage"),
    ('rate', "Rate"),
    ('efficiency', "Efficiency"),
    ('spend', "Spend"),
    ('volume', "Volume"),
]

KIND_ORDER = [key for key, _label in VARIANCE_KINDS]

# element -> (price-type kind, quantity-type kind)
ELEMENT_KINDS = {
    'material': ('price', 'usage'),
    'labour': ('rate', 'efficiency'),
    'variable_overhead': ('spend', 'efficiency'),
    'fixed_overhead': ('spend', 'volume'),
}

LINE_LABELS = {
    ('material', 'price'): "Material price variance",
    ('material', 'usage'): "Material usage variance",
    ('labour', 'rate'): "Labour rate variance",
    ('labour', 'efficiency'): "Labour efficiency variance",
    ('variable_overhead', 'spend'): "Variable overhead spend variance",
    ('variable_overhead', 'efficiency'):
        "Variable overhead efficiency variance",
    ('fixed_overhead', 'spend'): "Fixed overhead spend variance",
    ('fixed_overhead', 'volume'): "Fixed overhead volume variance",
}


class EhCostVarianceRun(models.Model):
    _name = 'eh.cost.variance.run'
    _description = "Standard cost variance run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard',
                'eh.post.once']
    _order = 'period_start desc, id desc'

    # State moves only through the run's own actions (compute / post /
    # reset / cancel), never a direct write: a draft run's state is not
    # otherwise frozen, so a raw write({'state': 'posted'}) would skip
    # action_post and its sealed journal entry.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('computed', "Computed"),
         ('posted', "Posted"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True,
        copy=False)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    period_start = fields.Date(required=True, tracking=True)
    period_end = fields.Date(required=True, tracking=True)

    actual_ids = fields.Many2many(
        'eh.cost.actual', 'eh_cost_variance_run_actual_rel',
        'run_id', 'actual_id', string="Period Actuals",
        domain="[]",
        help="Actual captures decomposed by this run; each brings its own "
             "cost card.")

    line_ids = fields.One2many(
        'eh.cost.variance.line', 'run_id', string="Variance Lines",
        copy=False)

    total_actual_cost = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Sum of every element's actual cost across the selected "
             "actuals.")
    total_absorbed_cost = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        string="Total Standard Cost Absorbed",
        help="Sum of the per-element standard cost absorbed "
             "(std price x std qty allowed; fixed rate x output).")
    total_variance = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Total actual cost minus total standard cost absorbed; equals "
             "the sum of the variance lines exactly (adverse positive, "
             "favourable negative).")

    post_variances = fields.Boolean(
        default=False, tracking=True, string="Post Variances",
        help="OFF (default): analysis-only mode, nothing touches the "
             "ledger. ON: Post books the variance set as one sealed "
             "journal entry against the accounts below.")
    price_variance_account_id = fields.Many2one(
        'account.account', string="Price Variance Account", tracking=True)
    usage_variance_account_id = fields.Many2one(
        'account.account', string="Usage Variance Account", tracking=True)
    rate_variance_account_id = fields.Many2one(
        'account.account', string="Rate Variance Account", tracking=True)
    efficiency_variance_account_id = fields.Many2one(
        'account.account', string="Efficiency Variance Account",
        tracking=True)
    spend_variance_account_id = fields.Many2one(
        'account.account', string="Spend Variance Account", tracking=True)
    volume_variance_account_id = fields.Many2one(
        'account.account', string="Volume Variance Account", tracking=True)
    absorption_account_id = fields.Many2one(
        'account.account', string="Absorption Account", tracking=True,
        help="Carries the balancing leg of the variance entry (the net "
             "over- or under-absorption of standard cost).")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]")

    move_ids = fields.One2many(
        'account.move', 'eh_cost_variance_run_id', copy=False)
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    _sql_constraints = [
        ('check_period', 'CHECK (period_end >= period_start)', 'The period end cannot precede the period start.'),
    ]

    # Once posted, the run is the audit record behind a sealed journal
    # entry; its inputs and configuration freeze.
    _FROZEN_FIELDS = (
        'period_start', 'period_end', 'actual_ids', 'post_variances',
        'company_id', 'price_variance_account_id',
        'usage_variance_account_id', 'rate_variance_account_id',
        'efficiency_variance_account_id', 'spend_variance_account_id',
        'volume_variance_account_id', 'absorption_account_id', 'journal_id')
    _FROZEN_STATES = ('posted',)

    def _compute_move_count(self):
        for run in self:
            run.move_count = len(run.move_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.cost.variance.run') or '/'
        return super().create(vals_list)

    def write(self, vals):
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        if frozen:
            posted = self.filtered(
                lambda r: r.state in self._FROZEN_STATES)
            if posted:
                raise UserError(_(
                    "A posted variance run is frozen (%(fields)s); its "
                    "sealed journal entry must stay reconcilable to it.",
                    fields=', '.join(frozen)))
        if 'state' in vals \
                and not self.env.context.get('eh_costing_state_change'):
            crossing = self.filtered(
                lambda r: r.state in self._FROZEN_STATES
                and r.state != vals['state'])
            if crossing:
                raise UserError(_(
                    "A posted variance run cannot be re-keyed to another "
                    "state; its journal entry would be orphaned."))
        return super().write(vals)

    def unlink(self):
        posted = self.filtered(lambda r: r.state == 'posted')
        if posted:
            raise UserError(_(
                "A posted variance run cannot be deleted; its journal "
                "entry would be orphaned."))
        return super().unlink()

    # ---- reconciliation identity ----

    def _check_reconciliation(self):
        """sum(variance lines) == total actual - total absorbed, exactly.

        Called after every compute and re-checked from the line-level
        constraint, so the identity cannot silently break.
        """
        for run in self:
            if not run.line_ids:
                continue
            total = sum(run.line_ids.mapped('amount'))
            expected = run.total_actual_cost - run.total_absorbed_cost
            if run.currency_id.compare_amounts(total, expected) != 0:
                raise ValidationError(_(
                    "Variance run %(run)s no longer reconciles: the lines "
                    "sum to %(total).2f but actual minus absorbed is "
                    "%(expected).2f. Recompute the run.",
                    run=run.name, total=total, expected=expected))

    # ---- actions ----

    def action_compute(self):
        self = self._eh_workflow_action()
        self.ensure_one()
        if self.state not in ('draft', 'computed'):
            raise UserError(_(
                "Only a draft or computed run can be (re)computed."))
        if not self.actual_ids:
            raise UserError(_(
                "Select the period actuals to decompose first."))
        bad_company = self.actual_ids.filtered(
            lambda a: a.company_id != self.company_id)
        if bad_company:
            raise UserError(_(
                "Actuals %s belong to another company.",
                ', '.join(bad_company.mapped('name'))))
        draft_cards = self.actual_ids.card_id.filtered(
            lambda c: c.state == 'draft')
        if draft_cards:
            raise UserError(_(
                "Activate cost card(s) %s first; draft standards are not "
                "final.", ', '.join(draft_cards.mapped('display_name'))))

        engine = self.with_context(eh_costing_engine=True)
        engine.line_ids.unlink()
        currency = self.currency_id
        line_vals = []
        total_actual = total_absorbed = 0.0
        for actual in self.actual_ids:
            card = actual.card_id
            units = actual.units_produced or 0.0
            card_lines = {line_item.element: line_item for line_item in card.line_ids}
            act_lines = {line_item.element: line_item for line_item in actual.line_ids}
            for element in ELEMENT_ORDER:
                if element not in card_lines and element not in act_lines:
                    continue
                cline = card_lines.get(element)
                aline = act_lines.get(element)
                std_qty = cline.std_qty if cline else 0.0
                std_price = cline.std_price if cline else 0.0
                actual_qty = aline.actual_qty_total if aline else 0.0
                actual_cost = aline.actual_cost_total if aline else 0.0
                price_kind, qty_kind = ELEMENT_KINDS[element]
                if element == 'fixed_overhead':
                    rate = std_qty * std_price
                    budget = currency.round(
                        rate * (card.normal_capacity or 0.0))
                    absorbed = currency.round(rate * units)
                    price_var = currency.round(actual_cost - budget)
                    qty_var = currency.round(budget - absorbed)
                    allowed = card.normal_capacity or 0.0
                    flexible = budget
                else:
                    flexible = currency.round(std_price * actual_qty)
                    allowed = std_qty * units
                    absorbed = currency.round(std_price * allowed)
                    price_var = currency.round(actual_cost - flexible)
                    qty_var = currency.round(flexible - absorbed)
                total_actual += actual_cost
                total_absorbed += absorbed
                common = {
                    'run_id': self.id, 'actual_id': actual.id,
                    'element': element, 'std_price': round(std_price, 4),
                    'actual_qty': round(actual_qty, 4),
                    'std_qty_allowed': round(allowed, 4),
                    'actual_cost': actual_cost,
                    'flexible_amount': flexible,
                    'absorbed_amount': absorbed,
                }
                line_vals.append(dict(
                    common, kind=price_kind, amount=price_var,
                    name=LINE_LABELS[(element, price_kind)]))
                line_vals.append(dict(
                    common, kind=qty_kind, amount=qty_var,
                    name=LINE_LABELS[(element, qty_kind)]))
        self.env['eh.cost.variance.line'].with_context(
            eh_costing_engine=True).create(line_vals)
        self.write({
            'total_actual_cost': currency.round(total_actual),
            'total_absorbed_cost': currency.round(total_absorbed),
            'total_variance': currency.round(
                total_actual - total_absorbed),
            'state': 'computed',
        })
        # Defensive: the telescoping construction makes this exact; a
        # failure here is an engine bug, never user error.
        self._check_reconciliation()
        return True

    def action_post(self):
        """Book the variance set as ONE sealed journal entry, aggregated
        per variance kind: net adverse = debit, net favourable = credit,
        with the absorption account carrying the balancing leg. Refused in
        analysis-only mode (posting is opt-in per run, default OFF)."""
        self = self._eh_workflow_action()
        self.ensure_one()
        self._check_manager()
        if self.state != 'computed':
            raise UserError(_("Compute the variance run before posting."))
        # Idempotency: the same period actuals must not be booked twice. The
        # actual freeze only blocks EDITING an actual, not adding it to a
        # second run, so without this guard a second run over the same
        # actual_ids would double-count the period's variances to the GL.
        self._eh_assert_source_unposted('actual_ids')
        if not self.post_variances:
            raise UserError(_(
                "%s is in analysis-only mode (the default): the variance "
                "decomposition never touches the ledger. Enable Post "
                "Variances and configure the variance accounts to book "
                "the entry.", self.display_name))
        currency = self.currency_id
        by_kind = {}
        for line in self.line_ids:
            by_kind[line.kind] = by_kind.get(line.kind, 0.0) + line.amount
        by_kind = {k: currency.round(v) for k, v in by_kind.items()
                   if not currency.is_zero(currency.round(v))}
        if not by_kind:
            raise UserError(_(
                "Every variance nets to nil; there is nothing to post."))

        kind_accounts = {
            'price': self.price_variance_account_id,
            'usage': self.usage_variance_account_id,
            'rate': self.rate_variance_account_id,
            'efficiency': self.efficiency_variance_account_id,
            'spend': self.spend_variance_account_id,
            'volume': self.volume_variance_account_id,
        }
        missing = [_("journal")] if not self.journal_id else []
        if not self.absorption_account_id:
            missing.append(_("absorption account"))
        labels = dict(VARIANCE_KINDS)
        for kind in KIND_ORDER:
            if kind in by_kind and not kind_accounts[kind]:
                missing.append(_(
                    "%s variance account", labels[kind]))
        if missing:
            raise UserError(_(
                "Configure the %s on %s first.",
                ', '.join(missing), self.display_name))

        legs = []
        net_total = 0.0
        for kind in KIND_ORDER:
            if kind not in by_kind:
                continue
            amount = by_kind[kind]
            net_total += amount
            label = _("%(kind)s variance %(run)s",
                      kind=labels[kind], run=self.name)
            if amount > 0:
                legs.append((kind_accounts[kind], amount, 0.0, label))
            else:
                legs.append((kind_accounts[kind], 0.0, -amount, label))
        net_total = currency.round(net_total)
        if net_total > 0:
            legs.append((self.absorption_account_id, 0.0, net_total,
                         _("Under-absorption %s", self.name)))
        elif net_total < 0:
            legs.append((self.absorption_account_id, -net_total, 0.0,
                         _("Over-absorption %s", self.name)))
        self._post_move(legs)
        self.with_context(eh_costing_state_change=True).state = 'posted'
        return True

    def action_reset_to_draft(self):
        self = self._eh_workflow_action()
        self.ensure_one()
        if self.state not in ('computed', 'cancelled'):
            raise UserError(_(
                "Only a computed or cancelled run can go back to draft."))
        self.with_context(eh_costing_engine=True).line_ids.unlink()
        self.write({
            'total_actual_cost': 0.0, 'total_absorbed_cost': 0.0,
            'total_variance': 0.0, 'state': 'draft',
        })
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        for run in self:
            if run.state == 'posted':
                raise UserError(_(
                    "A posted variance run cannot be cancelled; its "
                    "journal entry would be orphaned."))
            run.state = 'cancelled'
        return True

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_cost_variance_run_id', '=', self.id)],
        }

    # ---- helpers ----

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post variance entries."))

    def _post_move(self, legs):
        lines = []
        for account, debit, credit, label in legs:
            lines.append((0, 0, {
                'name': label, 'account_id': account.id,
                'debit': debit, 'credit': credit,
            }))
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': self.period_end or fields.Date.context_today(self),
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_cost_variance_run_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class EhCostVarianceLine(models.Model):
    _name = 'eh.cost.variance.line'
    _description = "Standard cost variance line"
    _order = 'run_id, actual_id, id'

    run_id = fields.Many2one(
        'eh.cost.variance.run', required=True, ondelete='cascade',
        index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)
    actual_id = fields.Many2one(
        'eh.cost.actual', required=True, ondelete='restrict', index=True,
        string="Actuals")
    card_id = fields.Many2one(
        related='actual_id.card_id', store=True, string="Cost Card")

    name = fields.Char(required=True, string="Variance")
    element = fields.Selection(COST_ELEMENTS, required=True)
    kind = fields.Selection(VARIANCE_KINDS, required=True, index=True)

    std_price = fields.Float(
        digits=(16, 4), string="Std Price",
        help="Standard price / rate behind this decomposition.")
    actual_qty = fields.Float(
        digits=(16, 4), string="Actual Qty",
        help="Actual input / driver quantity of the element.")
    std_qty_allowed = fields.Float(
        digits=(16, 4), string="Std Qty Allowed",
        help="Standard quantity allowed for the actual output (for fixed "
             "overhead: the normal capacity).")
    actual_cost = fields.Monetary(
        currency_field='currency_id', string="Actual Cost")
    flexible_amount = fields.Monetary(
        currency_field='currency_id', string="Flexed Standard",
        help="Std price x actual quantity (for fixed overhead: the "
             "budget). The pivot between the price-type and quantity-type "
             "variances.")
    absorbed_amount = fields.Monetary(
        currency_field='currency_id', string="Standard Absorbed")
    amount = fields.Monetary(
        currency_field='currency_id', string="Variance",
        help="Adverse positive, favourable negative.")
    is_favourable = fields.Boolean(
        compute='_compute_is_favourable', string="Favourable")

    @api.depends('amount')
    def _compute_is_favourable(self):
        for line in self:
            line.is_favourable = line.amount < 0.0

    @api.constrains('amount')
    def _check_run_reconciles(self):
        # Belt to the engine's own assert: any amount write outside the
        # engine context re-verifies the run's reconciliation identity.
        if self.env.context.get('eh_costing_engine'):
            return
        self.mapped('run_id')._check_reconciliation()

    # Variance lines are engine output, never user input: any manual line
    # would break the reconciliation identity, and a line under a posted
    # run backs a sealed journal entry.
    def _check_engine(self, runs=None):
        runs = runs if runs is not None else self.mapped('run_id')
        posted = runs.filtered(lambda r: r.state == 'posted')
        if posted:
            raise UserError(_(
                "The variance lines of a posted run are frozen (%s).",
                ', '.join(posted.mapped('name'))))
        if not self.env.context.get('eh_costing_engine'):
            raise UserError(_(
                "Variance lines are computed by the run; recompute it "
                "instead of editing them."))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_engine(self.env['eh.cost.variance.run'].browse(
            [v['run_id'] for v in vals_list if v.get('run_id')]))
        return super().create(vals_list)

    def write(self, vals):
        self._check_engine()
        return super().write(vals)

    def unlink(self):
        self._check_engine()
        return super().unlink()


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_cost_variance_run_id = fields.Many2one(
        'eh.cost.variance.run', string="Variance Run", readonly=True,
        index=True, ondelete='restrict', copy=False)
