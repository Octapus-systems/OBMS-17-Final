# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Audit log of every bank statement import attempt.

Every wizard run inserts one log row, whether it succeeded, was a no-op
(file already imported), or failed. Auditors and operators get a single
queryable record of what landed and where, which file produced it, and
which exception (if any) was raised.
"""

from odoo import fields, models


class EhBankStatementImportLog(models.Model):
    _name = 'eh.account.bank.statement.import.log'
    _description = "Bank statement import log"
    _order = 'imported_at desc, id desc'

    imported_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True,
    )
    imported_by_id = fields.Many2one(
        'res.users', required=True,
        default=lambda self: self.env.user,
    )
    journal_id = fields.Many2one('account.journal', required=True)
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )
    profile_id = fields.Many2one(
        'eh.account.bank.statement.import.profile',
    )
    format_key = fields.Char(required=True)
    filename = fields.Char()
    file_hash = fields.Char(
        size=64, index=True,
        help="SHA-256 of the uploaded file content. Drives the idempotent reimport guard.",
    )
    statement_id = fields.Many2one(
        'account.bank.statement', ondelete='set null',
    )
    line_count = fields.Integer(default=0)
    skipped_count = fields.Integer(
        default=0,
        help="Lines skipped because they were already imported in a prior run.",
    )
    state = fields.Selection(
        [
            ('done', "Done"),
            ('duplicate', "Duplicate (no-op)"),
            ('error', "Error"),
        ],
        required=True, default='done', index=True,
    )
    error_message = fields.Text()
