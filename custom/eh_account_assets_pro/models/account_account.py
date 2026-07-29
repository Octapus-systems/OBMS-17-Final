# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Account-driven automatic asset creation.

Tag a balance-sheet account with an asset category and a creation mode.
When a posted vendor bill books a line to that account, the bill posting
spins up a fixed asset from the category defaults (see account_move.py).
"""

from odoo import fields, models


class AccountAccount(models.Model):
    _inherit = 'account.account'

    eh_asset_category_id = fields.Many2one(
        'eh.asset.category', string="Asset Category",
        help="When a posted vendor bill books a line to this account, an "
             "asset is created automatically in this category.",
    )
    eh_asset_auto = fields.Selection(
        [
            ('no', "No automatic asset"),
            ('draft', "Create draft asset"),
            ('validate', "Create and validate asset"),
        ],
        string="Automatic Asset", default='no', required=True,
        help="'Create draft asset' leaves the new asset in draft for "
             "review; 'Create and validate' also generates the schedule "
             "and starts depreciation. Requires an asset category.",
    )
