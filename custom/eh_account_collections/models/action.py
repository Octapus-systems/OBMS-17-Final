# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.collections.action: log of every contact action against a case.

One row per action (call, email, SMS, meeting, free note). Each row
captures the user who acted, the timestamp, a short summary, optional
detail, and a contact_made flag for whether the partner was actually
reached. When the action results in a promise to pay, promise_amount and
promise_date are recorded so the case's promise tracking stays accurate.

Why a separate model and not chatter on the case:

* Queryable. Reports like "average actions before resolution" or
  "promises broken in the last 90 days" need structured rows.
* Per action user attribution. Chatter messages can be edited or
  deleted; audit logs cannot.
* Action types feed the collections KPI dashboard without parsing
  free text.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhCollectionsAction(models.Model):
    _name = 'eh.collections.action'
    _description = "Collections case action log"
    _order = 'action_date desc, id desc'

    case_id = fields.Many2one(
        'eh.collections.case',
        required=True,
        ondelete='restrict',
        index=True,
    )

    action_type = fields.Selection(
        [
            ('call', "Call"),
            ('email', "Email"),
            ('sms', "SMS"),
            ('meeting', "Meeting"),
            ('letter', "Letter"),
            ('note', "Note"),
        ],
        required=True,
        default='note',
    )
    action_date = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    user_id = fields.Many2one(
        'res.users',
        required=True,
        default=lambda self: self.env.user,
        index=True,
    )

    summary = fields.Char(required=True)
    detail = fields.Html()

    contact_made = fields.Boolean(
        default=False,
        help=(
            "Did we actually reach the partner? Voicemails, undelivered "
            "emails, and unanswered calls should be False."
        ),
    )

    promise_amount = fields.Monetary(
        currency_field='currency_id',
        help=(
            "When this action results in a promise to pay, record the "
            "amount here. The case's promise tracking pulls from the most "
            "recent action that has a non zero promise_amount."
        ),
    )
    promise_date = fields.Date(
        help="Date the partner promised to pay by.",
    )
    currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_currency',
        store=False,
    )

    @api.depends('case_id.currency_id')
    def _compute_currency(self):
        for rec in self:
            rec.currency_id = (
                rec.case_id.currency_id
                if rec.case_id else self.env.company.currency_id
            )

    # ----------- audit-log immutability -----------
    def write(self, vals):
        if not self.env.context.get('eh_internal_audit_write'):
            raise UserError(_(
                "Collections action log rows are append-only."
            ))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_immutable_audit(self):
        if not self.env.context.get('eh_internal_audit_unlink'):
            raise UserError(_(
                "Collections action log rows cannot be deleted."
            ))
