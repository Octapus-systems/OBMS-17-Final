# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
from odoo import api, models


class EhBudget(models.Model):
    _inherit = 'eh.budget.budget'

    @api.model
    def eh_report_budget_choices(self):
        """Budgets selectable in the P&L dynamic-report budget filter.

        Scoped to the companies the user is currently working in so the
        picker only offers budgets the user may report against. Returns a
        light [{id, name}] list for the OWL options panel.
        """
        budgets = self.search([
            ('company_id', 'in', self.env.companies.ids),
        ], order='name')
        return [{'id': b.id, 'name': b.display_name} for b in budgets]
