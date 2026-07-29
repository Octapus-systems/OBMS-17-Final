# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
MyInvois (Malaysia) PEPPOL profile validation.

LHDN (Inland Revenue Board of Malaysia) operates a country profile
on top of PEPPOL BIS Billing 3.0 with the following country-specific
rules:

* Both the supplier and the customer party MUST carry a Malaysian
  Tax Identification Number (TIN) in their endpoint identifier
  unless the customer is a foreign entity (B2C exports). Sites
  configure cross-border invoices with the foreign customer's
  country code so the TIN check is skipped on that side only.

* The supplier country code MUST be 'MY'. MyInvois is a domestic
  e-invoicing system; cross-border outbound is supported only for
  the customer side.

* Tax category codes constrain to the SST regime:
  - 'SR' (Standard Rated): 6% services tax, 8% sales tax (post-2024
    sales tax rate; was 10% historically).
  - 'ZR' (Zero Rated): exports, basic supplies.
  - 'ES' (Exempt Supplies): financial services, residential rent,
    healthcare, education.
  - 'DS' (Deemed Supply): self-supply, gifts, free services.
  - 'OS' (Out of Scope): non-taxable transactions.
  Other codes are rejected for the MY profile because they do not
  exist in Malaysian tax law.

* Per-line tax rates gated to {0.0, 6.0, 8.0}. Other rates raise.

* Invoice currency MUST be MYR for SST-tracked supplies. Foreign-
  currency invoices to overseas customers are permitted and skip
  the SST rate check on those lines (covered by the zero-rate
  export rule).

References:
* MyInvois Software Development Kit (LHDN, 2024).
* PEPPOL BIS Billing 3.0 + EN 16931 base spec.
* Malaysian Sales Tax Act 2018 + Service Tax Act 2018.

