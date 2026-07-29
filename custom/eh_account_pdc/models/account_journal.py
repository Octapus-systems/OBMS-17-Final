# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.journal extension: bounce charge expense account.

The PDC accounting resolver already reads the suspense and default
accounts from the bank journal; the bounce charge expense account
follows the same journal-level configuration pattern, with a company
level fallback (res.company.eh_pdc_bounce_charge_account_id) for
deployments that use one expense account across all banks.
"""

from odoo import fields, models


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    eh_pdc_bounce_charge_account_id = fields.Many2one(
        'account.account',
        string="Bounce Charges Account",
        domain="[('account_type', 'in', ('expense', 'expense_direct_cost'))]",
        help="Expense account debited when a post dated cheque bounces "
             "with bank charges. When empty, the company level account is "
             "used; when neither is set, bounce charges stay informational "
             "on the cheque and no charge entry is posted.",
    )
