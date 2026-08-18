# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.asset.book: a parallel depreciation book for a fixed asset.

Most jurisdictions require at least two books on the same asset:
the statutory book that posts to the General Ledger (governed by the
local accounting standard) and one or more tax books (governed by the
tax-deduction rules of each jurisdiction). Australia adds the
prime-cost vs diminishing-value choice for tax purposes; IFRS adds a
revaluation model that can diverge from the statutory book; large
groups add a management book for internal reporting.

This model holds one such book per asset, INDEPENDENT of the asset's
own primary depreciation parameters (which represent the statutory
book that posts to the GL). Books defined here are reporting-only by
default: they generate a parallel schedule that the user can extract
for the tax return or the management pack, but they do NOT post
journal entries unless `posts_to_gl` is explicitly ticked.

Schedule generation reuses the same per-method helpers as the asset's
primary schedule, parameterised on the book's own fields. This means
adding a new method (e.g. units of production at a different rate per
book) only needs the helper to be parameter-driven, which it already
is.
"""

import calendar
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


_BOOK_TYPES = [
    ('statutory', "Statutory (parallel)"),
    ('tax', "Tax"),
    ('ifrs', "IFRS"),
    ('mgmt', "Management"),
]

_METHODS = [
    ('straight_line', "Straight Line"),
    ('reducing_balance', "Reducing Balance"),
    ('prime_cost', "Prime Cost (AU tax)"),
    ('diminishing_value', "Diminishing Value (AU tax)"),
    ('manual', "Manual"),
]


class EhAssetBook(models.Model):
    _name = 'eh.asset.book'
    _description = "Asset depreciation book"
    _order = 'asset_id, book_type, id'
    _rec_name = 'name'

    name = fields.Char(
        required=True,
        help=(
            "Display label for the book, e.g. 'AU Tax Book' or "
            "'IFRS book'. Shown on the asset form and on every "
            "schedule line so reports can disambiguate parallel "
            "schedules."
        ),
    )
    asset_id = fields.Many2one(
        'eh.asset', required=True, ondelete='cascade', index=True,
        help="Parent asset this book belongs to.",
    )
    book_type = fields.Selection(
        _BOOK_TYPES, required=True, default='tax',
        help=(
            "Functional category of the book. Statutory parallel books "
            "duplicate the primary book under a different accounting "
            "standard. Tax books drive tax-return numbers. IFRS / "
            "management books drive secondary reporting. The category "
            "is informational; posting behaviour comes from the "
            "posts_to_gl flag."
        ),
    )
    posts_to_gl = fields.Boolean(
        default=False,
        help=(
            "When set, the book's schedule posts journal entries via "
            "the asset's depreciation journal. By default, additional "
            "books are reporting-only and do not post; the primary "
            "schedule on the asset record is the GL source of truth. "
            "Tick this only if your accounting policy requires "
            "parallel postings (uncommon)."
        ),
    )

    method = fields.Selection(
        _METHODS, required=True, default='straight_line',
        help=(
            "Depreciation method for this book. Prime Cost is the "
            "AU tax straight-line over the effective life. "
            "Diminishing Value is the AU tax reducing-balance with the "
            "200% factor applied to the prime-cost rate by default."
        ),
    )
    useful_life_months = fields.Integer(
        string="Useful Life (months)",
        required=True, default=60,
        help=(
            "Effective life used for this book's schedule. Tax-book "
            "lives are dictated by ATO ruling TR 2025/1 (and "
            "successors); statutory book lives reflect the entity's "
            "accounting policy."
        ),
    )
    salvage_value = fields.Monetary(
        default=0.0,
        currency_field='currency_id',
        help=(
            "Estimated residual value at end of useful life. Salvage "
            "for tax-book purposes is typically zero (write down to "
            "zero); statutory salvage may be non-zero for assets with "
            "expected resale value."
        ),
    )
    declining_factor = fields.Float(
        default=2.0,
        help=(
            "Multiplier on the straight-line rate when method is "
            "Reducing Balance or Diminishing Value. Australia uses "
            "200% (factor 2.0) for assets acquired on or after "
            "10 May 2006; legacy assets use 150% (factor 1.5)."
        ),
    )
    prorate_first_period = fields.Boolean(
        default=True,
        help=(
            "When set, the first period's depreciation is prorated "
            "based on days in service that month. AU tax usually "
            "prorates by days held; statutory accounting may use "
            "either convention."
        ),
    )

    currency_id = fields.Many2one(
        related='asset_id.currency_id', store=True, readonly=True,
    )
    company_id = fields.Many2one(
        related='asset_id.company_id', store=True, readonly=True,
    )

    line_ids = fields.One2many(
        'eh.asset.book.line', 'book_id', copy=False,
        help="Generated schedule lines for this book.",
    )
    line_count = fields.Integer(compute='_compute_totals', store=False)
    total_depreciation = fields.Monetary(
        compute='_compute_totals', store=False,
        currency_field='currency_id',
        help="Sum of every scheduled depreciation line on this book.",
    )
    final_book_value = fields.Monetary(
        compute='_compute_totals', store=False,
        currency_field='currency_id',
        help=(
            "Net book value at the end of the schedule. Should equal "
            "the salvage value; non-zero divergence is rounding."
        ),
    )

    notes = fields.Text(
        help=(
            "Notes on the book's purpose, regulatory reference, or "
            "differences from the primary book. Visible to the tax "
            "agent and the auditor."
        ),
    )

    _sql_constraints = [
        ('check_useful_life', 'CHECK (useful_life_months > 0)', 'Useful life must be greater than zero on every book.'),
        ('check_salvage', 'CHECK (salvage_value >= 0)', 'Salvage value cannot be negative.'),
    ]

    @api.depends('line_ids.amount')
    def _compute_totals(self):
        for book in self:
            book.line_count = len(book.line_ids)
            book.total_depreciation = sum(
                book.line_ids.mapped('amount'),
            )
            if book.line_ids:
                book.final_book_value = (
                    book.asset_id.acquisition_cost
                    - book.total_depreciation
                )
            else:
                book.final_book_value = book.asset_id.acquisition_cost

    @api.constrains('salvage_value', 'asset_id')
    def _check_salvage_le_cost(self):
        for book in self:
            if (book.asset_id and book.salvage_value
                    > book.asset_id.acquisition_cost):
                raise ValidationError(_(
                    "Salvage value on book %(book)s exceeds the asset's "
                    "acquisition cost. Lower the salvage or revise the "
                    "asset cost first.",
                    book=book.display_name,
                ))

    # ---- actions ----

    def action_compute_schedule(self):
        """Generate (or regenerate) the schedule for this book.

        Refuses to overwrite if posts_to_gl is True and any line
        carries a posted move; otherwise wipes existing lines and
        rebuilds from the current parameters.
        """
        for book in self:
            posted = book.line_ids.filtered(lambda line_item: line_item.is_posted)
            if posted:
                raise UserError(_(
                    "Book %(book)s has %(n)d posted line(s); recompute "
                    "would corrupt the GL. Mark the asset as paused, "
                    "reverse the postings, or create a new book.",
                    book=book.display_name, n=len(posted),
                ))
            book.line_ids.unlink()
            book._build_schedule()

    def _build_schedule(self):
        self.ensure_one()
        Line = self.env['eh.asset.book.line']
        rows = self._generate_rows()
        for row in rows:
            Line.create({
                'book_id': self.id,
                'sequence': row['sequence'],
                'depreciation_date': row['date'],
                'amount': row['amount'],
                'accumulated': row['accumulated'],
                'remaining_value': row['remaining'],
            })

    def _generate_rows(self):
        """Pure function: return the schedule rows for this book.

        Mirrors the asset's _schedule_* helpers but reads parameters
        from the book record so each book can carry an independent
        method / life / factor.
        """
        self.ensure_one()
        if self.method == 'manual':
            return []
        depreciable = (
            self.asset_id.acquisition_cost - self.salvage_value
        )
        if depreciable <= 0:
            return []
        if self.method in ('straight_line', 'prime_cost'):
            return self._rows_straight_line(depreciable)
        if self.method in ('reducing_balance', 'diminishing_value'):
            return self._rows_reducing_balance(depreciable)
        return []

    def _rows_straight_line(self, depreciable):
        """Straight-line / prime-cost rows.

        AU prime-cost = depreciable / effective_life expressed as a
        per-month figure. Identical maths to the existing straight-
        line helper; the label difference signals intent in tax
        reporting.
        """
        self.ensure_one()
        rows = []
        months = self.useful_life_months
        per_period = depreciable / months
        accumulated = 0.0
        period_date = self._first_period_date()
        for n in range(1, months + 1):
            if n == 1 and self.prorate_first_period:
                amount = self._first_period_prorated_amount(per_period)
            elif n == months:
                amount = depreciable - accumulated
            else:
                amount = per_period
            amount = self.currency_id.round(amount)
            accumulated = self.currency_id.round(accumulated + amount)
            remaining = self.currency_id.round(
                self.asset_id.acquisition_cost - accumulated,
            )
            rows.append({
                'sequence': n,
                'date': period_date,
                'amount': amount,
                'accumulated': accumulated,
                'remaining': remaining,
            })
            period_date = self._next_period_end(period_date)
        return rows

    def _rows_reducing_balance(self, depreciable):
        """Reducing-balance / diminishing-value rows.

        AU diminishing-value rate = (declining_factor / effective_life)
        applied to the opening NBV each period. Switches to straight-
        line when the SL on the remaining balance exceeds the DV
        amount, which is the AU pattern: 200% DV in early years,
        flatten to SL when the DV would otherwise drag the asset
        below zero before end of life.
        """
        self.ensure_one()
        rows = []
        months = self.useful_life_months
        years = max(1, months / 12.0)
        sl_rate_per_year = 1.0 / years
        rate_per_year = self.declining_factor * sl_rate_per_year
        rate_per_period = rate_per_year / 12.0
        accumulated = 0.0
        nbv = self.asset_id.acquisition_cost
        period_date = self._first_period_date()
        for n in range(1, months + 1):
            remaining_periods = months - n + 1
            sl_amount = max(
                0.0, (nbv - self.salvage_value) / remaining_periods,
            )
            rb_amount = max(
                0.0, (nbv - self.salvage_value),
            ) * rate_per_period
            amount = max(rb_amount, sl_amount)
            if n == 1 and self.prorate_first_period:
                amount = self._first_period_prorated_amount(amount)
            if n == months:
                amount = depreciable - accumulated
            amount = max(0.0, self.currency_id.round(amount))
            if accumulated + amount > depreciable:
                amount = self.currency_id.round(depreciable - accumulated)
            accumulated = self.currency_id.round(accumulated + amount)
            nbv = self.asset_id.acquisition_cost - accumulated
            rows.append({
                'sequence': n,
                'date': period_date,
                'amount': amount,
                'accumulated': accumulated,
                'remaining': self.currency_id.round(nbv),
            })
            period_date = self._next_period_end(period_date)
            if accumulated >= depreciable:
                break
        return rows

    def _first_period_date(self):
        d = self.asset_id.in_service_date
        return self._month_end(d)

    @staticmethod
    def _month_end(d):
        last = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, last)

    def _next_period_end(self, d):
        nxt = d + relativedelta(months=1)
        return self._month_end(nxt)

    def _first_period_prorated_amount(self, full_period_amount):
        d = self.asset_id.in_service_date
        last = calendar.monthrange(d.year, d.month)[1]
        days_in_service = last - d.day + 1
        if last <= 0:
            return full_period_amount
        return full_period_amount * (days_in_service / float(last))


class EhAssetBookLine(models.Model):
    _name = 'eh.asset.book.line'
    _description = "Asset book schedule line"
    _order = 'book_id, sequence, depreciation_date'

    book_id = fields.Many2one(
        'eh.asset.book', required=True, ondelete='cascade', index=True,
    )
    asset_id = fields.Many2one(
        related='book_id.asset_id', store=True, index=True,
    )
    sequence = fields.Integer(default=1)
    depreciation_date = fields.Date(
        required=True,
        help="Period-end date the depreciation amount falls on.",
    )
    amount = fields.Monetary(
        currency_field='currency_id',
        help="Period depreciation amount per the book's method.",
    )
    accumulated = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Cumulative depreciation through the end of this period. "
            "Independent of any other book's accumulator."
        ),
    )
    remaining_value = fields.Monetary(
        currency_field='currency_id',
        help="Net book value after this period's depreciation.",
    )
    is_posted = fields.Boolean(
        default=False,
        help=(
            "True when the book is GL-posting (posts_to_gl) and the "
            "schedule line has been booked. Reporting-only books leave "
            "this False at all times."
        ),
    )
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        help=(
            "Journal entry posted for this line (only set when the "
            "book is GL-posting and the line has been processed)."
        ),
    )

    currency_id = fields.Many2one(
        related='book_id.currency_id', store=True, readonly=True,
    )