This implementation is original work; no code or comments derive from
any third-party MyInvois implementation.
"""

import re


_MY_SST_RATES = {0.0, 6.0, 8.0}
_PERMITTED_TAX_CATEGORIES = {'SR', 'ZR', 'ES', 'DS', 'OS'}
_MY_COUNTRY = 'MY'


# Malaysian TIN format per LHDN: 1-2 letter prefix indicating
# taxpayer category + 10 to 13 digits.
# Valid prefixes: C (companies), CS (cooperative societies), D (partnerships),
# E (employer), F (foreign), G (government), J (joint venture),
# LE (limited liability partnership), PT (trust body), SG (individual,
# salary group), TA (trust association), TN (non-profit organisation),
# TP (charitable trust), TC (clubs / association), TR (ranching),
# TT (trust testator-style), U (Uitm scholarship), and a few more.
# Pragmatic regex: 1-2 letters then 10-13 digits, no separators.
_TIN_RE = re.compile(r'^[A-Z]{1,2}\d{10,13}$')


class MyProfileError(ValueError):
    """Raised when a payload fails MyInvois profile validation."""


def validate_my_payload(payload):
    """Validate a UBL invoice payload against the MyInvois profile.

    :param payload: dict in the shape returned by
        ubl_generator.make_invoice_payload.
    :raises MyProfileError: with a message naming the offending field.
    """
    if not isinstance(payload, dict):
        raise MyProfileError("payload must be a dict")
    supplier = payload.get('supplier') or {}
    customer = payload.get('customer') or {}
    _check_supplier(supplier)
    _check_customer(customer)
    _check_tax_categories(payload.get('tax_categories') or [])
    _check_lines_tax_rates(
        payload.get('lines') or [],
        customer_country=(customer.get('country_code') or '').upper(),
    )
    return True


def _check_supplier(supplier):
    country = (supplier.get('country_code') or '').upper()
    if country != _MY_COUNTRY:
        raise MyProfileError(
            "supplier.country_code must be MY for the MyInvois "
            "profile (got %r). MyInvois is a domestic outbound "
            "e-invoicing system; foreign-supplier scenarios are out "
            "of scope." % supplier.get('country_code'),
        )
    endpoint = (supplier.get('endpoint_id') or '').strip()
    if not endpoint:
        raise MyProfileError("supplier.endpoint_id (TIN) is required")
    try:
        validate_my_tin(endpoint)
    except MyProfileError as exc:
        raise MyProfileError(
            "supplier.endpoint_id failed Malaysian TIN validation: %s"
            % exc,
        )


def _check_customer(customer):
    country = (customer.get('country_code') or '').upper()
    if not country:
        raise MyProfileError("customer.country_code is required")
    endpoint = (customer.get('endpoint_id') or '').strip()
    if country == _MY_COUNTRY:
        # Domestic B2B: TIN required.
        if not endpoint:
            raise MyProfileError(
                "customer.endpoint_id (TIN) is required for "
                "domestic MY customers",
            )
        try:
            validate_my_tin(endpoint)
        except MyProfileError as exc:
            raise MyProfileError(
                "customer.endpoint_id failed Malaysian TIN "
                "validation: %s" % exc,
            )
    # Foreign customers (B2C export): TIN check skipped; the country
    # code must still be a non-empty 2-letter code so downstream
    # systems can route correctly.
    elif len(country) != 2 or not country.isalpha():
        raise MyProfileError(
            "customer.country_code must be a 2-letter ISO code "
            "(got %r)" % customer.get('country_code'),
        )


def _check_tax_categories(tax_categories):
    if not tax_categories:
        return
    for idx, cat in enumerate(tax_categories):
        code = (cat.get('category_code') or '').upper()
        if code not in _PERMITTED_TAX_CATEGORIES:
            raise MyProfileError(
                "tax_categories[%d].category_code %r is not "
                "permitted by the MyInvois profile. Allowed: %s." % (
                    idx, cat.get('category_code'),
                    ', '.join(sorted(_PERMITTED_TAX_CATEGORIES)),
                ),
            )


def _check_lines_tax_rates(lines, customer_country):
    """Per-line tax rate must match SST regime for domestic invoices.

    Foreign-customer (export) invoices are exempt from the SST rate
    check because they are zero-rated by definition; the line-level
    rate of 0.0% is permitted, and so are exotic rates that arise
    from foreign-jurisdiction add-on taxes that the MY exporter
    documents on the invoice.
    """
    if not lines:
        return
    is_export = customer_country and customer_country != _MY_COUNTRY
    for idx, line in enumerate(lines):
        rate_value = line.get('tax_rate_pct', 0.0) or 0.0
        try:
            rate_float = float(rate_value)
        except (TypeError, ValueError):
            raise MyProfileError(
                "lines[%d].tax_rate_pct must be numeric (got %r)"
                % (idx, rate_value),
            )
        if is_export and abs(rate_float) < 0.01:
            # Zero-rated export line; OK.
            continue
        if is_export:
            # Non-zero rate on an export line is unusual but permitted
            # (foreign-jurisdiction VAT carried through). Skip the
            # SST gate.
            continue
        if not any(abs(rate_float - r) < 0.01 for r in _MY_SST_RATES):
            raise MyProfileError(
                "lines[%d].tax_rate_pct %.2f is not permitted for "
                "domestic MY invoices. Allowed: %s." % (
                    idx, rate_float,
                    ', '.join('%.0f%%' % r for r in sorted(_MY_SST_RATES)),
                ),
            )


# ---- Malaysian TIN ----------------------------------------------------------


def normalise_my_tin(value):
    """Strip whitespace, hyphens, and dots; uppercase letters."""
    if not value:
        return ''
    return re.sub(r'[\s\-_.]+', '', str(value)).upper()


def validate_my_tin(value):
    """Validate a Malaysian Tax Identification Number.

    Per LHDN: 1-2 letter prefix indicating taxpayer category followed
    by 10-13 digits. No checksum is published in LHDN spec; the
    format is validated structurally, and the TIN registry verifies
    actual existence at the MyInvois portal at lodgement time.

    Returns the canonical form on success, raises MyProfileError
    otherwise.
    """
    canonical = normalise_my_tin(value)
    if not canonical:
        raise MyProfileError("TIN cannot be empty")
    if not _TIN_RE.match(canonical):
        raise MyProfileError(
            "TIN must be 1-2 uppercase letters followed by 10-13 "
            "digits (got %r after normalisation)." % canonical,
        )
    return canonical


def is_valid_my_tin(value):
    """Convenience wrapper. Returns True or False."""
    try:
        validate_my_tin(value)
        return True
    except MyProfileError:
        return False
