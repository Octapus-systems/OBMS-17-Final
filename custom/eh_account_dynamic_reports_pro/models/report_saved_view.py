# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.report.saved.view: per user saved filter set for a dynamic report.

Each saved view captures a complete options dict (date range, company set,
posted_only, show_zero, journal/partner/account filters, etc.) under a
human friendly name. Users can pin frequently used views to surface them
in a quick access list, or share a view across the company so a team
sees the same filtered baseline.

Why a model and not a user preference dict:

* Audit. We track use_count and last_used so we can surface which views
  are loved versus dead weight.
* Sharing. Per user dicts are private by design; saved views explicitly
  opt in to is_shared.
* Discoverability. Saved views appear in the OWL viewer's load list so
  users discover what their colleagues built without copying URLs.
"""

import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhReportSavedView(models.Model):
    _name = 'eh.report.saved.view'
    _description = "User saved view for a dynamic report"
    _order = 'pinned desc, sequence asc, name asc'

    name = fields.Char(required=True)
    user_id = fields.Many2one(
        'res.users',
        required=True,
        default=lambda self: self.env.user,
        index=True,
    )
    report_id = fields.Many2one(
        'eh.account.dynamic.report',
        required=True,
        ondelete='cascade',
        index=True,
    )

    options_json = fields.Text(
        required=True,
        help="JSON serialised options dict captured at save time.",
    )

    sequence = fields.Integer(default=10)
    pinned = fields.Boolean(
        default=False,
        help=(
            "Surface this view in the dashboard quick access list. "
            "Pinned views render above unpinned ones in the viewer."
        ),
    )
    is_shared = fields.Boolean(
        default=False,
        help=(
            "When enabled, this view is visible to every user of the same "
            "company. Useful for a team to share a baseline filter set."
        ),
    )
    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        index=True,
    )

    notes = fields.Text(help="Optional free text describing the view.")

    created_on = fields.Datetime(
        readonly=True,
        default=fields.Datetime.now,
    )
    last_used_at = fields.Datetime(readonly=True)
    use_count = fields.Integer(default=0, readonly=True)

    _sql_constraints = [
        ('unique_user_report_name', 'unique(user_id, report_id, name)', 'Saved view names must be unique per user and per report.'),
    ]

    # ---- public api ----

    @api.model
    def save_view(self, report_id, name, options, pinned=False,
                  is_shared=False, notes=None):
        """Persist a new saved view. Returns the created record.

        :param report_id: id of the eh.account.dynamic.report record.
        :param name: human friendly label.
        :param options: full options dict (will be JSON serialised).
        :param pinned: surface in quick access list.
        :param is_shared: visible to all users of the company.
        :param notes: optional description.
        """
        if not name:
            raise UserError(_("Saved view name is required."))
        if not isinstance(options, dict):
            raise UserError(_("Saved view options must be a dict."))
        return self.create({
            'name': name,
            'report_id': report_id,
            'options_json': json.dumps(options, sort_keys=True, default=str),
            'pinned': bool(pinned),
            'is_shared': bool(is_shared),
            'notes': notes or False,
        })

    def load_view(self):
        """Return the saved options dict and bump usage telemetry."""
        self.ensure_one()
        self.sudo().write({
            'use_count': self.use_count + 1,
            'last_used_at': fields.Datetime.now(),
        })
        try:
            return json.loads(self.options_json)
        except ValueError:
            return {}

    @api.model
    def list_for_report(self, report_id, include_shared=True):
        """Return saved views visible to the current user for a report.

        Visibility rule: a view is visible if the current user owns it,
        or it is shared and belongs to the same company. Pinned views
        rank ahead of unpinned. Sequence then name break ties.
        """
        domain = [('report_id', '=', report_id)]
        if include_shared:
            domain += [
                '|',
                ('user_id', '=', self.env.user.id),
                '&',
                ('is_shared', '=', True),
                ('company_id', '=', self.env.company.id),
            ]
        else:
            domain += [('user_id', '=', self.env.user.id)]
        records = self.search(domain)
        return [
            {
                'id': r.id,
                'name': r.name,
                'pinned': r.pinned,
                'is_shared': r.is_shared,
                'is_owner': r.user_id.id == self.env.user.id,
                'use_count': r.use_count,
                'last_used_at': (
                    r.last_used_at.isoformat() if r.last_used_at else None
                ),
                'notes': r.notes or '',
            }
            for r in records
        ]

    def action_toggle_pinned(self):
        for rec in self:
            if rec.user_id.id != self.env.user.id and not rec.is_shared:
                continue
            rec.pinned = not rec.pinned
        return True
