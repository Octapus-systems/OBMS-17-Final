# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.close.check: the result of one automated pre-close validation.

A close run can scan its period for anomalies in the ledger (draft
entries still open, unbalanced postings, open bank suspense). Each scan
writes one result row per check. A blocking check in 'fail' status stops
the run from requesting approval until the underlying issue is fixed.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Context flag set only by eh.close.run.action_run_checks when it rescans the
# ledger and refreshes these rows. Every other create / write / unlink is
# refused, so the pre-close blocking gate cannot be cleared by hand-editing a
# stored check row.
_EH_CHECK_WRITE_CTX = 'eh_close_check_write'


class EhCloseCheck(models.Model):
    _name = 'eh.close.check'
    _description = "Period close automated check result"
    _order = 'is_blocking desc, status desc, code'

    run_id = fields.Many2one(
        'eh.close.run', required=True, ondelete='cascade', index=True,
    )
    code = fields.Char(required=True)
    name = fields.Char(required=True)
    status = fields.Selection(
        [('pass', "Pass"), ('warn', "Warning"), ('fail', "Fail")],
        required=True, default='pass',
    )
    count = fields.Integer(default=0)
    detail = fields.Char()
    is_blocking = fields.Boolean(
        default=False,
        help="A blocking check in 'fail' status prevents the close from "
             "moving to approval.",
    )
    checked_at = fields.Datetime(default=fields.Datetime.now)

    # ---- system-written / append-only ----
    # These rows ARE the pre-close blocking gate
    # (eh.close.run.has_failed_blocking_checks). They carry the result of an
    # automated ledger rescan and are written only by action_run_checks. A
    # direct create / write / unlink - by anyone, including a manager - could
    # flip a failed blocking check to 'pass' and clear the gate without fixing
    # the underlying draft / unbalanced / unhashed entries, so every mutation
    # must carry the internal flag set by action_run_checks. The ACL grants no
    # group direct create / write / unlink on top of this guard.
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(_EH_CHECK_WRITE_CTX):
            raise UserError(_(
                "Close check rows are produced by Run Checks, not created "
                "directly."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.context.get(_EH_CHECK_WRITE_CTX):
            raise UserError(_(
                "Close check rows record an automated ledger scan and cannot "
                "be edited directly; re-run the checks to refresh them."))
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get(_EH_CHECK_WRITE_CTX):
            raise UserError(_(
                "Close check rows cannot be deleted directly; re-run the "
                "checks to refresh them."))
        return super().unlink()
