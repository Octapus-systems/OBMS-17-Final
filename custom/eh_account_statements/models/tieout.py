# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.statement.tieout: cross-statement reconciliation control.

A single control record asserting that, for one company and period, four
figures that must agree actually do:

  1. P&L net profit per the posted ledger,
  2. the statement of comprehensive income's profit for the period,
  3. the statement of changes in equity's profit movement, and
  4. the balance sheet current-year-earnings movement between the two
     period-end snapshots.

The check is a point-in-time snapshot: figures, residuals and tie flags are
written by ``action_check`` and the record is then frozen. A manager can
reset it to draft to re-run the check. Pairs without a source document are
marked not applicable: the pair reads tied so a missing optional statement
never blocks, but the source note records exactly which statements were
found so nothing fake-ties silently.
"""

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_is_zero

from .soce import _PL_ACCOUNT_TYPES

# Fields that make up the frozen control snapshot. After a check, these can
# only change through the internal-write context (action_check/action_reset).
_FROZEN_FIELDS = frozenset({
    'company_id', 'date_from', 'date_to',
    'pl_net_profit', 'soci_profit', 'soce_profit_movement',
    'bs_current_year_earnings_delta',
    'soci_id', 'soce_id',
    'soci_applicable', 'soce_applicable',
    'soci_residual', 'soce_residual', 'bs_residual',
    'soci_tied', 'soce_tied', 'bs_tied', 'all_tied',
    'source_note',
})


class EhStatementTieout(models.Model):
    _name = 'eh.statement.tieout'
    _description = "Cross-statement tie-out control"
    _inherit = ['eh.workflow.guard']
    _order = 'date_to desc, id desc'
    _rec_name = 'name'

    # State is a workflow field: it may only move through action_check /
    # action_reset (which run under sudo), never a direct RPC/ORM write. The
    # inherited eh.workflow.guard blocks a non-superuser write to it, closing
    # the "RPC-write state=checked to skip action_check" bypass. The frozen
    # snapshot in write() below is a separate, su-gated data-integrity control.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/')
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    date_from = fields.Date(required=True)
    date_to = fields.Date(required=True)
    state = fields.Selection(
        [('draft', "Draft"), ('checked', "Checked")],
        default='draft', required=True)

    # --- figures snapshot (written by action_check) ----------------------
    pl_net_profit = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="Net profit per the posted ledger over the period: the negated "
             "sum of income and expense account balances dated within the "
             "period. This is the anchor figure the other three are tied to.")
    soci_profit = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="Profit for the period per the statement of comprehensive "
             "income found for this company and period.")
    soce_profit_movement = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="Profit taken to equity across components per the statement of "
             "changes in equity found for this company and period.")
    bs_current_year_earnings_delta = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="Balance sheet current-year-earnings movement: the ledger P&L "
             "aggregate snapshot at the period end less the snapshot at the "
             "day before the period start.")

    # --- sources found ----------------------------------------------------
    soci_id = fields.Many2one(
        'eh.soci', string="Comprehensive income statement", readonly=True,
        help="The statement of comprehensive income the check tied against; "
             "empty when none exists for the company and period.")
    soce_id = fields.Many2one(
        'eh.soce', string="Changes in equity statement", readonly=True,
        help="The statement of changes in equity the check tied against; "
             "empty when none exists for the company and period.")
    soci_applicable = fields.Boolean(
        readonly=True,
        help="True when a statement of comprehensive income was found for "
             "the company and period. When False the SoCI pair is marked "
             "not applicable rather than tied against a real figure.")
    soce_applicable = fields.Boolean(
        readonly=True,
        help="True when a statement of changes in equity was found for the "
             "company and period. When False the SoCE pair is marked not "
             "applicable rather than tied against a real figure.")

    # --- per-pair residuals and tie flags ---------------------------------
    soci_residual = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="SoCI profit for the period less the ledger net profit; zero "
             "when the pair ties.")
    soce_residual = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="SoCE profit movement less the ledger net profit; zero when "
             "the pair ties.")
    bs_residual = fields.Monetary(
        currency_field='currency_id', readonly=True,
        help="Balance sheet current-year-earnings movement less the ledger "
             "net profit. Both come from the same posted-ledger aggregate, "
             "so this is an internal cross-check that must always be zero.")
    soci_tied = fields.Boolean(
        readonly=True,
        help="True when the SoCI profit ties to the ledger net profit "
             "within currency rounding, or when no SoCI exists for the "
             "period (not applicable; see the source note).")
    soce_tied = fields.Boolean(
        readonly=True,
        help="True when the SoCE profit movement ties to the ledger net "
             "profit within currency rounding, or when no SoCE exists for "
             "the period (not applicable; see the source note).")
    bs_tied = fields.Boolean(
        readonly=True,
        help="True when the balance sheet current-year-earnings movement "
             "equals the ledger net profit within currency rounding.")
    all_tied = fields.Boolean(
        readonly=True,
        help="True when every applicable pair ties: SoCI, SoCE and the "
             "balance sheet current-year-earnings movement all agree with "
             "the ledger net profit.")

    source_note = fields.Text(
        readonly=True,
        help="Which source statements the check found, and which pairs were "
             "marked not applicable because no statement exists.")
    notes = fields.Text()

    _sql_constraints = [
        ('check_period', 'CHECK (date_from <= date_to)', 'Tie-out start date must be on or before the end date.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.statement.tieout') or '/'
        return super().create(vals_list)

    def write(self, vals):
        # Freeze guard: once checked, the snapshot INPUT fields are frozen for
        # EVERYONE (a settled tie-out figure is not editable by anyone,
        # including server code), restated only by resetting to draft which
        # clears the snapshot. This is a data-integrity control, NOT su-gated:
        # a snapshot that server code could quietly overwrite is not frozen.
        # 'state' itself is deliberately excluded from the frozen set and is
        # owned by the inherited eh.workflow.guard (su-gated), so the
        # sanctioned action_check / action_reset (which run under sudo) can
        # still move state. action_reset lowers state to draft first, so the
        # subsequent snapshot clear is no longer frozen. Free-text notes stay
        # editable so a reviewer can annotate the control.
        touched = _FROZEN_FIELDS.intersection(vals)
        frozen = self.filtered(lambda t: t.state == 'checked')
        if touched and frozen:
            raise UserError(_(
                "This tie-out has been checked and is frozen. Reset it "
                "to draft (manager only) before changing %(fields)s on "
                "%(records)s.",
                fields=', '.join(sorted(touched)),
                records=', '.join(frozen.mapped('name'))))
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can run or reset the "
                "cross-statement tie-out."))

    def _pl_ledger_aggregate(self, date_from=None, date_to=None):
        """Posted-ledger P&L aggregate, credit-positive.

        Sums posted income + expense account balances for this company over
        the given date bounds (both inclusive, either optional) and negates
        them, because income accounts carry credit-negative balances and
        expenses debit-positive. This is the same derivation the primary
        statements use to tie their profit to the ledger.
        """
        self.ensure_one()
        domain = [
            ('company_id', '=', self.company_id.id),
            ('parent_state', '=', 'posted'),
            ('account_id.account_type', 'in', list(_PL_ACCOUNT_TYPES)),
        ]
        if date_from:
            domain.append(('date', '>=', date_from))
        if date_to:
            domain.append(('date', '<=', date_to))
        lines = self.env['account.move.line'].search(domain)
        return -sum(lines.mapped('balance'))

    def _find_source_statement(self, model_name):
        """Latest statement of ``model_name`` matching company + period.

        Prefers a confirmed statement; falls back to the latest one in any
        state so a draft still under preparation can be tied against.
        """
        self.ensure_one()
        domain = [
            ('company_id', '=', self.company_id.id),
            ('period_start', '=', self.date_from),
            ('period_end', '=', self.date_to),
        ]
        Model = self.env[model_name]
        record = Model.search(
            domain + [('state', '=', 'confirmed')], order='id desc', limit=1)
        if not record:
            record = Model.search(domain, order='id desc', limit=1)
        return record

    def action_check(self):
        """Run the cross-statement tie-out and freeze the result.

        Computes the ledger net profit for the period, looks up the period's
        statements of comprehensive income and of changes in equity, snapshots
        the balance sheet current-year-earnings movement, and records each
        pair's residual and tie flag. Pairs without a source statement are
        marked not applicable (tied, with the absence recorded in the source
        note) so a missing optional statement never blocks, but is never
        silently passed off as a real tie either.
        """
        self._check_manager()
        for tieout in self:
            if tieout.state == 'checked':
                raise UserError(_(
                    "Tie-out %(name)s has already been checked. Reset it to "
                    "draft before running the check again.",
                    name=tieout.name))
            rounding = (tieout.currency_id
                        or tieout.company_id.currency_id).rounding or 0.01
            notes = []

            # 1. Anchor: P&L net profit per the posted ledger.
            pl_net_profit = tieout._pl_ledger_aggregate(
                tieout.date_from, tieout.date_to)

            # 2. SoCI profit for the period, when a statement exists.
            soci = tieout._find_source_statement('eh.soci')
            if soci:
                soci_profit = soci.profit_for_period
                soci_residual = soci_profit - pl_net_profit
                soci_tied = float_is_zero(
                    soci_residual, precision_rounding=rounding)
                notes.append(_(
                    "SoCI: tied against %(name)s (%(state)s).",
                    name=soci.name, state=soci.state))
            else:
                soci_profit = 0.0
                soci_residual = 0.0
                soci_tied = True
                notes.append(_(
                    "SoCI: no statement of comprehensive income exists for "
                    "this company and period; pair marked not applicable."))

            # 3. SoCE profit movement, when a statement exists.
            soce = tieout._find_source_statement('eh.soce')
            if soce:
                soce_profit_movement = soce.total_profit
                soce_residual = soce_profit_movement - pl_net_profit
                soce_tied = float_is_zero(
                    soce_residual, precision_rounding=rounding)
                notes.append(_(
                    "SoCE: tied against %(name)s (%(state)s).",
                    name=soce.name, state=soce.state))
            else:
                soce_profit_movement = 0.0
                soce_residual = 0.0
                soce_tied = True
                notes.append(_(
                    "SoCE: no statement of changes in equity exists for "
                    "this company and period; pair marked not applicable."))

            # 4. Balance sheet current-year-earnings movement: the ledger
            # P&L aggregate snapshot at date_to less the snapshot at the day
            # before date_from. By construction this equals the period net
            # profit; recording it as an internal cross-check surfaces any
            # aggregation drift instead of assuming it away.
            opening_date = fields.Date.to_date(
                tieout.date_from) - timedelta(days=1)
            cye_at_end = tieout._pl_ledger_aggregate(date_to=tieout.date_to)
            cye_at_start = tieout._pl_ledger_aggregate(date_to=opening_date)
            bs_delta = cye_at_end - cye_at_start
            bs_residual = bs_delta - pl_net_profit
            bs_tied = float_is_zero(bs_residual, precision_rounding=rounding)
            if not bs_tied:
                notes.append(_(
                    "Balance sheet: current-year-earnings movement %(delta)s "
                    "does not equal the period net profit %(profit)s; the "
                    "internal cross-check failed (residual %(residual)s).",
                    delta=bs_delta, profit=pl_net_profit,
                    residual=bs_residual))

            # Runs under sudo so the frozen-snapshot write and the state move
            # pass the guards (env.su); the real env.user is preserved.
            tieout.sudo().write({
                'pl_net_profit': pl_net_profit,
                'soci_profit': soci_profit,
                'soce_profit_movement': soce_profit_movement,
                'bs_current_year_earnings_delta': bs_delta,
                'soci_id': soci.id if soci else False,
                'soce_id': soce.id if soce else False,
                'soci_applicable': bool(soci),
                'soce_applicable': bool(soce),
                'soci_residual': soci_residual,
                'soce_residual': soce_residual,
                'bs_residual': bs_residual,
                'soci_tied': soci_tied,
                'soce_tied': soce_tied,
                'bs_tied': bs_tied,
                'all_tied': soci_tied and soce_tied and bs_tied,
                'source_note': '\n'.join(notes),
                'state': 'checked',
            })
        return True

    def action_reset(self):
        """Manager-gated reset: unfreeze the control and clear the snapshot
        so the check can be run again."""
        self._check_manager()
        # Lower state to draft first (state is su-owned by eh.workflow.guard,
        # not part of the always-on frozen set), which unfreezes the record so
        # the snapshot clear below passes the freeze. Doing both in one write
        # would trip the always-on freeze on the still-checked record.
        self.sudo().write({'state': 'draft'})
        self.sudo().write({
            'pl_net_profit': 0.0,
            'soci_profit': 0.0,
            'soce_profit_movement': 0.0,
            'bs_current_year_earnings_delta': 0.0,
            'soci_id': False,
            'soce_id': False,
            'soci_applicable': False,
            'soce_applicable': False,
            'soci_residual': 0.0,
            'soce_residual': 0.0,
            'bs_residual': 0.0,
            'soci_tied': False,
            'soce_tied': False,
            'bs_tied': False,
            'all_tied': False,
            'source_note': False,
        })
        return True
