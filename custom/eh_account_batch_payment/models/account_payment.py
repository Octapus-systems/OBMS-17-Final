# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.payment extension for batch membership.

A payment is a member of at most one batch. The pointer is nullable
so existing payments are unaffected; assignment happens via the batch
build wizard or by editing the payment directly. ondelete='set null'
keeps payments alive even if a draft batch is deleted.
"""

from odoo import fields, models


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    eh_batch_payment_id = fields.Many2one(
        'eh.batch.payment',
        string="Batch",
        ondelete='set null',
        index=True,
        copy=False,
        help=(
            "Optional. When set, this payment is part of a batch run "
            "and is posted together with the batch's other members."
        ),
    )
    eh_source_move_ids = fields.Many2many(
        'account.move',
        'eh_batch_payment_source_move_rel',
        'payment_id', 'move_id',
        string="Settled documents",
        copy=False,
        help=(
            "Invoices/bills this batch payment settles. Reconciled against "
            "the payment when the batch is posted, so the source documents "
            "actually move to paid instead of leaving the payment and the "
            "invoices as separate open items."
        ),
    )

    def _eh_reconcile_sources(self):
        """Reconcile each posted payment against its source documents.

        A batch payment that is merely posted leaves both the payment and its
        source invoices/bills open on the partner ledger: the whole point of
        the batch (clear these documents) never happens. After posting, match
        the payment's receivable/payable leg against the open receivable/
        payable lines of the documents it settles so those documents move to
        paid. Idempotent: already-reconciled lines are skipped.
        """
        for payment in self:
            moves = payment.eh_source_move_ids
            if not moves or not payment.move_id:
                continue
            pay_lines = payment.move_id.line_ids.filtered(
                lambda line_item: line_item.account_id.account_type in (
                    'asset_receivable', 'liability_payable')
                and line_item.account_id.reconcile and not line_item.reconciled)
            inv_lines = moves.line_ids.filtered(
                lambda line_item: line_item.account_id.account_type in (
                    'asset_receivable', 'liability_payable')
                and line_item.account_id.reconcile and not line_item.reconciled
                and line_item.amount_residual != 0.0)
            # Reconcile per shared account: the framework matches the single
            # payment leg across every invoice leg on the same AR/AP account.
            for account in pay_lines.mapped('account_id'):
                group = (pay_lines + inv_lines).filtered(
                    lambda line_item: line_item.account_id == account and not line_item.reconciled)
                if len(group) >= 2:
                    group.reconcile()
