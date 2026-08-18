# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Wizard: build a batch payment from selected source invoices.

The user picks the source moves (open customer invoices for an inbound
batch, open vendor bills for an outbound batch) and the wizard creates
one draft account.payment per source, attaching them to the batch.

Per-partner aggregation: when enabled, multiple sources for the same
partner collapse into a single payment line, with the memo carrying
the comma separated source references. Dedicated accounting
documentation lives on the resulting payment record.
"""

from collections import defaultdict

from odoo import _, api, fields, models  # noqa: F401
from odoo.exceptions import UserError


class EhBatchPaymentBuildWizard(models.TransientModel):
    _name = 'eh.batch.payment.build.wizard'
    _description = "Wizard: build a batch payment from selected invoices"

    batch_id = fields.Many2one(
        'eh.batch.payment',
        required=True,
        ondelete='cascade',
    )
    move_ids = fields.Many2many(
        'account.move',
        domain=(
            "[('state', '=', 'posted'),"
            " ('payment_state', 'in', ['not_paid', 'partial']),"
            " ('move_type', 'in', "
            "  (['out_invoice', 'out_refund'] if batch_type == 'inbound'"
            "   else ['in_invoice', 'in_refund']))]"
        ),
        string="Source invoices / bills",
    )
    aggregate_per_partner = fields.Boolean(
        default=True,
        help=(
            "When enabled, multiple selected invoices for the same "
            "partner produce a single payment summing the residuals. "
            "Otherwise each invoice gets its own payment."
        ),
    )
    batch_type = fields.Selection(
        related='batch_id.batch_type',
        readonly=True,
    )
    journal_id = fields.Many2one(
        related='batch_id.journal_id',
        readonly=True,
    )
    payment_date = fields.Date(
        related='batch_id.payment_date',
        readonly=True,
    )
    company_id = fields.Many2one(
        related='batch_id.company_id',
        readonly=True,
    )

    def action_build(self):
        self.ensure_one()
        if not self.move_ids:
            raise UserError(_("Pick at least one invoice or bill."))
        if any(m.company_id != self.batch_id.company_id for m in self.move_ids):
            raise UserError(_(
                "Every selected document must belong to the batch's "
                "company. Mixed company batches are not supported.",
            ))
        # Reject documents that do not match the batch direction. The form
        # domain already filters by move_type, but move_ids can be set by
        # RPC bypassing the domain, and _eh_directional_residual relies on
        # the move_type being one of the four expected invoice/refund types.
        expected_types = (
            ['out_invoice', 'out_refund']
            if self.batch_id.batch_type == 'inbound'
            else ['in_invoice', 'in_refund']
        )
        wrong_direction = self.move_ids.filtered(
            lambda m: m.move_type not in expected_types)
        if wrong_direction:
            raise UserError(_(
                "These documents do not match the batch direction: %s. An "
                "inbound batch collects customer invoices/credit notes; an "
                "outbound batch pays vendor bills/refunds.",
                ", ".join(wrong_direction.mapped('display_name')),
            ))
        # Hard block against double disbursement: a source document still
        # claimed by a LIVE, unposted (draft) payment in another batch must
        # not be built into a second batch, or the vendor/customer is paid/
        # collected twice for one document. A posted or reversed prior payment
        # no longer holds a live claim (see _assert_not_double_batched), so a
        # bounced or partially-settled source stays re-collectable. The soft
        # chatter check below only catches sources ALREADY reconciled by a
        # posted batch; it cannot see a still-draft sibling batch whose payment
        # is unposted and unreconciled.
        self._assert_not_double_batched(self.move_ids)
        # Walk source AML -> partial reconciles -> counter AML -> payment
        # -> batch. Both directions (debit and credit) checked because
        # eh_batch_payment_id lives on account.payment, not on
        # account.move. The previous chain crashed with AttributeError.
        def _has_existing_batch(move):  # noqa: E306
            for line in move.line_ids:
                for partial in (line.matched_debit_ids
                                + line.matched_credit_ids):
                    counter = (
                        partial.debit_move_id
                        if partial.debit_move_id != line
                        else partial.credit_move_id
                    )
                    if counter.payment_id and counter.payment_id.eh_batch_payment_id:
                        return True
            return False

        already_batched = self.move_ids.filtered(_has_existing_batch)
        # Soft check; we do not block, but we surface in chatter.
        if self.aggregate_per_partner:
            payments = self._build_aggregated_payments()
        else:
            payments = self._build_per_invoice_payments()  # noqa: F841
        if already_batched:
            self.batch_id.message_post(body=_(
                "%d source document(s) were already partially settled "
                "by a previous batch. Verify amounts before posting.",
                len(already_batched),
            ))
        # Return navigation back to the batch form so the user lands on
        # the populated header.
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.batch.payment',
            'res_id': self.batch_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'main',
        }

    def _build_aggregated_payments(self):
        self.ensure_one()
        Payment = self.env['account.payment']
        groups = defaultdict(lambda: {'amount': 0.0, 'refs': [], 'moves': []})
        for move in self.move_ids:
            partner = move.partner_id
            if not partner:
                raise UserError(_(
                    "Document %s has no partner; cannot batch.",
                    move.display_name,
                ))
            key = partner.id
            groups[key]['partner_id'] = partner.id
            # Sign the residual to the batch direction so a credit note/refund
            # NETS against the partner's invoices instead of grossing the
            # payment up. abs()-summing here over-collects from customers and
            # over-pays vendors by twice the credit-note value.
            groups[key]['amount'] += self._eh_directional_residual(move)
            groups[key]['refs'].append(move.name or move.ref or '')
            groups[key]['moves'].append(move.id)

        created = self.env['account.payment']
        skipped = []
        for partner_id, data in groups.items():
            net = data['amount']
            if net <= 0:
                # The partner's selected credit notes/refunds meet or exceed
                # their invoices: nothing is owed in the batch direction, and
                # a payment here would move cash the wrong way. Skip.
                skipped.append(partner_id)
                continue
            payment_vals = self._payment_vals_common()
            payment_vals.update({
                'partner_id': partner_id,
                'amount': net,
                'ref': ', '.join(filter(None, data['refs']))[:200],
                # Remember which documents this payment settles so the batch
                # can reconcile them against the payment when it posts. The
                # credit notes are kept in the set so reconciliation nets them.
                'eh_source_move_ids': [(6, 0, data['moves'])],
            })
            created |= Payment.create(payment_vals)
        if skipped:
            self.batch_id.message_post(body=_(
                "%d partner(s) skipped: their selected credit notes/refunds "
                "net to zero or a reverse-direction balance, so no batch "
                "payment was created for them.",
                len(skipped),
            ))
        return created

    def _build_per_invoice_payments(self):
        self.ensure_one()
        Payment = self.env['account.payment']
        created = self.env['account.payment']
        skipped = []
        for move in self.move_ids:
            if not move.partner_id:
                raise UserError(_(
                    "Document %s has no partner; cannot batch.",
                    move.display_name,
                ))
            amount = self._eh_directional_residual(move)
            if amount <= 0:
                # A standalone credit note/refund would collect/pay in the
                # wrong direction (abs() of its residual moves cash the wrong
                # way). It can only net against an invoice, which per-document
                # mode cannot express, so skip it here and surface it.
                skipped.append(move.display_name)
                continue
            payment_vals = self._payment_vals_common()
            payment_vals.update({
                'partner_id': move.partner_id.id,
                'amount': amount,
                'ref': move.name or move.ref or '',
                # Remember the source document so the batch reconciles it
                # against this payment when it posts.
                'eh_source_move_ids': [(6, 0, move.ids)],
            })
            created |= Payment.create(payment_vals)
        if skipped:
            self.batch_id.message_post(body=_(
                "%d credit note(s)/refund(s) skipped in per-document mode: a "
                "standalone refund cannot be paid in the batch direction. "
                "Use per-partner aggregation to net them against invoices. "
                "Skipped: %s",
                len(skipped),
                ", ".join(skipped),
            ))
        return created

    def _eh_directional_residual(self, move):
        """Residual signed to the batch direction.

        An invoice/bill contributes the positive amount still owed in the
        batch direction; a credit note/refund contributes NEGATIVELY so it
        nets the partner's total down instead of grossing the payment up.
        ``move.amount_residual`` is the positive magnitude still outstanding
        on the document, so the sign is applied here from the move type.
        Summing ``abs()`` (the previous behaviour) over-collects from a
        customer and over-pays a vendor by twice each credit-note value.
        """
        residual = move.amount_residual
        if move.move_type in ('out_refund', 'in_refund'):
            return -residual
        return residual

    def _assert_not_double_batched(self, moves):
        """Refuse to build a source document that a LIVE, not-yet-posted batch
        payment already claims, so a partner is never paid or collected twice
        for one document.

        A claim only counts while the claiming payment is still ``draft`` -
        the true double-pay window, because a draft payment has not moved cash
        yet and WILL settle its source when the batch posts. Once that payment
        leaves draft it no longer strands the source:

        * a posted payment has already executed its settlement, and the
          framework's reconciliation is self-limiting, so any residual still
          owed (a partial settlement) is a genuine remainder a new batch may
          legitimately re-collect;
        * a reversed / cancelled payment (an NSF bounce reversal, a voided
          run) settles nothing, yet its ``account.payment`` record and its
          ``eh_source_move_ids`` claim survive - so keying purely on batch
          membership (the previous behaviour) permanently stranded a bounced
          collection, since a posted batch cannot be cancelled to release it.

        ``eh_source_move_ids`` records what each batch payment settles, so a
        still-draft sibling batch is caught here before its payment posts or
        reconciles - which the soft chatter check (which only sees sources
        already reconciled by a POSTED batch) cannot do. The ``state ==
        'draft'`` leaf is cross-version safe: 'draft' is the initial payment
        state on Odoo 16-19 (delegated from the move on 16/17, native on
        18/19)."""
        self.ensure_one()
        claimed = self.env['account.payment'].search([
            ('eh_source_move_ids', 'in', moves.ids),
            ('eh_batch_payment_id', '!=', False),
            ('eh_batch_payment_id.state', '!=', 'cancelled'),
            # Only a still-draft (live, unposted) payment holds a claim that
            # would double-pay. Excluding non-draft payments releases a
            # bounced/reversed or partially-settled source for re-collection
            # without ever letting two live payments settle one document.
            ('state', '=', 'draft'),
        ]).filtered(lambda p: p.eh_batch_payment_id != self.batch_id)
        if not claimed:
            return
        dupes = claimed.mapped('eh_source_move_ids').filtered(
            lambda m: m in moves)
        raise UserError(_(
            "These documents are already in another, still-unposted batch "
            "payment and would be paid or collected twice: %s. Remove them "
            "from that batch, or cancel it, before building this one.",
            ", ".join(dupes.mapped('display_name')),
        ))

    def _payment_vals_common(self):
        self.ensure_one()
        if self.batch_id.batch_type == 'inbound':
            payment_type = 'inbound'
            partner_type = 'customer'
        else:
            payment_type = 'outbound'
            partner_type = 'supplier'
        vals = {
            'eh_batch_payment_id': self.batch_id.id,
            'journal_id': self.batch_id.journal_id.id,
            'payment_type': payment_type,
            'partner_type': partner_type,
            'date': self.batch_id.payment_date,
            'company_id': self.batch_id.company_id.id,
        }
        if self.batch_id.payment_method_line_id:
            vals['payment_method_line_id'] = (
                self.batch_id.payment_method_line_id.id
            )
        return vals
