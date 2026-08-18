# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.approval.policy: configurable approval policy per company and document.

A policy targets a document model + move-type combination (vendor bill,
customer invoice, journal entry, refund) and carries an ordered list
of rules. Each rule says: for amounts in this band, this list of
approver groups must sign off in this order.

The first rule whose amount band matches wins; later rules are not
evaluated. Rules are sorted by sequence (drag handle).

A document with no matching policy posts without an approval gate.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


_DOC_TYPE_MAP = {
    'in_invoice': "Vendor bill",
    'in_refund': "Vendor refund",
    'out_invoice': "Customer invoice",
    'out_refund': "Customer refund",
    'entry': "Journal entry",
}


class EhApprovalPolicy(models.Model):
    _name = 'eh.approval.policy'
    _description = "Approval policy"
    _order = 'company_id, sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True,
    )

    document_type = fields.Selection(
        [
            ('in_invoice', "Vendor bill"),
            ('in_refund', "Vendor refund"),
            ('out_invoice', "Customer invoice"),
            ('out_refund', "Customer refund"),
            ('entry', "Journal entry"),
        ],
        required=True, default='in_invoice',
        help="Account.move move_type this policy applies to.",
    )
    rule_ids = fields.One2many(
        'eh.approval.policy.rule', 'policy_id',
        copy=True,
    )
    rule_count = fields.Integer(compute='_compute_rule_count')

    re_approval_threshold_pct = fields.Float(
        default=lambda self: (
            self.env.company.eh_approval_material_change_pct or 10.0
        ),
        help=(
            "When a partially approved move's amount changes by more "
            "than this percentage, the approval request resets and "
            "every step must re-sign. Default seeded from the company's "
            "Heritage settings."
        ),
    )
    re_approval_threshold_abs = fields.Float(
        default=0.0,
        help=(
            "Optional absolute floor for re-approval. A change of less "
            "than this amount never triggers re-approval, even if it "
            "exceeds the percentage threshold. 0 disables the floor."
        ),
    )

    allow_self_approval = fields.Boolean(
        default=False,
        help=(
            "When off (the default), the user who submitted a request "
            "cannot approve or reject it, even if they belong to a "
            "pending approver group. Segregation of duties: someone "
            "else must sign. Turn on only for single-operator companies "
            "that accept self sign-off."
        ),
    )

    notes = fields.Text()

    _sql_constraints = [
        ('check_thresholds', 'CHECK (re_approval_threshold_pct >= 0 AND re_approval_threshold_abs >= 0)', 'Re-approval thresholds cannot be negative.'),  # noqa: E501
    ]

    @api.depends('rule_ids')
    def _compute_rule_count(self):
        for policy in self:
            policy.rule_count = len(policy.rule_ids)

    @api.constrains('rule_ids')
    def _check_rules_present_and_cover(self):
        for policy in self:
            if not policy.rule_ids:
                raise ValidationError(_(
                    "Policy %s has no rules. Add at least one rule "
                    "with an amount band and an approver group.",
                    policy.name,
                ))

    def find_matching_rule(self, amount):
        """Return the first rule whose amount band matches.

        Returns False when no rule matches; the caller should treat
        that as 'no approval needed' rather than as an error, because
        a policy might intentionally exempt small documents.
        """
        self.ensure_one()
        absolute = abs(amount or 0.0)
        for rule in self.rule_ids.sorted('sequence'):
            min_ok = rule.min_amount <= absolute
            max_ok = (rule.max_amount == 0.0) or (absolute <= rule.max_amount)
            if min_ok and max_ok:
                return rule
        return self.env['eh.approval.policy.rule']

    @api.model
    def find_for_move(self, move):
        """Return the policy that governs this move, or empty.

        First match by (company, document_type, active=True), order by
        sequence. The framework picks the lowest-sequence policy when
        multiple are configured for the same document_type so admins
        can layer "default" + "override" policies.
        """
        return self.search(
            [
                ('company_id', '=', move.company_id.id),
                ('document_type', '=', move.move_type),
                ('active', '=', True),
            ],
            order='sequence, id',
            limit=1,
        )


