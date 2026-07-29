# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.nrv.line: one inventory item or group measured at the lower of cost and
net realisable value.

On the item-by-item basis the required write-down is the excess of cost
over net realisable value (IAS 2.9), floored at zero per line. On the
category basis (IAS 2.29 grouping of similar or related items) the run
nets surpluses and deficits within each product category before flooring
at zero, and allocates the category requirement over the deficit lines;
see eh.nrv.run._category_allocation for the allocation rules. Under both
bases a recovery only ever reverses a prior write-down and never lifts
inventory above cost (IAS 2.33).
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_FROZEN = frozenset({'posted', 'reversed'})


class EhNrvLine(models.Model):
    _name = 'eh.nrv.line'
    _description = "Inventory NRV line"
    _order = 'run_id, sequence, id'

    run_id = fields.Many2one(
        'eh.nrv.run', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    name = fields.Char(
        required=True, help="Inventory item or group, e.g. 'Finished goods - "
        "Model A' or 'Raw material X'.")
    product_id = fields.Many2one(
        'product.product', help="Optional link to a product.")
    product_category_id = fields.Many2one(
        'product.category', string="Category", index=True,
        compute='_compute_product_category', store=True, readonly=False,
        help="Group of similar or related items this line is assessed in "
             "when the run uses the category basis (IAS 2.29). Auto-filled "
             "from the product; required on every line of a category-basis "
             "run.")
    cost = fields.Monetary(
        currency_field='currency_id',
        help="Carrying cost of the inventory before any write-down.")
    net_realisable_value = fields.Monetary(
        currency_field='currency_id',
        help="Estimated selling price less the costs to complete and sell "
             "(IAS 2.6).")

    required_writedown = fields.Monetary(
        compute='_compute_writedown', store=True, currency_field='currency_id',
        help="Excess of cost over net realisable value, floored at zero.")
    opening_writedown = fields.Monetary(
        compute='_compute_opening_writedown', store=True, readonly=False,
        currency_field='currency_id',
        help="Write-down already recognised on this line at the start of the "
             "period. Defaults to the prior posted run's closing write-down "
             "for the same product; can be overridden.")
    opening_writedown_manual = fields.Boolean(
        default=False, copy=False,
        help="Set once a user overrides the rolled-forward opening write-down, "
             "so a later product or date change does not silently discard the "
             "manual value.")
    prior_closing_writedown = fields.Monetary(
        compute='_compute_prior_closing_writedown',
        currency_field='currency_id',
        help="Closing write-down carried by this product on the most recent "
             "posted NRV run, used to roll the opening position forward.")
    opening_tieout = fields.Boolean(
        compute='_compute_prior_closing_writedown',
        string="Opening Does Not Tie Out",
        help="Set when the opening write-down disagrees with the prior posted "
             "run's closing write-down for the same product.")
    movement = fields.Monetary(
        compute='_compute_writedown', store=True, currency_field='currency_id',
        help="Required less opening write-down; negative = recovery.")

    _sql_constraints = [
        ('check_cost', 'CHECK (cost >= 0)', 'Cost cannot be negative.'),
    ]

    @api.depends('product_id')
    def _compute_product_category(self):
        for line in self:
            if line.product_id:
                line.product_category_id = line.product_id.categ_id
            else:
                # Keep a manually chosen category on a free-text line.
                line.product_category_id = line.product_category_id

    @api.depends('cost', 'net_realisable_value', 'opening_writedown',
                 'run_id.assessment_basis', 'run_id.line_ids.cost',
                 'run_id.line_ids.net_realisable_value',
                 'run_id.line_ids.product_category_id')
    def _compute_writedown(self):
        # Category-basis runs are allocated run-wise so the per-line shares
        # tie exactly to each category's netted requirement (the rounding
        # residual lands on the last deficit line); see
        # eh.nrv.run._category_allocation for the IAS 2.29 rules.
        alloc = {}
        for run in self.run_id:
            if run.assessment_basis == 'category':
                alloc.update(run._category_allocation())
        for line in self:
            currency = line.currency_id
            if line.run_id and line.run_id.assessment_basis == 'category':
                required = alloc.get(line.id, 0.0)
            else:
                required = max(line.cost - line.net_realisable_value, 0.0)
                required = currency.round(required) if currency else required
            line.required_writedown = required
            line.movement = line.required_writedown - line.opening_writedown

    def _prior_closing_writedown(self):
        """Closing write-down for this line's product on the most recent
        posted NRV run before this run's reporting date. Zero when there is
        no product link or no prior posted run."""
        self.ensure_one()
        run = self.run_id
        if not self.product_id or not run.company_id:
            return 0.0
        cutoff = run.reporting_date or fields.Date.today()
        prior_run = self.env['eh.nrv.run'].search([
            ('company_id', '=', run.company_id.id),
            ('state', '=', 'posted'),
            ('reporting_date', '<', cutoff),
            ('line_ids.product_id', '=', self.product_id.id),
        ], order='reporting_date desc, id desc', limit=1)
        if not prior_run:
            return 0.0
        prior_line = prior_run.line_ids.filtered(
            lambda ln: ln.product_id == self.product_id)[:1]
        return prior_line.required_writedown if prior_line else 0.0

    @api.depends('product_id', 'run_id.reporting_date', 'company_id',
                 'opening_writedown_manual')
    def _compute_opening_writedown(self):
        for line in self:
            # Roll the opening position forward from the prior posted run,
            # UNLESS the user has manually overridden it. Without the manual
            # flag a later product/date change would silently re-fire this
            # compute and discard the user's value.
            if line.opening_writedown_manual:
                line.opening_writedown = line.opening_writedown
            else:
                line.opening_writedown = line._prior_closing_writedown()

    def _check_parent_not_posted(self):
        """Raise when any of these lines hangs off a posted/reversed run."""
        if any(r.state in _FROZEN for r in self.run_id):
            raise UserError(_(
                "This write-down run is posted; its lines can no longer "
                "change. Reverse it to reopen (EH Accounting Manager only)."))

    @api.model_create_multi
    def create(self, vals_list):
        # Adding a line to a posted run would recompute its closing write-down
        # and silently move the recognised movement, bypassing the freeze that
        # write()/unlink() enforce. Block it when the target run is posted or
        # reversed (IAS 2.34); reverse the run to reopen its lines.
        run_ids = {vals.get('run_id')
                   for vals in vals_list if vals.get('run_id')}
        if run_ids:
            frozen = self.env['eh.nrv.run'].browse(run_ids).filtered(
                lambda r: r.state in _FROZEN)
            if frozen:
                raise UserError(_(
                    "Lines cannot be added to a posted NRV run; its figures "
                    "are frozen. Reverse the run to reopen it (EH Accounting "
                    "Manager only)."))
        for vals in vals_list:
            # An explicit opening at create time is a manual override.
            if 'opening_writedown' in vals \
                    and 'opening_writedown_manual' not in vals:
                vals['opening_writedown_manual'] = True
        return super().create(vals_list)

    @api.depends('product_id', 'opening_writedown', 'run_id.reporting_date',
                 'company_id')
    def _compute_prior_closing_writedown(self):
        for line in self:
            prior = line._prior_closing_writedown()
            line.prior_closing_writedown = prior
            currency = line.currency_id
            diff = line.opening_writedown - prior
            line.opening_tieout = (
                not currency.is_zero(diff) if currency
                else abs(diff) > 1e-9)

    def write(self, vals):
        locked = {'cost', 'net_realisable_value', 'opening_writedown',
                  'opening_writedown_manual', 'product_id', 'run_id',
                  'product_category_id'}
        if locked.intersection(vals):
            self._check_parent_not_posted()
        # Moving a line INTO a posted / reversed run would recompute that run's
        # closing write-down. _check_parent_not_posted only inspects the
        # current (source) parent, so guard the target explicitly.
        if vals.get('run_id'):
            target = self.env['eh.nrv.run'].browse(vals['run_id'])
            if target.state in _FROZEN:
                raise UserError(_(
                    "Lines cannot be moved into a posted NRV run; its figures "
                    "are frozen. Reverse the run to reopen it (EH Accounting "
                    "Manager only)."))
        # A user-supplied opening write-down marks the line as manually
        # overridden so the roll-forward compute stops clobbering it on a
        # later product or date change.
        if 'opening_writedown' in vals \
                and 'opening_writedown_manual' not in vals:
            vals = dict(vals, opening_writedown_manual=True)
        return super().write(vals)

    def unlink(self):
        # Deleting a line from a posted run would drop it from the closing
        # write-down and move the recognised figure; block it while the parent
        # run is posted or reversed.
        self._check_parent_not_posted()
        return super().unlink()
