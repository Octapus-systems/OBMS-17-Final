# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.borrowing.cost: capitalisation of borrowing costs on a qualifying asset.

The capitalisable amount is the borrowing costs on specific borrowings net of
temporary investment income, plus the borrowing costs on general borrowings
applied by the capitalisation rate to the expenditure, capped at the
borrowing costs actually incurred (IAS 23.10-14).
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhBorrowingCost(models.Model):
    _name = 'eh.borrowing.cost'
    _description = "Borrowing cost capitalisation (IAS 23)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_end desc, id desc'
    _rec_name = 'name'

    # State moves only through the record's own actions (action_capitalise /
    # action_cancel), never a direct RPC/ORM write: otherwise a plain user
    # could write({'state': 'capitalised'}) to skip the manager check and the
    # journal entry entirely.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    qualifying_asset = fields.Char(
        required=True, tracking=True,
        help="The qualifying asset under construction or production.")
    state = fields.Selection(
        [('draft', "Draft"), ('capitalised', "Capitalised"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    period_start = fields.Date(
        tracking=True,
        help="Start of the capitalisation period. Used to time-apportion "
             "dated expenditure into a weighted-average base (IAS 23.14). "
             "Defaults to the earliest expenditure date when left blank.")
    period_end = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)

    # Capitalisation-period controls (IAS 23.17-25). All optional: when unset
    # the flat single-period behaviour above is preserved unchanged.
    commencement_date = fields.Date(
        tracking=True,
        help="Date capitalisation commences: expenditure and borrowing costs "
             "are being incurred and activities to prepare the asset are in "
             "progress (IAS 23.17-18). Borrowing cost before this date is not "
             "capitalised. Leave blank to capitalise from the period start.")
    cessation_date = fields.Date(
        tracking=True,
        help="Date the asset is substantially ready for its intended use or "
             "sale; capitalisation ceases (IAS 23.22-25). Borrowing cost after "
             "this date is not capitalised. Leave blank to capitalise to the "
             "period end.")
    suspension_line_ids = fields.One2many(
        'eh.borrowing.cost.suspension', 'borrowing_cost_id',
        string="Suspension Periods",
        help="Spans during which active development was suspended; "
             "capitalisation is suspended over these spans (IAS 23.20-21).")

    specific_borrowing_cost = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Borrowing costs on funds borrowed specifically for the asset.")
    temporary_investment_income = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Income on the temporary investment of specific borrowings, "
             "deducted from the amount capitalised (IAS 23.12-13).")
    general_expenditure = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Expenditure on the asset funded from general borrowings. Used "
             "as the base only when no dated expenditure lines are entered; "
             "otherwise the weighted-average base is derived from the lines "
             "(IAS 23.14).")
    expenditure_line_ids = fields.One2many(
        'eh.borrowing.cost.line', 'borrowing_cost_id',
        string="Dated Expenditure",
        help="Expenditure on the asset funded from general borrowings, dated "
             "so it can be time-apportioned into a weighted-average base "
             "(IAS 23.14).")
    weighted_average_base = fields.Monetary(
        compute='_compute_capitalisable', store=True,
        currency_field='currency_id',
        help="Weighted-average accumulated expenditure over the period to "
             "which the capitalisation rate is applied (IAS 23.14).")
    capitalisation_rate = fields.Float(
        digits=(6, 3), tracking=True,
        help="Weighted-average rate of general borrowings, as a percentage "
             "(IAS 23.14).")
    actual_borrowing_cost = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Total borrowing costs actually incurred in the period; the cap "
             "on the amount capitalised (IAS 23.14).")

    capitalisable = fields.Monetary(
        compute='_compute_capitalisable', store=True,
        currency_field='currency_id',
        help="Amount capitalised this period.")
    uncapped_amount = fields.Monetary(
        compute='_compute_capitalisable', store=True,
        currency_field='currency_id')

    asset_account_id = fields.Many2one(
        'account.account', string="Qualifying Asset Account", tracking=True)
    borrowing_cost_account_id = fields.Many2one(
        'account.account', string="Interest / Borrowing Cost Account",
        tracking=True,
        domain="[('account_type', '=', 'expense')]",
        help="The interest expense account from which the capitalised amount "
             "is reclassified to the asset.")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]")

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='set null')

    notes = fields.Text()

    @api.depends('specific_borrowing_cost', 'temporary_investment_income',
                 'general_expenditure', 'capitalisation_rate',
                 'actual_borrowing_cost', 'period_start', 'period_end',
                 'commencement_date', 'cessation_date',
                 'suspension_line_ids.date_start', 'suspension_line_ids.date_end',
                 'expenditure_line_ids.amount', 'expenditure_line_ids.date')
    def _compute_capitalisable(self):
        for c in self:
            base = c._weighted_average_base()
            c.weighted_average_base = (
                c.currency_id.round(base) if c.currency_id else base)
            # Investment income on temporarily invested specific borrowings is
            # deducted from that specific borrowing's OWN capitalisable amount
            # (IAS 23.12). Floor the net at zero so an excess of investment
            # income over the specific borrowing cost cannot spill over and
            # erode the general-borrowing component: income can reduce the
            # specific tranche to zero but never subsidise the general pool.
            specific_net = max(
                c.specific_borrowing_cost - c.temporary_investment_income, 0.0)
            general = base * c.capitalisation_rate / 100.0
            uncapped = specific_net + general
            c.uncapped_amount = (
                c.currency_id.round(uncapped) if c.currency_id else uncapped)
            capped = min(uncapped, c.actual_borrowing_cost) \
                if c.actual_borrowing_cost else uncapped
            c.capitalisable = max(
                c.currency_id.round(capped) if c.currency_id else capped, 0.0)

    def _has_capitalisation_window(self):
        """Whether any IAS 23.17-25 window control is set.

        When none is set, the flat single-period behaviour is preserved
        exactly; the window logic is opt-in.
        """
        self.ensure_one()
        return bool(self.commencement_date or self.cessation_date
                    or self.suspension_line_ids)

    def _active_spans(self, window_start, window_end):
        """Return the active (non-suspended) sub-spans of the window.

        Each span is a ``(start, end)`` pair of dates. The window
        ``[window_start, window_end]`` has the suspension periods
        (IAS 23.20-21) carved out. Returns an empty list when the window is
        degenerate or fully suspended.
        """
        self.ensure_one()
        if not window_start or not window_end or window_end <= window_start:
            return []
        # Clamp each suspension into the window and merge into disjoint spans.
        suspensions = []
        for s in self.suspension_line_ids:
            if not s.date_start or not s.date_end:
                continue
            lo = max(s.date_start, window_start)
            hi = min(s.date_end, window_end)
            if hi > lo:
                suspensions.append((lo, hi))
        suspensions.sort()
        active = []
        cursor = window_start
        for lo, hi in suspensions:
            if lo > cursor:
                active.append((cursor, lo))
            cursor = max(cursor, hi)
        if cursor < window_end:
            active.append((cursor, window_end))
        return active

    def _weighted_average_base(self):
        """Return the base to which the capitalisation rate is applied.

        Where dated expenditure lines exist, each amount is time-apportioned
        by the fraction of the capitalisation period for which it was
        outstanding (from its date to the period end), giving the
        weighted-average accumulated expenditure required by IAS 23.14. When
        no dated lines are entered, the flat ``general_expenditure`` is used
        unchanged.

        When any capitalisation-period control is set (commencement,
        cessation or a suspension span, IAS 23.17-25) apportionment is
        restricted to the active window: borrowing cost is not capitalised
        before commencement, during a suspension, or after the asset is ready
        for use. A cost whose window is empty (for example entirely past the
        cessation date) yields a nil base and nothing is capitalised.
        """
        self.ensure_one()
        if self._has_capitalisation_window():
            return self._windowed_base()
        lines = self.expenditure_line_ids.filtered(lambda line_item: line_item.date)
        if not lines:
            return self.general_expenditure
        period_end = self.period_end
        if not period_end:
            return sum(lines.mapped('amount'))
        starts = [line.date for line in lines]
        period_start = self.period_start or min(starts)
        total_days = (period_end - period_start).days
        if total_days <= 0:
            # Degenerate period; fall back to the raw sum of expenditure.
            return sum(lines.mapped('amount'))
        base = 0.0
        for line in lines:
            # Expenditure outstanding from its date (clamped into the period)
            # to the period end.
            outstanding_from = max(line.date, period_start)
            days_out = (period_end - outstanding_from).days
            if days_out <= 0:
                continue
            base += line.amount * days_out / total_days
        return base

    def _windowed_base(self):
        """Weighted-average base restricted to the active capitalisation
        window (IAS 23.17-25).

        The window runs from ``commencement_date`` (falling back to
        ``period_start`` then the earliest expenditure date) to
        ``cessation_date`` (falling back to ``period_end``), with suspension
        spans removed. Each dated expenditure is weighted by the active days
        it is outstanding within the window, over the full period length so
        the rate is applied consistently with the flat case. Flat
        ``general_expenditure`` is scaled by the active fraction of the
        period.
        """
        self.ensure_one()
        lines = self.expenditure_line_ids.filtered(lambda line_item: line_item.date)
        starts = [line.date for line in lines]
        earliest = min(starts) if starts else None
        period_start = self.commencement_date or self.period_start or earliest
        period_end = self.cessation_date or self.period_end
        if not period_start or not period_end:
            # Not enough dating to form a window; nothing to apportion.
            return 0.0
        # Denominator: the full nominal reporting period.
        nominal_start = self.period_start or self.commencement_date or earliest
        nominal_end = self.period_end or period_end
        total_days = (nominal_end - nominal_start).days
        if total_days <= 0:
            return 0.0
        # Clamp the active window INSIDE the reporting period. Capitalisation
        # for days before the period start (an earlier commencement) or after
        # the period end belongs to other periods, so the active window can
        # never exceed the nominal period and windowing only ever reduces the
        # base relative to the flat case (never inflates it).
        if nominal_start and period_start < nominal_start:
            period_start = nominal_start
        if nominal_end and period_end > nominal_end:
            period_end = nominal_end
        if period_end <= period_start:
            return 0.0
        spans = self._active_spans(period_start, period_end)
        if not spans:
            return 0.0
        if not lines:
            active_days = sum((hi - lo).days for lo, hi in spans)
            return self.general_expenditure * active_days / total_days
        base = 0.0
        for line in lines:
            outstanding_days = 0
            for lo, hi in spans:
                # Expenditure is outstanding from its date onward; count the
                # active days within each span that fall on or after it.
                out_from = max(line.date, lo)
                if hi > out_from:
                    outstanding_days += (hi - out_from).days
            if outstanding_days:
                base += line.amount * outstanding_days / total_days
        return base

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.borrowing.cost') or '/'
        return super().create(vals_list)

    # Inputs to the capitalisable amount. Once the period has been capitalised
    # they are frozen: editing them would recompute the capitalisable figure
    # away from the amount already posted to the qualifying asset (IAS 23).
    _FROZEN_AFTER_CAPITALISED = (
        'specific_borrowing_cost', 'temporary_investment_income',
        'general_expenditure', 'capitalisation_rate', 'actual_borrowing_cost',
        'period_start', 'period_end',
        'commencement_date', 'cessation_date',
    )

    def write(self, vals):
        frozen = [f for f in self._FROZEN_AFTER_CAPITALISED if f in vals]
        if frozen:
            posted = self.filtered(lambda c: c.state == 'capitalised')
            if posted:
                raise UserError(_(
                    "Measurement inputs (%(fields)s) are frozen once the "
                    "period is capitalised; the capitalisable amount must "
                    "equal the entry already posted. Reverse the entry to "
                    "re-measure (IAS 23).",
                    fields=', '.join(frozen)))
        return super().write(vals)

    def action_capitalise(self):
        self.ensure_one()
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can capitalise borrowing "
                "costs."))
        if self.state != 'draft':
            raise UserError(_("Only a draft record can be capitalised."))
        if not self.journal_id or not self.asset_account_id \
                or not self.borrowing_cost_account_id:
            raise UserError(_(
                "Configure the journal, qualifying asset account and "
                "borrowing cost account first."))
        currency = self.currency_id
        amount = currency.round(self.capitalisable)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_(
                "There is no capitalisable borrowing cost for this period."))
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': self.period_end,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_sealed': True,
            'line_ids': [
                (0, 0, {
                    'name': _("Capitalised borrowing cost %s", self.name),
                    'account_id': self.asset_account_id.id,
                    'debit': amount, 'credit': 0.0}),
                (0, 0, {
                    'name': _("Reclassify interest to asset %s", self.name),
                    'account_id': self.borrowing_cost_account_id.id,
                    'debit': 0.0, 'credit': amount}),
            ],
        })
        move.action_post()
        self.write({'state': 'capitalised', 'move_id': move.id})
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        for c in self:
            if c.state == 'capitalised':
                raise UserError(_(
                    "Reverse the entry before cancelling %s.", c.display_name))
            c.state = 'cancelled'

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move', 'res_id': self.move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }


