# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""eh.account.setup.task.line: per company progress on a setup task.

One line per (task definition, company). Holds the state machine
(todo, done, skipped) and the audit fields (completed_at,
completed_by_id, notes). A computed is_relevant flag hides tasks
that depend on optional modules that the customer did not install.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhAccountSetupTaskLine(models.Model):
    _name = 'eh.account.setup.task.line'
    _description = "Accounting setup task progress"
    _order = 'category, sequence, id'
    _rec_name = 'name'

    task_id = fields.Many2one(
        'eh.account.setup.task',
        required=True,
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        ondelete='cascade',
        index=True,
        default=lambda self: self.env.company,
    )

    name = fields.Char(
        related='task_id.name', readonly=True,
    )
    description = fields.Text(
        related='task_id.description', store=False, readonly=True,
    )
    category = fields.Selection(
        related='task_id.category', store=True, readonly=True,
    )
    sequence = fields.Integer(
        related='task_id.sequence', store=True, readonly=True,
    )
    icon = fields.Char(
        related='task_id.icon', store=False, readonly=True,
    )
    required_modules = fields.Char(
        related='task_id.required_modules', store=True, readonly=True,
    )
    action_xmlid = fields.Char(
        related='task_id.action_xmlid', store=False, readonly=True,
    )

    state = fields.Selection(
        [
            ('todo', "To do"),
            ('done', "Done"),
            ('skipped', "Skipped"),
        ],
        default='todo',
        required=True,
        index=True,
    )
    completed_at = fields.Datetime(readonly=True)
    completed_by_id = fields.Many2one('res.users', readonly=True)
    notes = fields.Text(
        help="Free notes the preparer leaves for the reviewer.",
    )

    is_relevant = fields.Boolean(
        compute='_compute_is_relevant',
        search='_search_is_relevant',
        help=(
            "True when every module listed on the task definition is "
            "installed. The Setup Guide action filters on this flag "
            "so the user does not see clutter for features they did "
            "not install."
        ),
    )

    sibling_line_ids = fields.Many2many(
        'eh.account.setup.task.line',
        'eh_setup_line_sibling_rel',
        'origin_id',
        'sibling_id',
        compute='_compute_sibling_line_ids',
        help=(
            "Other companies' progress on the same task. Renders as a "
            "kanban snapshot on the form so the operator can see how "
            "the rest of the tenant is progressing without leaving "
            "the record."
        ),
    )
    peer_line_ids = fields.Many2many(
        'eh.account.setup.task.line',
        'eh_setup_line_peer_rel',
        'origin_id',
        'peer_id',
        compute='_compute_peer_line_ids',
        help=(
            "Other tasks for this same company in the same category. "
            "Renders as a kanban snapshot on the form so the operator "
            "knows what is next in the same area."
        ),
    )

    company_done_count = fields.Integer(
        compute='_compute_company_progress',
        help="Number of relevant tasks this company has marked done.",
    )
    company_total_count = fields.Integer(
        compute='_compute_company_progress',
        help="Total number of relevant tasks on this company's checklist.",
    )
    task_done_count = fields.Integer(
        compute='_compute_task_progress',
        help="Number of companies that have completed this task.",
    )
    task_total_count = fields.Integer(
        compute='_compute_task_progress',
        help="Total number of companies that have this task on their checklist.",
    )

    _sql_constraints = [
        ('line_unique', 'unique(task_id, company_id)', "A task can only have one progress line per company."),
    ]

    @api.depends('required_modules')
    def _compute_is_relevant(self):
        if not self:
            return
        installed = self._installed_module_names()
        for line in self:
            required = (line.required_modules or '').split()
            line.is_relevant = all(m in installed for m in required)

    def _search_is_relevant(self, operator, value):
        if operator not in ('=', '!=') or value not in (True, False):
            raise UserError(_(
                "Unsupported search on is_relevant: %(op)s %(val)s",
                op=operator, val=value,
            ))
        wanted = (operator == '=' and value) or (operator == '!=' and not value)
        installed = self._installed_module_names()
        all_lines = self.with_context(active_test=False).search([])
        keep_ids = []
        for line in all_lines:
            required = (line.required_modules or '').split()
            ok = all(m in installed for m in required)
            if ok == wanted:
                keep_ids.append(line.id)
        return [('id', 'in', keep_ids)]

    @api.model
    def _installed_module_names(self):
        return set(self.env['ir.module.module'].sudo().search([
            ('state', '=', 'installed'),
        ]).mapped('name'))

    def action_open(self):
        self.ensure_one()
        if not self.action_xmlid:
            raise UserError(_(
                "Setup task '%(name)s' has no action configured.",
                name=self.name,
            ))
        action = self.env.ref(self.action_xmlid, raise_if_not_found=False)
        if not action:
            raise UserError(_(
                "Setup task '%(name)s' refers to an action that does "
                "not exist in this database: %(xmlid)s. Install or "
                "upgrade the module that owns the action, then retry.",
                name=self.name,
                xmlid=self.action_xmlid,
            ))
        return action.read()[0]

    def action_mark_done(self):
        self.write({
            'state': 'done',
            'completed_at': fields.Datetime.now(),
            'completed_by_id': self.env.user.id,
        })
        return True

    def action_mark_skipped(self):
        self.write({
            'state': 'skipped',
            'completed_at': fields.Datetime.now(),
            'completed_by_id': self.env.user.id,
        })
        return True

    def action_reset(self):
        self.write({
            'state': 'todo',
            'completed_at': False,
            'completed_by_id': False,
        })
        return True

    @api.depends('task_id')
    def _compute_sibling_line_ids(self):
        for line in self:
            if not line.task_id:
                line.sibling_line_ids = self.browse()
                continue
            line.sibling_line_ids = self.search([
                ('task_id', '=', line.task_id.id),
                ('id', '!=', line.id or 0),
            ])

    @api.depends('company_id', 'category')
    def _compute_peer_line_ids(self):
        for line in self:
            if not line.company_id or not line.category:
                line.peer_line_ids = self.browse()
                continue
            line.peer_line_ids = self.search([
                ('company_id', '=', line.company_id.id),
                ('category', '=', line.category),
                ('id', '!=', line.id or 0),
            ])

    @api.depends('company_id', 'state', 'is_relevant')
    def _compute_company_progress(self):
        for line in self:
            if not line.company_id:
                line.company_done_count = 0
                line.company_total_count = 0
                continue
            same_company = self.search([
                ('company_id', '=', line.company_id.id),
            ])
            relevant = same_company.filtered(lambda line_item: line_item.is_relevant)
            line.company_total_count = len(relevant)
            line.company_done_count = len(
                relevant.filtered(lambda line_item: line_item.state == 'done'),
            )

    @api.depends('task_id', 'state')
    def _compute_task_progress(self):
        for line in self:
            if not line.task_id:
                line.task_done_count = 0
                line.task_total_count = 0
                continue
            same_task = self.search([
                ('task_id', '=', line.task_id.id),
            ])
            line.task_total_count = len(same_task)
            line.task_done_count = len(
                same_task.filtered(lambda line_item: line_item.state == 'done'),
            )

    def action_view_company_lines(self):
        self.ensure_one()
        if not self.company_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': _("Setup tasks for %(company)s", company=self.company_id.name),
            'res_model': 'eh.account.setup.task.line',
            'view_mode': 'kanban,list,form',
            'domain': [('company_id', '=', self.company_id.id)],
            'context': {
                'search_default_group_category': 1,
                'search_default_filter_relevant': 1,
            },
        }

    def action_view_task_lines(self):
        self.ensure_one()
        if not self.task_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': _("Companies on task: %(task)s", task=self.task_id.name),
            'res_model': 'eh.account.setup.task.line',
            'view_mode': 'kanban,list,form',
            'domain': [('task_id', '=', self.task_id.id)],
            'context': {'search_default_group_state': 1},
        }
