# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.asset.impairment: IAS 36 impairment charge or reversal on a fixed asset.

IAS 36 requires entities to assess at each reporting date whether an
asset's carrying amount exceeds its recoverable amount (the higher of
fair value less costs of disposal and value in use). When it does, the
entity must write the asset down to recoverable amount and recognise an
impairment loss in the P&L.

Reversals are permitted (and required) when conditions reverse, except
for goodwill. The reversal is capped at what the carrying amount would
have been had the original impairment not been recognised (after
continued depreciation).

This model holds one row per impairment event (charge or reversal) on
an asset. The asset's net_book_value compute subtracts the running
balance (charges minus reversals) from the depreciated cost so the
NBV displayed on the asset form, the balance sheet, and downstream
reports is consistently impairment-aware.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhAssetImpairment(models.Model):
    _name = 'eh.asset.impairment'
    _description = "Asset impairment / reversal"
    _order = 'asset_id, impairment_date, id'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']

    # State may only move draft -> posted -> cancelled through action_post /
    # action_cancel (which post/reverse the GL entry), never a direct write.
    _eh_guarded_fields = ('state',)

    asset_id = fields.Many2one(
        'eh.asset', required=True, ondelete='restrict', index=True,
        help="Asset whose carrying amount is being adjusted.",
    )
    cgu_id = fields.Many2one(
        'eh.asset.cgu', ondelete='set null', index=True, copy=False,
        help=(
            "Cash-generating unit whose IAS 36 impairment test derived "
            "and allocated this charge. Blank for a hand-keyed "
            "impairment entered directly on the asset."
        ),
    )
    company_id = fields.Many2one(
        related='asset_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='asset_id.currency_id', store=True, readonly=True,
    )

    impairment_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help=(
            "Date of the impairment event. Drives the journal entry "
            "date and the period in which the loss / reversal hits "
            "the P&L."
        ),
    )
    amount = fields.Monetary(
        required=True, currency_field='currency_id', tracking=True,
        help=(
            "Absolute (positive) amount of the impairment charge or "
            "reversal. Sign is implied by is_reversal."
        ),
    )
    recoverable_amount = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help=(
            "Recoverable amount supporting this impairment event (the "
            "higher of fair value less costs of disposal and value in "
            "use, IAS 36.18). Optional for hand-keyed events; when "
            "stated, posting stamps it onto the asset as its latest "
            "recoverable-amount measurement, which the revaluation "
            "wizard uses to cap upward revaluations."
        ),
    )
    is_reversal = fields.Boolean(
        default=False, tracking=True,
        help=(
            "When False, this row is an impairment charge that "
            "reduces the asset's NBV. When True, it is a reversal "
            "that restores carrying amount up to the cap permitted "
            "by IAS 36 (the depreciated cost the asset would have "
            "carried had the original impairment not been "
            "recognised). The asset's NBV compute subtracts charges "
            "and adds back reversals."
        ),
    )
    reason = fields.Text(
        required=True,
        help=(
            "Documented basis for the impairment: indicator of "
            "impairment, recoverable-amount calculation, valuation "
            "method, key assumptions. Lands in the audit trail and "
            "in the close run's working papers."
        ),
    )

    impairment_account_id = fields.Many2one(
        'account.account',
        string="Impairment Loss Account",
        help=(
            "P&L account for the impairment loss (typically an "
            "expense account named 'Impairment Loss' or similar). "
            "Falls back to the company impairment expense account "
            "when blank."
        ),
    )
    accumulated_account_id = fields.Many2one(
        'account.account',
        string="Accumulated Impairment Account",
        help=(
            "Balance-sheet contra account against which the "
            "impairment is booked. Falls back to the asset's "
            "accumulated_depreciation_account_id when blank."
        ),
    )
    journal_id = fields.Many2one(
        'account.journal',
        help=(
            "Journal used to post the impairment entry. Defaults to "
            "the asset's depreciation journal when blank."
        ),
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('posted', "Posted"),
            ('cancelled', "Cancelled"),
        ],
        default='draft', required=True, tracking=True,
    )
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        help="Journal entry posted for this impairment.",
    )

    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)

    _sql_constraints = [
        ('check_amount_positive', 'CHECK (amount > 0)', 'Impairment amount must be positive (sign is implied by is_reversal).'),  # noqa: E501
    ]

    @api.constrains('amount', 'is_reversal', 'asset_id')
    def _check_reversal_cap(self):
        """A reversal cannot push the running impairment balance below zero.

        Reversing more than has been charged would imply the asset
        gained carrying amount above what it had before any
        impairment, which IAS 36 forbids. We guard at write time so
        the violation is loud.
        """
        for rec in self:
            if not rec.is_reversal:
                continue
            # IAS 36.124: an impairment loss recognised for goodwill shall
            # not be reversed in a subsequent period. This is absolute; it
            # sits ahead of the cumulative-balance and ceiling tests below,
            # which apply only to reversible (non-goodwill) assets.
            if rec.asset_id.is_goodwill:
                raise ValidationError(_(
                    "Goodwill impairment cannot be reversed on %(asset)s. "
                    "IAS 36.124 prohibits reversing an impairment loss "
                    "recognised for goodwill in any later period.",
                    asset=rec.asset_id.display_name,
                ))
            other_charges = sum(
                rec.asset_id.impairment_ids
                .filtered(lambda i: not i.is_reversal and i.id != rec.id)
                .mapped('amount'),
            )
            other_reversals = sum(
                rec.asset_id.impairment_ids
                .filtered(lambda i: i.is_reversal and i.id != rec.id)
                .mapped('amount'),
            )
            running = other_charges - other_reversals
            if rec.amount > running:
                raise ValidationError(_(
                    "Reversal of %(amt).2f exceeds the available "
                    "impairment balance of %(bal).2f on %(asset)s. "
                    "IAS 36 caps reversals at the cumulative "
                    "impairment previously recognised; reduce the "
                    "amount or split into multiple events.",
                    amt=rec.amount, bal=running,
                    asset=rec.asset_id.display_name,
                ))
            # IAS 36.117: the post-reversal carrying amount must not exceed
            # the depreciated historical cost (the carrying amount the asset
            # would have had if no impairment had ever been recognised).
            # Because depreciation after an impairment is re-amortised on the
            # lower base, reversing the full charge later can lift the asset
            # above that ceiling even when the cumulative-charge check above
            # passes; this guard blocks it.
            asset = rec.asset_id
            posted_dep = sum(
                asset.depreciation_line_ids
                .filtered(lambda line_item: line_item.is_posted)
                .mapped('amount'),
            )
            nbv_before_reversal = (
                asset.acquisition_cost - posted_dep - running
            )
            ceiling = asset._ias36_depreciated_cost(
                as_of_date=rec.impairment_date,
            )
            max_reversal = asset.currency_id.round(
                ceiling - nbv_before_reversal,
            )
            if rec.amount > max_reversal:
                raise ValidationError(_(
                    "Reversal of %(amt).2f would lift the carrying amount "
                    "of %(asset)s above its depreciated historical cost of "
                    "%(ceiling).2f. IAS 36.117 caps a reversal at the "
                    "carrying amount that would have been determined, net "
                    "of depreciation, had no impairment been recognised. "
                    "The maximum reversal permitted here is %(max).2f.",
                    amt=rec.amount, asset=asset.display_name,
                    ceiling=ceiling, max=max(0.0, max_reversal),
                ))

    # ---- freeze (IAS 16.39 / SoD) ----

    # Measurement fields frozen once the impairment is posted. Re-basing the
    # amount or flipping is_reversal on a posted row would desync the asset's
    # net_book_value (which counts posted charges minus posted reversals) from
    # the ledger entry the row already produced. A correction must be a further
    # impairment event (a reversal row) or a cancel/reset, never an in-place
    # edit of the posted row.
    _FROZEN_AFTER_POST = ('amount', 'is_reversal')

    def write(self, vals):
        frozen = [f for f in self._FROZEN_AFTER_POST if f in vals]
        if frozen:
            posted = self.filtered(lambda r: r.state == 'posted')
            if posted:
                raise UserError(_(
                    "Measurement fields (%(fields)s) are frozen once the "
                    "impairment is posted; the amount must equal the journal "
                    "entry it produced. Cancel the impairment (which reverses "
                    "the entry) or record a further impairment / reversal to "
                    "correct it.",
                    fields=', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        posted = self.filtered(lambda r: r.state == 'posted')
        if posted:
            raise UserError(_(
                "A posted impairment cannot be deleted; its journal entry is "
                "part of the ledger. Cancel it (which reverses the entry) "
                "instead.",
            ))
        return super().unlink()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('state') == 'posted':
                raise UserError(_(
                    "An impairment cannot be created directly in the posted "
                    "state; that would move the asset's carrying amount "
                    "without producing a balanced journal entry. Create it in "
                    "draft and post it through the Post action (a "
                    "segregation-of-duties control point).",
                ))
        return super().create(vals_list)

    # ---- actions ----

    def action_post(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can post an impairment charge "
                "or reversal to the general ledger. This posting is a "
                "segregation-of-duties control point.",
            ))
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_(
                    "Only draft impairment events can be posted "
                    "(state is %s).",
                ) % rec.state)
            asset = rec.asset_id
            journal = rec.journal_id or asset.journal_id
            if not journal:
                raise UserError(_(
                    "No journal configured for impairment on %s.",
                ) % asset.display_name)
            expense_acc = (
                rec.impairment_account_id
                or asset.disposal_loss_account_id
            )
            contra_acc = (
                rec.accumulated_account_id
                or asset.accumulated_depreciation_account_id
            )
            if not expense_acc or not contra_acc:
                raise UserError(_(
                    "Configure the impairment loss and accumulated "
                    "impairment accounts on the impairment record or "
                    "fall back to the asset's accumulated depreciation "
                    "and disposal loss accounts before posting.",
                ))
            if rec.is_reversal:
                # Reversal: credit P&L (recover loss); debit contra.
                debit_acc, credit_acc = contra_acc, expense_acc
            else:
                # Charge: debit P&L (expense); credit contra.
                debit_acc, credit_acc = expense_acc, contra_acc
            label = _("Impairment %s on %s") % (
                _("reversal") if rec.is_reversal else _("charge"),
                asset.display_name,
            )
            move_vals = {
                'move_type': 'entry',
                'eh_sealed': True,
                'journal_id': journal.id,
                'date': rec.impairment_date,
                'ref': "%s / %s" % (asset.name or '', rec.id),
                'line_ids': [
                    (0, 0, {
                        'name': label,
                        'account_id': debit_acc.id,
                        'debit': rec.amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': label,
                        'account_id': credit_acc.id,
                        'debit': 0.0,
                        'credit': rec.amount,
                    }),
                ],
            }
            move = self.env['account.move'].create(move_vals)
            move.action_post()
            rec.write({
                'state': 'posted',
                'move_id': move.id,
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
            })
            # IAS 36.63: after an impairment (or its reversal) is
            # recognised, re-amortise the revised carrying amount, less
            # residual, over the remaining useful life so future
            # depreciation does not keep running off the pre-impairment
            # base (which would over-depreciate the asset).
            asset._eh_rebuild_after_impairment()
            # Latest recoverable-amount measurement: when the event
            # states its recoverable amount, stamp it onto the asset so
            # the revaluation wizard can cap uplifts against it.
            asset_vals = {}
            if rec.recoverable_amount:
                asset_vals.update({
                    'recoverable_amount_latest': rec.recoverable_amount,
                    'recoverable_amount_date': rec.impairment_date,
                })
            # A posted impairment event is test evidence for the IAS 36
            # annual-test mandate; clear the overdue flag immediately
            # (the cron re-evaluates on its next pass).
            if asset.annual_test_overdue:
                asset_vals['annual_test_overdue'] = False
            if asset_vals:
                asset.write(asset_vals)
        return True

    def action_cancel(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can cancel an impairment charge "
                "or reversal, because cancelling reverses its journal entry "
                "and moves the asset's carrying amount. This is a "
                "segregation-of-duties control point.",
            ))
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'cancelled':
                continue
            if rec.state == 'posted' and rec.move_id:
                if rec.move_id.state == 'posted':
                    rec.move_id.sudo().with_context(eh_allow_unpost=True).button_draft()
                rec.move_id.button_cancel()
            rec.state = 'cancelled'
        return True
