# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.close.task: task instance on a close run.

Each task is one item from the parent checklist's task templates,
copied at run creation. Tasks carry their own state machine
(pending, in_progress, done, not_applicable, blocked) and audit
fields (completed_by, completed_at) so the close history shows who
finished what and when.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhCloseTask(models.Model):
    _name = 'eh.close.task'
    _description = "Close run task"
    _order = 'sequence, id'
    _inherit = ['mail.thread', 'eh.workflow.guard']

    # The task state machine gates the parent run's close (required/blocked
    # tasks stop approval). Its transitions run through action_* methods; the
    # guard stops a plain user RPC-writing state to bypass them. Actions below
    # carry the flag via self._eh_workflow_action().
    _eh_guarded_fields = ('state',)

    run_id = fields.Many2one(
        'eh.close.run',
        required=True,
        ondelete='cascade',
        index=True,
        help="Parent close run this task belongs to.",
    )
    sequence = fields.Integer(
        default=10,
        help=(
            "Display order on the run's task list. Lower numbers "
            "render first; the handle widget on the list view writes "
            "this field directly."
        ),
    )

    name = fields.Char(
        required=True,
        help=(
            "Short label visible on the kanban / list view. "
            "Convention: imperative verb + object, e.g. 'Reconcile "
            "bank accounts', 'Post depreciation'."
        ),
    )
    description = fields.Html(
        help=(
            "Detailed instructions for the preparer. Supports rich "
            "text and links so a checklist template can embed "
            "screenshots and references to other Odoo records."
        ),
    )

    state = fields.Selection(
        [
            ('pending', "Pending"),
            ('in_progress', "In Progress"),
            ('done', "Done"),
            ('not_applicable', "Not Applicable"),
            ('blocked', "Blocked"),
        ],
        default='pending',
        required=True,
        tracking=True,
        index=True,
        help=(
            "Task lifecycle state. pending / in_progress are open. "
            "done and not_applicable both count as complete. blocked "
            "prevents the run from closing until resolved."
        ),
    )

    responsible_role = fields.Selection(
        [
            ('accountant', "Accountant"),
            ('manager', "Manager"),
            ('both', "Both"),
        ],
        default='accountant',
        required=True,
        help=(
            "Role typically responsible for this task. Advisory only: "
            "Odoo group permissions are not enforced from this field. "
            "Used by the kanban for swim-lane grouping."
        ),
    )
    is_required = fields.Boolean(
        default=True,
        help=(
            "Required tasks block the run from moving to "
            "pending_approval until they are done or marked "
            "not_applicable. Optional tasks do not block."
        ),
    )

    assigned_user_id = fields.Many2one(
        'res.users', tracking=True,
        help=(
            "User the preparer assigns this task to. Auto-fills with "
            "the current user when action_start fires and the field "
            "is empty."
        ),
    )

    completed_by_id = fields.Many2one(
        'res.users', readonly=True,
        help=(
            "User who marked this task done or not_applicable. "
            "Captured for the close audit trail."
        ),
    )
    completed_at = fields.Datetime(
        readonly=True,
        help="Timestamp at which the task was marked complete.",
    )

    blocked_reason = fields.Char(
        tracking=True,
        help=(
            "Free-text explanation of why the task is blocked. "
            "Surfaces on the form and in the close-run audit pack so "
            "the reviewer understands the dependency."
        ),
    )
    notes = fields.Html(
        help=(
            "Working notes the preparer can leave for the reviewer. "
            "Distinct from chatter, which is the formal audit trail."
        ),
    )

    # ---- transitions ----

    def action_start(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'pending':
                continue
            rec.state = 'in_progress'
            if not rec.assigned_user_id:
                rec.assigned_user_id = self.env.user
        return True

    def action_mark_done(self):
        # Skip tasks already in a terminal state. Re-marking a 'done' or
        # 'not_applicable' task as done would overwrite completed_by_id
        # and completed_at, destroying the audit trail. The previous
        # guard checked for a non-existent 'closed' state, so the
        # protection was not actually in effect.
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state in ('done', 'not_applicable'):
                continue
            rec.write({
                'state': 'done',
                'completed_by_id': self.env.user.id,
                'completed_at': fields.Datetime.now(),
                'blocked_reason': False,
            })
        return True

    def action_mark_not_applicable(self):
        self = self._eh_workflow_action()
        for rec in self:
            rec.write({
                'state': 'not_applicable',
                'completed_by_id': self.env.user.id,
                'completed_at': fields.Datetime.now(),
                'blocked_reason': False,
            })
        return True

    def action_block(self):
        self = self._eh_workflow_action()
        for rec in self:
            rec.state = 'blocked'
        return True

    def action_unblock(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'blocked':
                continue
            rec.write({
                'state': 'in_progress',
                'blocked_reason': False,
            })
        return True

    def action_reset_to_pending(self):
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_(
                "Only an Accounting Manager can reset a task.",
            ))
        self = self._eh_workflow_action()
        for rec in self:
            rec.write({
                'state': 'pending',
                'completed_by_id': False,
                'completed_at': False,
                'blocked_reason': False,
            })
        return True
