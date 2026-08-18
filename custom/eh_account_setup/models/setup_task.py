# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""eh.account.setup.task: a setup checklist task definition.

Definitions are seeded by XML data and shared across companies.
Per company progress lives on eh.account.setup.task.line, which
holds the state machine and the audit fields. Splitting the two
keeps module upgrades from blowing away user state when a task
description is reworded.
"""

from odoo import _, api, fields, models


class EhAccountSetupTask(models.Model):
    _name = 'eh.account.setup.task'
    _description = "Accounting setup task"
    _order = 'category, sequence, id'

    key = fields.Char(
        required=True,
        index=True,
        copy=False,
        help=(
            "Stable technical identifier. Referenced from data XML "
            "external ids and from any future cross module rules. "
            "Renaming a key after release is a breaking change."
        ),
    )
    name = fields.Char(
        required=True,
        translate=True,
        help="Short verb led label, e.g. 'Configure approval policy'.",
    )
    description = fields.Text(
        translate=True,
        help=(
            "One paragraph explaining what completing this task "
            "achieves. Shown on the form view and the kanban card."
        ),
    )
    sequence = fields.Integer(default=10)
    category = fields.Selection(
        [
            ('foundations', "Foundations"),
            ('currencies', "Currencies"),
            ('receivables', "Receivables"),
            ('payables', "Payables"),
            ('approvals', "Approvals"),
            ('reporting', "Reporting"),
            ('close', "Period Close"),
        ],
        required=True,
        default='foundations',
    )
    required_modules = fields.Char(
        help=(
            "Whitespace separated list of technical module names. "
            "The task only renders for a company when every listed "
            "module is installed in the database. Empty means the "
            "task always renders."
        ),
    )
    action_xmlid = fields.Char(
        required=True,
        help=(
            "External id of the ir.actions.* record to open when the "
            "user clicks Open on the task. Resolved at click time, "
            "so the definition does not break if the target action "
            "is renamed; an unresolved id surfaces a clear error "
            "naming the missing xmlid."
        ),
    )
    icon = fields.Char(
        help=(
            "Optional font awesome class, without the leading 'fa fa-'. "
            "Rendered on the kanban card as a small category glyph."
        ),
    )

    line_ids = fields.One2many(
        'eh.account.setup.task.line',
        'task_id',
        help="Per company progress lines for this task.",
    )

    line_count = fields.Integer(
        compute='_compute_line_counts',
        store=True,
        help="Total number of per company progress lines for this task.",
    )
    line_done_count = fields.Integer(
        compute='_compute_line_counts',
        store=True,
        help="Number of companies that have marked this task done.",
    )
    line_todo_count = fields.Integer(
        compute='_compute_line_counts',
        store=True,
        help="Number of companies that still have this task to do.",
    )
    line_skipped_count = fields.Integer(
        compute='_compute_line_counts',
        store=True,
        help="Number of companies that explicitly skipped this task.",
    )
    completion_ratio = fields.Float(
        compute='_compute_line_counts',
        store=True,
        help=(
            "Percent of companies that have marked this task done. "
            "Skipped companies count as not done so the ratio reflects "
            "real progress, not paperwork shortcuts."
        ),
    )

    _sql_constraints = [
        ('key_unique', 'unique(key)', "Setup task keys must be unique."),
    ]

    @api.depends('line_ids', 'line_ids.state')
    def _compute_line_counts(self):
        for task in self:
            lines = task.line_ids
            done = len(lines.filtered(lambda line_item: line_item.state == 'done'))
            todo = len(lines.filtered(lambda line_item: line_item.state == 'todo'))
            skipped = len(lines.filtered(lambda line_item: line_item.state == 'skipped'))
            task.line_count = len(lines)
            task.line_done_count = done
            task.line_todo_count = todo
            task.line_skipped_count = skipped
            task.completion_ratio = (
                100.0 * done / len(lines) if lines else 0.0
            )

    def action_view_lines(self):
        self.ensure_one()
        return self._action_view_lines_with_state(state=None)

    def action_view_lines_done(self):
        self.ensure_one()
        return self._action_view_lines_with_state(state='done')

    def action_view_lines_todo(self):
        self.ensure_one()
        return self._action_view_lines_with_state(state='todo')

    def action_view_lines_skipped(self):
        self.ensure_one()
        return self._action_view_lines_with_state(state='skipped')

    def _action_view_lines_with_state(self, state):
        domain = [('task_id', '=', self.id)]
        if state:
            domain.append(('state', '=', state))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Per company progress: %(task)s", task=self.name),
            'res_model': 'eh.account.setup.task.line',
            'view_mode': 'kanban,list,form',
            'domain': domain,
            'context': {'search_default_group_company': 1},
        }
