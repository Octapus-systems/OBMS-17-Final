# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The approval state machine is a
# standard pattern from the corporate-controls literature, encoded
# afresh against the Odoo account.move post pipeline. No code or
# comments derive from any proprietary or third-party Odoo module.
#
##############################################################################
{
    'name': 'Approval Workflow',
    'summary': 'Multi-step spend authorization for vendor bills and journal entries that blocks posting until every required approver signs off. Configurable approval policies per company and document type, amount-band routing, ordered single-group steps, an atomic compare-and-set state machine, an append-only approval log, and a tested SLA and escalation engine. Search: odoo 19 approval workflow, vendor bill approval, journal entry approval, approval matrix by amount, multi-step approval accounting, block posting until approved, approval SLA escalation, audit log approval odoo.',  # noqa: E501
    'description': """Approval Workflow is the corporate spend control most Community accounting stacks skip: a configurable approval chain that blocks account.move posting until a defined set of approvers has signed off.  # noqa: E501

Amount-band policies. One policy per company and document type, with ordered rules. An amount band selects the single approver group required for that step, and first-match rule routing picks the band. A bill below one threshold might need only a manager; a bill above a higher one walks manager, then director, then CFO.  # noqa: E501

Multi-step ordered approvals. Each step is bound to exactly one approver group and any member of that group can sign off to advance the step. Steps run top-to-bottom in the policy's sequence order, with the order pinned to an explicit sequence column rather than the security-group primary key, so creating or deleting unrelated groups never silently reorders an in-flight chain.  # noqa: E501

Atomic step transitions. Advancing the current step uses a raw SQL UPDATE with a stale-step guard, so two concurrent approvers in the same group can never both advance the request from step N to N+1. One wins, the other gets a clean retry. This race is covered by a regression test.  # noqa: E501

Post gate. The account.move post pipeline is gated: when a matching policy applies and the request is not yet approved, posting raises with a message that names the policy, the pending group, and the step. The gate runs before upstream posting, so sequence numbering and reconciliation side effects only fire after approval, with no half-posted, gap-numbered move.  # noqa: E501

Re-approval on material change. A material amount change after full approval rolls the request back to step zero and requires every step to re-sign. The material threshold is a percentage with an optional absolute floor, configurable per policy, so a large-percentage but tiny-absolute change can be exempted. The watched edit fields are a fixed set (lines, amounts, currency, partner); edits while approval is still in progress are not gated.  # noqa: E501

Append-only audit log. Every transition (submitted, approved, rejected, withdrawn, restarted, reset, escalated, completed) lands as a row on the approval log with the actor, timestamp, and comment. The log is append-only: writes are blocked on every field except the comment, and deletion is refused at the model level.  # noqa: E501

SLA and escalation. Each rule can carry an SLA target in hours that computes a due time and an on-track, at-risk (at eighty percent of the window consumed), or breached state. An hourly cron posts a pre-deadline chatter reminder and performs one-shot auto-escalation to a configured group. Escalation is idempotent (a breached request escalates exactly once), and the sweep wraps each request in its own try/except so one bad rule cannot abort the batch.  # noqa: E501

Reject, withdraw, restart. Any approver in the pending group can reject the request (terminal). The requester or an accounting manager can withdraw the whole request. Withdrawn or rejected requests can be restarted from step zero.  # noqa: E501

No silent fallbacks. A missing policy, a missing matching rule, and an approver acting outside the pending group each raise an explicit message naming the bad config.  # noqa: E501

Activity-driven notifications. Each new step schedules a To-Do activity on the members of the responsible group, surfaced in their personal activity inbox and kanban.""",  # noqa: E501
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.2.0',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'data/cron.xml',
        'data/email_templates.xml',
        'views/approval_policy_views.xml',
        'views/approval_request_views.xml',
        'views/approval_log_views.xml',
        'views/account_move_views.xml',
        'views/res_partner_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_backend': ['eh_account_approval/static/src/js/tours/approval_tour.js'],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
