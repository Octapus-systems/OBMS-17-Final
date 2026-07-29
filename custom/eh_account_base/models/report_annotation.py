# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.account.report.annotation: a note pinned to a cell or row of a
dynamic financial report.

Annotations attach by (report_code, line_id, expression_label): the
orchestrator injects them into the rendered payload after the figures
are computed, so a note follows its line wherever the report is run and
is never baked into the cached result. expression_label empty annotates
the whole row; set it to a column label (e.g. 'amount') to annotate a
single cell.
"""

from odoo import fields, models


class EhAccountReportAnnotation(models.Model):
    _name = 'eh.account.report.annotation'
    _description = "Dynamic report annotation"
    _order = 'create_date desc, id desc'

    report_code = fields.Char(required=True, index=True)
    line_id = fields.Char(
        required=True, index=True,
        help="Id of the report line the note attaches to "
             "(e.g. 'account-5', 'net_profit').",
    )
    expression_label = fields.Char(
        help="Column label to pin the note to a single cell; empty "
             "annotates the whole row.",
    )
    text = fields.Text(required=True)
    company_id = fields.Many2one(
        'res.company', required=True, index=True,
        default=lambda self: self.env.company,
    )
