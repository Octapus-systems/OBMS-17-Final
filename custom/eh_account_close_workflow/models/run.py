# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.close.run: one execution of a checklist for a period.

Lifecycle:

  open -> in_progress -> pending_approval -> closed
  closed -> reopened -> in_progress (manager only path)

A run cannot move to pending_approval while any required task is still
pending or in_progress. A run cannot close while any task is blocked.
Reopening a closed run does not erase the prepared / reviewed / approved
audit trail; it sets reopened_at and reopened_by alongside.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# Segregation-of-duties anchors. The approve gate reads these to stop a
# preparer or reviewer from approving their own close, so they must be
# tamper-proof: each identity field may only ever record the acting user
# (or keep its current value), never be reassigned to another user or
# cleared, and the timestamps cannot move without their paired identity.
_SOD_ID_ANCHORS = (
    'prepared_by_id', 'reviewed_by_id', 'approved_by_id', 'reopened_by_id',
)
_SOD_AT_ANCHORS = {
    'prepared_at': 'prepared_by_id',
    'reviewed_at': 'reviewed_by_id',
    'approved_at': 'approved_by_id',
    'reopened_at': 'reopened_by_id',
}


class EhCloseRun(models.Model):
    _name = 'eh.close.run'
    _description = "Period close run"
    _order = 'state, period_to desc, id desc'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _rec_name = 'name'

    # State is a control-bearing lifecycle: each transition is gated by an
    # action_* method (SoD checks, blocking-check rescan, manager auth). The
    # guard blocks a plain user RPC-writing state to skip those gates; the
    # actions below carry the flag via self._eh_workflow_action().
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        required=True, tracking=True,
        help=(
            "Human-readable label for this close run. Convention: "
            "'<Company> <Period> Close', e.g. 'Acme March 2026 Close'."
        ),
    )

    checklist_id = fields.Many2one(
        'eh.close.checklist',
        required=True,
        ondelete='restrict',
        index=True,
        tracking=True,
        help=(
            "Reusable checklist template that seeds the run's task "
            "list. Tasks copied from the template are independent "
            "instances; editing a task on the run does not change "
            "the template."
        ),
    )

    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
        help=(
            "Legal entity this close covers. Each entity has its own "
            "independent close run; multi-entity groups should run one "
            "checklist per company."
        ),
    )

    period_from = fields.Date(
        help=(
            "Inclusive start date of the period being closed. Defaults "
            "to the first day of the month derived from period_to when "
            "left blank."
        ),
    )
    period_to = fields.Date(
        help="Inclusive end date of the period being closed.",
    )

    state = fields.Selection(
        [
            ('open', "Open"),
            ('in_progress', "In Progress"),
            ('pending_approval', "Pending Approval"),
            ('closed', "Closed"),
            ('reopened', "Reopened"),
        ],
        default='open',
        required=True,
        tracking=True,
        index=True,
        help=(
            "Lifecycle state. open: just instantiated, no tasks worked. "
            "in_progress: preparer is working through tasks. "
            "pending_approval: preparer requested manager sign-off; no "
            "more task edits without reopening. closed: approved and "
            "locked. reopened: manager unlocked for further edits; "
            "audit trail of original prepare/review/approve preserved."
        ),
    )

    prepared_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help=(
            "User who first transitioned the run to in_progress. "
            "Captured for the segregation-of-duties check at approval "
            "time: a preparer cannot also approve."
        ),
    )
    prepared_at = fields.Datetime(
        readonly=True, copy=False,
        help="Timestamp at which prepared_by_id moved the run to in_progress.",
    )
    reviewed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help=(
            "User who requested approval. Captured for the SoD check: "
            "the requester cannot also approve."
        ),
    )
    reviewed_at = fields.Datetime(
        readonly=True, copy=False,
        help="Timestamp at which reviewed_by_id requested approval.",
    )
    approved_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help=(
            "User who approved the close. Must be different from both "
            "the preparer and the reviewer per SoD enforcement."
        ),
    )
    approved_at = fields.Datetime(
        readonly=True, copy=False,
        help="Timestamp at which approved_by_id closed the run.",
    )
    reopened_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help=(
            "User who reopened the run after close. Restricted to "
            "Accounting Manager group. The original prepare / review / "
            "approve trio is preserved alongside."
        ),
    )
    reopened_at = fields.Datetime(
        readonly=True, copy=False,
        help="Timestamp at which reopened_by_id reopened the closed run.",
    )

    task_ids = fields.One2many(
        'eh.close.task', 'run_id', copy=False,
        help="Tasks instantiated from the checklist template for this run.",
    )

    task_count = fields.Integer(
        compute='_compute_task_progress',
        help="Total number of tasks instantiated on this run.",
    )
    task_done_count = fields.Integer(
        compute='_compute_task_progress',
        help=(
            "Tasks in done or not_applicable state. Both count as "
            "completed for progress reporting."
        ),
    )
    task_blocked_count = fields.Integer(
        compute='_compute_task_progress',
        help=(
            "Tasks marked blocked. Approval is gated until every "
            "blocked task is resolved or marked not_applicable."
        ),
    )
    task_pending_count = fields.Integer(
        compute='_compute_task_progress',
        help=(
            "Tasks still in pending or in_progress. Required pending "
            "tasks block the request-approval transition."
        ),
    )
    progress_pct = fields.Float(
        compute='_compute_task_progress',
        digits=(5, 2),
        help=(
            "Percentage of tasks completed (done + not_applicable / "
            "total). Used by the kanban progress bar."
        ),
    )

    notes = fields.Html(
        help=(
            "Free-form notes from preparer, reviewer, or approver. "
            "Goes into the audit pack alongside the chatter trail."
        ),
    )

    check_ids = fields.One2many(
        'eh.close.check', 'run_id', copy=False,
        help="Results of the most recent automated pre-close scan.",
    )
    has_failed_blocking_checks = fields.Boolean(
        compute='_compute_check_state',
        help="True when a blocking automated check is in 'fail' status.",
    )

    @api.constrains('company_id', 'period_from', 'period_to', 'state')
    def _check_no_overlapping_run(self):
        """Reject a second live close run over the same company + period.

        A period may only have one governing close run in flight. Two
        overlapping runs on one company/period would let two preparers
        sign off the same ledger independently, defeating the close
        governance trail. Overlap is date-range intersection: runs A and
        B collide when A.from <= B.to and B.from <= A.to, treating a
        missing bound as unbounded on that side.

        Opt-in-safe: a run whose period is entirely unbounded (both
        period_from and period_to blank) carries no period identity to
        collide on, so it is never guarded. This preserves the prior
        behaviour for period-less scratch runs and existing tests that
        create runs without dates.
        """
        for rec in self:
            if not rec.period_from and not rec.period_to:
                continue
            domain = [
                ('id', '!=', rec.id),
                ('company_id', '=', rec.company_id.id),
            ]
            # Only genuine, still-governing runs collide. Every state in
            # this model is live (there is no cancelled/abandoned state),
            # so all of them are considered, including 'closed' (an
            # approved run still owns its period) and 'reopened'.
            if rec.period_to:
                # other.period_from <= rec.period_to (unbounded start wins)
                domain += [
                    '|',
                    ('period_from', '=', False),
                    ('period_from', '<=', rec.period_to),
                ]
            if rec.period_from:
                # other.period_to >= rec.period_from (unbounded end wins)
                domain += [
                    '|',
                    ('period_to', '=', False),
                    ('period_to', '>=', rec.period_from),
                ]
            clash = self.search(domain, limit=1)
            if clash:
                raise ValidationError(_(
                    "A close run already governs this company and period: "
                    "'%(other)s'. Only one close run may cover a given "
                    "company and period at a time; reopen or supersede the "
                    "existing run instead of creating an overlapping one.",
                    other=clash.display_name,
                ))

    @api.depends('check_ids.status', 'check_ids.is_blocking')
    def _compute_check_state(self):
        for rec in self:
            rec.has_failed_blocking_checks = bool(rec.check_ids.filtered(
                lambda c: c.is_blocking and c.status == 'fail',
            ))

    @api.depends('task_ids.state')
    def _compute_task_progress(self):
        for rec in self:
            tasks = rec.task_ids
            total = len(tasks) or 1
            done = len(tasks.filtered(
                lambda t: t.state in ('done', 'not_applicable'),
            ))
            blocked = len(tasks.filtered(
                lambda t: t.state == 'blocked',
            ))
            pending = len(tasks.filtered(
                lambda t: t.state in ('pending', 'in_progress'),
            ))
            rec.task_count = len(tasks)
            rec.task_done_count = done
            rec.task_blocked_count = blocked
            rec.task_pending_count = pending
            rec.progress_pct = round(
                (done / total * 100.0) if tasks else 0.0, 2,
            )

    # ---- segregation-of-duties integrity ----

    @api.model_create_multi
    def create(self, vals_list):
        uid = self.env.user.id
        for vals in vals_list:
            for fname in _SOD_ID_ANCHORS:
                if vals.get(fname) and vals[fname] != uid:
                    raise UserError(_(
                        "The prepared / reviewed / approved audit fields are "
                        "set by the close workflow and cannot be pre-assigned "
                        "to another user at creation."))
        return super().create(vals_list)

    def write(self, vals):
        self._eh_guard_sod_anchors(vals)
        return super().write(vals)

    def _eh_guard_sod_anchors(self, vals):
        """Reject any write that would forge an SoD anchor.

        An identity anchor may only be set to the acting user's own id or
        left at its current value; it can never be reassigned to a different
        user or cleared. A timestamp anchor may not be written on its own,
        so a signature time cannot be backdated without also (validly)
        re-recording who signed.
        """
        uid = self.env.user.id
        for fname in _SOD_ID_ANCHORS:
            if fname not in vals:
                continue
            new = vals[fname]
            for rec in self:
                current = rec[fname].id
                if new and (new == current or new == uid):
                    continue
                raise UserError(_(
                    "The '%s' field anchors the segregation-of-duties control "
                    "and may only record the acting user; it cannot be "
                    "reassigned to another user or cleared.", fname))
        for at_field, id_field in _SOD_AT_ANCHORS.items():
            if at_field in vals and id_field not in vals:
                raise UserError(_(
                    "Close-run audit timestamps are stamped by the workflow "
                    "and cannot be edited on their own."))

    # ---- onchange (live form feedback) ----

    @api.onchange('period_to')
    def _onchange_period_to_derive_from(self):
        """Default period_from to the first day of the same month
        when the user picks period_to and hasn't set period_from yet.

        Most close runs cover a single month or quarter; this saves
        a click for the common case while leaving custom ranges
        intact (we only fill an empty period_from).
        """
        for rec in self:
            if not rec.period_to:
                continue
            if rec.period_from and rec.period_from <= rec.period_to:
                continue
            rec.period_from = rec.period_to.replace(day=1)

    # ---- lifecycle ----

    def action_start(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state not in ('open', 'reopened'):
                continue
            rec.write({
                'state': 'in_progress',
                'prepared_by_id': (
                    rec.prepared_by_id.id or self.env.user.id
                ),
                'prepared_at': rec.prepared_at or fields.Datetime.now(),
            })
        return True

    def action_request_approval(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state not in ('in_progress',):
                raise UserError(_(
                    "Can only request approval from in progress.",
                ))
            blocking = rec.task_ids.filtered(
                lambda t: t.is_required and t.state in ('pending', 'in_progress'),
            )
            if blocking:
                raise UserError(_(
                    "Cannot request approval: %d required task(s) still pending: %s",
                ) % (
                    len(blocking),
                    ', '.join(blocking.mapped('name')[:5]),
                ))
            # Recompute the automated checks against the live ledger at the
            # gate, so a stale or hand-tampered stored check row cannot pass
            # approval while the period still has draft / unbalanced / unhashed
            # entries. The freshly written rows drive has_failed_blocking_checks.
            rec.action_run_checks()
            if rec.has_failed_blocking_checks:
                failed = rec.check_ids.filtered(
                    lambda c: c.is_blocking and c.status == 'fail')
                raise UserError(_(
                    "Cannot request approval: automated close checks "
                    "failed: %s. Resolve them and re-run the checks.",
                    ', '.join(failed.mapped('name')),
                ))
            rec.write({
                'state': 'pending_approval',
                'reviewed_by_id': self.env.user.id,
                'reviewed_at': fields.Datetime.now(),
            })
        return True

    def action_approve(self):
        # Authorization gate: closing a period is a manager-only control.
        # This sits ahead of the different-person SoD checks so a
        # non-manager who was neither preparer nor reviewer still cannot
        # approve and close a period. The suite anchors this control on
        # eh_account_base.group_eh_manager.
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an Accounting Manager can approve and close a run.",
            ))
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'pending_approval':
                raise UserError(_(
                    "Can only approve from pending approval.",
                ))
            # Segregation of duties: the user who requested approval
            # (reviewed_by_id) cannot also approve. ICR / SOX control.
            if (
                rec.reviewed_by_id
                and rec.reviewed_by_id.id == self.env.user.id
            ):
                raise UserError(_(
                    "Segregation of duties: %s requested this approval "
                    "and cannot also approve it. Another manager must "
                    "review this close run.",
                    rec.reviewed_by_id.display_name,
                ))
            # Same guard against the original preparer self-approving.
            if (
                rec.prepared_by_id
                and rec.prepared_by_id.id == self.env.user.id
            ):
                raise UserError(_(
                    "Segregation of duties: %s prepared this run and "
                    "cannot approve their own work.",
                    rec.prepared_by_id.display_name,
                ))
            blocked = rec.task_ids.filtered(lambda t: t.state == 'blocked')
            if blocked:
                raise UserError(_(
                    "Cannot close: %d task(s) blocked: %s",
                ) % (
                    len(blocked),
                    ', '.join(blocked.mapped('name')[:5]),
                ))
            rec.write({
                'state': 'closed',
                'approved_by_id': self.env.user.id,
                'approved_at': fields.Datetime.now(),
            })
        return True

    def action_reopen(self):
        # Authorization gate: reopening a closed period is a manager-only
        # control and must be anchored on the suite manager group, not the
        # core account manager group. A non-EH account manager must not be
        # able to reopen a period the EH controls closed.
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an Accounting Manager can reopen a closed run.",
            ))
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'closed':
                raise UserError(_(
                    "Can only reopen a closed run.",
                ))
            rec.write({
                'state': 'reopened',
                'reopened_by_id': self.env.user.id,
                'reopened_at': fields.Datetime.now(),
            })
        return True

    # ---- automated pre-close checks ----

    def action_run_checks(self):
        """Rescan the ledger for the run's period and refresh check rows.

        The rows are system-written: the internal flag below is the only path
        that may create or replace them, and it is set here after the scan has
        produced the results from the ledger, never from user input. sudo lets
        the rescan run for a preparer whose ACL is read-only on the rows.
        """
        Check = self.env['eh.close.check'].sudo().with_context(
            eh_close_check_write=True)
        for rec in self:
            rec.check_ids.sudo().with_context(
                eh_close_check_write=True).unlink()
            for vals in rec._eh_compute_checks():
                Check.create(dict(vals, run_id=rec.id))
        return True

    def _eh_compute_checks(self):
        """Return a list of check-result dicts for this run's period."""
        self.ensure_one()
        return [
            self._eh_check_draft_entries(),
            self._eh_check_unbalanced_entries(),
            self._eh_check_open_suspense(),
        ]

    def _eh_period_move_domain(self, state=None):
        domain = [('company_id', '=', self.company_id.id)]
        if self.period_from:
            domain.append(('date', '>=', self.period_from))
        if self.period_to:
            domain.append(('date', '<=', self.period_to))
        if state:
            domain.append(('state', '=', state))
        return domain

    def _eh_check_draft_entries(self):
        count = self.env['account.move'].search_count(
            self._eh_period_move_domain(state='draft'))
        return {
            'code': 'draft_entries',
            'name': _("Draft journal entries in period"),
            'status': 'fail' if count else 'pass',
            'count': count,
            'is_blocking': True,
            'detail': (_("%d draft entries must be posted or deleted "
                         "before close.") % count) if count else '',
        }

    def _eh_check_unbalanced_entries(self):
        posted = self.env['account.move'].search(
            self._eh_period_move_domain(state='posted'))
        currency = self.company_id.currency_id
        count = sum(
            1 for move in posted
            if not currency.is_zero(sum(move.line_ids.mapped('balance')))
        )
        return {
            'code': 'unbalanced_entries',
            'name': _("Unbalanced posted entries"),
            'status': 'fail' if count else 'pass',
            'count': count,
            'is_blocking': True,
            'detail': (_("%d posted entries do not balance to zero.")
                       % count) if count else '',
        }

    def _eh_check_open_suspense(self):
        journals = self.env['account.journal'].search([
            ('type', 'in', ('bank', 'cash')),
            ('company_id', '=', self.company_id.id),
        ])
        suspense = journals.mapped('suspense_account_id')
        count = 0
        if suspense:
            domain = [
                ('account_id', 'in', suspense.ids),
                ('reconciled', '=', False),
                ('parent_state', '=', 'posted'),
                ('company_id', '=', self.company_id.id),
            ]
            if self.period_to:
                domain.append(('date', '<=', self.period_to))
            count = self.env['account.move.line'].search_count(domain)
        return {
            'code': 'open_suspense',
            'name': _("Open bank/cash suspense lines"),
            'status': 'warn' if count else 'pass',
            'count': count,
            'is_blocking': False,
            'detail': (_("%d statement lines remain in suspense.")
                       % count) if count else '',
        }

    def action_view_tasks(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Tasks"),
            'res_model': 'eh.close.task',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('run_id', '=', self.id)],
            'context': {'default_run_id': self.id},
        }
