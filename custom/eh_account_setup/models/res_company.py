# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""On res.company create, seed setup task lines for the new company.

Without this hook, a tenant adding a second company after install
would see the Setup Guide menu render empty for that company,
because the post_init_hook only ran once at module install time.
"""

from odoo import api, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    @api.model_create_multi
    def create(self, vals_list):
        companies = super().create(vals_list)
        Task = self.env['eh.account.setup.task'].sudo()
        Line = self.env['eh.account.setup.task.line'].sudo()
        tasks = Task.search([])
        if not tasks or not companies:
            return companies
        Line.create([
            {'task_id': t.id, 'company_id': c.id, 'state': 'todo'}
            for c in companies
            for t in tasks
        ])
        return companies
