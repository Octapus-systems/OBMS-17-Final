# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Pluggable foreign-exchange rate providers.

Each provider is a callable class with a single public method:

    fetch(base, quotes, on_date) -> dict[quote_code -> Decimal]

* base       three-letter ISO 4217 base currency (e.g. 'EUR' for ECB).
* quotes     iterable of ISO codes the caller wants quoted.
* on_date    a datetime.date. Most providers serve "the rate on or
             before this date"; the returned mapping uses the rate
             dated on_date if available, else the most recent prior
             publication.

Returns a dict of quote currency code to Decimal rate where the rate
expresses 1 unit of base currency in units of the quote currency. A
provider that has no rate for a quote leaves it out of the dict; the
caller treats the gap as "no rate available" and surfaces it.

Network errors raise RateProviderError. The Odoo cron wrapper catches
this and logs a chatter line so a one-off outage does not poison the
run record.

References:
* ECB daily reference rates XML feed:
  https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml
  https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml
* ECB legal terms permit free reuse with attribution; the URL above
  is the source of truth.

This module is original work; the parsers below implement the published
XML schema and do not derive from any third-party rate library.
"""

import datetime
import json
import logging
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

_logger = logging.getLogger(__name__)


# Known providers and their characteristics. The Odoo config field
# uses the keys of this dict, so adding a provider is purely a matter
# of registering a class below and adding an entry here.
_PROVIDERS = {}
_LABELS = {}
# Providers that require an operator-supplied API key. Surfaced so the
# UI can hint which sources need the key field populated.
_NEEDS_KEY = set()

# Sent on every outbound request. Some central-bank endpoints reject the
# default urllib agent, so we identify the module politely.
_USER_AGENT = (
    'eh_account_fx_revaluation/19.0 (+https://www.erpheritage.com.au)'
)


def register(name, factory, label=None, needs_key=False):
    """Register a provider factory by short name. Idempotent.

    :param label: human label shown in the Odoo selection. Defaults to
        the upper-cased key.
    :param needs_key: True when the source needs an operator API key.
    """
    _PROVIDERS[name] = factory
    _LABELS[name] = label or name.upper()
    if needs_key:
        _NEEDS_KEY.add(name)
    else:
        _NEEDS_KEY.discard(name)


def get(name):
    """Return the factory registered under `name` or raise KeyError."""
    if name not in _PROVIDERS:
        raise KeyError("Unknown FX rate provider: %r" % name)
    return _PROVIDERS[name]


def known_providers():
    """Return the list of registered provider keys, sorted."""
    return sorted(_PROVIDERS.keys())


def provider_label(name):
    """Return the human label for a provider key."""
    return _LABELS.get(name, name.upper())


def provider_needs_key(name):
    """Return True when the provider requires an operator API key."""
    return name in _NEEDS_KEY


# Country (ISO 3166-1 alpha-2) -> preferred provider key. Used to default
# a new company's FX config to the most relevant source for its
# jurisdiction, the way an operator would expect. A country with no
# dedicated feed falls back to ECB, whose EUR base cross-derives into any
# company currency. The map is extended as new national feeds register.
_EUR_COUNTRIES = (
    'AT', 'BE', 'HR', 'CY', 'EE', 'FI', 'FR', 'DE', 'GR', 'IE', 'IT', 'LV',
    'LT', 'LU', 'MT', 'NL', 'PT', 'SK', 'SI', 'ES',
)
COUNTRY_PROVIDER = {country: 'ecb' for country in _EUR_COUNTRIES}
COUNTRY_PROVIDER.update({
    'CA': 'boc',        # Bank of Canada
    'PL': 'nbp',        # National Bank of Poland
    'RU': 'cbr',        # Bank of Russia
    'TR': 'tcmb',       # Central Bank of Turkey
    'AU': 'rba',        # Reserve Bank of Australia
    'BR': 'bcb',        # Central Bank of Brazil
    'GB': 'hmrc',       # HM Revenue and Customs (Odoo country code GB)
    'UK': 'hmrc',       # tolerate the non-ISO 'UK' alias some data carries
    'CZ': 'cnb',        # Czech National Bank
    'BG': 'bnb',        # Bulgarian National Bank
    'RO': 'bnr',        # National Bank of Romania
    'SE': 'srb',        # Sveriges Riksbank
    'KZ': 'nbkz',       # National Bank of Kazakhstan
    'UZ': 'cbu',        # Central Bank of Uzbekistan
    'MY': 'bnm',        # Bank Negara Malaysia
    'CO': 'banrepco',   # Bank of the Republic (Colombia TRM)
    'PE': 'bcrp',       # Central Reserve Bank of Peru
    'MX': 'banxico',    # Bank of Mexico (needs a free token)
    'UY': 'bcu',        # Central Bank of Uruguay (SOAP, open data)
    'KW': 'cbk',        # Central Bank of Kuwait (KWD floats, live feed)
    'BH': 'cbb',        # Central Bank of Bahrain (live feed)
    'SA': 'gcc_peg',    # Saudi Riyal, decree peg
    'AE': 'gcc_peg',    # UAE Dirham, decree peg
    'QA': 'gcc_peg',    # Qatari Riyal, decree peg
    'OM': 'gcc_peg',    # Omani Rial, decree peg
})


def provider_for_country(code, default='ecb'):
    """Return the preferred provider key for an ISO country code.

    Falls back to ``default`` (ECB) when the country has no dedicated
    feed, mirroring how an operator would pick the closest sensible
    source. ``code`` may be None or empty.
    """
    if not code:
        return default
    return COUNTRY_PROVIDER.get(code.upper(), default)


class RateProviderError(Exception):
    """Raised when a provider cannot deliver rates (network, parse)."""


# ---- Shared HTTP + cross-derivation helpers ----


def http_get(url, timeout, headers=None):
    """GET a URL and return the raw bytes, or raise RateProviderError.

    Centralises the urllib boilerplate so every provider gets the same
    timeout handling, user-agent, and error wrapping. Network and HTTP
    failures are wrapped into RateProviderError; the cron handler treats
    that as a recoverable per-config outage rather than a crash.
    """
    req_headers = {'User-Agent': _USER_AGENT}
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.read()
    except RateProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RateProviderError(
            "HTTP fetch failed for %s: %s" % (url, exc),
        ) from exc


def http_post(url, body, timeout, headers=None):
    """POST a request body and return the raw response bytes.

    Used by SOAP providers, which must POST an XML envelope rather than
    GET a URL. Shares the timeout, user-agent, and error wrapping of
    http_get so a SOAP outage is a recoverable RateProviderError, not a
    crash. ``body`` may be str (encoded utf-8) or bytes.
    """
    if isinstance(body, str):
        body = body.encode('utf-8')
    req_headers = {'User-Agent': _USER_AGENT}
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, data=body, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.read()
    except RateProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RateProviderError(
            "HTTP POST failed for %s: %s" % (url, exc),
        ) from exc


def to_decimal(raw):
    """Parse a provider rate string into Decimal, tolerating a decimal
    comma (used by several European central banks) and surrounding
    whitespace. Returns None when the value cannot be parsed so callers
    can skip a single bad row instead of failing the whole feed.
    """
    if raw is None:
        return None
    text = str(raw).strip().replace('\xa0', '').replace(' ', '')
    if not text:
        return None
    # Central banks such as CBR and TCMB publish "70,1234"; normalise to
    # a dot. A value never legitimately contains both separators here.
    if ',' in text and '.' not in text:
        text = text.replace(',', '.')
    try:
        return Decimal(text)
    except (InvalidOperation, TypeError):
        return None


def cross_derive(native_base, native_rates, base, quotes):
    """Re-express a provider's native-base table against an arbitrary base.

    :param native_base: ISO code the provider publishes against (e.g.
        'PLN' for the National Bank of Poland).
    :param native_rates: dict {code -> Decimal} where each value is
        "1 unit of native_base expressed in units of code". The
        native_base itself is implicitly 1 and need not appear.
    :param base: the company currency the caller wants quotes against.
    :param quotes: iterable of ISO codes wanted.
    :return: dict {quote -> Decimal} where the value is "1 unit of base
        expressed in units of quote". Quotes the table cannot reach are
        omitted so the caller surfaces the gap.

    When base differs from native_base we pivot through native_base:
        rate(base -> quote) = rate(native -> quote) / rate(native -> base).
    A base the table does not carry cannot be pivoted, so we raise rather
    than guess.
    """
    native_base = (native_base or '').upper()
    base = (base or '').upper()
    wanted = {q.upper() for q in quotes}
    table = {}
    for code, value in native_rates.items():
        dec = value if isinstance(value, Decimal) else to_decimal(value)
        if dec is not None:
            table[code.upper()] = dec

    if base == native_base:
        return {q: table[q] for q in wanted if q in table and q != base}

    if base not in table:
        raise RateProviderError(
            "Source base %s carries no rate for company currency %s; "
            "cannot derive cross rates." % (native_base or '?', base)
        )
    base_rate = table[base]
    if not base_rate:
        raise RateProviderError(
            "Source returned a zero rate for %s; cannot derive cross "
            "rates." % base
        )

    out = {}
    for quote in wanted:
        if quote == base:
            continue
        if quote == native_base:
            out[quote] = Decimal(1) / base_rate
        elif quote in table:
            out[quote] = table[quote] / base_rate
    return out


class BaseHttpProvider:
    """Common scaffolding for HTTP rate providers.

    A subclass sets ``name``, ``native_base`` (the ISO code the source
    publishes against, or None for a multi-base API that takes the base
    as a parameter), and implements ``_fetch_native`` to return a
    {code -> Decimal} table expressed as "1 native_base in units of
    code". The base class handles the cross-derivation, the API-key
    guard, and the network seam.

    Multi-base sources (an API that serves any requested base directly)
    override ``fetch`` instead and skip ``cross_derive`` entirely.

    ``_download`` is the single network seam; unit tests replace it with
    a stub so the parsers are exercised hermetically with no live call.
    """

    name = None
    native_base = None
    needs_key = False

    def __init__(self, timeout=10, api_key=None, **kwargs):
        self.timeout = int(timeout or 10)
        self.api_key = api_key or None

    def _download(self, url, headers=None):
        """Network seam. Overridden in tests to serve canned bytes."""
        return http_get(url, self.timeout, headers)

    def _download_post(self, url, body, headers=None):
        """POST network seam for SOAP providers. Overridden in tests."""
        return http_post(url, body, self.timeout, headers)

    def _download_post_xml(self, url, body, headers=None):
        raw = self._download_post(url, body, headers)
        try:
            return ET.fromstring(raw)
        except ET.ParseError as exc:
            raise RateProviderError(
                "%s returned malformed XML: %s" % (self.name, exc),
            ) from exc

    def _download_json(self, url, headers=None):
        raw = self._download(url, headers)
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise RateProviderError(
                "%s returned malformed JSON: %s" % (self.name, exc),
            ) from exc

    def _download_xml(self, url, headers=None):
        raw = self._download(url, headers)
        try:
            return ET.fromstring(raw)
        except ET.ParseError as exc:
            raise RateProviderError(
                "%s returned malformed XML: %s" % (self.name, exc),
            ) from exc

    def _require_key(self):
        if self.needs_key and not self.api_key:
            raise RateProviderError(
                "Provider %s requires an API key. Set it on the FX rate "
                "configuration." % self.name
            )

    def fetch(self, base, quotes, on_date):
        self._require_key()
        native = self._fetch_native(base, quotes, on_date)
        return cross_derive(self.native_base, native, base, quotes)

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as '1 native_base in units of code'.

        The returned table should include an entry for ``base`` (unless
        base == native_base) so the cross derivation can pivot; most
        sources publish a full table so this is automatic.
        """
        raise NotImplementedError


