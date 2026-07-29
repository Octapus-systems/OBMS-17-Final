# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################

from odoo import fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_benefit_valuation_id = fields.Many2one(
        'eh.benefit.valuation', string="Benefit Valuation", readonly=True,
        index=True, ondelete='restrict', copy=False)
    eh_benefit_dc_accrual_id = fields.Many2one(
        'eh.benefit.dc.accrual', string="DC Accrual", readonly=True,
        index=True, ondelete='restrict', copy=False)
