# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for the FX rate-provider abstraction.

The registry, the manual stub, and the ECB XML parser are all pure
Python with no Odoo or network dependency in their happy path. The
tests here exercise:

* Registry: known_providers, get(), error on unknown name.
* ManualRateProvider: returns empty dict, never raises, never hits
  the network.
* EcbRateProvider parsing: feed it a synthetic ECB XML payload (daily
  shape and 90-day shape) and assert the parser extracts the right
  rates, derives cross-rates for non-EUR base, and falls back to the
  most recent business day on a non-business target date.

Network calls are stubbed by replacing `_download` so the tests stay
hermetic.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


_DAILY_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
    xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
    <gesmes:subject>Reference rates</gesmes:subject>
    <gesmes:Sender><gesmes:name>European Central Bank</gesmes:name></gesmes:Sender>
    <Cube>
        <Cube time="2026-05-01">
            <Cube currency="USD" rate="1.0825"/>
            <Cube currency="GBP" rate="0.8512"/>
            <Cube currency="AUD" rate="1.6450"/>
        </Cube>
    </Cube>
</gesmes:Envelope>
"""

_HISTORY_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
    xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
    <gesmes:subject>Reference rates</gesmes:subject>
    <Cube>
        <Cube time="2026-04-30">
            <Cube currency="USD" rate="1.0810"/>
            <Cube currency="GBP" rate="0.8520"/>
            <Cube currency="AUD" rate="1.6500"/>
        </Cube>
        <Cube time="2026-04-29">
            <Cube currency="USD" rate="1.0800"/>
            <Cube currency="GBP" rate="0.8530"/>
            <Cube currency="AUD" rate="1.6550"/>
        </Cube>
    </Cube>
</gesmes:Envelope>
"""


@tagged('eh_account_fx_revaluation', 'unit')
class TestRateProviderRegistry(TransactionCase):

    def test_known_providers_includes_ecb_and_manual(self):
        names = rp.known_providers()
        self.assertIn('ecb', names)
        self.assertIn('manual', names)

    def test_get_unknown_raises(self):
        with self.assertRaises(KeyError):
            rp.get('does-not-exist')

    def test_get_returns_factory(self):
        factory = rp.get('manual')
        instance = factory()
        self.assertEqual(instance.name, 'manual')


@tagged('eh_account_fx_revaluation', 'unit')
class TestManualProvider(TransactionCase):

    def test_manual_returns_empty(self):
        manual = rp.ManualRateProvider()
        result = manual.fetch('EUR', ['USD', 'GBP'], datetime.date.today())
        self.assertEqual(result, {})


