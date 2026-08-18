# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Period Close Workflow',
    'summary': 'Checklist-driven monthly, quarterly, and year-end close for Odoo 17 Community, with a prepared, reviewed, approved sign-off chain and an enforced segregation of duties on approval. Reusable checklist templates instantiate independent per-run task copies, a state machine gates the run from open to closed, required tasks must clear before approval is requested, blocked tasks stop the close, and every transition is tracked in chatter with user and timestamp. Search terms: odoo 19 month end close checklist, period close workflow odoo community, accounting close sign off chain, segregation of duties close approval, financial close task tracking, year end close checklist template, multi company period close, close run audit trail prepared reviewed approved, reusable accounting close checklist.',  # noqa: E501
    'description': """Period Close Workflow turns the repeatable month-end close that most teams run from a spreadsheet into a tracked process inside Odoo. Build a reusable checklist once, instantiate it for a period, work the tasks, request approval, and close, with a defensible audit trail at the end.  # noqa: E501

Reusable checklist templates carry ordered tasks. Each run copies those tasks into independent per-run instances, so editing a live run never mutates the template and never touches a closed prior period. One default Standard Monthly Close checklist (8 tasks) ships and installs on first install. Deeper cadence and per-subsidiary catalogues are deployment-specific.  # noqa: E501

Each task instance moves through pending, in progress, done, not applicable, or blocked. The run itself moves through open, in progress, pending approval, closed, and reopened. The sign-off chain records who prepared, who reviewed, and who approved, each with a readonly signer and timestamp.  # noqa: E501

The state machine enforces the controls a spreadsheet cannot. A run cannot request approval while any required task is still pending or in progress. Optional tasks are intentionally non-blocking, so a close can proceed with optional items open. A run cannot close while any task is blocked. On approval, segregation of duties is enforced in code: the reviewer who requested approval cannot also approve, and the original preparer cannot approve their own work. A different manager must sign off.  # noqa: E501

Re-marking an already-completed task is a no-op. The original signer and timestamp are preserved, never overwritten. Where rework is genuinely needed, an Accounting Manager can run a Reset to Pending action that clears the completion and any blocked reason, and that reset is itself recorded in chatter. A closed run can be reopened by a manager for post-close adjustments, and the reopen preserves the original prepared, reviewed, approved trio while stamping who reopened it and when.  # noqa: E501

Every state transition and sign-off is recorded in the standard Odoo chatter via mail.thread tracking, with user and timestamp. Tasks are real records, not free text, so they are queryable, exportable, and audit ready. Per-company record rules isolate checklists, runs, and tasks across companies.  # noqa: E501

Pairs naturally with the dynamic reports and reconciliation modules in the suite at close time.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.1.3',
    'depends': ['eh_account_base', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/checklist_default.xml',
        'views/checklist_views.xml',
        'views/run_views.xml',
        'data/menus.xml',
    ],
    'demo': ['demo/close_demo.xml'],
    'assets': {
        'web.assets_backend': ['eh_account_close_workflow/static/src/js/tours/close_tour.js'],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
