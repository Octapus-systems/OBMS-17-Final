# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.partner extension: per-partner credit limit override and live exposure.

Adds an optional eh_credit_limit override and an optional policy
override on top of the company default. Computed eh_credit_exposure
and eh_credit_status surface the partner's live position so the
sales team can see the gate state on the partner form before the
invoice is even drafted.
"""

from odoo import api, fields, models
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_credit_limit = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Per-partner credit limit override. Zero means use the "
            "company default policy. Negative values are not allowed."
        ),
    )
    eh_credit_policy_id = fields.Many2one(
        'eh.credit.policy',
        domain="[]",
        help=(
            "Per-partner policy override. When set, takes precedence "
            "over the company default. Useful for known-risk customers "
            "that need stricter enforcement than the default policy "
            "applies."
        ),
    )

    eh_credit_exposure_currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_eh_credit_exposure_currency',
        help="Company currency exposure and limit are expressed in.",
    )
    eh_credit_exposure = fields.Monetary(
        compute='_compute_eh_credit_exposure',
        currency_field='eh_credit_exposure_currency_id',
        help=(
            "Sum of unpaid open AR plus, when the effective policy "
            "says so, draft customer invoices and not-yet-invoiced "
            "sale orders. Multi-currency lines are converted to the "
            "company currency at the rate captured on each document. "
            "Live; recomputes on every form load."
        ),
    )
    eh_effective_credit_limit = fields.Monetary(
        compute='_compute_eh_credit_exposure',
        currency_field='eh_credit_exposure_currency_id',
        help=(
            "Per-partner override if set, else the company default "
            "policy's default_credit_limit. Zero means no enforcement. "
            "Expressed in the company currency to match exposure."
        ),
    )
    eh_credit_status = fields.Selection(
        [
            ('ok', "OK"),
            ('warn', "Warning"),
            ('over', "Over limit"),
            ('disabled', "No policy"),
        ],
        compute='_compute_eh_credit_exposure',
        help=(
            "OK: exposure under 80 percent of limit. Warning: between "
            "80 and 100 percent. Over: above the limit. Disabled: no "
            "company policy in place."
        ),
    )

    @api.depends('company_id')
    def _compute_eh_credit_exposure_currency(self):
        for partner in self:
            company = partner.company_id or self.env.company
            partner.eh_credit_exposure_currency_id = company.currency_id

    @api.depends('credit_limit', 'eh_credit_limit', 'eh_credit_policy_id')
    def _compute_eh_credit_exposure(self):
        for partner in self:
            policy = partner._eh_resolve_credit_policy()
            if not policy:
                partner.eh_credit_exposure = 0.0
                partner.eh_effective_credit_limit = 0.0
                partner.eh_credit_status = 'disabled'
                continue
            limit = partner.eh_credit_limit or policy.default_credit_limit
            partner.eh_effective_credit_limit = limit
            partner.eh_credit_exposure = partner._eh_compute_exposure(policy)
            if not limit:
                partner.eh_credit_status = 'disabled'
            elif partner.eh_credit_exposure > limit:
                partner.eh_credit_status = 'over'
            elif partner.eh_credit_exposure > limit * 0.80:
                partner.eh_credit_status = 'warn'
            else:
                partner.eh_credit_status = 'ok'

    def _eh_resolve_credit_policy(self):
        """Return the policy that governs this partner.

        Per-partner override takes precedence; otherwise the company's
        default policy. Returns empty recordset when no policy is set
        for the partner's company.
        """
        self.ensure_one()
        if self.eh_credit_policy_id:
            return self.eh_credit_policy_id
        if not self.company_id:
            # Global partners use whichever company is current; sales
            # write a partner.company_id when they create one.
            return self.env['eh.credit.policy'].find_for_company(
                self.env.company,
            )
        return self.env['eh.credit.policy'].find_for_company(
            self.company_id,
        )

    def _eh_compute_exposure(self, policy):
        """Sum of open AR plus optional drafts and confirmed-but-unbilled
        sales orders for this partner, expressed in the company's
        currency.

        Multi-currency: AR uses amount_residual (already in company
        currency on account.move.line). Draft moves use
        amount_total_signed (company currency, refunds negative). Sale
        orders convert per-record from order currency to company
        currency via res.currency._convert at order_date so a USD
        order on an AUD company contributes its AUD value, not the
        raw USD figure.
        """
        self.ensure_one()
        AML = self.env['account.move.line'].sudo()
        company = self.company_id or self.env.company
        company_currency = company.currency_id
        domain = [
            ('partner_id', '=', self.commercial_partner_id.id),
            ('account_id.account_type', '=', 'asset_receivable'),
            ('parent_state', '=', 'posted'),
            ('amount_residual', '!=', 0),
            ('company_id', '=', company.id),
        ]
        # amount_residual on account.move.line is already in the company
        # currency: a EUR receivable on an AUD company sums its AUD
        # equivalent at the exchange rate captured on the move.
        posted_rows = read_group_compat(AML, domain, [], ['amount_residual:sum'])
        total = posted_rows[0][0] if posted_rows else 0.0

        if policy.include_drafts:
            Move = self.env['account.move'].sudo()
            # amount_total_signed is in company currency and signs
            # refunds negative, so a draft refund reduces exposure
            # rather than inflating it.
            draft_rows = read_group_compat(Move, 
                [
                    ('partner_id', '=', self.commercial_partner_id.id),
                    ('move_type', 'in', ('out_invoice', 'out_refund')),
                    ('state', '=', 'draft'),
                    ('company_id', '=', company.id),
                ],
                [],
                ['amount_total_signed:sum'],
            )
            total += draft_rows[0][0] if draft_rows else 0.0

        # Sales orders contribute the not-yet-invoiced portion of any
        # confirmed-and-not-cancelled order. Convert each order from its
        # own currency to company currency at the order's effective
        # date so multi-currency exposure is comparable to the AR side.
        if policy.include_sale_orders and 'sale.order' in self.env:
            SaleOrder = self.env['sale.order'].sudo()
            so_domain = [
                ('partner_id', '=', self.commercial_partner_id.id),
                ('state', 'in', ('sale', 'done')),
                ('company_id', '=', company.id),
            ]
            orders = SaleOrder.search(so_domain)
            for order in orders:
                if 'amount_to_invoice' in order._fields:
                    raw = order.amount_to_invoice or 0.0
                else:
                    invoiced = getattr(order, 'amount_invoiced', 0.0) or 0.0
                    raw = max((order.amount_total or 0.0) - invoiced, 0.0)
                if not raw:
                    continue
                order_currency = order.currency_id or company_currency
                if order_currency == company_currency:
                    total += raw
                else:
                    rate_date = (
                        getattr(order, 'date_order', False)
                        or fields.Date.context_today(self)
                    )
                    total += order_currency._convert(
                        raw, company_currency, company, rate_date,
                    )

        return total

    def action_view_eh_credit_status(self):
        """Open the override audit log filtered to this partner.

        Surfaced from the credit stat-button so a user clicking
        through gets the historical context (every override that
        bumped this partner past the limit, who authorised it, why)
        rather than just the current-state badge.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Credit overrides',
            'res_model': 'eh.credit.override.log',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('partner_id', '=', self.id)],
            'context': {
                'search_default_partner_id': self.id,
            },
        }
