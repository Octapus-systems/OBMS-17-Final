# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.consol.unrealised.profit: unrealised profit in ending inventory that
arose on an intra-group sale (IFRS 10 / IAS 27).

When one group member sells inventory to another at a margin and the
buyer still holds that inventory at period end, the seller's profit is
recognised in the group accounts even though, from the group's point of
view, nothing has been sold to an outside party. Consolidated inventory
is carried above cost and consolidated profit is overstated by the same
amount.

Each record on a run names one such unrealised margin. At compute time
the run emits a balanced pair of elimination run-lines that removes it:

  Dr cost-of-sales / retained-earnings   +amount
  Cr inventory                           -amount

The two legs net to zero, so the consolidated set stays balanced while
inventory is written back to cost and the intra-group profit is
reversed. Records are optional: a run with none behaves exactly as
before.
"""

from odoo import fields, models


class EhConsolUnrealisedProfit(models.Model):
    _name = 'eh.consol.unrealised.profit'
    _description = "Consolidation unrealised profit in inventory"
    _order = 'run_id, id'

    run_id = fields.Many2one(
        'eh.consol.run', required=True,
        ondelete='cascade', index=True,
    )
    presentation_currency_id = fields.Many2one(
        related='run_id.presentation_currency_id',
        store=True, readonly=True,
    )

    name = fields.Char(
        required=True, default='/',
        help=(
            "Description of the intra-group sale carrying the unrealised "
            "margin, e.g. 'IC sales: AU sub to UK sub, ending stock'."
        ),
    )
    unrealised_amount = fields.Monetary(
        currency_field='presentation_currency_id',
        help=(
            "Profit still sitting in the buyer's ending inventory from an "
            "intra-group sale, in the presentation currency. This is the "
            "margin removed by the elimination: inventory is written back "
            "to group cost and consolidated profit is reduced by the same "
            "amount (IFRS 10 / IAS 27)."
        ),
    )
    inventory_account_id = fields.Many2one(
        'account.account',
        string="Inventory Account",
        help=(
            "Asset account on the consolidated chart carrying the buyer's "
            "ending inventory. Credited by the unrealised amount so the "
            "stock is restated to group cost."
        ),
    )
    cogs_or_re_account_id = fields.Many2one(
        'account.account',
        string="COGS / Retained Earnings Account",
        help=(
            "Account debited to reverse the intra-group profit: the "
            "cost-of-sales account when the profit arose in the current "
            "period, or retained earnings when it was recognised in a "
            "prior period."
        ),
    )

    notes = fields.Char()
