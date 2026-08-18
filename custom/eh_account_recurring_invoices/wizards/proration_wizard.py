# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Proration wizard for mid-period recurring invoice changes.

A subscription customer changes plan mid-month. The recurring invoice
template needs to: (1) issue a credit note for the unused remainder of
the current period at the old price, (2) issue an invoice for the new
plan from today through the period end at the new price, (3) update
the template so the next full-period invoice uses the new price.

This wizard does all three in one transaction. Both the credit and the
catch-up invoice are posted under one savepoint so a partial failure
rolls back cleanly. The wizard records the change on the template's
chatter so the auditor can trace pricing history per subscription.

Maths convention: prorate by calendar days remaining versus calendar
days in the period (not 30/360, not actual/365). This is the
convention every subscription-billing platform uses; the alternative
30-day-month rule is documented but not implemented here because it
diverges from the contract on every February.
"""

from datetime import timedelta  # noqa: F401

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError  # noqa: F401
from dateutil.relativedelta import relativedelta


class EhRecurringInvoiceProrationWizard(models.TransientModel):
    _name = 'eh.recurring.invoice.proration.wizard'
    _description = "Recurring invoice proration wizard"

    template_id = fields.Many2one(
        'eh.recurring.invoice.template',
        required=True,
        ondelete='cascade',
    )
    change_date = fields.Date(
        required=True, default=fields.Date.context_today,
        help=(
            "Date the new pricing takes effect. The remaining unused "
            "portion of the current period at the old price is "
            "credited; the same window at the new price is invoiced."
        ),
    )
    new_amount = fields.Monetary(
        required=True,
        currency_field='currency_id',
        help="The unit price for the new plan, per period.",
    )
    currency_id = fields.Many2one(
        related='template_id.currency_id', readonly=True,
    )
    period_end_date = fields.Date(
        compute='_compute_period_end',
        help="Last day of the current billing period under the existing schedule.",
    )
    days_remaining = fields.Integer(
        compute='_compute_period_end',
    )
    period_length_days = fields.Integer(
        compute='_compute_period_end',
    )
    old_amount = fields.Monetary(
        compute='_compute_period_end',
        currency_field='currency_id',
    )
    credit_amount = fields.Monetary(
        compute='_compute_period_end',
        currency_field='currency_id',
        help="Refund for the unused remainder at the old price.",
    )
    new_period_amount = fields.Monetary(
        compute='_compute_period_end',
        currency_field='currency_id',
        help="Charge for the same remaining window at the new price.",
    )
    net_amount = fields.Monetary(
        compute='_compute_period_end',
        currency_field='currency_id',
        help="Net invoice (positive: customer owes; negative: company refunds).",
    )

    @api.depends('template_id', 'change_date', 'new_amount')
    def _compute_period_end(self):
        for wiz in self:
            if not wiz.template_id or not wiz.change_date:
                wiz.period_end_date = False
                wiz.days_remaining = 0
                wiz.period_length_days = 0
                wiz.old_amount = 0.0
                wiz.credit_amount = 0.0
                wiz.new_period_amount = 0.0
                wiz.net_amount = 0.0
                continue
            tpl = wiz.template_id
            unit = tpl.interval_unit
            n = max(int(tpl.interval or 1), 1)
            period_start = tpl.next_run_date or wiz.change_date
            if unit == 'day':
                period_end = period_start + relativedelta(days=n - 1)
            elif unit == 'week':
                period_end = period_start + relativedelta(weeks=n, days=-1)
            elif unit == 'month':
                period_end = period_start + relativedelta(months=n, days=-1)
            elif unit == 'quarter':
                period_end = period_start + relativedelta(months=3 * n, days=-1)
            elif unit == 'year':
                period_end = period_start + relativedelta(years=n, days=-1)
            else:
                period_end = period_start + relativedelta(days=29)
            wiz.period_end_date = period_end
            length = (period_end - period_start).days + 1
            remaining = max(
                (period_end - wiz.change_date).days + 1, 0,
            )
            wiz.period_length_days = length
            wiz.days_remaining = remaining
            # The period amount is quantity * price_unit, exactly as the
            # normal generation path posts it (_build_invoice_vals posts
            # 'quantity': line.quantity). Summing price_unit alone ignores
            # per-seat quantity>1 lines and under-credits the unused old-plan
            # portion by the quantity factor.
            old_unit = sum(
                (line_item.quantity or 0.0) * (line_item.price_unit or 0.0)
                for line_item in tpl.line_ids
            ) or 0.0
            total_qty = sum(tpl.line_ids.mapped('quantity')) or 0.0
            wiz.old_amount = old_unit
            if length > 0:
                ratio = remaining / float(length)
            else:
                ratio = 0.0
            wiz.credit_amount = round(old_unit * ratio, 2)
            # new_amount is a per-unit price (it becomes line.price_unit on
            # apply), so charge it against the same quantity the next
            # full-period invoice will bill.
            wiz.new_period_amount = round(
                wiz.new_amount * total_qty * ratio, 2)
            wiz.net_amount = round(
                wiz.new_period_amount - wiz.credit_amount, 2,
            )

    def action_apply(self):
        """Issue the credit + new-price invoice in one savepoint, then
        update the template price to `new_amount`. The change is logged
        on the template's chatter.
        """
        self.ensure_one()
        if self.days_remaining <= 0:
            raise UserError(_(
                "No days remaining in the current period; nothing to prorate. "
                "Adjust the change date or the next run date on the template."
            ))
        if not self.template_id.line_ids:
            raise UserError(_(
                "Template %s has no lines to prorate.",
            ) % self.template_id.name)
        if len(self.template_id.line_ids) > 1:
            raise UserError(_(
                "Proration on a multi-line template is ambiguous. Split the "
                "template so each plan has its own template, or apply the "
                "proration manually."
            ))

        with self.env.cr.savepoint():
            credit = self._create_credit_note()
            invoice = self._create_new_price_invoice()
            # Post both under the savepoint. Left in draft (the prior
            # behaviour) the proration credit and catch-up charge never reach
            # AR or revenue, so the price change is invisible to the ledger;
            # the savepoint keeps the pair atomic on any failure.
            (credit + invoice).action_post()
            self.template_id.line_ids[0].price_unit = self.new_amount
            self.template_id.message_post(body=_(
                "Proration applied %(date)s: credited %(credit).2f "
                "(unused %(rem)s of %(len)s days at old price), invoiced "
                "%(charge).2f at new price %(new).2f. Credit move: "
                "%(credit_ref)s; new-price invoice: %(invoice_ref)s.",
                date=self.change_date.isoformat(),
                credit=self.credit_amount, rem=self.days_remaining,
                len=self.period_length_days,
                charge=self.new_period_amount, new=self.new_amount,
                credit_ref=credit.name or credit.id,
                invoice_ref=invoice.name or invoice.id,
            ))

        return {
            'type': 'ir.actions.act_window',
            'name': _("Proration result"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('id', 'in', [credit.id, invoice.id])],
        }

    def _create_credit_note(self):
        tpl = self.template_id
        line = tpl.line_ids[0]
        AccountMove = self.env['account.move']
        line_dict = {
            'name': "Proration credit: " + (line.name or tpl.name),
            'quantity': 1.0,
            'price_unit': self.credit_amount,
            'account_id': line.account_id.id,
        }
        # Carry the template line's output tax onto the proration line, and
        # the fiscal position onto the move, exactly as the normal generation
        # path does (_build_invoice_vals). Without this the credit reverses
        # only the net and drops the GST/VAT it originally clawed back.
        if line.tax_ids:
            line_dict['tax_ids'] = [(6, 0, line.tax_ids.ids)]
        vals = {
            'move_type': 'out_refund',
            'partner_id': tpl.partner_id.id,
            'journal_id': tpl.journal_id.id,
            'invoice_date': self.change_date,
            'date': self.change_date,
            'narration': _(
                "Proration credit: unused %(rem)s of %(len)s days at "
                "previous price.",
                rem=self.days_remaining, len=self.period_length_days,
            ),
            'invoice_line_ids': [(0, 0, line_dict)],
        }
        if tpl.fiscal_position_id:
            vals['fiscal_position_id'] = tpl.fiscal_position_id.id
        return AccountMove.create(vals)

    def _create_new_price_invoice(self):
        tpl = self.template_id
        line = tpl.line_ids[0]
        AccountMove = self.env['account.move']
        line_dict = {
            'name': "Proration charge: " + (line.name or tpl.name),
            'quantity': 1.0,
            'price_unit': self.new_period_amount,
            'account_id': line.account_id.id,
        }
        # Same output tax and fiscal-position treatment as the normal
        # generation path, so the catch-up charge is a compliant tax invoice.
        if line.tax_ids:
            line_dict['tax_ids'] = [(6, 0, line.tax_ids.ids)]
        vals = {
            'move_type': 'out_invoice',
            'partner_id': tpl.partner_id.id,
            'journal_id': tpl.journal_id.id,
            'invoice_date': self.change_date,
            'date': self.change_date,
            'narration': _(
                "Proration charge at new price for %(rem)s of %(len)s days.",
                rem=self.days_remaining, len=self.period_length_days,
            ),
            'invoice_line_ids': [(0, 0, line_dict)],
        }
        if tpl.fiscal_position_id:
            vals['fiscal_position_id'] = tpl.fiscal_position_id.id
        return AccountMove.create(vals)