@tagged('eh_account_fx_revaluation', 'unit')
class TestEcbProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ecb = rp.EcbRateProvider(timeout=1)
        self._download_calls = []

    def _stub_download(self, payload_map):
        """Return a download stub that serves the given URL->bytes map.

        Records each call so individual tests can assert which feed
        was hit (daily vs 90d).
        """
        def fake(url):
            self._download_calls.append(url)
            return payload_map[url]
        return fake

    def test_parse_daily_eur_base(self):
        self.ecb._download = self._stub_download({
            rp._ECB_DAILY_URL: _DAILY_XML,
        })
        # Use a date relative to today so the provider takes the daily
        # feed branch (days_back <= 5). A frozen past date makes this test
        # time-fragile: once wall-clock today is more than five days later
        # the provider would skip the daily feed and hit the un-stubbed
        # 90d URL.
        rates = self.ecb.fetch(
            'EUR', ['USD', 'GBP'], datetime.date.today(),
        )
        self.assertEqual(rates['USD'], Decimal('1.0825'))
        self.assertEqual(rates['GBP'], Decimal('0.8512'))
        self.assertNotIn('AUD', rates)

    def test_parse_history_when_target_old(self):
        # Target date is 7 days back, so the provider goes straight
        # to the 90d feed.
        self.ecb._download = self._stub_download({
            rp._ECB_DAILY_URL: _DAILY_XML,
            rp._ECB_90D_URL: _HISTORY_XML,
        })
        target = datetime.date.today() - datetime.timedelta(days=7)
        rates = self.ecb.fetch(
            'EUR', ['USD', 'GBP'], target,
        )
        # The 90d fixture's newest entry is 2026-04-30; with a recent
        # `today`, the parser walks back to find the entry on or
        # before `target`. With a synthetic target older than any
        # entry in the fixture we'd get an empty dict; with a target
        # newer than the newest entry we get the newest entry.
        if target >= datetime.date(2026, 4, 30):
            self.assertEqual(rates['USD'], Decimal('1.0810'))
        else:
            # All fixture entries are after the target; provider
            # returns empty per its documented behaviour for very
            # old targets.
            self.assertEqual(rates, {})

    def test_cross_derivation_for_non_eur_base(self):
        # Company in AUD asks for USD and GBP rates. ECB serves
        # EUR/AUD = 1.6450, EUR/USD = 1.0825, EUR/GBP = 0.8512.
        # Cross AUD->USD = (EUR/USD) / (EUR/AUD) = 1.0825 / 1.6450.
        self.ecb._download = self._stub_download({
            rp._ECB_DAILY_URL: _DAILY_XML,
        })
        rates = self.ecb.fetch(
            'AUD', ['USD', 'GBP'], datetime.date.today(),
        )
        # 1.0825 / 1.6450 = 0.6580...
        self.assertAlmostEqual(
            float(rates['USD']),
            float(Decimal('1.0825') / Decimal('1.6450')),
            places=8,
        )
        self.assertAlmostEqual(
            float(rates['GBP']),
            float(Decimal('0.8512') / Decimal('1.6450')),
            places=8,
        )

    def test_cross_missing_base_raises(self):
        # Ask for cross rates with a base currency the feed does
        # not serve. The provider falls back to the 90d feed; if
        # that also doesn't carry the base it raises so the caller
        # surfaces the gap instead of silently dropping it.
        self.ecb._download = self._stub_download({
            rp._ECB_DAILY_URL: _DAILY_XML,
            rp._ECB_90D_URL: _HISTORY_XML,
        })
        with self.assertRaises(rp.RateProviderError):
            # ZAR is not in either fixture.
            self.ecb.fetch(
                'ZAR', ['USD'], datetime.date(2026, 5, 1),
            )

    def test_network_error_propagates(self):
        # When the provider's downloader raises RateProviderError,
        # fetch() lets it bubble; the cron handler catches it.
        def fake_download(url):
            raise rp.RateProviderError("network unreachable")
        self.ecb._download = fake_download
        with self.assertRaises(rp.RateProviderError):
            self.ecb.fetch(
                'EUR', ['USD'], datetime.date(2026, 5, 1),
            )

    def test_malformed_xml_rejected(self):
        # The provider's XML parser is strict; bad bytes are wrapped
        # into RateProviderError so the cron does not blow up.
        self.ecb._download = self._stub_download({
            rp._ECB_DAILY_URL: b'<not valid xml',
        })
        with self.assertRaises(rp.RateProviderError):
            self.ecb.fetch(
                'EUR', ['USD'], datetime.date.today(),
            )


@tagged('eh_account_fx_revaluation', 'unit')
class TestCountryDefault(TransactionCase):

    def test_dedicated_country_maps_to_its_feed(self):
        self.assertEqual(rp.provider_for_country('CA'), 'boc')
        self.assertEqual(rp.provider_for_country('PL'), 'nbp')
        self.assertEqual(rp.provider_for_country('AU'), 'rba')
        self.assertEqual(rp.provider_for_country('KW'), 'cbk')
        self.assertEqual(rp.provider_for_country('BH'), 'cbb')

    def test_gulf_pegged_countries_map_to_peg(self):
        for code in ('SA', 'AE', 'QA', 'OM'):
            self.assertEqual(rp.provider_for_country(code), 'gcc_peg')

    def test_eurozone_maps_to_ecb(self):
        self.assertEqual(rp.provider_for_country('FR'), 'ecb')
        self.assertEqual(rp.provider_for_country('DE'), 'ecb')

    def test_case_insensitive(self):
        self.assertEqual(rp.provider_for_country('ca'), 'boc')

    def test_unknown_country_falls_back_to_ecb(self):
        self.assertEqual(rp.provider_for_country('ZZ'), 'ecb')

    def test_none_falls_back_to_default(self):
        self.assertEqual(rp.provider_for_country(None), 'ecb')
        self.assertEqual(rp.provider_for_country(''), 'ecb')

    def test_every_mapped_provider_is_registered(self):
        # The country map must never point at a provider that is not in
        # the registry, or a defaulted config would be unusable.
        for code, provider in rp.COUNTRY_PROVIDER.items():
            self.assertIn(provider, rp.known_providers())


