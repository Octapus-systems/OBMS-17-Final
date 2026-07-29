# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Inherit res.partner to attach an AP tolerance profile override and a
parser regex profile.
"""

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_ap_tolerance_profile_id = fields.Many2one(
        'eh.ap.tolerance.profile',
        string="AP Tolerance Profile",
        help="Override the company default tolerance profile for this "
             "vendor's bills.",
    )
    eh_ap_invoice_ref_regex = fields.Char(
        string="Invoice Ref Regex",
        help="Regular expression with one capture group used to extract "
             "the vendor invoice reference from incoming bill text. "
             "Falls back to a generic 'Invoice[: #]+([A-Z0-9-]+)' "
             "pattern if not set.",
    )
    eh_ap_total_regex = fields.Char(
        string="Total Regex",
        help="Regular expression with one capture group used to extract "
             "the total amount from incoming bill text. Falls back to a "
             "generic 'Total[: ]+([0-9.,]+)' pattern.",
    )
