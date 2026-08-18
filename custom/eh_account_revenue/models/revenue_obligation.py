# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.revenue.obligation: one performance obligation on a revenue contract.

The transaction price is allocated across obligations in proportion to their
standalone selling prices (IFRS 15.74). An obligation is satisfied at a point
in time (recognise the full allocated price once marked satisfied) or over
time (recognise the allocated price by percentage of completion, IFRS
15.35-38). recognised_amount is the cumulative revenue already posted; the
recognition run posts the difference to the target.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_round

_FROZEN = frozenset({'done', 'cancelled'})


class EhRevenueObligation(models.Model):
    _name = 'eh.revenue.obligation'
    _description = "Performance obligation"
    _inherit = ['eh.workflow.guard']
    _order = 'contract_id, sequence, id'

    # recognised_amount is the cumulative-posted anchor (its own help text:
    # "cumulative revenue already posted"). It is the baseline the recognition
    # run trusts (to_recognise = target - recognised_amount) and the IFRS
    # 15.105-107 contract-asset/liability figure, so it may change ONLY through
    # the contract's sanctioned recognition run, which posts the balanced GL
    # move first and then elevates the write through _eh_workflow_write. The
    # shared eh.workflow.guard refuses every other write: provenance is proven
    # by env.su, NOT a context flag (Odoo passes client-supplied context
    # straight into call_kw, so a context sentinel is forgeable by the client
    # and gives no real protection). readonly=True on the field is only a
    # client/view hint the ORM does not enforce.
    _eh_guarded_fields = ('recognised_amount',)

    contract_id = fields.Many2one(
        'eh.revenue.contract', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        related='contract_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='contract_id.currency_id', store=True, readonly=True)

    name = fields.Char(
        required=True, help="Distinct good or service promised to the "
        "customer (IFRS 15.22-30).")
    standalone_price = fields.Monetary(
        currency_field='currency_id', required=True,
        help="Standalone selling price used to allocate the transaction "
             "price (IFRS 15.76-80).")
    allocated_price = fields.Monetary(
        compute='_compute_allocated', store=True, currency_field='currency_id',
        help="Share of the transaction price allocated to this obligation, "
             "including any specifically allocated discount and any "
             "constrained variable consideration.")

    # ---- variable consideration + constraint (IFRS 15.50-59) ----
    # Opt-in per obligation. When off, all three fields are zero and the
    # allocated price is the plain pro-rata share, byte-identical to before.
    variable_consideration = fields.Boolean(
        help="This obligation carries variable consideration (a bonus, "
             "rebate, penalty or similar) that must be estimated and "
             "constrained (IFRS 15.50-59).")
    variable_method = fields.Selection(
        [('expected_value', "Expected value"),
         ('most_likely', "Most likely amount")],
        default='expected_value',
        help="Estimation method for the variable amount: the probability "
             "weighted expected value, or the single most likely outcome "
             "(IFRS 15.53).")
    variable_estimate = fields.Monetary(
        currency_field='currency_id',
        help="Estimated variable consideration before the constraint: the "
             "expected value or the most likely amount depending on the "
             "chosen method (IFRS 15.53).")
    variable_constraint = fields.Monetary(
        currency_field='currency_id',
        help="Cap on the variable amount: only the portion that is highly "
             "probable not to reverse is included in the transaction price "
             "(IFRS 15.56). Leave zero to include none of the estimate.")
    variable_included = fields.Monetary(
        compute='_compute_allocated', store=True, currency_field='currency_id',
        help="Constrained variable consideration actually added to the "
             "allocated price: min(estimate, constraint), never negative.")

    # ---- specific discount allocation (IFRS 15.81-83) ----
    # Opt-in per obligation. When zero the pro-rata allocation is unchanged.
    discount_specific = fields.Monetary(
        currency_field='currency_id',
        help="A bundle discount that observably relates only to this "
             "obligation is deducted from its allocated price rather than "
             "spread pro-rata across the contract (IFRS 15.82).")

    satisfaction = fields.Selection(
        [('point_in_time', "Point in time"), ('over_time', "Over time")],
        default='point_in_time', required=True)
    percent_complete = fields.Float(
        digits=(5, 2), default=0.0,
        help="Progress towards complete satisfaction, for over-time "
             "obligations (0-100). Entered manually for the milestone and "
             "time-elapsed methods; computed from the cost or unit drivers "
             "for the costs-incurred and units-delivered methods.")
    satisfied = fields.Boolean(
        help="Mark a point-in-time obligation as satisfied so its full "
             "allocated price is recognised.")

    # ---- measurement of progress (IFRS 15.39-45, B14-B19) ----
    # The measurement method is a required, documented choice for over-time
    # obligations. Output methods measure the value transferred to the
    # customer directly (IFRS 15.B15-B17); input methods measure the
    # entity's efforts or inputs (IFRS 15.B18-B19). Records created before
    # the field existed default to output milestones with a 'migrated'
    # basis note, so behaviour on upgrade is byte-identical.
    progress_method = fields.Selection(
        [('output_milestones', "Output: milestones reached"),
         ('output_units', "Output: units delivered"),
         ('input_cost', "Input: costs incurred"),
         ('input_time', "Input: time elapsed")],
        default='output_milestones',
        help="How progress toward complete satisfaction is measured for an "
             "over-time obligation (IFRS 15.39-45). Output methods "
             "(B15-B17) appraise the value transferred to the customer; "
             "input methods (B18-B19) appraise the entity's inputs. Costs "
             "incurred and units delivered drive the percentage complete "
             "automatically; milestones and time elapsed keep a manual "
             "percentage backed by the basis note.")
    method_basis = fields.Text(
        default='migrated',
        help="Why the chosen method faithfully depicts the transfer of "
             "control of the good or service to the customer (IFRS 15.B14). "
             "Required for over-time obligations. Records migrated from "
             "before the method existed carry the note 'migrated'.")
    cost_incurred = fields.Monetary(
        currency_field='currency_id',
        help="Costs incurred to date toward satisfying the obligation. "
             "With the costs-incurred input method the percentage complete "
             "is cost incurred over the total expected cost "
             "(IFRS 15.B18-B19); it cannot be typed directly.")
    cost_total_estimate = fields.Monetary(
        currency_field='currency_id',
        help="Total expected cost to fully satisfy the obligation, the "
             "denominator of the cost-to-cost measure. Revising it "
             "reprofiles the percentage complete; the next recognition run "
             "posts the balanced catch-up or reversal.")
    units_delivered = fields.Float(
        help="Units delivered to the customer to date. With the "
             "units-delivered output method the percentage complete is "
             "units delivered over total units (IFRS 15.B15); it cannot be "
             "typed directly.")
    units_total = fields.Float(
        help="Total units promised to the customer, the denominator of the "
             "units-delivered measure.")

    # Set by a prospective contract modification (IFRS 15.21(a)): the
    # obligation's allocation is pinned to what has already been recognised and
    # it takes no share of the remaining transaction price going forward. Zero
    # by default, so the ordinary allocation is byte-identical.
    allocation_frozen = fields.Boolean(
        copy=False,
        help="Allocation pinned by a prospective modification; this "
             "obligation takes no share of the remaining transaction price.")
    frozen_allocation = fields.Monetary(
        copy=False, currency_field='currency_id',
        help="Allocated price locked in at the point of a prospective "
             "modification.")

    target_recognised = fields.Monetary(
        compute='_compute_target', store=True, currency_field='currency_id',
        help="Cumulative revenue that should be recognised to date.")
    recognised_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Cumulative revenue already posted for this obligation.")
    to_recognise = fields.Monetary(
        compute='_compute_target', store=True, currency_field='currency_id',
        help="Increment still to post (target less already recognised).")

    _sql_constraints = [
        ('check_ssp', 'CHECK (standalone_price >= 0)', 'Standalone selling price cannot be negative.'),
        ('check_pct', 'CHECK (percent_complete >= 0 AND percent_complete <= 100)', 'Percent complete must be between 0 and 100.'),  # noqa: E501
        ('check_var_estimate', 'CHECK (variable_estimate >= 0)', 'Variable consideration estimate cannot be negative.'),
        ('check_var_constraint', 'CHECK (variable_constraint >= 0)', 'Variable consideration constraint cannot be negative.'),  # noqa: E501
        ('check_specific_discount', 'CHECK (discount_specific >= 0)', 'A specific discount cannot be negative.'),
        ('check_cost_incurred', 'CHECK (cost_incurred >= 0)', 'Cost incurred cannot be negative.'),
        ('check_cost_total', 'CHECK (cost_total_estimate >= 0)', 'The total cost estimate cannot be negative.'),
        ('check_units_delivered', 'CHECK (units_delivered >= 0)', 'Units delivered cannot be negative.'),
        ('check_units_total', 'CHECK (units_total >= 0)', 'Total units cannot be negative.'),
    ]

    # Methods whose percentage complete is derived from stored drivers, so a
    # typed percentage is refused (the drivers are the audit evidence).
    _PROGRESS_DRIVEN = ('input_cost', 'output_units')

    @api.constrains('satisfaction', 'progress_method', 'method_basis')
    def _check_progress_method(self):
        # IFRS 15.39-41 + B14: an over-time obligation must state how
        # progress is measured and why that method depicts the transfer of
        # control. Point-in-time obligations carry the defaults untouched.
        for ob in self:
            if ob.satisfaction != 'over_time':
                continue
            if not ob.progress_method:
                raise ValidationError(_(
                    "Over-time obligation %s needs a progress measurement "
                    "method (IFRS 15.39-41).", ob.display_name))
            if not (ob.method_basis or '').strip():
                raise ValidationError(_(
                    "Over-time obligation %s needs a basis note explaining "
                    "why the chosen method depicts the transfer of control "
                    "(IFRS 15.B14).", ob.display_name))

    @staticmethod
    def _derive_percent(numerator, denominator):
        """Percentage complete implied by the progress drivers, capped to
        [0, 100] and rounded to the field's 2dp so the stored value is
        exactly what the recognition run uses. A zero denominator means no
        measurable progress basis yet, so 0."""
        if denominator <= 0.0:
            return 0.0
        pct = numerator / denominator * 100.0
        return float_round(min(max(pct, 0.0), 100.0), precision_digits=2)

    def _driver_percent(self):
        """Percentage implied by this obligation's drivers, or None when the
        method keeps a manual percentage."""
        self.ensure_one()
        if self.progress_method == 'input_cost':
            return self._derive_percent(
                self.cost_incurred, self.cost_total_estimate)
        if self.progress_method == 'output_units':
            return self._derive_percent(self.units_delivered, self.units_total)
        return None

    def _sync_driver_progress(self):
        """Re-derive percent_complete from the drivers after they change.
        The internal write carries the sync context so the manual-entry
        guard lets it through; recognition itself still flows through the
        normal run, posting a balanced catch-up or reversal."""
        for ob in self:
            pct = ob._driver_percent()
            if pct is not None and float_compare(
                    pct, ob.percent_complete, precision_digits=2) != 0:
                ob.with_context(eh_revenue_progress_sync=True).write(
                    {'percent_complete': pct})

    @api.depends('standalone_price', 'contract_id.transaction_price',
                 'contract_id.total_ssp',
                 'contract_id.obligation_ids.discount_specific',
                 'contract_id.obligation_ids.allocation_frozen',
                 'contract_id.obligation_ids.frozen_allocation',
                 'variable_consideration', 'variable_method',
                 'variable_estimate', 'variable_constraint')
    def _compute_allocated(self):
        for ob in self:
            currency = ob.currency_id
            # Constrained variable consideration (IFRS 15.50-59): include only
            # the portion that is highly probable not to reverse, i.e. capped
            # at the constraint and never negative. Off => zero, no effect.
            if ob.variable_consideration:
                estimate = ob.variable_estimate
                included = min(max(estimate, 0.0), max(ob.variable_constraint,
                                                       0.0))
            else:
                included = 0.0
            ob.variable_included = (
                currency.round(included) if currency else included)

            # A prospective modification (IFRS 15.21(a)) pins this obligation's
            # allocation to what was locked in; it takes no share of the
            # remaining transaction price.
            if ob.allocation_frozen:
                ob.allocated_price = (
                    currency.round(ob.frozen_allocation) if currency
                    else ob.frozen_allocation)
                continue

            contract = ob.contract_id
            siblings = contract.obligation_ids
            frozen = siblings.filtered('allocation_frozen')
            price = contract.transaction_price
            # Remaining consideration and remaining SSP after removing any
            # frozen obligations. With no frozen obligation these equal the
            # contract totals, so the ordinary allocation below is unchanged.
            price -= sum(frozen.mapped('frozen_allocation'))
            open_obs = siblings - frozen
            total = sum(open_obs.mapped('standalone_price'))
            if not total:
                ob.allocated_price = ob.variable_included
                continue
            specific_total = sum(open_obs.mapped('discount_specific'))
            if not specific_total:
                # Unchanged pro-rata allocation (byte-identical default).
                base = price * ob.standalone_price / total
            else:
                # Discount allocation (IFRS 15.81-83): a discount that
                # observably relates only to specific obligations is deducted
                # from those obligations' standalone selling price; every other
                # obligation is allocated its full SSP. The customer pays the
                # SSP total less the specific discounts, so the allocated
                # prices sum to that amount by construction. The residual
                # (transaction_price less SSP-minus-discount total) is spread
                # pro-rata by SSP so any further whole-bundle discount still
                # allocates, but the specific portion is not pro-rated.
                net_total = total - specific_total
                residual = price - net_total
                net_base = ob.standalone_price - ob.discount_specific
                pro_rata = (residual * ob.standalone_price / total
                            if total else 0.0)
                base = net_base + pro_rata
            allocated = base + ob.variable_included
            ob.allocated_price = (
                currency.round(allocated) if currency else allocated)

    @api.depends('allocated_price', 'satisfaction', 'percent_complete',
                 'satisfied', 'recognised_amount')
    def _compute_target(self):
        for ob in self:
            if ob.satisfaction == 'over_time':
                target = ob.allocated_price * ob.percent_complete / 100.0
            else:
                target = ob.allocated_price if ob.satisfied else 0.0
            currency = ob.currency_id
            ob.target_recognised = (
                currency.round(target) if currency else target)
            ob.to_recognise = ob.target_recognised - ob.recognised_amount

    @api.model_create_multi
    def create(self, vals_list):
        # Guard the child line feeding a frozen or posted parent. A closed or
        # cancelled contract never accepts new obligations. A contract that has
        # already posted revenue only accepts new obligations through the
        # sanctioned modification path (prospective / separate), which sets the
        # eh_revenue_modification context and re-runs recognition so nothing is
        # silently restated.
        modifying = self.env.context.get('eh_revenue_modification')
        # Driver-measured methods derive the percentage from their drivers
        # from the first record: any typed percentage is overridden by the
        # derived one, so a driver obligation can never start from a manual
        # figure the drivers do not support.
        for vals in vals_list:
            method = vals.get('progress_method') or 'output_milestones'
            if method == 'input_cost':
                vals['percent_complete'] = self._derive_percent(
                    vals.get('cost_incurred') or 0.0,
                    vals.get('cost_total_estimate') or 0.0)
            elif method == 'output_units':
                vals['percent_complete'] = self._derive_percent(
                    vals.get('units_delivered') or 0.0,
                    vals.get('units_total') or 0.0)
        contract_ids = {
            v['contract_id'] for v in vals_list if v.get('contract_id')}
        if contract_ids:
            contracts = self.env['eh.revenue.contract'].browse(contract_ids)
            for c in contracts:
                if c.state in _FROZEN:
                    raise UserError(_(
                        "This contract is closed; no performance obligation "
                        "can be added."))
                if c._has_posted_revenue() and not modifying:
                    raise UserError(_(
                        "Contract %s has posted revenue; add obligations "
                        "through a contract modification so the transaction "
                        "price is reallocated correctly.", c.display_name))
        return super().create(vals_list)

    def unlink(self):
        # Symmetric to create: an obligation cannot be removed from a closed or
        # posted contract outside a sanctioned modification, or the allocation
        # basis behind already-posted revenue would change with no entry.
        modifying = self.env.context.get('eh_revenue_modification')
        for ob in self:
            c = ob.contract_id
            if c.state in _FROZEN:
                raise UserError(_(
                    "This contract is closed; its performance obligations can "
                    "no longer be removed."))
            if c._has_posted_revenue() and not modifying:
                raise UserError(_(
                    "Contract %s has posted revenue; remove obligations "
                    "through a contract modification.", c.display_name))
        return super().unlink()

    def write(self, vals):
        # recognised_amount is guarded by the shared eh.workflow.guard mixin
        # (see _eh_guarded_fields above): a direct RPC/ORM write is refused for
        # any non-superuser regardless of context, and the sanctioned
        # recognition run advances it through _eh_workflow_write (env.su). No
        # local context-flag check is used here, because a context sentinel is
        # forgeable by the client and provides no real protection.
        # The parameters that drive recognition are frozen once the contract
        # is closed; recognised_amount is written only by the recognition run.
        locked = {'standalone_price', 'satisfaction', 'percent_complete',
                  'satisfied', 'contract_id', 'variable_consideration',
                  'variable_method', 'variable_estimate', 'variable_constraint',
                  'discount_specific', 'progress_method', 'method_basis',
                  'cost_incurred', 'cost_total_estimate', 'units_delivered',
                  'units_total'}
        if locked.intersection(vals) and any(
            c.state in _FROZEN for c in self.contract_id
        ):
            raise UserError(_(
                "This contract is closed; its performance obligations can no "
                "longer be changed."))
        # Once revenue has posted the allocation basis (standalone selling
        # price, satisfaction method, measurement method, parent contract) is
        # frozen: re-basing it would silently restate revenue already
        # recognised, with no matching journal entry. Progress levers
        # (percent_complete, satisfied, the cost/unit drivers) stay editable
        # because they flow through the recognition run, which posts a
        # balanced entry (upward) or a balanced reversal (downward
        # correction). One sanctioned exception: a constraint review
        # (IFRS 15.56) may revise the variable-consideration estimate and
        # constraint under its own context; its apply action re-runs
        # recognition so the change lands as a balanced catch-up and the
        # applied review is the audit trail.
        basis_locked = {'standalone_price', 'satisfaction', 'contract_id',
                        'variable_consideration', 'variable_method',
                        'variable_estimate', 'variable_constraint',
                        'discount_specific', 'progress_method'}
        if self.env.context.get('eh_revenue_constraint_review'):
            basis_locked -= {'variable_estimate', 'variable_constraint'}
        if basis_locked.intersection(vals) and any(
            c._has_posted_revenue() for c in self.contract_id
        ):
            raise UserError(_(
                "This contract has posted revenue; the standalone selling "
                "price, satisfaction and measurement methods, variable "
                "consideration and specific discount are frozen. Adjust "
                "progress and post a correction, or use a constraint review "
                "for the variable estimate."))
        # A driver-measured percentage is computed, never typed
        # (IFRS 15.B15/B18: the drivers are the evidence of progress). The
        # internal resync passes the sync context.
        if ('percent_complete' in vals
                and not self.env.context.get('eh_revenue_progress_sync')):
            for ob in self:
                method = vals.get('progress_method', ob.progress_method)
                if method in self._PROGRESS_DRIVEN:
                    raise UserError(_(
                        "The percentage complete on %s is computed from its "
                        "progress drivers (costs incurred or units "
                        "delivered); update the drivers instead of typing "
                        "the percentage.", ob.display_name))
        res = super().write(vals)
        if {'progress_method', 'cost_incurred', 'cost_total_estimate',
                'units_delivered', 'units_total'}.intersection(vals):
            self._sync_driver_progress()
        return res
