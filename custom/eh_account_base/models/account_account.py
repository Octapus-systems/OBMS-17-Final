# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.account extension for reporting-cache freshness.

Renaming an account, changing its type or code, or moving it between account
groups all change report output (labels, classification, subtotals) without
posting a journal entry. The reporting cache keys on the per-company
eh_move_version counter, so those edits must bump it or a cached report keeps
serving figures/labels computed under the old account metadata until an
unrelated move posts.
"""

from odoo import models


class AccountAccount(models.Model):
    _inherit = 'account.account'

    # Fields whose change alters report presentation or classification.
    _EH_FIGURE_FIELDS = frozenset({'code', 'name', 'account_type', 'group_id'})

    def write(self, vals):
        res = super().write(vals)
        if self and self._EH_FIGURE_FIELDS.intersection(vals):
            # Account metadata is shared across the companies that use the
            # account; a broad per-company bump is cheap next to how rarely
            # accounts are re-typed or renamed.
            companies = self.env['res.company'].sudo().search([])
            companies._eh_bump_move_version(companies.ids)
        return res
