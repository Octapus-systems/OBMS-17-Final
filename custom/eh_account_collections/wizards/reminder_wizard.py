# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Manual reminder wizard.

Lets a collector compose and send an ad-hoc reminder over email and/or
SMS to the case partner, with an editable subject and body, outside the
automated follow-up ladder. Every send is written to the case action
log so the manual touch sits alongside the automated ones.
"""

from odoo import _, fields, models
from odoo.exceptions import UserError


class EhCollectionsReminderWizard(models.TransientModel):
    _name = 'eh.collections.reminder.wizard'
    _description = "Collections manual reminder"

    case_id = fields.Many2one(
        'eh.collections.case', required=True, ondelete='cascade',
    )
    partner_id = fields.Many2one('res.partner', required=True)
    channel = fields.Selection(
        [('email', "Email"), ('sms', "SMS"), ('both', "Email and SMS")],
        default='email', required=True,
    )
    subject = fields.Char()
    body = fields.Text(required=True)

    def action_send(self):
        self.ensure_one()
        case = self.case_id
        sent = []
        if self.channel in ('email', 'both'):
            case.message_post(
                body=self.body,
                subject=self.subject or _("Payment reminder"),
                partner_ids=self.partner_id.ids,
                message_type='comment',
                subtype_xmlid='mail.mt_comment',
            )
            sent.append('email')
        if self.channel in ('sms', 'both'):
            partner = self.partner_id
            number = partner.phone or (
                partner['mobile'] if 'mobile' in partner._fields else False)
            if not number:
                raise UserError(_(
                    "%s has no mobile or phone number for SMS.",
                    self.partner_id.display_name))
            self.env['sms.sms'].sudo().create({
                'partner_id': self.partner_id.id,
                'number': number,
                'body': self.body,
            })
            sent.append('sms')
        case.action_log_action(
            action_type='email' if 'email' in sent else 'sms',
            summary=_("Manual reminder (%s)") % ', '.join(sent),
            contact_made=True,
        )
        return {'type': 'ir.actions.act_window_close'}
