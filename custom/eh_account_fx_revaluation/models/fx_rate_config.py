# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Per-company FX rate provider configuration.

Wraps the pure-python provider registry in `tools.rate_providers` so
finance can pick a source and the cron can fetch on a schedule. Rates
are written into Odoo's standard res.currency.rate table; the
revaluation run reads them through res.currency._get_conversion_rate
exactly as before, so existing valuation paths are untouched.

One row per company. Multiple companies running different providers
(some on ECB, some manual) coexist; the cron iterates active configs.
"""

import datetime
import logging
from decimal import Decimal

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.eh_account_fx_revaluation.tools import rate_providers

_logger = logging.getLogger(__name__)


class EhFxRateConfig(models.Model):
    _name = 'eh.fx.rate.config'
    _description = "FX rate provider configuration"
    _rec_name = 'company_id'

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    provider = fields.Selection(
        selection='_selection_provider',
        required=True,
        default=lambda self: self._default_provider(),
        help=(
            "External source for daily rates. Central-bank sources (ECB, "
            "Bank of Canada, National Bank of Poland, Bank of Russia, "
            "Central Bank of Turkey, Reserve Bank of Australia, Central "
            "Bank of Brazil, HMRC) need no API key; cross-rates are "
            "derived into the company currency. Aggregator sources serve "
            "broad coverage; some need an API key. Manual disables "
            "automatic fetch so finance enters rates by hand."
        ),
    )
    provider_needs_key = fields.Boolean(
        compute='_compute_provider_needs_key',
        help="True when the selected provider requires an API key.",
    )
    api_key = fields.Char(
        help=(
            "API key for sources that require one (aggregator services). "
            "Central-bank sources ignore this field."
        ),
    )
    fallback_provider = fields.Selection(
        selection='_selection_provider',
        default='manual',
        help=(
            "Optional second source used to fill any currency the primary "
            "provider does not return, or to cover the run when the primary "
            "is temporarily unreachable. Leave on Manual to disable the "
            "fallback. A broad aggregator is a good choice here so a "
            "national feed that omits an exotic currency still resolves."
        ),
    )
    override_line_ids = fields.One2many(
        'eh.fx.rate.override', 'config_id',
        help=(
            "Manual per-currency rates. An override always wins over the "
            "fetched value, so finance can pin a currency that has no clean "
            "feed while every other currency keeps auto-updating."
        ),
    )
    enabled = fields.Boolean(
        default=True,
        help="Master toggle. Disable to suspend the cron without losing config.",
    )
    fetch_frequency = fields.Selection(
        [
            ('manual', "Manual only"),
            ('daily', "Daily"),
            ('weekly', "Weekly"),
            ('monthly', "Monthly"),
        ],
        default='daily', required=True,
        help=(
            "How often the scheduled job refreshes rates for this "
            "company. 'Manual only' never auto-fetches (finance pulls on "
            "demand); the others gate the daily cron on the elapsed time "
            "since the last successful fetch."
        ),
    )
    last_fetched_at = fields.Datetime(readonly=True)
    last_fetch_summary = fields.Char(readonly=True)
    last_error = fields.Char(readonly=True)
    timeout_seconds = fields.Integer(
        default=10,
        help="Network timeout in seconds for the provider HTTP call.",
    )

    _sql_constraints = [
        ('unique_per_company', 'unique(company_id)', 'One FX rate config per company.'),
    ]

    @api.model
    def _selection_provider(self):
        # Driven by the registry so adding a provider in tools/
        # surfaces in the UI without view edits.
        return [
            (name, rate_providers.provider_label(name))
            for name in rate_providers.known_providers()
        ]

    @api.model
    def _default_provider(self):
        # Default a new config to the source most relevant to the
        # company's country, falling back to ECB. Mirrors the operator's
        # expectation that, say, a Canadian company starts on Bank of
        # Canada without hunting through the list.
        country = self.env.company.country_id
        return rate_providers.provider_for_country(
            country.code if country else None
        )

    @api.onchange('company_id')
    def _onchange_company_id_provider(self):
        # When the company changes on the form, re-suggest the country
        # default so the provider tracks the chosen company. The operator
        # can still override afterwards.
        for config in self:
            country = config.company_id.country_id
            config.provider = rate_providers.provider_for_country(
                country.code if country else None
            )

    @api.depends('provider')
    def _compute_provider_needs_key(self):
        for config in self:
            config.provider_needs_key = bool(
                config.provider
                and rate_providers.provider_needs_key(config.provider)
            )

    def _provider_instance(self, provider=None):
        self.ensure_one()
        provider = provider or self.provider
        try:
            factory = rate_providers.get(provider)
        except KeyError as exc:
            raise UserError(_(
                "Provider %s is not registered.",
            ) % provider) from exc
        return factory(
            timeout=int(self.timeout_seconds or 10),
            api_key=self.api_key or None,
        )

    def _override_map(self):
        """Return {currency_code -> Decimal} of active manual overrides.

        The stored value is the Odoo inverse rate (units of the listed
        currency per one unit of the company currency), which is exactly
        the shape the fetch pipeline carries, so it merges in directly.
        """
        self.ensure_one()
        out = {}
        for line in self.override_line_ids:
            if line.active and line.currency_id and line.inverse_company_rate:
                out[line.currency_id.name] = Decimal(
                    str(line.inverse_company_rate)
                )
        return out

    def fetch_rates(self, currency_codes=None, on_date=None,
                    raise_on_empty=True):
        """Fetch rates from the provider and write to res.currency.rate.

        :param currency_codes: iterable of ISO codes to fetch. When
            None, fetches every active foreign res.currency on the
            company.
        :param on_date: date for which to fetch. Defaults to today.
        :param raise_on_empty: when True, an empty provider response
            raises UserError so the operator notices missing data.
        :return: dict {iso -> Decimal} of rates written.
        """
        self.ensure_one()
        if not self.enabled:
            return {}
        on_date = on_date or fields.Date.context_today(self)
        if isinstance(on_date, str):
            on_date = datetime.date.fromisoformat(on_date[:10])
        company_ccy = self.company_id.currency_id
        if currency_codes is None:
            target = self.env['res.currency'].search([
                ('active', '=', True),
                ('id', '!=', company_ccy.id),
            ])
            currency_codes = target.mapped('name')
        currency_codes = sorted({c for c in currency_codes if c})

        # 1) Primary provider. A provider error is remembered, not raised
        #    immediately, so a configured fallback can still cover the run.
        errors = []
        rates = {}
        try:
            rates = dict(
                self._provider_instance().fetch(
                    company_ccy.name, currency_codes, on_date,
                ) or {}
            )
        except rate_providers.RateProviderError as exc:
            errors.append("%s: %s" % (self.provider, exc))

        # 2) Fallback provider fills only the currencies still missing
        #    (or the whole run when the primary failed). The primary value
        #    always wins, so the fallback never overwrites a real fetch.
        fb = self.fallback_provider
        if fb and fb != self.provider:
            missing = [c for c in currency_codes if c not in rates]
            if missing:
                try:
                    fb_rates = self._provider_instance(provider=fb).fetch(
                        company_ccy.name, missing, on_date,
                    ) or {}
                    for code, value in fb_rates.items():
                        rates.setdefault(code, value)
                except rate_providers.RateProviderError as exc:
                    errors.append("fallback %s: %s" % (fb, exc))

        # 3) Manual overrides win over everything fetched.
        overrides = self._override_map()
        rates.update(overrides)

        if not rates:
            note = '; '.join(errors) if errors else _("No rates returned")
            self.write({
                'last_error': '; '.join(errors)[:255],
                'last_fetched_at': fields.Datetime.now(),
                'last_fetch_summary': note[:255],
            })
            if raise_on_empty:
                if errors:
                    raise UserError(_(
                        "No rates could be fetched for %(date)s: %(err)s",
                        date=on_date.isoformat(), err='; '.join(errors),
                    ))
                raise UserError(_(
                    "Provider %(provider)s returned no rates for %(date)s. "
                    "If %(date)s is a non-business day, try a prior date "
                    "or wait for the next publication.",
                    provider=self.provider, date=on_date.isoformat(),
                ))
            return {}

        written = self._write_rates(rates, on_date, company_ccy)
        summary = _(
            "%(count)s rates written for %(date)s",
            count=len(written), date=on_date.isoformat(),
        )
        extra = []
        if fb and fb != self.provider:
            extra.append(_("fallback %s", fb))
        if overrides:
            extra.append(_("%s manual override(s)", len(overrides)))
        if extra:
            summary = "%s (%s)" % (summary, ', '.join(extra))
        self.write({
            'last_error': '; '.join(errors)[:255],
            'last_fetched_at': fields.Datetime.now(),
            'last_fetch_summary': summary[:255],
        })
        return written

    def _write_rates(self, rates, on_date, company_ccy):
        """Upsert rates into res.currency.rate for this company.

        Provider returns rates as "1 unit of base in units of quote".
        Odoo stores `inverse_company_rate` as "1 unit of company
        currency in units of foreign currency". So we feed rates
        directly when company is the provider's base; otherwise we
        invert. The provider already cross-derives via EUR for ECB
        when the company runs in another currency, so the dict is
        always keyed by quote currency code with rate = "company
        currency to that quote".
        """
        Currency = self.env['res.currency'].sudo()
        Rate = self.env['res.currency.rate'].sudo()
        written = {}
        for code, value in rates.items():
            ccy = Currency.search([('name', '=', code)], limit=1)
            if not ccy or ccy == company_ccy:
                continue
            inverse = float(value)
            if inverse <= 0:
                continue
            existing = Rate.search([
                ('currency_id', '=', ccy.id),
                ('name', '=', on_date),
                ('company_id', '=', self.company_id.id),
            ], limit=1)
            # Write the stored technical `rate` field directly rather than
            # the computed inverse_company_rate/company_rate. On Odoo 16 the
            # inverse methods of those computed fields do not cascade to the
            # stored `rate` on create, leaving the rate unset (reads back 0).
            # Across all series inverse_company_rate = last_rate / rate, and
            # for the base currency last_rate = 1, so rate = 1 / inverse
            # yields the intended inverse_company_rate on 16/17/18/19 alike.
            vals = {
                'currency_id': ccy.id,
                'name': on_date,
                'company_id': self.company_id.id,
                'rate': 1.0 / inverse,
            }
            if existing:
                existing.write(vals)
            else:
                Rate.create(vals)
            written[code] = value
            _logger.info(
                "FX rate updated: %s -> %s = %s on %s (company %s)",
                company_ccy.name, code, inverse, on_date, self.company_id.name,
            )
        return written

    def action_update_now(self):
        """Fetch rates immediately for this config and report the result.

        Gives the operator an on-demand pull instead of waiting for the
        daily cron. Surfaces an empty or failed run as a warning so the
        outcome is visible without leaving the form.
        """
        self.ensure_one()
        written = self.fetch_rates(raise_on_empty=False)
        if written:
            message = _("%(count)s rates updated.", count=len(written))
            kind = 'success'
        else:
            message = self.last_fetch_summary or _("No rates returned.")
            kind = 'warning'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("FX rate update"),
                'message': message,
                'type': kind,
                'sticky': False,
            },
        }

    def _is_fetch_due(self, now=None):
        """Whether the scheduled fetch should run for this config now,
        per its frequency and last successful fetch."""
        self.ensure_one()
        if self.fetch_frequency == 'manual':
            return False
        if not self.last_fetched_at:
            return True
        now = now or fields.Datetime.now()
        elapsed = now - self.last_fetched_at
        thresholds = {
            # 20h, not 24h, so a fixed daily cron tick is never skipped
            # by a few minutes of drift.
            'daily': datetime.timedelta(hours=20),
            'weekly': datetime.timedelta(days=7),
            'monthly': datetime.timedelta(days=28),
        }
        return elapsed >= thresholds.get(
            self.fetch_frequency, datetime.timedelta(0))

    @api.model
    def _cron_fetch_daily(self):
        """Cron entry-point: fetch rates for every active config.

        Each company's fetch is wrapped in try/except so a single
        provider outage does not block the others. The error message
        is recorded on the config row for the next operator visit.
        """
        configs = self.search([('enabled', '=', True)])
        for config in configs:
            if not config._is_fetch_due():
                continue
            try:
                config.fetch_rates(raise_on_empty=False)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Daily rate fetch failed for company %s: %s",
                    config.company_id.name, exc,
                )
                config.sudo().write({
                    'last_error': str(exc)[:255],
                    'last_fetched_at': fields.Datetime.now(),
                })


class EhFxRateOverride(models.Model):
    _name = 'eh.fx.rate.override'
    _description = "FX manual rate override"

    config_id = fields.Many2one(
        'eh.fx.rate.config', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        related='config_id.company_id', store=True, index=True,
    )
    currency_id = fields.Many2one(
        'res.currency', required=True,
        help="The currency this manual rate applies to.",
    )
    inverse_company_rate = fields.Float(
        string="Units per company currency",
        digits=(16, 6), required=True,
        help=(
            "Units of this currency for one unit of the company currency "
            "(Odoo inverse rate). For example, with an AUD company and a "
            "USD override, 0.65 means 1 AUD = 0.65 USD."
        ),
    )
    active = fields.Boolean(default=True)
    note = fields.Char()

    _sql_constraints = [
        ('unique_currency', 'unique(config_id, currency_id)', 'One manual override per currency on a configuration.'),
    ]
