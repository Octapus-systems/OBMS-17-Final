# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Single line of an asset's depreciation schedule.

Each line is a real record so it can be queried, exported and audited.
A line stores the planned amount; on action_post it produces a balanced
account.move (debit Depreciation Expense, credit Accumulated
Depreciation) and stamps the user/timestamp.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhAssetDepreciationLine(models.Model):
    _name = 'eh.asset.depreciation.line'
    _inherit = ['eh.workflow.guard']
    _description = "Asset Depreciation Schedule Line"
    _order = 'asset_id, sequence, depreciation_date'

    # A posted line's identity/posting fields may only change through the
    # record's own action (which runs as su). A plain RPC write cannot flip
    # is_posted True->False to re-arm the poster (which would book a second
    # depreciation move), nor repoint move_id at another entry. readonly on
    # the field blocks only the web client, never an ORM/RPC write.
    _eh_guarded_fields = ('is_posted', 'move_id')

    asset_id = fields.Many2one(
        'eh.asset', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(required=True, default=10)
    depreciation_date = fields.Date(required=True)
    amount = fields.Monetary(required=True)
    accumulated = fields.Monetary(
        help="Accumulated depreciation up to and including this line.",
    )
    remaining_value = fields.Monetary(
        help="Net book value after this line is posted.",
    )

    is_posted = fields.Boolean(default=False, copy=False, readonly=True)
    posted_at = fields.Datetime(readonly=True, copy=False)
    posted_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
    )

    currency_id = fields.Many2one(
        related='asset_id.currency_id', store=True, readonly=True,
    )
    company_id = fields.Many2one(
        related='asset_id.company_id', store=True, readonly=True,
    )

    _sql_constraints = [
        ('uniq_asset_sequence', 'unique(asset_id, sequence)', 'Sequence must be unique within an asset.'),
        ('check_amount_non_negative', 'CHECK (amount >= 0)', 'Depreciation amount cannot be negative.'),
    ]

    @api.constrains('asset_id')
    def _check_ias38_indefinite_life(self):
        """IAS 38.107: an indefinite-life intangible is not amortised.

        Guard on the child line (not only the parent form) so every
        creation path - manual schedule keying, imports, code - is
        blocked, mirroring the parent-side constraint on eh.asset.
        """
        for line in self:
            asset = line.asset_id
            if asset.asset_class == 'intangible' and asset.is_indefinite_life:
                raise ValidationError(_(
                    "%(asset)s has an indefinite useful life; IAS 38.107 "
                    "prohibits amortisation, so no depreciation line can "
                    "be recorded on it. It is subject to the annual "
                    "impairment test instead (IAS 36.10).",
                    asset=asset.display_name,
                ))

    # Schedule measurement fields frozen once the line has produced its
    # journal entry. Re-basing amount or date on a posted line would move the
    # charge away from the ledger it already booked; a correction must be a
    # further posting, not an in-place edit.
    _FROZEN_AFTER_POST = ('amount', 'depreciation_date', 'accumulated',
                          'remaining_value')

    def write(self, vals):
        frozen = [f for f in self._FROZEN_AFTER_POST if f in vals]
        if frozen:
            posted = self.filtered(lambda line: line.is_posted)
            if posted:
                raise UserError(_(
                    "Schedule fields (%(fields)s) are frozen once the "
                    "depreciation line is posted; the charge must equal the "
                    "journal entry it produced. Reverse the entry to correct "
                    "it.",
                    fields=', '.join(frozen)))
        return super().write(vals)

    def _eh_lock_for_post(self):
        """Serialise concurrent posters on the schedule lines.

        The daily cron and the manual 'Post Due Lines' button (and a plain
        double-click / browser retry) both read-then-post the same unposted
        line. Under READ COMMITTED both would read is_posted=False and each
        create a posted move, silently doubling the GL charge. Take a row
        lock and re-read is_posted from the database so the loser blocks on
        the winner and then sees the committed True and skips.
        """
        if not self.ids:
            return
        self.env.cr.execute(
            'SELECT id FROM eh_asset_depreciation_line WHERE id IN %s '
            'FOR UPDATE',
            (tuple(self.ids),),
        )
        self.invalidate_recordset(['is_posted', 'move_id'])

    def action_post(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can post depreciation to the "
                "general ledger. This posting is a segregation-of-duties "
                "control point.",
            ))
        self._eh_lock_for_post()
        for line in self:
            # Idempotent: a line that already carries a live posted move must
            # never be re-booked (that duplicates the period's depreciation
            # and orphans the first entry). Skip silently so a concurrent
            # cron/manual race, a double-submit, or a re-run is a no-op.
            if line.is_posted or (
                line.move_id and line.move_id.state == 'posted'
            ):
                continue
            asset = line.asset_id
            asset._validate_posting_setup()
            move_lines = line._build_move_lines_for_type(asset)
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'eh_sealed': True,
                'date': line.depreciation_date,
                'journal_id': asset.journal_id.id,
                'ref': line._move_ref_for_type(asset),
                'line_ids': move_lines,
            })
            move.action_post()
            # is_posted / move_id are guarded; stamp them through the
            # sanctioned action path (runs as su) so a real, non-superuser
            # manager can post while a direct RPC write stays blocked.
            line._eh_workflow_write({
                'is_posted': True,
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'move_id': move.id,
            })

    def _build_move_lines_for_type(self, asset):
        """Return the (debit, credit) leg pair appropriate for the parent
        asset's deferred_type.

        * asset (default fixed asset): DR Depreciation Expense,
          CR Accumulated Depreciation. Net book value declines.
        * deferred_revenue: the asset's `asset_account_id` is the
          deferred revenue liability that pre-paid customers credit;
          recognition reduces the liability and credits the
          `depreciation_account_id` (used here as the revenue
          recognition account). Net of the period: DR Deferred Revenue,
          CR Revenue.
        * deferred_expense: the asset's `asset_account_id` is the
          prepaid expense asset; recognition reduces it and debits the
          `depreciation_account_id` (used here as the expense account).
          Net of the period: DR Expense, CR Prepaid Asset.
        """
        self.ensure_one()
        amount = self.amount
        if asset.deferred_type == 'deferred_revenue':
            return [
                (0, 0, {
                    'name': _("Deferred revenue release %s", asset.display_name),
                    'account_id': asset.asset_account_id.id,
                    'debit': amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': _("Revenue recognition %s", asset.display_name),
                    'account_id': asset.depreciation_account_id.id,
                    'debit': 0.0,
                    'credit': amount,
                }),
            ]
        if asset.deferred_type == 'deferred_expense':
            return [
                (0, 0, {
                    'name': _("Expense recognition %s", asset.display_name),
                    'account_id': asset.depreciation_account_id.id,
                    'debit': amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': _("Prepaid asset release %s", asset.display_name),
                    'account_id': asset.asset_account_id.id,
                    'debit': 0.0,
                    'credit': amount,
                }),
            ]
        return [
            (0, 0, {
                'name': _("Depreciation %s", asset.display_name),
                'account_id': asset.depreciation_account_id.id,
                'debit': amount,
                'credit': 0.0,
            }),
            (0, 0, {
                'name': _("Accumulated depreciation %s", asset.display_name),
                'account_id': asset.accumulated_depreciation_account_id.id,
                'debit': 0.0,
                'credit': amount,
            }),
        ]

    def _move_ref_for_type(self, asset):
        if asset.deferred_type == 'deferred_revenue':
            return _("Deferred revenue release %(asset)s #%(seq)s",
                     asset=asset.display_name, seq=self.sequence)
        if asset.deferred_type == 'deferred_expense':
            return _("Deferred expense release %(asset)s #%(seq)s",
                     asset=asset.display_name, seq=self.sequence)
        return _("Depreciation %(asset)s #%(seq)s",
                 asset=asset.display_name, seq=self.sequence)

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No journal entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }
