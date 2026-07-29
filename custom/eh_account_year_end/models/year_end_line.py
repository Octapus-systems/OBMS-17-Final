# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.year.end.line: per-account breakdown row on a year-end run.

Two kinds of row share the model:

* ``line_kind='pl'``: one row per income or expense account that
  contributed a non-zero balance to the fiscal year; closed to retained
  earnings.
* ``line_kind='oci'``: one row per mapped OCI flow account with a non-zero
  NET period movement; reclassified to its AOCI sub-reserve account
  (never to retained earnings, IAS 1.106).

The form view shows the breakdown so a manager can verify what the closing
entry will produce before posting. Lines are read-only after the parent run
leaves draft state.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .aoci_reserve_map import AOCI_KINDS

# Once a run is posted or reversed, its breakdown lines are the basis of a
# posted closing entry and must not be re-figured behind the manager who
# approved them. Only these material fields are frozen, so framework
# recomputes of the related helper fields still flow through.
_FROZEN_RUN_STATES = frozenset({'posted', 'reversed'})
_LOCKED_LINE_FIELDS = frozenset(
    {'income_balance', 'expense_balance', 'account_id', 'run_id',
     'line_kind', 'oci_kind', 'oci_balance', 'reserve_account_id'})


class EhYearEndLine(models.Model):
    _name = 'eh.year.end.line'
    _description = "Year-end closing breakdown line"
    _order = 'run_id, account_id'

    run_id = fields.Many2one(
        'eh.year.end.run', required=True,
        ondelete='cascade', index=True,
    )
    account_id = fields.Many2one(
        'account.account', required=True,
        ondelete='restrict',
    )
    account_type = fields.Selection(
        related='account_id.account_type', store=True, readonly=True,
    )

    income_balance = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Closing income balance for this account, sign-flipped to "
            "positive. Credits the income account in the closing entry."
        ),
    )
    expense_balance = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Closing expense balance for this account. Credits the "
            "expense account in the closing entry."
        ),
    )

    # -- AOCI sub-reserve reclassification rows (IAS 1.106) ---------------
    line_kind = fields.Selection(
        [('pl', "Profit or Loss"), ('oci', "OCI Reclassification")],
        default='pl', required=True,
        help=(
            "Profit-or-loss rows close to retained earnings. OCI rows "
            "reclassify a mapped OCI flow account's net period movement "
            "into its AOCI sub-reserve account, never into retained "
            "earnings."
        ),
    )
    oci_kind = fields.Selection(
        AOCI_KINDS, string="AOCI Component",
        help="Which accumulated-OCI component this reclassification row "
             "belongs to (from the company's AOCI sub-reserve mapping).",
    )
    oci_balance = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Net posted movement of the OCI flow account over the fiscal "
            "year, ledger-signed (debit positive). A negative value is an "
            "accumulated credit (an OCI gain for the period); the closing "
            "entry moves exactly this net amount to the sub-reserve, so "
            "amounts recycled to P&L during the year are never moved "
            "twice."
        ),
    )
    reserve_account_id = fields.Many2one(
        'account.account', string="AOCI Sub-Reserve",
        ondelete='restrict',
        help="Destination sub-reserve equity account for this OCI "
             "component's period movement.",
    )

    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True,
    )
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True,
    )

    # -- Integrity: freeze the figures once the close is posted ----------
    # The form marks these read-only after draft, but that is UI-only; the
    # figures drive the closing entry's debits, credits and retained-earnings
    # amount, so they are enforced immutable at the ORM/RPC layer too. The
    # legitimate compute path rebuilds lines while the run is still draft or
    # computed, so it is unaffected.

    @api.model_create_multi
    def create(self, vals_list):
        run_ids = [v.get('run_id') for v in vals_list if v.get('run_id')]
        runs = self.env['eh.year.end.run'].browse(run_ids)
        if any(r.state in _FROZEN_RUN_STATES for r in runs):
            raise UserError(_(
                "Breakdown lines cannot be added to a year-end run that has "
                "already been posted or reversed."))
        return super().create(vals_list)

    def write(self, vals):
        if _LOCKED_LINE_FIELDS.intersection(vals) and any(
            r.state in _FROZEN_RUN_STATES for r in self.run_id
        ):
            raise UserError(_(
                "Year-end breakdown figures are locked once the run is posted "
                "or reversed; they are the basis of a posted closing entry. "
                "Reverse the run to reopen the period, then recompute."))
        # Moving a line INTO a posted / reversed run would recompute that run's
        # totals and closing entry. The source check above inspects only the
        # current parent, so guard the target run explicitly.
        if vals.get('run_id'):
            target = self.env['eh.year.end.run'].browse(vals['run_id'])
            if target.state in _FROZEN_RUN_STATES:
                raise UserError(_(
                    "Breakdown lines cannot be moved into a year-end run that "
                    "has already been posted or reversed."))
        return super().write(vals)

    def unlink(self):
        if any(r.state in _FROZEN_RUN_STATES for r in self.run_id):
            raise UserError(_(
                "Year-end breakdown lines cannot be deleted once the run is "
                "posted or reversed."))
        return super().unlink()