class EhApprovalPolicyRule(models.Model):
    _name = 'eh.approval.policy.rule'
    _description = "Approval policy rule"
    _order = 'policy_id, sequence, id'

    policy_id = fields.Many2one(
        'eh.approval.policy', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(
        help="Optional label shown in the policy form rule list.",
    )
    min_amount = fields.Float(
        digits=(16, 2), default=0.0,
        help="Lower bound of the amount band (inclusive).",
    )
    max_amount = fields.Float(
        digits=(16, 2), default=0.0,
        help=(
            "Upper bound of the amount band (inclusive). Use 0 for "
            "no upper bound; the rule then matches any amount at or "
            "above min_amount."
        ),
    )
    approval_mode = fields.Selection(
        [('sequential', "Sequential"), ('parallel', "Parallel")],
        default='sequential', required=True,
        help=(
            "Sequential: steps are signed in order, one after another. "
            "Parallel: every step is open at once and the request is "
            "approved when all steps are satisfied."
        ),
    )
    step_ids = fields.One2many(
        'eh.approval.policy.rule.step', 'rule_id',
        string="Approval steps (in order)",
        copy=True,
        help=(
            "Ordered approval steps. The request walks the steps one "
            "at a time, top to bottom; any user in the step's group "
            "can sign off to advance the request. The step sequence is "
            "the single source of truth for order, so creating or "
            "removing unrelated security groups in the system never "
            "reorders the steps of a rule already in use."
        ),
    )
    approver_summary = fields.Char(
        compute='_compute_approver_summary',
        string="Approver chain",
        help="Read-only preview of the ordered approver groups.",
    )
    approver_count = fields.Integer(compute='_compute_approver_count')

    sla_hours = fields.Integer(
        default=0,
        help=(
            "Service-level target in hours from request submission to "
            "the final approval. Zero disables SLA tracking for this "
            "band. The cron uses this value to compute due_at and to "
            "send reminders before the deadline."
        ),
    )
    reminder_after_hours = fields.Integer(
        default=0,
        help=(
            "Send a chatter ping to the current approver group when "
            "this many hours of the SLA have elapsed. Zero disables "
            "the pre-deadline reminder. Reminders are idempotent per "
            "step; a subsequent run will not double-post."
        ),
    )
    escalate_after_hours = fields.Integer(
        default=0,
        help=(
            "When the SLA breach exceeds this many hours past due_at, "
            "the cron promotes the request to the escalation group. "
            "Zero disables auto-escalation; in that case the breach "
            "is logged but the queue stays with the original group."
        ),
    )
    escalate_to_group_id = fields.Many2one(
        'res.groups',
        help=(
            "Group the request is escalated to after the breach window "
            "expires. Leave empty to disable escalation; the cron will "
            "still post a chatter warning so the requester sees the "
            "miss."
        ),
    )

    _sql_constraints = [
        ('check_amount_band', 'CHECK (min_amount >= 0 AND max_amount >= 0 AND (max_amount = 0 OR max_amount >= min_amount))', 'Amount band invalid: min and max must be non-negative and max >= min when max is non-zero.'),  # noqa: E501
        ('check_sla_hours_non_negative', 'CHECK (sla_hours >= 0 AND reminder_after_hours >= 0 AND escalate_after_hours >= 0)', 'SLA, reminder and escalation hours must be zero or positive.'),  # noqa: E501
    ]

    @api.depends('step_ids')
    def _compute_approver_count(self):
        for rule in self:
            rule.approver_count = len(rule.step_ids)

    @api.depends('step_ids.group_id', 'step_ids.sequence')
    def _compute_approver_summary(self):
        for rule in self:
            names = rule.step_ids.mapped('group_id.name')
            rule.approver_summary = " > ".join(n for n in names if n)

    @api.constrains('step_ids')
    def _check_at_least_one_step(self):
        for rule in self:
            if not rule.step_ids:
                raise ValidationError(_(
                    "Rule %s has no approval steps. Add at least one "
                    "step; the rule is meaningless without one.",
                    rule.name or rule.policy_id.name,
                ))


class EhApprovalPolicyRuleStep(models.Model):
    _name = 'eh.approval.policy.rule.step'
    _description = "Approval policy rule step"
    _order = 'rule_id, sequence, id'

    rule_id = fields.Many2one(
        'eh.approval.policy.rule', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(
        default=10,
        help=(
            "Position of this step in the approval chain. The request "
            "advances through the steps in ascending sequence; ties "
            "break on record id. This column is the only thing that "
            "decides step order, so it cannot drift when unrelated "
            "security groups are created or removed elsewhere in the "
            "system, even for a request already in review."
        ),
    )
    group_id = fields.Many2one(
        'res.groups', ondelete='restrict',
        string="Approver group",
        help=(
            "Any user in this group can sign off when the request "
            "reaches this step. Optional when named approvers or "
            "manager injection are configured instead."
        ),
    )
    approver_ids = fields.Many2many(
        'res.users', 'eh_approval_step_approver_rel',
        'step_id', 'user_id',
        string="Named approvers",
        help=(
            "Specific users who may sign this step, in addition to any "
            "group members. Use for per-person routing rather than a "
            "broad security group."
        ),
    )
    required_approver_ids = fields.Many2many(
        'res.users', 'eh_approval_step_required_rel',
        'step_id', 'user_id',
        string="Required approvers",
        help=(
            "Approvers who MUST sign before this step advances, even if "
            "the minimum count is otherwise met. Must be a subset of "
            "the eligible approvers (named approvers or group members)."
        ),
    )
    approval_minimum = fields.Integer(
        default=1,
        help=(
            "Minimum number of distinct approvals before the step "
            "advances (N-of-M). Default 1 = any single eligible "
            "approver. The required approvers always count toward and "
            "must be included in this minimum."
        ),
    )
    inject_manager = fields.Boolean(
        string="Add requester's manager",
        help=(
            "When set, the HR manager of the user who submitted the "
            "request is added as an eligible approver for this step."
        ),
    )
    company_id = fields.Many2one(
        related='rule_id.policy_id.company_id',
        store=True, index=True, readonly=True,
    )

    _sql_constraints = [
        ('check_minimum', 'CHECK (approval_minimum >= 1)', 'Approval minimum must be at least 1.'),
    ]

    @api.constrains('group_id', 'approver_ids', 'inject_manager')
    def _check_step_has_approver_source(self):
        for step in self:
            if not (step.group_id or step.approver_ids
                    or step.inject_manager):
                raise ValidationError(_(
                    "Step %s has no approver source: set a group, name "
                    "approvers, or enable manager injection.",
                    step.sequence,
                ))