# ---- ECB ----

_ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_ECB_90D_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
# ECB XML namespaces. The Cube root sits under gesmes:Envelope/Cube.
_NS = {
    'gesmes': 'http://www.gesmes.org/xml/2002-08-01',
    'eurofxref':
        'http://www.ecb.int/vocabulary/2002-08-01/eurofxref',
}


class EcbRateProvider:
    """European Central Bank reference rates.

    Base currency is always EUR. The daily feed serves today's rates
    plus a fallback to the latest published business day. The 90-day
    feed serves a per-day series; we hit it for historical lookups so
    the user can backfill rates when the revaluation_date is older
    than today.

    Network calls go through urllib.request with a configurable timeout
    so a server with a flaky outbound connection does not hang an Odoo
    worker. The default timeout (10s) is comfortably above the ECB's
    typical 200ms response.
    """

    name = 'ecb'

    def __init__(self, timeout=10, api_key=None, **kwargs):
        self.timeout = timeout
        self.api_key = api_key or None

    def fetch(self, base, quotes, on_date):
        # ECB only publishes EUR-base rates. If the caller wants
        # something else, derive by inverse cross via EUR.
        base = (base or '').upper()
        wanted = {q.upper() for q in quotes}
        if base == 'EUR':
            return self._fetch_eur_base(wanted, on_date)
        # Cross via EUR. We need EUR/<base> AND EUR/<each-quote>; then
        # rate(base->quote) = (EUR/quote) / (EUR/base).
        bridge = {base, *wanted}
        eur_rates = self._fetch_eur_base(bridge, on_date)
        if base not in eur_rates:
            raise RateProviderError(
                "ECB has no rate for base currency %s on %s; cannot "
                "derive cross rates." % (base, on_date.isoformat())
            )
        eur_to_base = eur_rates[base]
        out = {}
        for quote in wanted:
            if quote == base:
                continue
            if quote not in eur_rates:
                continue
            out[quote] = eur_rates[quote] / eur_to_base
        return out

    def _fetch_eur_base(self, wanted, on_date):
        today = datetime.date.today()
        # Pick the right feed: the 90d feed has the daily series, the
        # daily feed has only the latest business-day rates. If
        # on_date is more than ~5 days old, go straight to the 90d feed.
        days_back = (today - on_date).days if on_date <= today else 0
        if days_back <= 5:
            xml_bytes = self._download(_ECB_DAILY_URL)
            rates = self._parse_latest_day(xml_bytes, wanted)
            if rates:
                return rates
            # Some weekends / holidays: daily feed still resolves to
            # the latest published day; fall through to the 90d feed
            # so we never silently return an empty dict.
        xml_bytes = self._download(_ECB_90D_URL)
        return self._parse_dated(xml_bytes, wanted, on_date)

    def _download(self, url):
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            raise RateProviderError(
                "ECB rate fetch failed: %s" % exc,
            ) from exc

    @staticmethod
    def _parse_latest_day(xml_bytes, wanted):
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise RateProviderError("ECB XML parse error: %s" % exc) from exc
        # The daily feed has a single dated Cube; pull rates straight off.
        for cube in root.iter('{%s}Cube' % _NS['eurofxref']):
            if cube.get('time'):
                return _extract_rates(cube, wanted)
        return {}

    @staticmethod
    def _parse_dated(xml_bytes, wanted, on_date):
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise RateProviderError("ECB XML parse error: %s" % exc) from exc
        # The 90d feed has one dated Cube per business day, newest
        # first. Walk back from `on_date` until we find a published
        # day; ECB does not publish on weekends and on TARGET2
        # holidays.
        target = on_date.isoformat()
        candidates = []
        for cube in root.iter('{%s}Cube' % _NS['eurofxref']):
            t = cube.get('time')
            if t:
                candidates.append((t, cube))
        if not candidates:
            return {}
        candidates.sort(reverse=True)
        for cube_date, cube in candidates:
            if cube_date <= target:
                return _extract_rates(cube, wanted)
        # Requested date is older than every entry in the 90d feed.
        # Falling all the way through means the user is asking for a
        # rate older than 90 days; the ECB historical feed (daily
        # since 1999) is a separate, much larger XML and is left as
        # a future enhancement.
        return {}


def _extract_rates(cube, wanted):
    out = {}
    for child in cube.findall('{%s}Cube' % _NS['eurofxref']):
        ccy = child.get('currency')
        rate = child.get('rate')
        if not ccy or not rate:
            continue
        if wanted and ccy not in wanted:
            continue
        try:
            out[ccy] = Decimal(rate)
        except (InvalidOperation, TypeError):
            continue
    return out


# ---- Manual / no-op provider ----


class ManualRateProvider:
    """Operator-managed: this provider never fetches. Useful for
    offline test environments and shops where finance enters rates
    by hand. The Odoo cron is a no-op when this provider is selected.
    """
    name = 'manual'

    def __init__(self, timeout=10, api_key=None, **kwargs):
        self.timeout = timeout
        self.api_key = api_key or None

    def fetch(self, base, quotes, on_date):
        return {}


register('ecb', EcbRateProvider, label="ECB (European Central Bank)")
register('manual', ManualRateProvider, label="Manual (no automatic fetch)")

# Extra HTTP providers register themselves on import. Kept at the very
# bottom so the registry, helpers, and BaseHttpProvider are all defined
# before the submodules import them.
from . import fx_sources  # noqa: E402,F401
