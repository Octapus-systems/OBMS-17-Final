# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sbp.valuation: option-pricing helper for grant-date fair values.

Two pure-Python models, no scipy:

* Black-Scholes(-Merton) European call with a continuous dividend yield.
  The standard normal CDF uses the Abramowitz-Stegun 7.1.26 erf
  approximation, absolute error below 1.5e-7, which is orders of
  magnitude inside the 4dp storage precision of the result.
* Cox-Ross-Rubinstein binomial tree (European exercise, default 100
  steps) as the lattice alternative; it converges to Black-Scholes as the
  step count grows.

Degenerate inputs are priced deterministically: zero term returns the
intrinsic value, zero volatility returns the discounted forward
intrinsic. The result is copied onto a grant with the grant's "Use
Valuation" action; the inputs stay manual (IFRS 2 wants the entity's own
market inputs, the module automates only the arithmetic).
"""

import math

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


def _norm_cdf(x):
    """Standard normal CDF via the Abramowitz-Stegun 7.1.26 erf
    approximation (|error| < 1.5e-7)."""
    sign = 1.0 if x >= 0.0 else -1.0
    z = abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * z)
    poly = (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
             - 0.284496736) * t + 0.254829592) * t
    erf = 1.0 - poly * math.exp(-z * z)
    return 0.5 * (1.0 + sign * erf)


def _bs_call(spot, strike, vol, rate, term, dividend=0.0):
    """Black-Scholes-Merton European call value."""
    if term <= 0.0:
        return max(spot - strike, 0.0)
    if vol <= 0.0:
        return max(spot * math.exp(-dividend * term)
                   - strike * math.exp(-rate * term), 0.0)
    sq = vol * math.sqrt(term)
    d1 = (math.log(spot / strike)
          + (rate - dividend + vol * vol / 2.0) * term) / sq
    d2 = d1 - sq
    return (spot * math.exp(-dividend * term) * _norm_cdf(d1)
            - strike * math.exp(-rate * term) * _norm_cdf(d2))


def _crr_call(spot, strike, vol, rate, term, dividend=0.0, steps=100):
    """Cox-Ross-Rubinstein binomial European call value."""
    if term <= 0.0:
        return max(spot - strike, 0.0)
    if vol <= 0.0:
        return max(spot * math.exp(-dividend * term)
                   - strike * math.exp(-rate * term), 0.0)
    dt = term / steps
    up = math.exp(vol * math.sqrt(dt))
    down = 1.0 / up
    growth = math.exp((rate - dividend) * dt)
    prob = (growth - down) / (up - down)
    prob = min(max(prob, 0.0), 1.0)
    disc = math.exp(-rate * dt)
    values = [
        max(spot * (up ** j) * (down ** (steps - j)) - strike, 0.0)
        for j in range(steps + 1)
    ]
    for i in range(steps, 0, -1):
        values = [
            disc * (prob * values[j + 1] + (1.0 - prob) * values[j])
            for j in range(i)
        ]
    return values[0]


class EhSbpValuation(models.Model):
    _name = 'eh.sbp.valuation'
    _description = "Option valuation (Black-Scholes / binomial)"
    _order = 'id desc'
    _rec_name = 'name'

    name = fields.Char(required=True, copy=False, default='/')
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    pricing_model = fields.Selection(
        [('black_scholes', "Black-Scholes"),
         ('binomial', "Binomial (CRR)")],
        default='black_scholes', required=True, string="Model")
    spot = fields.Float(digits=(16, 4), required=True,
                        string="Spot Price")
    strike = fields.Float(digits=(16, 4), required=True,
                          string="Strike Price")
    volatility_pct = fields.Float(
        digits=(6, 3), string="Volatility %",
        help="Annualised volatility of the underlying, in percent.")
    rate_pct = fields.Float(
        digits=(6, 3), string="Risk-free Rate %",
        help="Continuously compounded annual risk-free rate, in percent.")
    term_years = fields.Float(
        digits=(6, 4), string="Term (Years)",
        help="Expected term to exercise in years. IFRS 2.B17: use the "
             "expected term, not the contractual life.")
    dividend_yield_pct = fields.Float(
        digits=(6, 3), string="Dividend Yield %")
    steps = fields.Integer(
        default=100,
        help="Binomial tree steps (CRR). 100 steps sit within a few cents "
             "of Black-Scholes for typical employee-option inputs.")
    result_value = fields.Float(
        digits=(16, 4), readonly=True, copy=False,
        string="Value / Instrument",
        help="Computed call value per instrument, pullable into a grant's "
             "grant-date fair value.")
    notes = fields.Char()

    _sql_constraints = [
        ('check_positive', 'CHECK (spot >= 0 AND strike >= 0)', 'Spot and strike prices cannot be negative.'),
    ]

    @api.constrains('volatility_pct', 'rate_pct', 'term_years',
                    'dividend_yield_pct', 'steps')
    def _check_inputs(self):
        for val in self:
            if val.volatility_pct < 0 or val.dividend_yield_pct < 0 \
                    or val.term_years < 0:
                raise ValidationError(_(
                    "Volatility, dividend yield and term cannot be "
                    "negative."))
            if not 1 <= val.steps <= 2000:
                raise ValidationError(_(
                    "Binomial steps must lie between 1 and 2000."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.sbp.valuation') or '/'
        return super().create(vals_list)

    def action_compute(self):
        for val in self:
            if val.spot <= 0.0:
                raise UserError(_(
                    "Enter a positive spot price to value the option."))
            args = (val.spot, val.strike, val.volatility_pct / 100.0,
                    val.rate_pct / 100.0, val.term_years,
                    val.dividend_yield_pct / 100.0)
            if val.pricing_model == 'binomial':
                val.result_value = _crr_call(*args, steps=val.steps)
            else:
                val.result_value = _bs_call(*args)
        return True
