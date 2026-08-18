# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
IFRS 16 lease modification wizard (lessee remeasurement, IFRS 16.39-46).

The wizard handles a modification that is NOT a separate lease. Two
paths:

1. Remeasurement (IFRS 16.39-43) - a change in the discount rate and / or
   the lease payments (a floating-rate reassessment, an index / rate
   change, a residual-value guarantee reassessment, or a modification
   that changes consideration without decreasing scope). The liability
   is remeasured to the PV of the revised payments at the revised
   discount rate; the ROU asset is adjusted by the SAME amount. If a
   DECREASE would take the ROU below zero, the ROU is floored at zero
   and the excess goes to P&L (IFRS 16.39).

     Dr or Cr Lease Liability   delta_liability
     Dr or Cr ROU Asset         (bounded to keep ROU >= 0)
     Cr or Dr P&L               (only the ROU-floor excess)

2. Partial scope decrease (IFRS 16.45-46) - the lease term is shortened
   or the leased capacity reduced. The ROU asset is decreased in
   PROPORTION to the reduction in scope; the liability is remeasured to
   the PV of the revised payments; the difference between the reduction
   in the liability and the proportionate reduction in the ROU is a
   gain or loss to P&L (IFRS 16.46(a)).

     Dr Lease Liability          liability reduction
       Cr ROU Asset              proportionate ROU reduction
       Cr / Dr P&L               difference (gain / loss)

