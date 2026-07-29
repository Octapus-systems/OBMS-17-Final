# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.approval.log: per-step audit row.

Append-only by design (inherited from eh.audit.mixin). The request's
transitions create rows here; nothing else writes. Auditors and
operators query by request_id to get the full lineage of who acted,
when, and what they said.
"""

from odoo import fields, models


_ACTION_SELECTION = [
    ('submitted', "Submitted"),
    ('approved', "Approved"),
    ('rejected', "Rejected"),
    ('withdrawn', "Withdrawn"),
    ('restarted', "Restarted"),
    ('reset', "Reset (re-approval triggered)"),
    ('escalated', "Escalated"),
    ('completed', "Completed"),
    ('delegated', "Delegated"),
    ('force_approved', "Force approved"),
]


class EhApprovalLog(models.Model):
    _name = 'eh.approval.log'
    _description = "Approval log entry"
    _inherit = ['eh.audit.mixin']

    _eh_audit_unlink_message = (
        "Approval log rows cannot be deleted. Reject the request "
        "instead if the action should be voided."
    )

    request_id = fields.Many2one(
        'eh.approval.request', required=True,
        ondelete='restrict', index=True,
    )
    action = fields.Selection(
        _ACTION_SELECTION, required=True,
    )
    step = fields.Integer(
        help="Approval step at which this action took place.",
    )
