# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.asset.lvp.pool: Australian low-value asset pool.

Australian tax law allows depreciable assets under the low-value
threshold (currently AUD 1,000 cost or AUD 1,000 opening adjustable
value when transferred from individual depreciation) to be grouped
into a single low-value pool. The pool depreciates as a unit at
fixed ATO rates:

  * 18.75% in the first year for assets transferred during the year
    (the "half-year rule" applied by the ATO via the lower rate).
  * 37.5% per year for assets that have been in the pool for at least
    one full year and for the opening pool balance each year.

This implementation models one pool per company (sites can override to
allow multiple pools by year of allocation if their workflow needs
that). Assets transferred into the pool freeze their own schedule;
the pool's annual depreciation cron posts a single JE per year per
pool.

Out of scope (queued for follow-ups):
  * Software development pool (separate ATO regime, similar mechanics).
  * Disposal proceeds adjustment (proceeds reduce the pool balance
    rather than triggering a per-asset gain/loss).
"""

from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError  # noqa: F401


_FIRST_YEAR_RATE = 18.75
_SUBSEQUENT_YEAR_RATE = 37.5


class EhAssetLvpPool(models.Model):
    _name = 'eh.asset.lvp.pool'
    _description = "AU low-value asset pool"
    _order = 'name'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(
        required=True,
        help=(
            "Display label for the pool. Convention: 'Low-Value Pool "
            "<FY>'. Reused across years; the per-year balance lives "
            "in line_ids."
        ),
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True,
    )
    active = fields.Boolean(
        default=True,
        help=(
            "Soft-archive flag. Inactive pools are hidden from the "
            "transfer-into picker but existing data stays readable."
        ),
    )

    threshold = fields.Monetary(
        currency_field='currency_id',
        default=1000.0,
        help=(
            "Maximum cost of an asset eligible for transfer into this "
            "pool. AU tax sets this at AUD 1,000 for general low-"
            "value assets; the field is configurable so the same "
            "model can support future threshold changes."
        ),
    )
    first_year_rate = fields.Float(
        default=_FIRST_YEAR_RATE, digits=(5, 2),
        help=(
            "Depreciation rate (%) applied to assets transferred "
            "during the year. ATO default: 18.75%."
        ),
    )
    subsequent_year_rate = fields.Float(
        default=_SUBSEQUENT_YEAR_RATE, digits=(5, 2),
        help=(
            "Depreciation rate (%) applied to the opening pool "
            "balance each year. ATO default: 37.5%."
        ),
    )

    asset_ids = fields.One2many(
        'eh.asset', 'lvp_pool_id',
        help="Assets transferred into this pool.",
    )
    asset_count = fields.Integer(
        compute='_compute_pool_totals', store=False,
    )
    pool_balance = fields.Monetary(
        compute='_compute_pool_totals', store=False,
        currency_field='currency_id',
        help=(
            "Sum of opening adjustable values of every asset "
            "transferred in, less cumulative pool depreciation. The "
            "balance approaches zero as the pool depreciates each "
            "year and grows as new assets transfer in."
        ),
    )
    transferred_in_total = fields.Monetary(
        compute='_compute_pool_totals', store=False,
        currency_field='currency_id',
        help="Lifetime total of opening adjustable values transferred in.",
    )
    accumulated_depreciation = fields.Monetary(
        compute='_compute_pool_totals', store=False,
        currency_field='currency_id',
        help="Lifetime depreciation posted from the pool's annual runs.",
    )

    line_ids = fields.One2many(
        'eh.asset.lvp.pool.line', 'pool_id', copy=False,
        help=(
            "Annual depreciation lines for the pool. One row per year "
            "per pool; computed by action_compute_year."
        ),
    )

    pool_account_id = fields.Many2one(
        'account.account', string="Pool Asset Account",
        help=(
            "Balance-sheet account that carries the pool's gross "
            "value. Assets transferred in shift their NBV here."
        ),
    )
    accumulated_account_id = fields.Many2one(
        'account.account', string="Accumulated Pool Depreciation",
        help="Balance-sheet contra account for pool depreciation.",
    )
    expense_account_id = fields.Many2one(
        'account.account', string="Pool Depreciation Expense",
    )
    journal_id = fields.Many2one(
        'account.journal', string="Pool Journal",
    )

    notes = fields.Text()

    @api.depends(
        'asset_ids', 'asset_ids.lvp_opening_value',
        'asset_ids.net_book_value', 'line_ids.amount',
    )
    def _compute_pool_totals(self):
        for pool in self:
            pool.asset_count = len(pool.asset_ids)
            transferred = sum(
                self._lvp_asset_base(asset) for asset in pool.asset_ids
            )
            depreciation = sum(pool.line_ids.mapped('amount'))
            pool.transferred_in_total = transferred
            pool.accumulated_depreciation = depreciation
            pool.pool_balance = transferred - depreciation

    @api.model
    def _lvp_asset_base(self, asset):
        """Depreciable base a pooled asset contributes: the opening
        adjustable value captured when it was transferred in (its net book
        value at that moment), which is exactly what was reclassified into
        the pool asset account. Falls back to the live net book value for any
        asset linked to the pool without a captured value (defensive; the
        transfer flow always stamps it)."""
        return asset.lvp_opening_value or asset.net_book_value

    # ---- transfer flow ----

    def action_transfer_asset(self, asset, transfer_date=None):
        """Move an eligible asset into the pool.

        Validates the asset meets the threshold, freezes its primary
        schedule, links lvp_pool_id, and adds the asset's current
        net book value to the pool's transferred_in total.

        GL handling: when the pool carries its own asset account and the
        asset carries its own gross-asset and accumulated-depreciation
        accounts (and the pool has a journal), a balanced reclassification
        move is posted that removes the asset's gross cost from its asset
        account, clears its accumulated depreciation, and lands the net
        book value in the pool asset account:

          CR asset gross-asset account         (acquisition cost)
          DR asset accumulated-depreciation    (accumulated depreciation)
          DR pool asset account                (net book value)

        The two debits sum to the single credit by construction
        (accumulated_depreciation + net_book_value == acquisition_cost),
        so the entry balances. When the pool has no GL accounts configured
        (a tax-only pool that carries no carrying value in the ledger), no
        journal entry is posted and only the operational link is recorded.

        :param asset: eh.asset record to transfer.
        :param transfer_date: optional date for the JE. Defaults to today.
        """
        self.ensure_one()
        if asset.lvp_pool_id and asset.lvp_pool_id.id != self.id:
            raise UserError(_(
                "Asset %(asset)s is already in pool %(pool)s.",
                asset=asset.display_name,
                pool=asset.lvp_pool_id.display_name,
            ))
        if asset.acquisition_cost > self.threshold:
            raise UserError(_(
                "Asset %(asset)s acquisition cost %(amt).2f exceeds the "
                "pool threshold %(thr).2f. Configure a higher threshold "
                "or transfer this asset individually.",
                asset=asset.display_name,
                amt=asset.acquisition_cost,
                thr=self.threshold,
            ))
        transfer_date = transfer_date or fields.Date.context_today(self)
        self._transfer_asset_reclass_move(asset, transfer_date)
        # Persist the opening adjustable value (net book value at transfer)
        # and the allocation date so the pool depreciates and reports on the
        # value actually reclassified into the pool GL account, and rates the
        # asset by the year it was allocated rather than its in-service year.
        opening_value = (self.currency_id or asset.currency_id).round(
            asset.net_book_value or 0.0,
        )
        asset.write({
            'lvp_pool_id': self.id,
            'lvp_opening_value': opening_value,
            'lvp_allocation_date': transfer_date,
        })
        if asset.state == 'running':
            asset._eh_workflow_write({'state': 'paused'})
        asset.message_post(body=_(
            "Transferred to low-value pool %(pool)s on %(date)s. "
            "Individual depreciation paused; pool will depreciate as a unit.",
            pool=self.display_name,
            date=transfer_date,
        ))
        return True

    def _transfer_asset_reclass_move(self, asset, transfer_date):
        """Post the balanced GL reclassification for a pool transfer.

        Returns the posted account.move, or False when the pool is a
        tax-only pool with no GL accounts configured (no move is posted).
        Posting is a segregation-of-duties control point: only a manager
        may move carrying value between GL accounts.
        """
        self.ensure_one()
        # Tax-only pool: no ledger carrying value to reclassify.
        if not (self.pool_account_id and self.journal_id):
            return False
        if not (asset.asset_account_id
                and asset.accumulated_depreciation_account_id):
            raise UserError(_(
                "Asset %(asset)s has no gross-asset and accumulated-"
                "depreciation accounts configured, so its carrying value "
                "cannot be reclassified into pool %(pool)s. Configure the "
                "asset accounts or clear the pool's GL accounts to run a "
                "tax-only pool.",
                asset=asset.display_name, pool=self.display_name,
            ))
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can reclassify an asset's "
                "carrying value into a low-value pool. This posting is a "
                "segregation-of-duties control point.",
            ))
        currency = self.currency_id or asset.currency_id
        gross = currency.round(asset.acquisition_cost or 0.0)
        nbv = currency.round(asset.net_book_value or 0.0)
        accumulated = currency.round(gross - nbv)
        label = _("LVP transfer %(asset)s to %(pool)s",
                  asset=asset.display_name, pool=self.display_name)
        line_ids = [
            (0, 0, {
                'name': label,
                'account_id': asset.asset_account_id.id,
                'debit': 0.0,
                'credit': gross,
            }),
        ]
        if accumulated:
            line_ids.append((0, 0, {
                'name': label,
                'account_id': asset.accumulated_depreciation_account_id.id,
                'debit': accumulated,
                'credit': 0.0,
            }))
        line_ids.append((0, 0, {
            'name': label,
            'account_id': self.pool_account_id.id,
            'debit': nbv,
            'credit': 0.0,
        }))
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'eh_sealed': True,
            'journal_id': self.journal_id.id,
            'date': transfer_date,
            'ref': label,
            'line_ids': line_ids,
        })
        move.action_post()
        return move

    def action_compute_year(self, year=None):
        """Compute and persist a depreciation line for the given year.

        Uses the simplified ATO formula:
          depreciation = subsequent_rate% * opening_pool_balance
                         + first_year_rate% * additions_during_year

        Returns the created line. Idempotent per-year: re-running for
        an existing year raises rather than producing duplicate lines.
        """
        self.ensure_one()
        year = year or fields.Date.context_today(self).year
        existing = self.line_ids.filtered(lambda line_item: line_item.year == year)
        if existing:
            raise UserError(_(
                "A line already exists for pool %(pool)s in year "
                "%(year)s. Cancel that line before recomputing.",
                pool=self.display_name, year=year,
            ))
        # Opening balance = transferred-in lifetime - depreciation
        # already booked in prior years.
        prior_lines = self.line_ids.filtered(lambda line_item: line_item.year < year)
        prior_dep = sum(prior_lines.mapped('amount'))
        # Classify each asset by the year it was ALLOCATED into the pool
        # (its transfer date), not its in-service year: the ATO first-year
        # 18.75% rate applies in the year of allocation, the 37.5% rate to
        # the opening pool balance thereafter. An asset allocated in a later
        # year is not depreciated in the pool before it was transferred in.
        # Depreciate on the opening adjustable value reclassified into the
        # pool (net book value at transfer), never the gross acquisition cost.
        additions = 0.0
        opening_balance = 0.0
        for asset in self.asset_ids:
            alloc_date = asset.lvp_allocation_date or asset.in_service_date
            if not alloc_date:
                continue
            if alloc_date.year > year:
                # Not yet allocated to the pool in this year: no charge.
                continue
            base = self._lvp_asset_base(asset)
            if alloc_date.year == year:
                additions += base
            else:
                opening_balance += base
        opening_balance = opening_balance - prior_dep
        amount = (
            (opening_balance * self.subsequent_year_rate / 100.0)
            + (additions * self.first_year_rate / 100.0)
        )
        amount = self.currency_id.round(amount)
        line = self.env['eh.asset.lvp.pool.line'].create({
            'pool_id': self.id,
            'year': year,
            'opening_balance': self.currency_id.round(opening_balance),
            'additions': self.currency_id.round(additions),
            'amount': amount,
        })
        return line


class EhAssetLvpPoolLine(models.Model):
    _name = 'eh.asset.lvp.pool.line'
    _inherit = ['eh.workflow.guard']
    _description = "AU LVP annual depreciation line"
    _order = 'pool_id, year desc'

    # is_posted / move_id may only change through the record's own posting
    # action (which runs as su). A plain RPC write (or the old editable
    # boolean toggle) cannot flip is_posted True->False to re-arm the poster
    # and book a second pool move, nor repoint move_id.
    _eh_guarded_fields = ('is_posted', 'move_id')

    pool_id = fields.Many2one(
        'eh.asset.lvp.pool', required=True, ondelete='cascade', index=True,
    )
    currency_id = fields.Many2one(
        related='pool_id.currency_id', store=True, readonly=True,
    )
    year = fields.Integer(
        required=True,
        help="AU financial year for this depreciation line.",
    )
    opening_balance = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Pool opening balance at the start of the year (after "
            "prior-year depreciation, before this year's additions)."
        ),
    )
    additions = fields.Monetary(
        currency_field='currency_id',
        help="Acquisition cost of assets transferred in during the year.",
    )
    amount = fields.Monetary(
        required=True, currency_field='currency_id',
        help=(
            "Annual depreciation: subsequent_rate% * opening_balance "
            "+ first_year_rate% * additions."
        ),
    )

    is_posted = fields.Boolean(default=False, copy=False, readonly=True)
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
    )

    def _posting_date(self):
        """Fiscal year-end of the pool year this line depreciates.

        A pool run for year Y is routinely posted during the following
        year's close (e.g. a FY2025 run posted in Feb 2026). Dating the move
        on the day the button is clicked would push the whole charge into the
        wrong reporting period. Book it at the close of the year it relates
        to instead, honouring the company's configured fiscal year-end
        (defaults to an AU 30-June year for a low-value pool).
        """
        self.ensure_one()
        company = self.pool_id.company_id or self.env.company
        anchor = date(self.year, 6, 30)
        try:
            fy = company.compute_fiscalyear_dates(anchor)
            return fy.get('date_to') or anchor
        except Exception:  # noqa: BLE001 - fall back to the anchor date
            return anchor

    def _eh_lock_for_post(self):
        """Serialise concurrent posters so a cron and a manual click (or a
        double-submit) cannot each create a journal entry for the same pool
        line. Take a row lock, then re-read is_posted from the database."""
        if not self.ids:
            return
        self.env.cr.execute(
            'SELECT id FROM eh_asset_lvp_pool_line WHERE id IN %s '
            'FOR UPDATE',
            (tuple(self.ids),),
        )
        self.invalidate_recordset(['is_posted', 'move_id'])

    def action_post(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can post low-value pool "
                "depreciation to the general ledger. This posting is a "
                "segregation-of-duties control point.",
            ))
        self._eh_lock_for_post()
        for rec in self:
            # Idempotent: never re-book a line that already carries a live
            # posted move (that duplicates the pool's annual charge).
            if rec.is_posted or (
                rec.move_id and rec.move_id.state == 'posted'
            ):
                continue
            pool = rec.pool_id
            if not (pool.expense_account_id and pool.accumulated_account_id
                    and pool.journal_id):
                raise UserError(_(
                    "Configure the pool's expense, accumulated, and "
                    "journal accounts before posting.",
                ))
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'eh_sealed': True,
                'journal_id': pool.journal_id.id,
                'date': rec._posting_date(),
                'ref': "LVP %s %s" % (pool.name, rec.year),
                'line_ids': [
                    (0, 0, {
                        'name': "LVP depreciation %s" % rec.year,
                        'account_id': pool.expense_account_id.id,
                        'debit': rec.amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': "LVP depreciation %s" % rec.year,
                        'account_id': pool.accumulated_account_id.id,
                        'debit': 0.0,
                        'credit': rec.amount,
                    }),
                ],
            })
            move.action_post()
            # is_posted / move_id are guarded; stamp through the sanctioned
            # action path (runs as su) so a real, non-superuser manager can
            # post while a direct write to those fields stays blocked.
            rec._eh_workflow_write({'is_posted': True, 'move_id': move.id})
        return True
