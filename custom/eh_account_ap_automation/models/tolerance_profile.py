# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Tolerance profile.

Captures the qty, price and amount tolerances applied to a 3 way match
between a vendor bill line, a purchase order line and the goods
receipt. Each partner can carry an override; otherwise the default
profile (is_default=True) applies.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhApToleranceProfile(models.Model):
    _name = 'eh.ap.tolerance.profile'
    _description = "AP Tolerance Profile"
    _order = 'sequence, name'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    is_default = fields.Boolean(
        default=False,
        help="If set, this profile applies to partners that do not "
             "carry an explicit override. Only one profile per company "
             "can be flagged as default.",
    )
    active = fields.Boolean(default=True)

    qty_tolerance_pct = fields.Float(
        string="Qty Tolerance (%)",
        default=0.0,
        help="Allowed delta between billed qty and received qty as a "
             "percentage of received qty.",
    )
    price_tolerance_pct = fields.Float(
        string="Price Tolerance (%)",
        default=0.0,
        help="Allowed delta between billed unit price and PO unit price "
             "as a percentage of PO unit price.",
    )
    amount_tolerance = fields.Float(
        string="Absolute Amount Tolerance",
        default=0.0,
        help="Allowed absolute delta on the line subtotal in addition "
             "to the percentage tolerances.",
    )
    over_receipt_pct = fields.Float(
        string="Over Receipt Tolerance (%)",
        default=0.0,
        help="Allowed over receipt of qty beyond ordered qty as a "
             "percentage of ordered qty.",
    )

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )

    _sql_constraints = [
        ('check_qty_pct_non_negative', 'CHECK (qty_tolerance_pct >= 0)', 'Qty tolerance must be non negative.'),
        ('check_price_pct_non_negative', 'CHECK (price_tolerance_pct >= 0)', 'Price tolerance must be non negative.'),
        ('check_amount_non_negative', 'CHECK (amount_tolerance >= 0)', 'Amount tolerance must be non negative.'),
    ]

    @api.constrains('is_default', 'company_id')
    def _check_one_default_per_company(self):
        for prof in self:
            if not prof.is_default:
                continue
            existing = self.search([
                ('is_default', '=', True),
                ('company_id', '=', prof.company_id.id),
                ('id', '!=', prof.id),
            ], limit=1)
            if existing:
                raise ValidationError(_(
                    "Company %(company)s already has a default tolerance "
                    "profile (%(profile)s). Unset that one first.",
                    company=prof.company_id.display_name,
                    profile=existing.name,
                ))

    @api.model
    def get_default(self, company=None):
        company = company or self.env.company
        prof = self.search([
            ('is_default', '=', True),
            ('company_id', '=', company.id),
        ], limit=1)
        if not prof:
            raise UserError(_(
                "No default AP tolerance profile configured for company "
                "%s.", company.display_name,
            ))
        return prof

    @api.model
    def resolve_for_partner(self, partner, company=None):
        """Return the tolerance profile to use for `partner`.

        Falls back to the company default if the partner does not
        carry an explicit override.
        """
        company = company or self.env.company
        if partner and partner.eh_ap_tolerance_profile_id:
            return partner.eh_ap_tolerance_profile_id
        return self.get_default(company=company)
