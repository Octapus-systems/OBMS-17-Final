# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Adds an explicit "represented company" link to res.partner.

The intercompany mirror walks `partner.commercial_partner_id.company_id`
to find the destination company. Setting `res.partner.company_id`
directly to the represented company forbids the partner from being
used on invoices in any other company (Odoo's `_check_company`
guard). The dedicated field below sidesteps that by leaving
`partner.company_id` global (False) while still recording which
company the partner represents.
"""

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_represented_company_id = fields.Many2one(
        'res.company', string="Represented Company",
        index=True, copy=False,
        help=(
            "When set, posting an out_invoice for this partner triggers "
            "an inter-company mirror as a vendor bill in the named "
            "company. Leave blank if the partner is a regular customer."
        ),
    )
