# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Currency-conversion helper for multi-company consolidated reporting.

When a report consolidates several companies that share one currency (the
overwhelmingly common case) every ``aml.balance`` is already expressed in the
presentation currency, so summing them raw is correct. The moment two
companies report in different currencies (e.g. AUD and USD), a raw
``SUM(aml.balance)`` adds dollars and Aussie dollars as if they were equal,
producing a meaningless consolidated figure.

CurrencyTable resolves a single presentation currency and, for each company
in scope, the rate that converts that company's currency into the
presentation currency. It then exposes the SQL fragments the MoveLineQuery
needs to LEFT JOIN a small per-company rate table and multiply each line's
monetary columns by that rate before aggregation.

Design rules (mirroring MoveLineQuery):

1. Plain Python class, no ORM model, so it is unit-testable without the
   registry. Rate seeding is the only env-dependent step and is performed
   lazily; a stub rate map can be injected for unit tests.
2. Two modes. ``monocurrency`` (every company already in the presentation
   currency) emits NO join and NO rate multiply, so the hot path for the
   95% of installs that never consolidate across currencies is byte-for-byte
   identical to a query built without a CurrencyTable. ``multicurrency``
   emits a ``LEFT JOIN (VALUES ...) ct(company_id, rate)`` keyed on the
   indexed ``aml.company_id`` and a ``rate_expr()`` of
   ``COALESCE(ct.rate, 1)``.
3. Rates come from the ORM (res.currency conversion), seeded as of the
   report's as-of date. A single spot rate per company is used (date-aware
   on the report date_to). Per-aml date-effective rates are a documented
   follow-up; the join column and ``rate_expr`` are already shaped so that
   refinement is a drop-in.
4. Never raise on a missing or zero rate. A company whose rate cannot be
   resolved falls back to the latest available rate, then to 1.0, and the
   fallback is recorded in ``fallback_flags`` so the caller can surface it
   in the payload meta. A crash here would take down an otherwise valid
   report.
