# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sepa.export: audit row per generated PAIN.001 file.

The XML payload is stored as an attachment so re-downloading it does
not require regeneration, and so the auditor can inspect the exact
file that was uploaded to the bank. The SHA-256 fingerprint detects
accidental double exports of the same batch.

State machine: draft -> generated -> downloaded -> superseded. The
superseded state is set when a new export is generated for the same
batch (the prior file is kept, just flagged so the audit log shows
which file the bank actually received).
"""

import hashlib
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class EhSepaExport(models.Model):
    _name = 'eh.sepa.export'
    _description = "SEPA Credit Transfer export file"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'generated_at desc, id desc'
    _rec_name = 'message_id'

    # The state machine (generated -> downloaded -> superseded) may only
    # advance through this model's own actions, which run under sudo. A
    # plain user cannot RPC-write state to jump straight to 'downloaded'
    # (skipping action_download and its attachment check) or flip an audit
    # row's state to hide which file the bank received. 'state' defaults to
    # 'generated' so a non-su create is simply born in the initial state.
    _eh_guarded_fields = ('state',)

    batch_id = fields.Many2one(
        'eh.batch.payment', required=True,
        ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        related='batch_id.company_id', store=True, readonly=True,
    )
    message_id = fields.Char(
        required=True, copy=False, index=True,
        help="GrpHdr/MsgId stamped on the XML. Globally unique.",
    )
    generated_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True,
    )
    generated_by_id = fields.Many2one(
        'res.users', required=True,
        default=lambda self: self.env.user,
    )
    transaction_count = fields.Integer(readonly=True)
    control_sum = fields.Float(
        digits=(16, 2), readonly=True,
        help="Sum of every transaction amount in the file.",
    )
    file_hash = fields.Char(
        size=64, readonly=True, index=True,
        help="SHA-256 of the rendered XML bytes.",
    )
    state = fields.Selection(
        [
            ('generated', "Generated"),
            ('downloaded', "Downloaded"),
            ('superseded', "Superseded"),
        ],
        default='generated', required=True, tracking=True,
    )
    attachment_id = fields.Many2one(
        'ir.attachment', readonly=True, ondelete='set null',
    )

    notes = fields.Text()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # When a new export is generated for a batch that already has
        # a generated/downloaded export, mark the prior ones superseded
        # so the audit trail shows which file is the active one.
        for rec in records:
            siblings = self.search([
                ('batch_id', '=', rec.batch_id.id),
                ('id', '!=', rec.id),
                ('state', 'in', ('generated', 'downloaded')),
            ])
            # Server-initiated state transition: route through sudo so it
            # passes the workflow guard (env.su), regardless of who ran the
            # export that spawned this new row.
            siblings.sudo().write({'state': 'superseded'})
        return records

    def action_download(self):
        self.ensure_one()
        if not self.attachment_id:
            raise UserError(_(
                "Export %s has no attached XML file. The export was "
                "either created in error or the attachment was "
                "deleted. Generate a fresh export from the batch.",
                self.message_id,
            ))
        # Sanctioned state transition: run the guarded write as su.
        self = self._eh_workflow_action()
        self.state = 'downloaded'
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % self.attachment_id.id,
            'target': 'download',
        }

    def action_void_for_recut(self):
        """Explicitly retire this export so its batch may be re-cut.

        The batch's export action refuses to generate a second PAIN.001 while
        an active (generated/downloaded) file exists, because two files for one
        posted batch are two independent bank instructions (a double supplier
        payment). When a file was genuinely NOT submitted to the bank and must
        be regenerated, a manager voids it here first: this flips the audit row
        to 'superseded' and records who did it, so the trail shows the file was
        retired on purpose rather than silently overwritten. Voiding does NOT
        regenerate; the manager then triggers a fresh export deliberately.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can void a SEPA export.",
            ))
        for rec in self:
            if rec.state not in ('generated', 'downloaded'):
                raise UserError(_(
                    "Export %s is already retired and cannot be voided "
                    "again.",
                    rec.message_id,
                ))
        # Sanctioned state transition: run the guarded write as su, but post
        # the audit note as the real manager so the trail names who voided it.
        self._eh_workflow_action().write({'state': 'superseded'})
        for rec in self:
            rec.message_post(body=_(
                "SEPA export voided for re-cut by %(user)s. This file must "
                "NOT be submitted to the bank; a fresh export supersedes it.",
                user=self.env.user.display_name,
            ))
        return True

    @staticmethod
    def compute_hash(content_bytes):
        return hashlib.sha256(content_bytes).hexdigest()
