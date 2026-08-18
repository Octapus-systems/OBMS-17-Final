# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.cost.actual: one period's actual production capture for a cost card.

Manual, CSV-friendly entry: the units produced plus, per cost element, the
TOTAL input quantity and TOTAL cost for the period. The element lines mirror
the card's elements:

* material: total input quantity (e.g. kg) and total material cost;
* labour: total hours worked and total labour cost;
* variable overhead: the DRIVER quantity (normally the same actual hours as
  the labour line, when overhead is applied on labour hours) and the total
  variable overhead incurred;
* fixed overhead: total fixed overhead incurred (the quantity is unused and
  stays zero).

v1 scope note (documented, deliberate): there is NO stock-module coupling.
Actuals are keyed or imported, not pulled from stock moves or work orders;
inventory valuation integration is a later wave. This keeps the module
installable on any inventory setup, including none.

Freeze rule: once a POSTED variance run references an actual, its
measurement (card, period, units, element lines) is frozen; the posted
variance journal entry derived from these numbers must stay reconcilable to
them.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .cost_card import COST_ELEMENTS


class EhCostActual(models.Model):
    _name = 'eh.cost.actual'
    _description = "Period actual costs for a cost card"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'period_start desc, id desc'

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    card_id = fields.Many2one(
        'eh.cost.card', required=True, index=True, tracking=True,
        string="Cost Card", domain="[('state', 'in', ('active', 'superseded'))]",
        help="Standard cost card these actuals are measured against. The "
             "card must be activated (draft standards are not final).")
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    period_start = fields.Date(required=True, tracking=True)
    period_end = fields.Date(required=True, tracking=True)
    units_produced = fields.Float(
        digits=(16, 4), tracking=True, string="Units Produced",
        help="Actual output of the period; drives the standard quantity "
             "allowed and the fixed overhead absorbed.")

    line_ids = fields.One2many(
        'eh.cost.actual.line', 'actual_id', string="Actual Cost Elements")
    total_actual_cost = fields.Monetary(
        compute='_compute_total_actual_cost', store=True,
        currency_field='currency_id', string="Total Actual Cost")

    notes = fields.Text()

    _sql_constraints = [
        ('check_units', 'CHECK (units_produced >= 0)', 'Units produced cannot be negative.'),
        ('check_period', 'CHECK (period_end >= period_start)', 'The period end cannot precede the period start.'),
    ]

    _FROZEN_FIELDS = (
        'card_id', 'units_produced', 'period_start', 'period_end',
        'company_id')

    @api.depends('line_ids.actual_cost_total')
    def _compute_total_actual_cost(self):
        for actual in self:
            total = sum(actual.line_ids.mapped('actual_cost_total'))
            actual.total_actual_cost = (
                actual.currency_id.round(total) if actual.currency_id
                else round(total, 2))

    @api.constrains('card_id', 'company_id')
    def _check_card_company(self):
        for actual in self:
            if actual.card_id.company_id != actual.company_id:
                raise ValidationError(_(
                    "The cost card %(card)s belongs to %(card_company)s; "
                    "the actuals are captured in %(company)s.",
                    card=actual.card_id.display_name,
                    card_company=actual.card_id.company_id.display_name,
                    company=actual.company_id.display_name))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.cost.actual') or '/'
        return super().create(vals_list)

    def _posted_runs(self):
        """Posted variance runs referencing these actuals. Only called on
        existing records (write/unlink paths), so ids are real."""
        if not self.ids:
            return self.env['eh.cost.variance.run']
        return self.env['eh.cost.variance.run'].search([
            ('state', '=', 'posted'), ('actual_ids', 'in', self.ids)])

    def _check_open(self, actuals=None):
        actuals = actuals if actuals is not None else self
        posted = actuals._posted_runs()
        if posted:
            raise UserError(_(
                "These actuals feed the posted variance run(s) %s and are "
                "frozen; the posted variance entry must stay reconcilable "
                "to them.", ', '.join(posted.mapped('name'))))

    def write(self, vals):
        if any(f in vals for f in self._FROZEN_FIELDS):
            self._check_open()
        return super().write(vals)

    def unlink(self):
        # Draft-run references disappear with the run's lines; a posted run
        # would be orphaned of its evidence.
        self._check_open()
        runs = self.env['eh.cost.variance.run'].search([
            ('state', 'in', ('draft', 'computed')),
            ('actual_ids', 'in', self.ids)]) if self.ids else None
        if runs:
            raise UserError(_(
                "These actuals are selected on variance run(s) %s. Remove "
                "them from the run first.", ', '.join(runs.mapped('name'))))
        return super().unlink()


class EhCostActualLine(models.Model):
    _name = 'eh.cost.actual.line'
    _description = "Period actual cost element"
    _order = 'actual_id, id'

    actual_id = fields.Many2one(
        'eh.cost.actual', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='actual_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='actual_id.currency_id', store=True, readonly=True)

    element = fields.Selection(
        COST_ELEMENTS, required=True, default='material')
    actual_qty_total = fields.Float(
        digits=(16, 4), string="Actual Qty (Total)",
        help="Total input quantity of the period (kg, hours, driver units). "
             "Leave zero for fixed overhead.")
    actual_cost_total = fields.Monetary(
        currency_field='currency_id', string="Actual Cost (Total)")
    actual_price_unit = fields.Float(
        compute='_compute_actual_price_unit', digits=(16, 4),
        string="Actual Price",
        help="actual cost / actual quantity; informational.")

    _sql_constraints = [
        ('check_qty', 'CHECK (actual_qty_total >= 0)', 'An actual quantity cannot be negative.'),
        ('check_cost', 'CHECK (actual_cost_total >= 0)', 'An actual cost cannot be negative.'),
    ]

    @api.depends('actual_qty_total', 'actual_cost_total')
    def _compute_actual_price_unit(self):
        for line in self:
            line.actual_price_unit = round(
                line.actual_cost_total / line.actual_qty_total, 4) \
                if line.actual_qty_total else 0.0

    @api.constrains('element', 'actual_id')
    def _check_element_unique(self):
        # Cache-based sibling check (no search): safe on the create path.
        for line in self:
            siblings = line.actual_id.line_ids.filtered(
                lambda line_item: line_item.element == line.element)
            if len(siblings) > 1:
                raise ValidationError(_(
                    "%(actual)s already has a %(element)s line; one line "
                    "per element.",
                    actual=line.actual_id.display_name,
                    element=dict(COST_ELEMENTS)[line.element]))

    # The parent's measurement freezes once a posted variance run references
    # it; these lines feed that measurement, so they freeze with it at
    # create, write and unlink.
    @api.model_create_multi
    def create(self, vals_list):
        parents = self.env['eh.cost.actual'].browse(
            [v['actual_id'] for v in vals_list if v.get('actual_id')])
        parents._check_open()
        return super().create(vals_list)

    def write(self, vals):
        self.mapped('actual_id')._check_open()
        if vals.get('actual_id'):
            self.env['eh.cost.actual'].browse(
                vals['actual_id'])._check_open()
        return super().write(vals)

    def unlink(self):
        self.mapped('actual_id')._check_open()
        return super().unlink()