"""

from odoo.tools import SQL


class CurrencyTable:
    """Resolve per-company conversion rates into a presentation currency.

    Usage::

        ct = CurrencyTable(env, company_ids=[1, 2], presentation_currency_id=7)
        if not ct.is_monocurrency:
            # MoveLineQuery emits ct.join_sql('aml') and multiplies by
            # ct.rate_expr().
            ...

    The instance is cheap to construct; rate seeding runs lazily the first
    time a multicurrency consumer asks for the join or rate expression.
    """

    def __init__(self, env, company_ids, presentation_currency_id=None,
                 as_of_date=None, rate_map=None):
        self.env = env
        self.company_ids = tuple(
            int(c) for c in (company_ids or ()) if c is not None
        )
        # Presentation currency: explicit option wins, else the active
        # company's currency. Resolved to an int id so the seeding step and
        # the monocurrency decision never browse a falsey record.
        if presentation_currency_id:
            self.presentation_currency_id = int(presentation_currency_id)
        else:
            self.presentation_currency_id = (
                env.company.currency_id.id if env is not None else False
            )
        self.as_of_date = as_of_date
        # fallback_flags records any company whose rate had to be defaulted,
        # so the report meta can disclose it instead of silently using 1.0.
        self.fallback_flags = []
        # _rate_map: {company_id: float rate}. Injected (unit tests) or
        # seeded lazily from the ORM. None means "not yet seeded".
        self._rate_map = dict(rate_map) if rate_map is not None else None
        self._seeded = rate_map is not None
        # Cache the monocurrency decision so repeated property reads are free.
        self._is_monocurrency = None

    # ---- mode decision ----

    @property
    def is_monocurrency(self):
        """True when no conversion is needed (zero-overhead hot path).

        Monocurrency holds when there are fewer than two companies, when no
        presentation currency could be resolved (degrade to raw sum, exactly
        like today), or when every company in scope already reports in the
        presentation currency. In every monocurrency case ``join_sql`` and
        the converted-sum expressions fall back to the legacy raw form, so a
        single-company / single-currency report is unaffected.
        """
        if self._is_monocurrency is not None:
            return self._is_monocurrency
        result = self._compute_is_monocurrency()
        self._is_monocurrency = result
        return result

    def _compute_is_monocurrency(self):
        if len(self.company_ids) <= 1:
            return True
        if not self.presentation_currency_id:
            return True
        try:
            companies = self.env['res.company'].sudo().browse(
                list(self.company_ids))
            currency_ids = set(companies.mapped('currency_id').ids)
        except Exception:  # pragma: no cover - defensive
            return True
        if not currency_ids:
            return True
        # All companies already in the presentation currency -> no conversion.
        return currency_ids == {self.presentation_currency_id}

    # ---- rate seeding (lazy, env-dependent) ----

    def _seed_rates(self):
        """Populate {company_id: rate-to-presentation-currency}.

        Uses the ORM conversion rate (res.currency._get_conversion_rate),
        which converts an amount expressed in the company currency into the
        presentation currency for the given company and date. A company
        already in the presentation currency gets exactly 1.0 and never hits
        a rate lookup. Any failure for a company degrades to 1.0 and is
        flagged, never raised.
        """
        if self._seeded:
            return
        rate_map = {}
        Currency = self.env['res.currency'].sudo()
        presentation = Currency.browse(self.presentation_currency_id)
        companies = self.env['res.company'].sudo().browse(
            list(self.company_ids))
        for company in companies:
            company_currency = company.currency_id
            if not company_currency or not presentation:
                rate_map[company.id] = 1.0
                self.fallback_flags.append({
                    'company_id': company.id,
                    'reason': 'missing_currency',
                })
                continue
            if company_currency.id == self.presentation_currency_id:
                rate_map[company.id] = 1.0
                continue
            rate = self._resolve_company_rate(
                company_currency, presentation, company)
            rate_map[company.id] = rate
        self._rate_map = rate_map
        self._seeded = True

    def _resolve_company_rate(self, company_currency, presentation, company):
        """Rate to convert company_currency -> presentation for one company.

        Tries the report as-of date first, then the latest available rate,
        then 1.0. Every fallback is flagged. Wrapped so a single bad company
        cannot break the consolidation.
        """
        try:
            rate = self.env['res.currency']._get_conversion_rate(
                company_currency, presentation, company, self.as_of_date,
            )
            if rate and float(rate) > 0.0:
                return float(rate)
        except Exception:  # pragma: no cover - defensive
            pass
        # Fallback 1: latest available rate, date-agnostic.
        try:
            rate = self.env['res.currency']._get_conversion_rate(
                company_currency, presentation, company, None,
            )
            if rate and float(rate) > 0.0:
                self.fallback_flags.append({
                    'company_id': company.id,
                    'reason': 'no_rate_on_date_used_latest',
                })
                return float(rate)
        except Exception:  # pragma: no cover - defensive
            pass
        # Fallback 2: identity. Better a same-currency-style sum than a crash.
        self.fallback_flags.append({
            'company_id': company.id,
            'reason': 'no_rate_found_used_identity',
        })
        return 1.0

    @property
    def rate_map(self):
        """The seeded {company_id: rate} map (seeds lazily)."""
        if not self._seeded:
            self._seed_rates()
        return dict(self._rate_map or {})

    # ---- SQL fragments consumed by MoveLineQuery ----

    def join_sql(self, aml_alias='aml'):
        """LEFT JOIN fragment binding a per-company rate, or empty SQL.

        Monocurrency -> empty (no join), so the rendered query is identical
        to one built without a CurrencyTable. Multicurrency -> a VALUES-based
        inline table ``ct(company_id, rate)`` left-joined on the company id.
        The join is LEFT so a company with no seeded rate yields NULL, which
        ``rate_expr`` coalesces to 1.0 rather than dropping the row.
        """
        if self.is_monocurrency:
            return SQL("")
        if not self._seeded:
            self._seed_rates()
        rate_map = self._rate_map or {}
        if not rate_map:
            return SQL("")
        # Build "(company_id, rate), (company_id, rate), ..." with every
        # value bound as a parameter (no interpolation of user/runtime data).
        value_rows = []
        for company_id in self.company_ids:
            rate = rate_map.get(company_id, 1.0)
            value_rows.append(SQL("(%s, %s)", int(company_id), float(rate)))
        values_clause = SQL(", ").join(value_rows)
        # aml_alias is a fixed internal identifier ('aml'); guard anyway and
        # bind it as a quoted identifier so it can never carry an injection.
        alias = aml_alias if (
            isinstance(aml_alias, str) and aml_alias.isidentifier()
        ) else 'aml'
        return SQL(
            "LEFT JOIN (VALUES %s) AS ct (company_id, rate) "
            "ON ct.company_id = %s.company_id",
            values_clause, SQL.identifier(alias),
        )

    def rate_expr(self):
        """SQL scalar the converted-sum expressions multiply by.

        Monocurrency -> ``SQL("1")`` so ``balance * 1`` folds to the legacy
        plain sum. Multicurrency -> ``COALESCE(ct.rate, 1)`` so an unmatched
        company (NULL rate) converts at identity instead of nullifying the
        row.
        """
        if self.is_monocurrency:
            return SQL("1")
        return SQL("COALESCE(ct.rate, 1)")
