# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Install hook for eh_account_setup.

On install, ensure one eh.account.setup.task.line row exists per
(seeded task definition, active company) so the Setup Guide menu
shows every relevant task for every company without the user
having to seed by hand. Idempotent: re-running the hook on an
already seeded company is a no-op.
"""

import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    Task = env['eh.account.setup.task'].sudo()
    Line = env['eh.account.setup.task.line'].sudo()
    Company = env['res.company'].sudo()

    tasks = Task.search([])
    companies = Company.search([])
    if not tasks or not companies:
        return

    for company in companies:
        existing_task_ids = set(Line.search([
            ('company_id', '=', company.id),
        ]).mapped('task_id.id'))
        missing = [t for t in tasks if t.id not in existing_task_ids]
        if not missing:
            continue
        Line.create([
            {'task_id': t.id, 'company_id': company.id, 'state': 'todo'}
            for t in missing
        ])
        _logger.info(
            "eh_account_setup post_init: seeded %d task lines for company %s",
            len(missing), company.name,
        )
