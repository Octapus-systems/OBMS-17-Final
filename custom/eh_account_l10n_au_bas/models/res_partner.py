# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.partner extension: Australian Business Number with mod-89 validation.

Adds eh_au_abn and eh_au_gst_registered. The ABN is validated at write
time using the standalone validator in tools/abn_validator.py; storage
is the canonical 11-digit form (whitespace and punctuation stripped).

This is independent of any e-invoicing module: the ABN is needed for
BAS reporting, partner identity verification, and the W4 (no-ABN
withholding) check, regardless of whether the site sends PEPPOL.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.eh_account_l10n_au_bas.tools.abn_validator import (
    validate_abn, normalise_abn, AbnValidationError, is_valid_abn,
)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_au_abn = fields.Char(
        string="ABN",
        help=(
            "Australian Business Number. 11 digits with mod-89 weighted "
            "checksum. Whitespace and punctuation are stripped on save; "
            "the canonical 11-digit form is stored. Required when "
            "issuing tax invoices to Australian customers."
        ),
    )
    eh_au_gst_registered = fields.Boolean(
        string="GST registered",
        help=(
            "True when the partner is registered for GST. Drives the "
            "W4 no-ABN withholding gate on supplier bills: an unticked "
            "supplier with no ABN triggers the 47% withholding warning."
        ),
    )
    eh_au_abn_valid = fields.Boolean(
        compute='_compute_eh_au_abn_valid',
        store=False,
        help=(
            "Live indicator that the stored ABN passes the mod-89 "
            "checksum. Surfaces on the partner form so a typo is "
            "visible without saving."
        ),
    )

    @api.depends('eh_au_abn')
    def _compute_eh_au_abn_valid(self):
        for partner in self:
            partner.eh_au_abn_valid = bool(
                partner.eh_au_abn and is_valid_abn(partner.eh_au_abn),
            )

    @api.constrains('eh_au_abn')
    def _check_eh_au_abn(self):
        for partner in self:
            if not partner.eh_au_abn:
                continue
            try:
                validate_abn(partner.eh_au_abn)
            except AbnValidationError as exc:
                raise ValidationError(_(
                    "%(partner)s: ABN validation failed. %(detail)s",
                    partner=partner.display_name,
                    detail=str(exc),
                ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('eh_au_abn'):
                vals['eh_au_abn'] = normalise_abn(vals['eh_au_abn'])
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_au_abn' in vals and vals.get('eh_au_abn'):
            vals['eh_au_abn'] = normalise_abn(vals['eh_au_abn'])
        return super().write(vals)