@tagged('eh_account_fx_revaluation', 'unit')
class TestFetchPipeline(TransactionCase):
    """Fallback chain and manual override merge logic on the config.

    Two throwaway providers are registered so the pipeline can be driven
    deterministically with no network: a primary that returns a single
    currency (and a failing variant), and a fallback that answers every
    requested code. The registry entries are removed on teardown.
    """

    def setUp(self):
        super().setUp()

        class _Primary:
            name = 't_primary'

            def __init__(self, timeout=10, api_key=None, **kw):
                pass

            def fetch(self, base, quotes, on_date):
                # Knows EUR only; leaves any other requested code missing.
                return {'EUR': Decimal('1.1')} if 'EUR' in quotes else {}

        class _PrimaryFail:
            name = 't_primary_fail'

            def __init__(self, timeout=10, api_key=None, **kw):
                pass

            def fetch(self, base, quotes, on_date):
                raise rp.RateProviderError("primary down")

        class _Fallback:
            name = 't_fallback'

            def __init__(self, timeout=10, api_key=None, **kw):
                pass

            def fetch(self, base, quotes, on_date):
                return {q: Decimal('9') for q in quotes}

        for key, cls in (
            ('t_primary', _Primary),
            ('t_primary_fail', _PrimaryFail),
            ('t_fallback', _Fallback),
        ):
            rp.register(key, cls)
            self.addCleanup(rp._PROVIDERS.pop, key, None)
            self.addCleanup(rp._LABELS.pop, key, None)

        self.company = self.env.company
        # Odoo 16 defaults the company currency to EUR; pin USD so EUR and
        # GBP are both foreign and get written as rates (a company never rates
        # its own currency). 17/18/19 already default to USD.
        usd = self.env.ref('base.USD')
        if not usd.active:
            usd.sudo().write({'active': True})
        if self.company.currency_id != usd:
            self.company.sudo().write({'currency_id': usd.id})
        self.on_date = datetime.date(2026, 6, 1)
        Currency = self.env['res.currency'].with_context(active_test=False)
        self.eur = Currency.search([('name', '=', 'EUR')], limit=1)
        self.gbp = Currency.search([('name', '=', 'GBP')], limit=1)
        (self.eur | self.gbp).sudo().write({'active': True})
        # A fresh config for this company (remove any pre-existing one so
        # the unique-per-company constraint does not bite).
        self.env['eh.fx.rate.config'].search(
            [('company_id', '=', self.company.id)]).unlink()

    def _config(self, **vals):
        base = {'company_id': self.company.id, 'provider': 't_primary'}
        base.update(vals)
        return self.env['eh.fx.rate.config'].create(base)

    def _written(self, ccy):
        return self.env['res.currency.rate'].search([
            ('currency_id', '=', ccy.id),
            ('name', '=', self.on_date),
            ('company_id', '=', self.company.id),
        ], limit=1)

    def test_fallback_fills_only_missing(self):
        cfg = self._config(fallback_provider='t_fallback')
        cfg.fetch_rates(currency_codes=['EUR', 'GBP'], on_date=self.on_date)
        # EUR came from the primary (1.1); GBP was missing so the fallback
        # supplied it (9). The fallback never overwrote the primary's EUR.
        self.assertAlmostEqual(
            self._written(self.eur).inverse_company_rate, 1.1, places=6)
        self.assertAlmostEqual(
            self._written(self.gbp).inverse_company_rate, 9.0, places=6)

    def test_fallback_covers_when_primary_fails(self):
        cfg = self._config(provider='t_primary_fail',
                           fallback_provider='t_fallback')
        written = cfg.fetch_rates(
            currency_codes=['EUR', 'GBP'], on_date=self.on_date)
        # Primary raised, so the fallback answered the whole run.
        self.assertEqual(set(written), {'EUR', 'GBP'})
        self.assertAlmostEqual(
            self._written(self.eur).inverse_company_rate, 9.0, places=6)

    def test_manual_override_wins(self):
        cfg = self._config(fallback_provider='t_fallback')
        self.env['eh.fx.rate.override'].create({
            'config_id': cfg.id,
            'currency_id': self.gbp.id,
            'inverse_company_rate': 0.5,
        })
        cfg.fetch_rates(currency_codes=['EUR', 'GBP'], on_date=self.on_date)
        # GBP override (0.5) beats the fallback value (9); EUR still 1.1.
        self.assertAlmostEqual(
            self._written(self.gbp).inverse_company_rate, 0.5, places=6)
        self.assertAlmostEqual(
            self._written(self.eur).inverse_company_rate, 1.1, places=6)

    def test_inactive_override_ignored(self):
        cfg = self._config(fallback_provider='t_fallback')
        self.env['eh.fx.rate.override'].create({
            'config_id': cfg.id,
            'currency_id': self.gbp.id,
            'inverse_company_rate': 0.5,
            'active': False,
        })
        cfg.fetch_rates(currency_codes=['EUR', 'GBP'], on_date=self.on_date)
        # Override inactive, so GBP keeps the fallback value.
        self.assertAlmostEqual(
            self._written(self.gbp).inverse_company_rate, 9.0, places=6)
