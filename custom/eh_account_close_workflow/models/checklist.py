# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.close.checklist: reusable close checklist template.

A checklist defines a sequence of task templates. Each instantiation
(eh.close.run) copies the checklist's task templates into per run task
records. The template stays untouched so subsequent closes start from the
same baseline.
"""

import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


_CODE_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]*$')


class EhCloseChecklist(models.Model):
    _name = 'eh.close.checklist'
    _description = "Period close checklist template"
    _order = 'sequence, name'

    name = fields.Char(
        required=True,
        help=(
            "Display name of the checklist template. Convention: "
            "'<Cadence> Close', e.g. 'Monthly Close', 'Quarterly Close'."
        ),
    )
    code = fields.Char(
        required=True, copy=False,
        help=(
            "Unique programmatic identifier per company. Must match "
            "[a-zA-Z][a-zA-Z0-9_]*. Used for cross-record references "
            "(reports, automations) so a renamed name does not break "
            "external links."
        ),
    )
    description = fields.Text(
        help=(
            "Long-form description of when and how this checklist is "
            "used. Visible to preparers when they pick a checklist."
        ),
    )
    sequence = fields.Integer(
        default=10,
        help="Sort order in the checklist picker. Lower values render first.",
    )
    active = fields.Boolean(
        default=True,
        help=(
            "Soft-archive flag. Inactive checklists do not appear in "
            "the run-creation picker but existing runs that reference "
            "them stay readable."
        ),
    )

    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        index=True,
        help=(
            "Optional company scope. When set, the checklist is only "
            "selectable for that company; when blank, it is shared "
            "across all companies."
        ),
    )

    task_template_ids = fields.One2many(
        'eh.close.task.template',
        'checklist_id',
        copy=True,
        help=(
            "Ordered list of task templates instantiated on each new "
            "run. Editing a template here does not back-port to "
            "existing runs."
        ),
    )
    task_template_count = fields.Integer(
        compute='_compute_task_template_count',
        help="Number of task templates currently defined on this checklist.",
    )

    run_ids = fields.One2many(
        'eh.close.run', 'checklist_id', readonly=True,
        help="Past and current close runs created from this checklist.",
    )
    run_count = fields.Integer(
        compute='_compute_run_count',
        help="Total run count across the checklist's lifetime.",
    )

    _sql_constraints = [
        ('unique_code', 'unique(code, company_id)', 'Checklist code must be unique per company.'),
    ]

    @api.constrains('code')
    def _check_code_format(self):
        for rec in self:
            if not _CODE_RE.match(rec.code or ''):
                raise ValidationError(_(
                    "Checklist code must match [a-zA-Z][a-zA-Z0-9_]* (got %r).",
                ) % rec.code)

    @api.depends('task_template_ids')
    def _compute_task_template_count(self):
        for rec in self:
            rec.task_template_count = len(rec.task_template_ids)

    @api.depends('run_ids')
    def _compute_run_count(self):
        for rec in self:
            rec.run_count = len(rec.run_ids)

    def action_create_run(self, name=None, period_from=None, period_to=None,
                          company_id=None):
        """Create a new run from this checklist with copied task instances.

        Returns the created run record.
        """
        self.ensure_one()
        if not self.task_template_ids:
            raise UserError(_(
                "Checklist '%s' has no task templates; cannot start a run.",
            ) % self.name)
        Run = self.env['eh.close.run']
        company = (
            self.env['res.company'].browse(company_id)
            if company_id else (self.company_id or self.env.company)
        )
        run_vals = {
            'name': name or _("Close from %s") % self.name,
            'checklist_id': self.id,
            'company_id': company.id,
            'period_from': period_from,
            'period_to': period_to,
            'task_ids': [
                (0, 0, {
                    'sequence': tpl.sequence,
                    'name': tpl.name,
                    'description': tpl.description,
                    'responsible_role': tpl.responsible_role,
                    'is_required': tpl.is_required,
                })
                for tpl in self.task_template_ids.sorted('sequence')
            ],
        }
        return Run.create(run_vals)

    def action_view_runs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Runs"),
            'res_model': 'eh.close.run',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('checklist_id', '=', self.id)],
            'context': {'default_checklist_id': self.id},
        }
