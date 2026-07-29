# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.company extension: vendor-bill OCR extractor credentials.

The line-item extractor adapters (OpenAI / local / Claude vision
packages) read their API credentials from this single JSON field so the
core AP automation module carries no provider-specific configuration and
no provider dependency. Absent any value, the regex-only path runs
unchanged.
"""

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    eh_ap_ocr_config = fields.Text(
        string="AP OCR credentials (JSON)",
        groups="base.group_system",
        help=(
            "JSON-encoded credentials handed to the configured vendor-"
            "bill line-item extractor. Shape varies per extractor "
            "(api_key + model for hosted vision, endpoint + model for "
            "local). Only read when an intake names a registered "
            "extractor; the deterministic regex parser needs none. "
            "Restricted to system administrators because it holds the "
            "extractor API key; the intake reads it via sudo so OCR "
            "still works for ordinary users without exposing the "
            "secret to them."
        ),
    )
