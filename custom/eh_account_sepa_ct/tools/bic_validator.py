# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
BIC validation built from ISO 9362.

ISO 9362 defines a Business Identifier Code (BIC) as either 8 or 11
characters with the structure:

    AAAA  4 letters     bank code
    BB    2 letters     country code (ISO 3166-1 alpha-2)
    CC    2 alnum       location code
    [DDD] 3 alnum       branch code (optional, default 'XXX')

The location code letters/digits are case-sensitive only in the spec's
upper-case form; we uppercase before validating.

This implementation is original work. The structure comes directly
from the published ISO 9362 specification at iso.org.
"""

import re


_BIC_RE = re.compile(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$')


class BicValidationError(ValueError):
    """Raised when a BIC string fails validation."""


def normalise_bic(value):
    if not value:
        return ''
    return value.replace(' ', '').upper()


def validate_bic(value):
    """Validate a BIC and return the normalised 11-character form.

    8-character BICs are extended with the default branch 'XXX' to
    canonicalise to the 11-character representation. The 11-character
    form is what PAIN.001 expects in BIC fields.
    """
    cleaned = normalise_bic(value)
    if not cleaned:
        raise BicValidationError("BIC is empty")
    if not _BIC_RE.match(cleaned):
        raise BicValidationError(
            "BIC %r does not match ISO 9362 structure "
            "(4 bank + 2 country + 2 location [+ 3 branch])"
            % cleaned,
        )
    if len(cleaned) == 8:
        cleaned = cleaned + 'XXX'
    return cleaned


def is_bic(value):
    try:
        validate_bic(value)
        return True
    except BicValidationError:
        return False
