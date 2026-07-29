# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.budget.commitment: encumbrance accounting on a budget line.

Government and not-for-profit accounting recognise three positions
against a budget at any point in time:

  available = budgeted - actual - committed

`actual` is what has already been posted to the GL (the existing
budget-line compute).
`committed` is what has been promised but not yet posted: an
approved purchase order, an open contract, etc. The amount is
released back to availability when the underlying document either
becomes a posted bill (it shifts from committed to actual) or is
cancelled.

This model holds one row per source document (typically a
purchase.order line) per budget line. The two computed fields on
eh.budget.line (`committed_amount` and `available_amount`) sum the
matching commitment rows so reports and the PO confirm gate see a
consistent picture.

The lifecycle is deliberately simple:

  draft -> reserved -> released

draft: created as a placeholder while the source document is in
quote / approval state; does not deduct availability.
reserved: source document is confirmed; deducts availability.
released: source document was cancelled OR became a posted bill;
restores availability.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhBudgetCommitment(models.Model):
    _name = 'eh.budget.commitment'
    _description = "Budget commitment (encumbrance)"
    _order = 'budget_line_id, create_date desc, id desc'
    _inherit = ['mail.thread', 'eh.workflow.guard']

    # The reserve/release lifecycle drives budget availability. It advances
    # only through action_reserve / action_release and the PO source hooks
    # (which run as su); a direct non-superuser write to state is refused by
    # eh.workflow.guard so availability cannot be moved by a raw RPC write.
    _eh_guarded_fields = ('state',)

    budget_line_id = fields.Many2one(
        'eh.budget.line', required=True,
        ondelete='cascade', index=True,
        help=(
            "Budget line this commitment reserves against. Resolved "
            "from the source document's account / analytic / date."
        ),
    )
    budget_id = fields.Many2one(
        related='budget_line_id.budget_id', store=True, readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related='budget_line_id.budget_id.company_id',
        store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='budget_line_id.currency_id', store=True, readonly=True,
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('reserved', "Reserved"),
            ('released', "Released"),
        ],
        default='draft', required=True, tracking=True, index=True,
        help=(
            "Lifecycle. draft: source document is still in quote / "
            "approval; does not deduct availability. reserved: "
            "source document is confirmed; deducts availability. "
            "released: source document was cancelled or became a "
            "posted bill; restored availability."
        ),
    )
    amount = fields.Monetary(
        required=True, currency_field='currency_id', tracking=True,
        help=(
            "Commitment amount in budget currency. Always entered "
            "positive: a confirmed purchase order for AUD 5,000 "
            "creates a 5,000 commitment regardless of the account "
            "sign. This is the CURRENT reserved amount: partial "
            "releases (partial billing) reduce it toward zero while "
            "leaving original_amount untouched."
        ),
    )
    original_amount = fields.Monetary(
        currency_field='currency_id',
        help=(
            "The commitment amount as first reserved, before any "
            "partial release. Partial billing computes the release "
            "against this stable original so sequential partial bills "
            "add up to the full release and never leave a phantom "
            "residual. Populated on create and refreshed whenever "
            "amount is written above the recorded original."
        ),
    )

    # ---- source document ----
    source_model = fields.Char(
        index=True,
        help=(
            "Model name of the source document (typically "
            "'purchase.order' or 'purchase.order.line'). Sites that "
            "want to commit against sales orders or contracts add "
            "their own hook and write the relevant model name here."
        ),
    )
    source_id = fields.Integer(
        index=True,
        help="Database id of the source document.",
    )
    source_line_id = fields.Integer(
        index=True,
        help=(
            "Database id of the source document line (e.g. the "
            "purchase.order.line) when the commitment is line-level. "
            "Together with source_model and source_id this forms the "
            "idempotency key: one commitment per source line per "
            "budget line. Two purchase order lines that resolve to the "
            "same budget line therefore keep separate commitments "
            "instead of one overwriting the other. Zero when the "
            "source document has no line granularity."
        ),
    )
    source_ref = fields.Char(
        help=(
            "Display reference of the source document (e.g. PO "
            "number). Captured at creation so the row stays "
            "informative even if the source document is deleted."
        ),
    )

    notes = fields.Text()

    _sql_constraints = [
        ('check_amount_positive', 'CHECK (amount >= 0)', 'Commitment amount must be zero or positive.'),
    ]

    # ---- original-amount bookkeeping ----
    #
    # original_amount tracks the amount as first reserved so partial
    # releases can be computed against a stable baseline. It is set on
    # create (defaulting to amount) and refreshed whenever amount is
    # written to a value at or above the current original (a fresh
    # reserve or a PO re-confirm that raises the line amount). Writes
    # that only lower amount (partial release) leave it untouched.

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('original_amount') in (None, False):
                vals['original_amount'] = vals.get('amount', 0.0)
        return super().create(vals_list)

    def write(self, vals):
        if 'amount' in vals and 'original_amount' not in vals:
            new_amount = vals['amount']
            for rec in self:
                if new_amount >= (rec.original_amount or 0.0):
                    rec.original_amount = new_amount
        return super().write(vals)

    # ---- transitions ----

    def action_reserve(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'reserved':
                continue
            if rec.state == 'released':
                raise UserError(_(
                    "Released commitments cannot be re-reserved. "
                    "Create a fresh commitment for the document.",
                ))
            rec.state = 'reserved'

    def action_release(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'released':
                continue
            rec.state = 'released'

    # ---- lookup helpers used by the source-document hooks ----

    @api.model
    def _resolve_budget_line(
        self, account, date, company, analytic_account=None,
    ):
        """Pick the budget line that should carry the commitment.

        Resolution: find a line on a confirmed budget whose period
        contains date, whose account matches, and whose analytic
        account matches (or is empty when analytic_account is None).
        Returns the first match; sites with overlapping budgets
        should narrow via analytic.

        Returns an empty recordset when no line matches; the caller
        decides whether to skip silently or raise.
        """
        if not account or not date:
            return self.env['eh.budget.line']
        Line = self.env['eh.budget.line'].sudo()
        domain = [
            ('budget_id.state', '=', 'confirmed'),
            ('budget_id.company_id', '=', company.id),
            ('account_id', '=', account.id),
            ('period_from', '<=', date),
            ('period_to', '>=', date),
        ]
        if analytic_account:
            domain.append((
                'analytic_account_id', '=', analytic_account.id,
            ))
        else:
            domain.append((
                'analytic_account_id', '=', False,
            ))
        return Line.search(domain, limit=1)
