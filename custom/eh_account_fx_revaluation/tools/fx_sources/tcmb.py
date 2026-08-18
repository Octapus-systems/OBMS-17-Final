# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Central Bank of the Republic of Turkey (TCMB) indicative rates.

TCMB publishes a daily XML bulletin whose native base is the Turkish
lira. Each Currency row carries a Unit (1 for most pairs, 100 for the
yen and a handful of others) and a set of buying and selling quotes
expressed as "Unit units of the listed code equals N lira". We read
ForexSelling, fall back to BanknoteSelling when the forex column is
empty, and invert into the native-base direction the cross derivation
expects: "1 lira expressed in units of code".

The base class re-expresses that table against the company currency,
so this module never calls cross_derive itself.
"""

from decimal import Decimal  # noqa: F401

from ..rate_providers import (  # noqa: F401
    BaseHttpProvider,
    RateProviderError,
    register,
    to_decimal,
)

_TCMB_TODAY_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"


class TcmbRateProvider(BaseHttpProvider):
    """Central Bank of Turkey daily indicative rates, native base TRY.

    The today.xml feed is a single dated <Tarih_Date> element holding
    one <Currency> per quoted code. Selling values use a decimal comma,
    handled by to_decimal. A row may carry only banknote columns (no
    forex quote), in which case we use BanknoteSelling; a row with
    neither usable selling value is skipped so a single gap does not
    fail the whole feed.
    """

    name = 'tcmb'
    native_base = 'TRY'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as '1 TRY expressed in units of code'.

        Upstream publishes the inverse, "Unit units of code = selling
        lira", so 1 code = selling/Unit lira and therefore
        1 lira = Unit/selling code. We invert per row using Decimal
        arithmetic throughout.
        """
        root = self._download_xml(_TCMB_TODAY_URL)
        native = {}
        for currency in root.findall('Currency'):
            code = currency.get('CurrencyCode') or currency.get('Kod')
            if not code:
                continue
            code = code.strip().upper()

            unit = to_decimal(currency.findtext('Unit'))
            if unit is None or unit == 0:
                continue

            # Prefer the forex selling quote; fall back to banknote when
            # the forex column is empty or unparseable on this row.
            selling = to_decimal(currency.findtext('ForexSelling'))
            if selling is None:
                selling = to_decimal(currency.findtext('BanknoteSelling'))
            if selling is None or selling == 0:
                continue

            # "1 lira = Unit / selling units of code" inverts the
            # published "Unit code = selling lira" direction.
            native[code] = unit / selling
        return native


register(
    'tcmb',
    TcmbRateProvider,
    label="Central Bank of Turkey",
    needs_key=False,
)