Then the unposted schedule is wiped and a new one is built starting
from the modification date.
"""

import calendar
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


CADENCE_MONTHS = {
    'monthly': 1,
    'quarterly': 3,
    'semi_annual': 6,
    'annual': 12,
}


class EhLeaseModifyWizard(models.TransientModel):
    _name = 'eh.lease.modify.wizard'
    _description = "Lease Modification Wizard"

    lease_id = fields.Many2one(
        'eh.lease.contract', required=True, ondelete='cascade',
    )
    modification_date = fields.Date(
        required=True, default=fields.Date.context_today,
    )
    modification_type = fields.Selection(
        [
            ('remeasure', "Remeasurement (rate / payment change)"),
            ('scope_decrease', "Partial scope decrease"),
        ],
        required=True, default='remeasure',
        help=(
            "Remeasurement (IFRS 16.39-43): a change in the discount "
            "rate and / or the payments; the ROU is adjusted by the same "
            "amount as the liability (floored at zero, excess to P&L). "
            "Partial scope decrease (IFRS 16.45-46): the ROU is reduced "
            "in proportion to the scope given up and the difference "
            "against the liability reduction posts to P&L."
        ),
    )
    scope_decrease_pct = fields.Float(
        string="Scope decrease %", digits=(5, 2),
        help=(
            "Percentage of the right-of-use given up in a partial scope "
            "decrease (IFRS 16.46(a)): the ROU asset is reduced by this "
            "proportion of its carrying amount."
        ),
    )
    pl_account_id = fields.Many2one(
        'account.account', string="P/L Account",
        help=(
            "Gain / loss account for the P&L effect of the modification: "
            "the ROU-floor excess on a remeasurement decrease "
            "(IFRS 16.39) or the difference between the liability and "
            "proportionate ROU reductions on a partial scope decrease "
            "(IFRS 16.46(a)). Required whenever the modification produces "
            "a P&L amount."
        ),
    )
    new_term_months = fields.Integer(required=True)
    new_payment_amount = fields.Monetary(required=True)
    new_ibr = fields.Float(string="New IBR (annual %)", required=True)
    notes = fields.Text()

    currency_id = fields.Many2one(
        related='lease_id.currency_id', readonly=True,
    )
    company_id = fields.Many2one(
        related='lease_id.company_id', readonly=True,
    )
    current_liability = fields.Monetary(
        compute='_compute_current', readonly=True,
    )
    current_rou = fields.Monetary(
        compute='_compute_current', readonly=True,
    )
    new_liability = fields.Monetary(
        compute='_compute_new_liability', readonly=True,
    )
    delta = fields.Monetary(
        compute='_compute_new_liability', readonly=True,
    )
    rou_reduction = fields.Monetary(
        compute='_compute_effects', readonly=True,
        help="Reduction applied to the ROU asset by this modification.",
    )
    pl_amount = fields.Monetary(
        compute='_compute_effects', readonly=True,
        help="P&L effect of this modification (positive = gain).",
    )

    @api.depends('lease_id')
    def _compute_current(self):
        for w in self:
            if w.lease_id:
                w.current_liability = w.lease_id._liability_balance_after_last_post()
                w.current_rou = w.lease_id._rou_carrying_amount()
            else:
                w.current_liability = 0.0
                w.current_rou = 0.0

    @api.depends('new_term_months', 'new_payment_amount', 'new_ibr',
                 'lease_id', 'modification_date')
    def _compute_new_liability(self):
        for w in self:
            if not w.lease_id:
                w.new_liability = 0.0
                w.delta = 0.0
                continue
            try:
                pv = w._compute_new_pv()
            except Exception:  # noqa: BLE001
                w.new_liability = 0.0
                w.delta = 0.0
                continue
            w.new_liability = w.lease_id.currency_id.round(pv)
            w.delta = w.lease_id.currency_id.round(pv - w.current_liability)

    def _compute_new_pv(self):
        self.ensure_one()
        cadence = self.lease_id.cadence
        period_months = CADENCE_MONTHS[cadence]
        if self.new_term_months % period_months:
            raise UserError(_(
                "Revised term must be a whole multiple of the cadence.",
            ))
        n = int(self.new_term_months // period_months)
        annual = self.new_ibr / 100.0
        r = (1.0 + annual) ** (period_months / 12.0) - 1.0
        pmt = self.new_payment_amount
        if r == 0:
            pv = pmt * n
        else:
            pv = pmt * (1.0 - (1.0 + r) ** (-n)) / r
            if self.lease_id.payment_timing == 'advance':
                pv = pv * (1.0 + r)
        return pv

    @api.depends('modification_type', 'scope_decrease_pct',
                 'new_term_months', 'new_payment_amount', 'new_ibr',
                 'lease_id', 'modification_date')
    def _compute_effects(self):
        for w in self:
            try:
                effects = w._effects()
            except Exception:  # noqa: BLE001
                w.rou_reduction = 0.0
                w.pl_amount = 0.0
                continue
            w.rou_reduction = -effects['rou_change']
            w.pl_amount = effects['pl_amount']

    def _effects(self):
        """Resolve the accounting effects of the modification, rounded to
        the company currency. Returns a dict with:

        * delta          - change in the lease liability (new - current);
        * rou_change      - signed change applied to the ROU asset
                            (negative reduces it);
        * pl_amount       - P&L effect (positive = gain, credit);
        * new_liability   - remeasured liability the schedule rebuilds on.

        Remeasurement (IFRS 16.39-43): the ROU moves with the liability;
        a decrease is bounded so the ROU never goes below zero, with the
        excess to P&L. Partial scope decrease (IFRS 16.45-46): the ROU is
        reduced proportionately and the difference against the liability
        reduction is the P&L gain / loss.
        """
        self.ensure_one()
        lease = self.lease_id
        rnd = lease.currency_id.round
        current_liability = lease._liability_balance_after_last_post()
        current_rou = lease._rou_carrying_amount()
        new_liability = rnd(self._compute_new_pv())

        if self.modification_type == 'scope_decrease':
            pct = (self.scope_decrease_pct or 0.0) / 100.0
            rou_reduction = rnd(current_rou * pct)
            liability_reduction = rnd(current_liability - new_liability)
            # IFRS 16.46(a): gain / loss = liability reduction less the
            # proportionate ROU reduction.
            pl_amount = rnd(liability_reduction - rou_reduction)
            return {
                'delta': rnd(new_liability - current_liability),
                'rou_change': -rou_reduction,
                'pl_amount': pl_amount,
                'new_liability': new_liability,
                'current_rou': current_rou,
            }

        # Remeasurement (rate / payment change).
        delta = rnd(new_liability - current_liability)
        if delta >= 0:
            # Increase (or no change): ROU rises by the full delta, no P&L.
            return {
                'delta': delta,
                'rou_change': delta,
                'pl_amount': 0.0,
                'new_liability': new_liability,
                'current_rou': current_rou,
            }
        # Decrease: ROU falls, floored at zero; excess to P&L (gain).
        decrease = -delta
        rou_absorbed = min(current_rou, decrease)
        excess = rnd(decrease - rou_absorbed)
        return {
            'delta': delta,
            'rou_change': -rnd(rou_absorbed),
            'pl_amount': excess,
            'new_liability': new_liability,
            'current_rou': current_rou,
        }

    def action_modify(self):
        self.ensure_one()
        lease = self.lease_id
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_(
                "Only accounting managers can modify leases.",
            ))
        if lease.state not in ('active', 'modified'):
            raise UserError(_(
                "Only active leases can be modified.",
            ))
        lease._check_remeasurement_supported(_("modified"))

        rnd = lease.currency_id.round
        effects = self._effects()
        delta = effects['delta']            # liability change (signed)
        rou_change = effects['rou_change']  # ROU change (signed)
        pl_amount = effects['pl_amount']    # P&L (positive = gain)

        if pl_amount and not self.pl_account_id:
            raise UserError(_(
                "This modification produces a P&L amount of %(amt)s; "
                "select a P/L account for the gain or loss "
                "(IFRS 16.39 ROU-floor excess or IFRS 16.46(a) scope-"
                "decrease difference).",
                amt=pl_amount,
            ))

        lines = []
        # Lease liability leg (Dr when it decreases, Cr when it increases).
        if delta > 0:
            lines.append((0, 0, {
                'name': _("Lease modification liability uplift %s",
                          lease.display_name),
                'account_id': lease.lease_liability_account_id.id,
                'debit': 0.0, 'credit': delta,
            }))
        elif delta < 0:
            lines.append((0, 0, {
                'name': _("Lease modification liability decrease %s",
                          lease.display_name),
                'account_id': lease.lease_liability_account_id.id,
                'debit': -delta, 'credit': 0.0,
            }))
        # ROU asset leg (Dr when it increases, Cr when it decreases).
        if rou_change > 0:
            lines.append((0, 0, {
                'name': _("Lease modification ROU uplift %s",
                          lease.display_name),
                'account_id': lease.rou_asset_account_id.id,
                'debit': rou_change, 'credit': 0.0,
            }))
        elif rou_change < 0:
            lines.append((0, 0, {
                'name': _("Lease modification ROU decrease %s",
                          lease.display_name),
                'account_id': lease.rou_asset_account_id.id,
                'debit': 0.0, 'credit': -rou_change,
            }))
        # P&L leg: a positive P&L amount is a gain (credit), negative a
        # loss (debit).
        if pl_amount > 0:
            lines.append((0, 0, {
                'name': _("Lease modification gain %s", lease.display_name),
                'account_id': self.pl_account_id.id,
                'debit': 0.0, 'credit': pl_amount,
            }))
        elif pl_amount < 0:
            lines.append((0, 0, {
                'name': _("Lease modification loss %s", lease.display_name),
                'account_id': self.pl_account_id.id,
                'debit': -pl_amount, 'credit': 0.0,
            }))
        if lines:
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'date': self.modification_date,
                'journal_id': lease.journal_id.id,
                'ref': _("Lease modification %s", lease.display_name),
                'line_ids': lines,
            })
            move.action_post()

        # Update lease parameters and rebuild the remaining schedule. The
        # ROU carrying amount going into the rebuild moves by rou_change
        # (which already reflects the floor / proportionate reduction).
        posted_rou = sum(lease.schedule_line_ids.filtered(
            lambda line_item: line_item.is_posted,
        ).mapped('rou_amount'))
        lease._eh_workflow_write({
            'term_months': self.new_term_months,
            'payment_amount': self.new_payment_amount,
            'incremental_borrowing_rate': self.new_ibr,
            'liability_initial_value': rnd(effects['new_liability']),
            'rou_initial_value': rnd(
                effects['current_rou'] + rou_change + posted_rou,
            ),
            'state': 'modified',
            'modification_count': lease.modification_count + 1,
            'last_modified_at': fields.Datetime.now(),
        })

        unposted = lease.schedule_line_ids.filtered(lambda line_item: not line_item.is_posted)
        unposted.unlink()
        self._build_modified_schedule(lease, effects)
        lease.message_post(
            body=_("Lease modified at %(date)s (%(kind)s): term=%(term)sm, "
                   "payment=%(pmt)s, IBR=%(ibr)s%%, liability delta=%(delta)s, "
                   "ROU change=%(rou)s, P&L=%(pl)s. %(notes)s",
                   date=self.modification_date,
                   kind=self.modification_type,
                   term=self.new_term_months,
                   pmt=self.new_payment_amount,
                   ibr=self.new_ibr,
                   delta=delta, rou=rou_change, pl=pl_amount,
                   notes=self.notes or '/'),
        )
        return {'type': 'ir.actions.act_window_close'}

    def _build_modified_schedule(self, lease, effects):
        Line = self.env['eh.lease.schedule.line']
        cadence = lease.cadence
        period_months = CADENCE_MONTHS[cadence]
        n = int(self.new_term_months // period_months)
        annual = self.new_ibr / 100.0
        r = (1.0 + annual) ** (period_months / 12.0) - 1.0
        pmt = self.new_payment_amount

        # ROU carrying amount to amortise over the revised term already
        # reflects the floor / proportionate reduction (rou_change).
        rou_remaining = lease.currency_id.round(
            effects['current_rou'] + effects['rou_change'],
        )
        rou_per_month = (
            rou_remaining / self.new_term_months
            if self.new_term_months else 0.0
        )
        rou_accumulated_at_mod = sum(
            lease.schedule_line_ids.filtered(
                lambda line_item: line_item.is_posted,
            ).mapped('rou_amount'),
        )

        posted_seqs = lease.schedule_line_ids.filtered(
            lambda line_item: line_item.is_posted,
        ).mapped('sequence')
        last_seq = max(posted_seqs) if posted_seqs else 0

        period_date = self._first_modified_period_date(lease)

        # Reuse the lease's consistent amortisation builder so the
        # modified schedule satisfies the same invariant: principal +
        # interest == payment_amount on every row, liability_close ==
        # liability_open - principal, and the last row trues up to zero.
        rows = lease._compute_amortisation_rows(
            opening_liability=effects['new_liability'], n=n, r=r, pmt=pmt,
        )
        target_rou_total = rou_accumulated_at_mod + rou_remaining

        for n_idx, row in enumerate(rows, start=1):
            is_last = (n_idx == n)
            if is_last:
                rou_amount = target_rou_total - rou_accumulated_at_mod
            else:
                rou_amount = rou_per_month * period_months
            rou_amount = lease.currency_id.round(max(0.0, rou_amount))
            rou_accumulated_at_mod = lease.currency_id.round(
                rou_accumulated_at_mod + rou_amount,
            )

            Line.create({
                'lease_id': lease.id,
                'sequence': last_seq + n_idx,
                'period_date': period_date,
                'liability_open': row['liability_open'],
                'payment_amount': row['payment_amount'],
                'interest': row['interest'],
                'principal': row['principal'],
                'liability_close': row['liability_close'],
                'rou_amount': rou_amount,
                'rou_accumulated': rou_accumulated_at_mod,
            })
            period_date = self._next_period_date(period_date, period_months)

    def _first_modified_period_date(self, lease):
        period_months = CADENCE_MONTHS[lease.cadence]
        if lease.payment_timing == 'advance':
            return self._month_end(self.modification_date)
        d = self.modification_date + relativedelta(months=period_months)
        return self._month_end(d)

    @staticmethod
    def _month_end(d):
        last = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, last)

    def _next_period_date(self, current, months):
        nxt = current + relativedelta(months=months)
        return self._month_end(nxt)
