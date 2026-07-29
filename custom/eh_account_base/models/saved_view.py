# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Saved filter views for dynamic reports.

A user picks dimensions and date settings on the OWL viewer, names them,
and reuses the named bundle on the next render. Visibility is per user
by default; a sharing toggle promotes the view to company-wide so a
finance team can publish a "month-end pack" to every member.

The options field stores the OWL viewer's `state.options` block as JSON.
The OWL viewer reads it back verbatim into state on load. Schema drift
between viewer revisions does not corrupt the stored bundle: unknown
keys are ignored on load and missing keys fall back to defaults.
"""

import json

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class EhReportSavedView(models.Model):
    _name = 'eh.account.report.saved_view'
    _description = "Saved filter view for a dynamic report"
    _order = 'shared desc, name'
    _rec_name = 'name'

    name = fields.Char(required=True)
    report_code = fields.Char(required=True, index=True)
    user_id = fields.Many2one(
        'res.users',
        default=lambda self: self.env.user,
        ondelete='cascade',
        index=True,
        help=(
            "Owner of the view. The owner can edit and delete; other users "
            "can read it only when shared is True."
        ),
    )
    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        index=True,
    )
    options_json = fields.Text(
        required=True,
        help="JSON-encoded options dict as captured from the OWL viewer.",
    )
    shared = fields.Boolean(
        default=False,
        help=(
            "When True, the view is visible to every user in the company "
            "with access to the report. The owner remains the only user "
            "who can edit or delete."
        ),
    )
    notes = fields.Char()

    _sql_constraints = [
        ('unique_per_user_code', 'unique(user_id, report_code, name)', 'A user cannot save two views with the same name on the same report.'),
    ]

    @api.constrains('options_json')
    def _check_options_json(self):
        for rec in self:
            if not rec.options_json:
                raise ValidationError(_("options_json must not be empty."))
            try:
                payload = json.loads(rec.options_json)
            except ValueError as exc:
                raise ValidationError(_(
                    "options_json must be valid JSON (got %s).",
                ) % exc)
            if not isinstance(payload, dict):
                raise ValidationError(_("options_json must encode a dict."))

    @api.model
    def list_for(self, report_code):
        """Return saved views the current user can see for the report.

        Includes the user's own views plus any view shared in the active
        company. Result is a list of dicts keyed by id, name, shared,
        owned (whether the active user is the owner).
        """
        domain = [
            ('report_code', '=', report_code),
            '|',
            ('user_id', '=', self.env.user.id),
            '&',
            ('shared', '=', True),
            ('company_id', 'in', list(
                self.env.context.get(
                    'allowed_company_ids', [self.env.company.id],
                ),
            )),
        ]
        records = self.search(domain, order='shared desc, name')
        return [{
            'id': r.id,
            'name': r.name,
            'shared': r.shared,
            'owned': r.user_id.id == self.env.user.id,
            'notes': r.notes or '',
        } for r in records]

    @api.model
    def save_view(self, name, report_code, options, shared=False, notes=None):
        """Upsert a saved view. The (user, report_code, name) tuple is
        unique, so saving the same name overwrites the prior bundle.

        Returns the persisted record id.
        """
        existing = self.search([
            ('user_id', '=', self.env.user.id),
            ('report_code', '=', report_code),
            ('name', '=', name),
        ], limit=1)
        vals = {
            'name': name,
            'report_code': report_code,
            'options_json': json.dumps(options or {}, sort_keys=True),
            'shared': bool(shared),
            'notes': notes or False,
        }
        if existing:
            existing.write(vals)
            return existing.id
        return self.create(vals).id

    def load(self):
        """Return the parsed options dict for the OWL viewer."""
        self.ensure_one()
        try:
            return json.loads(self.options_json)
        except ValueError:
            return {}

    @api.ondelete(at_uninstall=False)
    def _unlink_owner_only(self):
        # Owner-only delete. The view is visible to others when shared,
        # but only the owner can remove it.
        for rec in self:
            if rec.user_id and rec.user_id.id != self.env.user.id:
                raise ValidationError(_(
                    "Only the owner can delete a saved view (%s).",
                ) % rec.name)
