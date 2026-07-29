# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sepa.creditor: per-company SEPA creditor identifier and journal.

A creditor identifier is the registration number a national authority
issues to a company that wants to collect via SEPA Direct Debit. In
Germany it is the Glaeubiger-ID, in Spain the suffix-encoded VAT id, in
France the ICS, and so on. Format varies; we store it as a string with
a length cap and let the rendering layer place it into CdtrSchmeId.

One creditor record per (company, journal) pair so a multi-currency or
multi-bank business can hold multiple identifiers without contention.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.eh_account_sepa_ct.tools.iban_validator import (
    validate_iban, IbanValidationError,
)
from odoo.addons.eh_account_sepa_ct.tools.bic_validator import (
    validate_bic, BicValidationError,
)


class EhSepaCreditor(models.Model):
    _name = 'eh.sepa.creditor'
    _description = "SEPA Direct Debit creditor (per company / journal)"
    _order = 'company_id, journal_id'

    name = fields.Char(required=True, translate=True)
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
    )
    journal_id = fields.Many2one(
        'account.journal', required=True,
        domain="[('type', '=', 'bank')]",
        ondelete='cascade',
    )
    creditor_identifier = fields.Char(
        required=True,
        help=(
            "SEPA Creditor Identifier (CI) issued by the company's "
            "national authority. Capped at 35 characters per the "
            "scheme. Format and validation differ by country; the "
            "module stores the string verbatim."
        ),
    )
    creditor_name = fields.Char(
        required=True,
        help=(
            "Name to render in the Cdtr block of every PmtInf section. "
            "Capped at 70 characters per the scheme. Should match the "
            "name on the creditor identifier registration."
        ),
    )
    iban = fields.Char(
        string="Collection IBAN", required=True,
        help="Where collected funds land. Validated locally on save.",
    )
    bic = fields.Char(string="BIC / SWIFT")
    default_local_instrument = fields.Selection(
        [
            ('CORE', "CORE (consumer)"),
            ('B2B', "B2B (business-to-business)"),
            ('COR1', "COR1 (one-day legacy)"),
        ],
        required=True,
        default=lambda self: (
            self.env.company.eh_sepa_dd_default_instrument or 'CORE'
        ),
        help=(
            "Default scheme variant. CORE is the consumer scheme. B2B "
            "is for collections from business debtors and gives no "
            "refund right. COR1 is a faster legacy variant; most "
            "banks now treat it as CORE. Default seeded from the "
            "company's Heritage settings."
        ),
    )
    pre_notification_days = fields.Integer(
        default=14,
        help=(
            "Minimum days between a pre-notification (sent to the "
            "debtor) and the collection date. Default 14 follows the "
            "scheme rule for first / one-off collections; many "
            "banks accept fewer days for recurring once a mandate is "
            "active."
        ),
    )
    notes = fields.Text()

    _sql_constraints = [
        ('unique_per_company_journal', 'unique(company_id, journal_id)', 'Only one SEPA creditor per company per journal.'),
    ]

    @api.constrains('iban')
    def _check_iban(self):
        for rec in self:
            try:
                validate_iban(rec.iban)
            except IbanValidationError as exc:
                raise ValidationError(_(
                    "Collection IBAN failed validation: %s",
                ) % str(exc))

    @api.constrains('bic')
    def _check_bic(self):
        for rec in self:
            if not rec.bic:
                continue
            try:
                validate_bic(rec.bic)
            except BicValidationError as exc:
                raise ValidationError(_(
                    "BIC failed validation: %s",
                ) % str(exc))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('iban'):
                try:
                    vals['iban'] = validate_iban(vals['iban'])
                except IbanValidationError as exc:
                    raise ValidationError(_(
                        "Collection IBAN failed validation: %s",
                    ) % str(exc))
            if vals.get('bic'):
                try:
                    vals['bic'] = validate_bic(vals['bic'])
                except BicValidationError as exc:
                    raise ValidationError(_(
                        "BIC failed validation: %s",
                    ) % str(exc))
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('iban'):
            try:
                vals['iban'] = validate_iban(vals['iban'])
            except IbanValidationError as exc:
                raise ValidationError(_(
                    "Collection IBAN failed validation: %s",
                ) % str(exc))
        if vals.get('bic'):
            try:
                vals['bic'] = validate_bic(vals['bic'])
            except BicValidationError as exc:
                raise ValidationError(_(
                    "BIC failed validation: %s",
                ) % str(exc))
        return super().write(vals)

    @api.constrains('creditor_identifier')
    def _check_identifier_length(self):
        for rec in self:
            if rec.creditor_identifier and len(rec.creditor_identifier) > 35:
                raise ValidationError(_(
                    "Creditor identifier is capped at 35 characters "
                    "per the SEPA scheme; got %d.",
                ) % len(rec.creditor_identifier))

    @api.constrains('creditor_name')
    def _check_name_length(self):
        for rec in self:
            if rec.creditor_name and len(rec.creditor_name) > 70:
                raise ValidationError(_(
                    "Creditor name is capped at 70 characters per the "
                    "SEPA scheme; got %d.",
                ) % len(rec.creditor_name))
