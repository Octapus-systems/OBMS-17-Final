# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
purchase.order extension: encumbrance lifecycle.

When a purchase order is confirmed, this module looks up matching
budget lines (by account, date, optional analytic) and creates a
commitment per PO line. The commitments deduct availability until
the PO is billed (commitment released, actual takes over) or
cancelled (commitment released, no actual).

The match-then-create logic intentionally tolerates missing budget
lines: a PO for an account that nobody has budgeted simply does not
create commitments. The overrun policy on the budget governs what
happens when a matching line is found and would go negative.

The button_confirm override consults the budget's overrun_policy:

* off: no commitments, no gate. The historical behaviour for sites
  that have not adopted encumbrance accounting.
* warn: commitments are created, the gate posts a chatter warning
  on the budget when availability goes negative, but does not
  block the confirm.
* block: commitments are pre-checked; if any matched budget line
  would go negative after this PO confirms, the confirm raises a
  UserError naming the line.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    eh_budget_commitment_ids = fields.One2many(
        'eh.budget.commitment',
        compute='_compute_eh_commitments',
        string="Budget commitments",
        help=(
            "Commitments this purchase order has created against "
            "matching budget lines. One per (PO line, budget line) "
            "pair where a budget line was resolved at confirm time."
        ),
    )
    eh_budget_commitment_count = fields.Integer(
        compute='_compute_eh_commitments',
        help="Quick count for the smart button.",
    )

    @api.depends('order_line')
    def _compute_eh_commitments(self):
        Commitment = self.env['eh.budget.commitment']
        for po in self:
            rows = Commitment.search([
                ('source_model', '=', 'purchase.order'),
                ('source_id', '=', po.id),
            ])
            po.eh_budget_commitment_ids = rows
            po.eh_budget_commitment_count = len(rows)

    # ---- confirm + cancel hooks ----

    def button_confirm(self):
        """Reserve commitments at confirm; honour the overrun policy.

        Commitment creation runs BEFORE super() so the gate can
        refuse to confirm when the policy is 'block'. The check is
        per matched budget line: a PO line that lands on a non-
        budgeted account is silently allowed.
        """
        # Pre-check the block policy. Computed without writing rows
        # so a refused confirm leaves no draft commitments behind.
        for po in self:
            po._eh_check_block_policy()
        result = super().button_confirm()
        # On a successful confirm, write the commitment rows for
        # every matched line.
        for po in self.filtered(lambda p: p.state in ('purchase', 'done')):
            po._eh_create_commitments()
        return result

    def button_cancel(self):
        """Release every commitment when the PO is cancelled."""
        for po in self:
            po._eh_release_commitments(reason='Cancelled')
        return super().button_cancel()

    # ---- internals ----

    def _eh_iter_budget_targets(self):
        """Yield (line, account, date, analytic) per PO line.

        The date used for budget resolution is the PO's `date_order`
        (or `date_planned` if set on the line). Analytic resolution
        is best-effort: we honour the first explicit analytic on the
        line's analytic_distribution, ignoring percentages (the
        commitment is intentionally coarse: full PO line amount
        against a single budget line). Sites that need split
        commitments per analytic share extend this method.
        """
        self.ensure_one()
        for line in self.order_line:
            account = line.product_id and (
                line.product_id.property_account_expense_id
                or line.product_id.categ_id.property_account_expense_categ_id
            )
            if not account:
                continue
            date = line.date_planned or self.date_order
            analytic = self.env['account.analytic.account']
            if hasattr(line, 'analytic_distribution') and line.analytic_distribution:
                first_key = next(iter(line.analytic_distribution.keys()))
                try:
                    analytic = self.env['account.analytic.account'].browse(
                        int(first_key),
                    )
                except (TypeError, ValueError):
                    analytic = self.env['account.analytic.account']
            yield line, account, (
                date.date() if hasattr(date, 'date') else date
            ), analytic or None

    def _eh_check_block_policy(self):
        """Refuse confirm when overrun_policy='block' and any line
        would go negative.

        Walks the PO lines, resolves the matching budget line for
        each, computes the post-confirm availability (current
        available minus the line's price_subtotal), and raises
        UserError if any drops below zero on a budget whose policy
        is 'block'.
        """
        self.ensure_one()
        Commitment = self.env['eh.budget.commitment'].sudo()
        # Accumulate the required amount per resolved budget line before
        # checking availability. Several PO lines can land on the same
        # budget line; each one individually may fit, yet their sum may
        # exceed availability. Checking line-by-line against the same
        # untouched available_amount would let that overrun through.
        need_by_line = {}
        for po_line, account, date, analytic in self._eh_iter_budget_targets():
            budget_line = Commitment._resolve_budget_line(
                account, date, self.company_id,
                analytic_account=analytic,
            )
            if not budget_line:
                continue
            if budget_line.budget_id.overrun_policy != 'block':
                continue
            need_by_line.setdefault(budget_line, 0.0)
            need_by_line[budget_line] += po_line.price_subtotal
        if not need_by_line:
            return
        # Serialise concurrent confirms on the shared availability total.
        # available_amount is a non-stored compute over reserved commitment
        # rows, so there is no stored column or DB constraint to protect it:
        # two PO confirms resolving to the same 'block' budget line could each
        # read the pre-confirm availability, both pass this gate, then both
        # write commitments and drive availability negative -- breaking the
        # 'block' guarantee. Take a FOR UPDATE row lock on every resolved
        # budget line before reading availability. Because the lock is held to
        # commit (through super().button_confirm and _eh_create_commitments),
        # the second confirm queues behind the first's transaction and, once it
        # proceeds, re-reads the already-encumbered availability and correctly
        # raises. Mirrors the discipline in
        # eh_fx_revaluation_run._eh_lock_for_post and cheque_book serial
        # consumption. ORDER BY id gives a deterministic lock order so two POs
        # touching the same set of lines cannot deadlock.
        locked = self.env['eh.budget.line'].sudo().browse(
            [bl.id for bl in need_by_line],
        )
        locked.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_budget_line WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(locked.ids),),
        )
        # Drop the cached availability so the read below recomputes against
        # commitment rows committed by any transaction we just queued behind.
        locked.invalidate_recordset(['committed_amount', 'available_amount'])
        violations = []
        for budget_line, need in need_by_line.items():
            available = budget_line.available_amount
            if (available - need) < 0:
                violations.append((budget_line, available, need))
        if violations:
            lines = [
                _(
                    "%(budget)s line %(account)s: available "
                    "%(avail).2f, needs %(need).2f",
                    budget=v[0].budget_id.display_name,
                    account=v[0].account_id.display_name,
                    avail=v[1], need=v[2],
                )
                for v in violations
            ]
            raise UserError(_(
                "Purchase order %(po)s would exceed available "
                "budget on %(n)d line(s):\n  %(detail)s",
                po=self.display_name, n=len(violations),
                detail="\n  ".join(lines),
            ))

    def _eh_create_commitments(self):
        """Create one commitment per (PO line, matched budget line).

        Idempotent: re-running on a re-confirmed PO updates existing
        commitments rather than duplicating.
        """
        self.ensure_one()
        Commitment = self.env['eh.budget.commitment'].sudo()
        for po_line, account, date, analytic in self._eh_iter_budget_targets():
            budget_line = Commitment._resolve_budget_line(
                account, date, self.company_id,
                analytic_account=analytic,
            )
            if not budget_line:
                continue
            policy = budget_line.budget_id.overrun_policy
            if policy == 'off':
                continue
            # Idempotency key includes the PO line id. Without it, two
            # PO lines that resolve to the same budget line (same
            # expense account, period and analytic) would collapse onto
            # one commitment, the second overwriting the first line's
            # reserved amount and silently dropping its encumbrance.
            existing = Commitment.search([
                ('source_model', '=', 'purchase.order'),
                ('source_id', '=', self.id),
                ('source_line_id', '=', po_line.id),
                ('budget_line_id', '=', budget_line.id),
            ], limit=1)
            if existing:
                existing.write({
                    'amount': po_line.price_subtotal,
                    'state': 'reserved',
                    'source_ref': self.name,
                })
            else:
                Commitment.create({
                    'budget_line_id': budget_line.id,
                    'amount': po_line.price_subtotal,
                    'source_model': 'purchase.order',
                    'source_id': self.id,
                    'source_line_id': po_line.id,
                    'source_ref': self.name,
                    'state': 'reserved',
                })
            # Warn-mode: post a chatter note on the budget when this
            # commitment puts availability into negative territory.
            if policy == 'warn':
                budget_line.invalidate_recordset(['available_amount'])
                if budget_line.available_amount < 0:
                    budget_line.budget_id.message_post(body=_(
                        "Budget overrun warning: line for %(acc)s is "
                        "now %(avail).2f after PO %(po)s commitment.",
                        acc=budget_line.account_id.display_name,
                        avail=budget_line.available_amount,
                        po=self.name,
                    ))

    def _eh_release_commitments(self, reason=''):
        """Release every reserved commitment owned by this PO."""
        self.ensure_one()
        Commitment = self.env['eh.budget.commitment'].sudo()
        rows = Commitment.search([
            ('source_model', '=', 'purchase.order'),
            ('source_id', '=', self.id),
            ('state', '=', 'reserved'),
        ])
        if not rows:
            return
        rows.action_release()
        for row in rows:
            row.message_post(body=_(
                "Released. %(reason)s",
                reason=reason or '',
            ))

    def action_eh_view_commitments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Budget commitments"),
            'res_model': 'eh.budget.commitment',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('source_model', '=', 'purchase.order'),
                ('source_id', '=', self.id),
            ],
        }