class EhBorrowingCostLine(models.Model):
    _name = 'eh.borrowing.cost.line'
    _description = "Dated expenditure for borrowing cost capitalisation"
    _order = 'date, id'

    borrowing_cost_id = fields.Many2one(
        'eh.borrowing.cost', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='borrowing_cost_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='borrowing_cost_id.currency_id', store=True, readonly=True)
    date = fields.Date(
        required=True,
        help="Date the expenditure was incurred. Determines the fraction of "
             "the period it is outstanding for the weighted average.")
    amount = fields.Monetary(
        currency_field='currency_id',
        help="Expenditure on the asset funded from general borrowings.")
    label = fields.Char(string="Description")

    # Dated expenditure drives the weighted-average base, so its measurement
    # fields are frozen once the parent period is capitalised.
    _FROZEN_AFTER_CAPITALISED = ('amount', 'date')

    @staticmethod
    def _frozen_error(fields_):
        return UserError(_(
            "Dated expenditure (%(fields)s) is frozen once the period is "
            "capitalised; it feeds the weighted-average base of an amount "
            "already posted (IAS 23).",
            fields=', '.join(fields_)))

    @api.model_create_multi
    def create(self, vals_list):
        # A line cannot be added to a period that is already capitalised: it
        # would change the weighted-average base behind a posted entry.
        parent_ids = [v.get('borrowing_cost_id') for v in vals_list
                      if v.get('borrowing_cost_id')]
        if parent_ids:
            posted = self.env['eh.borrowing.cost'].browse(parent_ids).filtered(
                lambda c: c.state == 'capitalised')
            if posted:
                raise self._frozen_error(('date', 'amount'))
        return super().create(vals_list)

    def write(self, vals):
        frozen = [f for f in self._FROZEN_AFTER_CAPITALISED if f in vals]
        if frozen:
            posted = self.filtered(
                lambda line_item: line_item.borrowing_cost_id.state == 'capitalised')
            if posted:
                raise self._frozen_error(frozen)
        # Re-parenting a line into or out of a capitalised period is equally a
        # measurement change behind a posted entry.
        if 'borrowing_cost_id' in vals:
            target = self.env['eh.borrowing.cost'].browse(
                vals['borrowing_cost_id'])
            if self.filtered(
                    lambda line_item: line_item.borrowing_cost_id.state == 'capitalised') \
                    or target.state == 'capitalised':
                raise self._frozen_error(('borrowing_cost_id',))
        return super().write(vals)

    def unlink(self):
        posted = self.filtered(
            lambda line_item: line_item.borrowing_cost_id.state == 'capitalised')
        if posted:
            raise self._frozen_error(('date', 'amount'))
        return super().unlink()


class EhBorrowingCostSuspension(models.Model):
    _name = 'eh.borrowing.cost.suspension'
    _description = "Suspension span for borrowing cost capitalisation"
    _order = 'date_start, id'

    borrowing_cost_id = fields.Many2one(
        'eh.borrowing.cost', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='borrowing_cost_id.company_id', store=True, index=True)
    date_start = fields.Date(
        required=True,
        help="First day active development was suspended (IAS 23.20-21).")
    date_end = fields.Date(
        required=True,
        help="Day active development resumed; capitalisation resumes on this "
             "day (IAS 23.20-21).")
    label = fields.Char(string="Reason")

    _sql_constraints = [
        ('order_constraint', 'CHECK (date_end > date_start)', "A suspension period must end after it starts."),
    ]

    @staticmethod
    def _frozen_error():
        return UserError(_(
            "Suspension periods are frozen once the borrowing cost is "
            "capitalised; they define the active window of an amount already "
            "posted (IAS 23)."))

    @api.model_create_multi
    def create(self, vals_list):
        parent_ids = [v.get('borrowing_cost_id') for v in vals_list
                      if v.get('borrowing_cost_id')]
        if parent_ids:
            posted = self.env['eh.borrowing.cost'].browse(parent_ids).filtered(
                lambda c: c.state == 'capitalised')
            if posted:
                raise self._frozen_error()
        return super().create(vals_list)

    def write(self, vals):
        guarded = {'date_start', 'date_end', 'borrowing_cost_id'} & set(vals)
        if guarded and self.filtered(
                lambda s: s.borrowing_cost_id.state == 'capitalised'):
            raise self._frozen_error()
        if 'borrowing_cost_id' in vals:
            target = self.env['eh.borrowing.cost'].browse(
                vals['borrowing_cost_id'])
            if target.state == 'capitalised':
                raise self._frozen_error()
        return super().write(vals)

    def unlink(self):
        if self.filtered(lambda s: s.borrowing_cost_id.state == 'capitalised'):
            raise self._frozen_error()
        return super().unlink()
