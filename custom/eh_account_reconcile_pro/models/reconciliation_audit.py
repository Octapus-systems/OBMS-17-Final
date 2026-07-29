# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.reconciliation.audit: per line audit of reconciliation decisions.

One row per (statement_line, decision) pair. For a match decision we record
the target AML id, the suggestion engine confidence at decision time, the
heuristics that fired, the user, and whether the user accepted a suggestion
or made the decision manually. For write-off decisions we record the same
metadata with aml_id null.

Why a separate model and not chatter on bank.statement.line:

* The audit log is queryable: how many decisions per partner per month, what
  is the average confidence on confirmed matches, how often does the
  suggestion engine win versus manual matching.
* The history score in the suggestion engine queries this table directly.
* It survives statement line deletion via ondelete='cascade' on the
  statement_line side; we lose the link but keep aggregate stats.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhReconciliationAudit(models.Model):
    _name = 'eh.reconciliation.audit'
    _description = "Per line reconciliation decision audit"
    _order = 'decided_at desc'
    _rec_name = 'display_name'

    display_name = fields.Char(compute='_compute_display_name', store=True)

    session_id = fields.Many2one(
        'eh.reconciliation.session',
        ondelete='restrict',
        index=True,
    )
    statement_line_id = fields.Many2one(
        'account.bank.statement.line',
        required=True,
        ondelete='restrict',
        index=True,
    )
    aml_id = fields.Many2one(
        'account.move.line',
        ondelete='set null',
        index=True,
    )
    user_id = fields.Many2one(
        'res.users',
        required=True,
        default=lambda self: self.env.user,
        index=True,
    )
    decided_at = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        index=True,
    )

    confidence = fields.Float(
        default=0.0,
        digits=(3, 4),
        help=(
            "Suggestion engine total score at decision time (0 to 1). "
            "0 indicates a manual match without engine guidance."
        ),
    )
    rules_fired = fields.Char(
        help=(
            "Comma separated names of heuristics that scored above zero "
            "for this match. For example: amount,date,partner."
        ),
    )

    decision = fields.Selection(
        [
            ('match', "Matched"),
            ('write_off', "Written off"),
            ('skip', "Skipped"),
        ],
        required=True,
        index=True,
    )
    source = fields.Selection(
        [
            ('manual', "Manual"),
            ('drag_drop', "Drag and Drop"),
            ('suggestion', "Accepted Suggestion"),
            ('rule', "Reconciliation Rule"),
            ('bulk', "Bulk Operation"),
        ],
        default='manual',
        required=True,
    )

    @api.depends('decision', 'statement_line_id', 'decided_at')
    def _compute_display_name(self):
        for rec in self:
            sl_label = rec.statement_line_id.payment_ref or 'statement line'
            ts = (
                fields.Datetime.to_string(rec.decided_at)
                if rec.decided_at else ''
            )
            rec.display_name = "%s: %s (%s)" % (
                rec.decision or 'pending', sl_label, ts,
            )

    # ----------- audit-log immutability -----------
    # These rows are the decision trail the suggestion engine's history score
    # and every reconciliation review read as fact. They are written once at
    # decision time and never legitimately mutated or deleted, so they are
    # strictly append-only: write() and unlink() are refused for everyone. The
    # previous context-flag escape was caller-controllable (any RPC caller
    # could pass eh_internal_audit_write/unlink) and was never set by the app,
    # so it was a bypass, not a control. The ACL sets perm_write=0/perm_unlink=0
    # on top of these guards as defence in depth.
    def write(self, vals):
        raise UserError(_(
            "Reconciliation audit rows are append-only and cannot be "
            "edited once recorded."))

    @api.ondelete(at_uninstall=False)
    def _unlink_immutable_audit(self):
        raise UserError(_(
            "Reconciliation audit rows cannot be deleted; they are an "
            "immutable decision trail."))
