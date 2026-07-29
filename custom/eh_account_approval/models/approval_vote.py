# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.approval.vote: one approver's decision on one step of one request.

The base workflow advances a step as soon as any one group member signs.
Named approvers, N-of-M minimums, and required approvers all need to
know *who* signed *which* step, so each decision is recorded as a vote.
A unique (request, step_index, user) constraint stops one approver from
voting twice on the same step; the request reads these rows to decide
when a step is satisfied.
"""

from odoo import api, fields, models, _
from odoo.exceptions import AccessError

# Evidence anchors: who signed, which step, when. Immutable once a vote
# exists so a recorded signature cannot be re-attributed or backdated. Only
# decision/comment may change, and only when an approver re-signs a step.
_EVIDENCE_FIELDS = frozenset({'request_id', 'step_index', 'user_id', 'voted_at'})


class EhApprovalVote(models.Model):
    _name = 'eh.approval.vote'
    _description = "Approval vote"
    _order = 'voted_at desc, id desc'

    request_id = fields.Many2one(
        'eh.approval.request', required=True,
        ondelete='cascade', index=True,
    )
    step_index = fields.Integer(
        required=True,
        help="Zero-based index of the step this vote applies to.",
    )
    user_id = fields.Many2one('res.users', required=True, index=True)
    decision = fields.Selection(
        [('approve', "Approved"), ('reject', "Rejected")],
        required=True,
    )
    comment = fields.Text()
    voted_at = fields.Datetime(default=fields.Datetime.now, readonly=True)

    _sql_constraints = [
        ('unique_vote', 'unique(request_id, step_index, user_id)', 'An approver can vote at most once per step.'),
    ]

    # -- Tamper evidence -------------------------------------------------
    # A vote row is the sole source of truth for whether an approval step is
    # satisfied, so it must be as trustworthy as a wet signature. Votes are
    # cast, revised and cleared *only* by the approval workflow, which records
    # them in sudo after checking the acting user's eligibility. Every direct
    # (non-sudo) mutation is refused; even in sudo the evidence anchors are
    # frozen and the server stamps the vote time. Behind these guards the ACL
    # grants no group direct write/create/unlink on this model.

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_(
                "Approval votes are recorded by the approval workflow and "
                "cannot be created directly."))
        now = fields.Datetime.now()
        for vals in vals_list:
            vals['voted_at'] = now
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            raise AccessError(_(
                "Approval votes are immutable once recorded."))
        locked = _EVIDENCE_FIELDS.intersection(vals)
        if locked:
            raise AccessError(_(
                "The approver, step and time recorded on a vote cannot be "
                "changed."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError(_(
                "Approval votes cannot be deleted directly."))
        return super().unlink()
