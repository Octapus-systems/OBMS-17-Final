# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move extension: release commitments when a PO becomes a bill.

When a vendor bill is posted and any of its lines reference back to a
purchase order line (the standard purchase_line_id field on
account.move.line), the corresponding commitment on the
purchase.order is released. This shifts the dollar from `committed`
to `actual` on the budget line: the actual_amount compute picks up
the new posted JE, the committed_amount compute drops the released
row.

Partial billing: we release the commitment proportionally, measured
against the CUMULATIVE amount billed on the PO so far. A first bill
covering 60% of the PO releases 60% of the commitment; a second bill
covering the remaining 40% releases the residual so the encumbrance
lands at exactly zero. The release is computed against each
commitment row's original reserved amount (see original_amount on
eh.budget.commitment), never against an already-reduced current
amount, so sequential partial bills can never leave a phantom
residual reserved forever.
"""

from odoo import _, api, fields, models  # noqa: F401


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _post(self, soft=True):
        """Release matching commitments after the bill is posted.

        Runs AFTER super()._post so the line linkage is final
        (Odoo's PO matching may write purchase_line_id during the
        post pipeline). On a multi-bill PO, the first post releases
        the proportion covered by that bill; subsequent posts
        release further proportions.
        """
        result = super()._post(soft=soft)
        for move in self:
            if move.move_type not in ('in_invoice', 'in_refund'):
                continue
            move._eh_release_po_commitments()
        return result

    def _eh_release_po_commitments(self):
        self.ensure_one()
        Commitment = self.env['eh.budget.commitment'].sudo()
        # Collect every PO this bill touches. We release against the
        # PO's CUMULATIVE billed total (all posted vendor bills to
        # date, including this one), not just this bill in isolation.
        # Sequential partial bills therefore add up: the release is a
        # function of "how much of the PO has been billed so far",
        # measured against each commitment row's original reserved
        # amount, so the second partial bill releases the residual the
        # first left behind instead of scaling an already-reduced row.
        po_orders = self.env['purchase.order']
        for line in self.invoice_line_ids:
            po_line = getattr(line, 'purchase_line_id', False)
            if not po_line:
                continue
            po_orders |= po_line.order_id
        if not po_orders:
            return
        # Single search across every PO this bill touches; group in
        # Python by source_id so the per-PO loop below stays in
        # memory. We match reserved rows (still carrying encumbrance)
        # AND already-released rows, because the original committed
        # total must include rows a prior bill fully released.
        all_rows = Commitment.search([
            ('source_model', '=', 'purchase.order'),
            ('source_id', 'in', po_orders.ids),
            ('state', 'in', ('reserved', 'released')),
        ])
        rows_by_po = {}
        for row in all_rows:
            bucket = rows_by_po.get(row.source_id, Commitment)
            rows_by_po[row.source_id] = bucket | row
        for po in po_orders:
            rows = rows_by_po.get(po.id)
            if not rows:
                continue
            reserved_rows = rows.filtered(lambda r: r.state == 'reserved')
            if not reserved_rows:
                continue
            # Original committed total: the sum of what was first
            # reserved across ALL rows of this PO (reserved and already
            # released), giving a stable denominator independent of any
            # partial release already applied.
            original_total = sum(rows.mapped('original_amount'))
            if original_total <= 0:
                reserved_rows.action_release()
                continue
            # Cumulative billed to date across every posted vendor bill
            # / refund line that points back at this PO. Refunds carry a
            # negative subtotal, so a credit note correctly re-reserves.
            billed = self._eh_cumulative_billed_for_po(po)
            billed = max(0.0, billed)
            billed_fraction = min(1.0, billed / original_total)
            for row in reserved_rows:
                # Target remaining reserved for this row, computed from
                # its ORIGINAL amount so sequential bills converge to
                # zero exactly instead of chipping the already-reduced
                # current amount.
                original = row.original_amount or row.amount
                target_remaining = round(original * (1.0 - billed_fraction), 2)
                if target_remaining <= 0.0 or billed_fraction >= 0.999:
                    row.action_release()
                    continue
                if target_remaining >= round(row.amount, 2):
                    # Nothing further to release for this row yet.
                    continue
                released_amount = round(row.amount - target_remaining, 2)
                row.amount = target_remaining
                row.message_post(body=_(
                    "Partial release: %(amt).2f covered by bill "
                    "%(move)s; %(rem).2f remains reserved.",
                    amt=released_amount, move=self.display_name,
                    rem=target_remaining,
                ))

    def _eh_cumulative_billed_for_po(self, po):
        """Sum of posted vendor bill / refund subtotals against a PO.

        Uses purchase_line_id linkage on posted account.move.line
        rows. Vendor bills add to the billed total; vendor refunds
        (in_refund) subtract, so cancelling a bill via a credit note
        re-reserves the commitment on the next release pass.
        """
        Line = self.env['account.move.line'].sudo()
        move_lines = Line.search([
            ('purchase_line_id', 'in', po.order_line.ids),
            ('parent_state', '=', 'posted'),
            ('move_id.move_type', 'in', ('in_invoice', 'in_refund')),
        ])
        total = 0.0
        for ml in move_lines:
            sign = -1.0 if ml.move_id.move_type == 'in_refund' else 1.0
            total += sign * ml.price_subtotal
        return total
