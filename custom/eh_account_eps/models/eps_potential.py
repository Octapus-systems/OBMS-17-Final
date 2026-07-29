# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.eps.potential: a class of dilutive potential ordinary shares.

Each instrument would, on conversion or exercise, add potential ordinary
shares to the denominator and (for convertibles) add back an after-tax
earnings amount to the numerator. Its incremental EPS (earnings adjustment
per potential share) sequences the dilution test: instruments are added most-
dilutive first, and only while they continue to reduce EPS (IAS 33.44).
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhEpsPotential(models.Model):
    _name = 'eh.eps.potential'
    _description = "EPS potential ordinary shares"
    _order = 'run_id, incremental_eps, id'

    run_id = fields.Many2one(
        'eh.eps.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    name = fields.Char(required=True)
    instrument_type = fields.Selection(
        [('options', "Options / warrants"),
         ('convertible_bond', "Convertible bond"),
         ('convertible_pref', "Convertible preference shares")],
        default='options', required=True)

    potential_shares = fields.Float(
        digits=(16, 2), required=True,
        help="Ordinary shares that would be issued on conversion or exercise "
             "(net of any assumed buy-back for options).")
    earnings_adjustment = fields.Monetary(
        currency_field='currency_id',
        help="After-tax amount added back to earnings on conversion, e.g. "
             "interest saved on a convertible bond or preference dividends "
             "avoided. Zero for options.")

    exercise_price = fields.Monetary(
        currency_field='currency_id',
        help="Option/warrant exercise price. With the average market price, "
             "enables the treasury-stock method (IAS 33.45-46): assumed "
             "proceeds are treated as buying back shares at the average "
             "market price, so only the net increment dilutes.")
    average_market_price = fields.Monetary(
        currency_field='currency_id',
        compute='_compute_average_market_price', store=True, readonly=False,
        help="Average market price of the ordinary share over the period. "
             "Used with the exercise price for the treasury-stock method "
             "(IAS 33.45-46). With dated price observations recorded, this "
             "resolves to the arithmetic mean of the observations inside "
             "the run period; without observations it stays a manual "
             "scalar.")
    observation_ids = fields.One2many(
        'eh.eps.price.observation', 'potential_id', copy=True,
        string="Price observations")
    has_observations = fields.Boolean(
        compute='_compute_has_observations',
        help="True when dated price observations resolve the average "
             "market price, making the scalar a computed result rather "
             "than an input.")

    net_incremental_shares = fields.Float(
        compute='_compute_net_incremental', store=True, digits=(16, 2),
        help="Shares actually added to the diluted denominator. For options "
             "under the treasury-stock method this is potential shares net of "
             "the assumed buy-back; out-of-the-money options add zero. For "
             "all other cases it equals the gross potential shares.")

    incremental_eps = fields.Float(
        compute='_compute_incremental', store=True, digits=(16, 6),
        help="Earnings adjustment per potential share; the ordering key for "
             "the dilution test (lower is more dilutive).")
    is_dilutive = fields.Boolean(
        readonly=True, copy=False,
        help="Set by the run when the instrument is included as dilutive.")

    _sql_constraints = [
        ('check_potential', 'CHECK (potential_shares >= 0)', 'Potential shares cannot be negative.'),
    ]

    def _check_run_frozen(self):
        # Potential-share lines drive the diluted denominator and numerator, so
        # editing or deleting one after the run is computed would silently move
        # diluted EPS away from the disclosed figure. A parent-only guard would
        # be bypassed by editing the child directly, so freeze here too.
        for pot in self:
            if pot.run_id.state == 'computed':
                raise UserError(_(
                    "Potential-share lines are frozen once run %s is computed. "
                    "Set the run back to draft to change them (IAS 33).",
                    pot.run_id.display_name))

    @api.model_create_multi
    def create(self, vals_list):
        # Appending a potential-share line to an already-computed run would
        # silently move the diluted denominator/numerator. Block create on a
        # frozen run unless it is the run's own compute writing the line.
        if not self.env.context.get('eh_eps_compute'):
            Run = self.env['eh.eps.run']
            for vals in vals_list:
                run = Run.browse(vals['run_id']) if vals.get('run_id') else None
                if run and run.state == 'computed':
                    raise UserError(_(
                        "Potential-share lines cannot be added to run %s once "
                        "it is computed. Set the run back to draft first "
                        "(IAS 33).", run.display_name))
        return super().create(vals_list)

    def write(self, vals):
        # The run's own dilution test flips is_dilutive on these lines; that
        # internal write carries the eh_eps_compute context and is allowed.
        if not self.env.context.get('eh_eps_compute'):
            self._check_run_frozen()
        return super().write(vals)

    def unlink(self):
        self._check_run_frozen()
        return super().unlink()

    @api.depends('observation_ids')
    def _compute_has_observations(self):
        for p in self:
            p.has_observations = bool(p.observation_ids)

    @api.depends('observation_ids.date', 'observation_ids.price',
                 'run_id.period_start', 'run_id.period_end')
    def _compute_average_market_price(self):
        """Resolve the period-average market price (IAS 33.45).

        IAS 33 applies the treasury-stock method at the average market price
        DURING the period. With price observations recorded, the average is
        the arithmetic mean of the observations dated inside the run period
        (out-of-period observations are ignored); without any in-period
        observation the manually entered scalar is preserved, so existing
        runs behave exactly as before."""
        for p in self:
            start = p.run_id.period_start
            end = p.run_id.period_end
            obs = p.observation_ids.filtered(
                lambda o: o.date
                and (not start or o.date >= start)
                and (not end or o.date <= end))
            if obs:
                p.average_market_price = (
                    sum(obs.mapped('price')) / len(obs))
            else:
                # Preserve the manual scalar (or 0.0 on a fresh line).
                p.average_market_price = p.average_market_price

    @api.depends('instrument_type', 'potential_shares',
                 'exercise_price', 'average_market_price')
    def _compute_net_incremental(self):
        """Treasury-stock method for options (IAS 33.45-46).

        When an option's exercise price and the average market price are both
        set, assume the proceeds buy back shares at the average market price,
        so only the net increment dilutes:
            net = potential_shares * (1 - exercise_price / average_market_price)
        floored at 0 (out-of-the-money options add nothing). In every other
        case (non-options, or prices not supplied) net equals the gross
        potential shares, so existing runs are unchanged."""
        for p in self:
            if (p.instrument_type == 'options'
                    and p.exercise_price
                    and p.average_market_price):
                net = p.potential_shares * (
                    1.0 - p.exercise_price / p.average_market_price)
                p.net_incremental_shares = net if net > 0.0 else 0.0
            else:
                p.net_incremental_shares = p.potential_shares

    @api.depends('earnings_adjustment', 'net_incremental_shares')
    def _compute_incremental(self):
        for p in self:
            p.incremental_eps = (
                p.earnings_adjustment / p.net_incremental_shares
                if p.net_incremental_shares else 0.0)
