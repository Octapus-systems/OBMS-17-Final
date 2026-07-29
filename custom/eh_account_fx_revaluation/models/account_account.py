# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Inherit account.account to flag accounts subject to FX revaluation.

Accounts marked eh_fx_revalue have their open foreign currency balances
translated at the period end closing rate by an eh.fx.revaluation.run.
By default, the flag is on for typical monetary accounts (receivable,
payable, bank, credit card, current asset / liability) and off for
non monetary accounts (inventory, fixed assets, equity).
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


_DEFAULT_REVALUE_TYPES = (
    'asset_cash',
    'asset_receivable',
    'liability_payable',
    'liability_credit_card',
)

# Account types that are non monetary under IAS 21 and therefore must
# not be retranslated at the closing rate. Monetary items (cash,
# receivables, payables, loans held in current / non current asset and
# liability accounts) remain eligible; the types below are carried at
# historical rate and are rejected if flagged for revaluation.
_NON_MONETARY_TYPES = (
    'asset_prepayments',
    'asset_fixed',
    'equity',
    'equity_unaffected',
    'income',
    'income_other',
    'expense',
    'expense_depreciation',
    'expense_direct_cost',
    'off_balance',
)


class AccountAccount(models.Model):
    _inherit = 'account.account'

    eh_fx_revalue = fields.Boolean(
        string="FX Revaluation",
        default=False,
        help="If set, open foreign currency balances on this account are "
             "translated at the closing rate during a period end FX "
             "revaluation run.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if (
                'eh_fx_revalue' not in rec._fields
                or rec.eh_fx_revalue
            ):
                continue
            if rec.account_type in _DEFAULT_REVALUE_TYPES:
                rec.eh_fx_revalue = True
        return records

    @api.constrains('eh_fx_revalue', 'account_type')
    def _check_fx_revalue_monetary(self):
        for rec in self:
            if rec.eh_fx_revalue and rec.account_type in _NON_MONETARY_TYPES:
                raise ValidationError(_(
                    "Account %(code)s is non monetary (%(type)s) and cannot "
                    "be flagged for FX revaluation. IAS 21 restricts period "
                    "end retranslation to monetary items (cash, receivables, "
                    "payables, loans); non monetary items such as fixed "
                    "assets, prepayments and equity are carried at historical "
                    "rate.",
                    code=rec.code or rec.name or rec.id,
                    type=rec.account_type,
                ))
