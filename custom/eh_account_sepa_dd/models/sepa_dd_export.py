# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sepa.dd.export: audit row per generated PAIN.008 file.

Mirrors the design of eh.sepa.export from the credit transfer module
(file hash, state machine, supersedes prior generation for the same
batch). Kept as a separate model so direct debit and credit transfer
exports can be queried, audited, and reported on independently.
"""

import hashlib

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhSepaDdExport(models.Model):
    _name = 'eh.sepa.dd.export'
    _description = "SEPA Direct Debit export file"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'generated_at desc, id desc'
    _rec_name = 'message_id'

    # State (generated -> downloaded / superseded) moves only through the
    # record's own actions, which run under sudo. A plain user cannot RPC
    # a file to downloaded/superseded to hide it from the audit trail.
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
    )
    generated_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True,
    )
    generated_by_id = fields.Many2one(
        'res.users', required=True,
        default=lambda self: self.env.user,
    )
    transaction_count = fields.Integer(readonly=True)
    control_sum = fields.Float(digits=(16, 2), readonly=True)
    file_hash = fields.Char(size=64, readonly=True, index=True)
    sequence_type = fields.Selection(
        [
            ('FRST', "First"),
            ('RCUR', "Recurring"),
            ('FNAL', "Final"),
            ('OOFF', "One-off"),
        ],
        readonly=True,
        help=(
            "The sequence type used by every transaction in this file. "
            "Each PAIN.008 export groups one sequence type per file "
            "for clarity; mixed-sequence collections produce multiple "
            "files."
        ),
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
        for rec in records:
            siblings = self.search([
                ('batch_id', '=', rec.batch_id.id),
                ('id', '!=', rec.id),
                ('sequence_type', '=', rec.sequence_type),
                ('state', 'in', ('generated', 'downloaded')),
            ])
            # Server-initiated supersede of the prior generation; sudo so the
            # state guard passes.
            siblings.sudo().write({'state': 'superseded'})
        return records

    def action_download(self):
        self.ensure_one()
        if not self.attachment_id:
            raise UserError(_(
                "Export %s has no attached XML file.", self.message_id,
            ))
        self = self._eh_workflow_action()
        self.state = 'downloaded'
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % self.attachment_id.id,
            'target': 'download',
        }

    def action_void_for_recut(self):
        """Explicitly retire this export so its batch may be re-collected.

        action_export_sepa_dd refuses to generate a second PAIN.008 while a
        live (generated/downloaded) file exists for the batch, because a
        second file re-consumes every mandate (double-advancing the
        FRST -> RCUR counter) and cuts a duplicate collection the bank could
        debit twice. When a file was genuinely NOT submitted to the bank
        (rejected, lost, never transmitted) and the collection must be
        re-cut, a manager voids it here first: this flips the audit row to
        'superseded' and records who did it, so the trail shows the file was
        retired on purpose rather than silently overwritten. Voiding does NOT
        re-collect; the manager then triggers a fresh export deliberately.
        This is the escape hatch action_export_sepa_dd's refuse message
        promises ("Void the existing export first").
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can void a SEPA Direct "
                "Debit export.",
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
                "SEPA Direct Debit export voided for re-cut by %(user)s. "
                "This file must NOT be submitted to the bank; a fresh "
                "export supersedes it.",
                user=self.env.user.display_name,
            ))
        return True

    @staticmethod
    def compute_hash(content_bytes):
        return hashlib.sha256(content_bytes).hexdigest()
