# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bridge: recurring invoice templates -> IFRS 15 revenue contracts.

A template optionally links a revenue contract. When linked, every invoice
the template generates routes its revenue lines to the contract's contract
liability account instead of the line income account, and posting the
invoice registers the billed amount on the contract, mirroring the
contract's own Record Billing convention:

* the receivable is debited by the invoice itself (core behaviour);
* the credit lands on the contract liability account (deferred revenue);
* if the contract carries a contract asset (revenue recognised ahead of
  billing), a balanced reclassification entry moves the asset-clearing
  portion out of the liability, so the general ledger shows exactly the
  same split Record Billing would have produced (credit the contract asset
  first, then the contract liability);
* contract.amount_billed advances, so the recognition run releases the
  liability as performance obligations are satisfied.

The contract-side bookkeeping runs as superuser: the invoice may be posted
by a billing user who has no access to the revenue contract models, and the
registration is a deterministic system consequence of a manager-approved
template link, not a discretionary action. Unlinked templates are untouched
(byte-identical default).
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhRecurringInvoiceTemplate(models.Model):
    _inherit = 'eh.recurring.invoice.template'

    revenue_contract_id = fields.Many2one(
        'eh.revenue.contract', string="Revenue Contract", tracking=True,
        domain="[]",
        help="Optional IFRS 15 revenue contract behind this recurring "
             "billing. When set, generated invoices credit the contract's "
             "contract liability account (deferred revenue) instead of the "
             "line income accounts, and the billed amount registers on the "
             "contract so the recognition run releases it as performance "
             "obligations are satisfied.")

    @api.constrains('revenue_contract_id', 'company_id')
    def _check_revenue_contract_company(self):
        for tpl in self:
            if (tpl.revenue_contract_id
                    and tpl.revenue_contract_id.company_id != tpl.company_id):
                raise ValidationError(_(
                    "The linked revenue contract must belong to the "
                    "template's company."))

    def _build_invoice_vals(self):
        vals = super()._build_invoice_vals()
        contract = self.revenue_contract_id
        if not contract:
            return vals
        if contract.state != 'active':
            raise UserError(_(
                "Template %(template)s is linked to revenue contract "
                "%(contract)s, which is not active. Activate the contract "
                "or unlink it before generating.",
                template=self.name, contract=contract.display_name))
        if not contract.contract_liability_account_id:
            raise UserError(_(
                "Configure the contract liability account on revenue "
                "contract %s before generating linked invoices.",
                contract.display_name))
        for command in vals.get('invoice_line_ids', []):
            command[2]['account_id'] = (
                contract.contract_liability_account_id.id)
        vals['eh_revenue_contract_id'] = contract.id
        return vals


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_revenue_billing_registered = fields.Boolean(
        readonly=True, copy=False,
        help="This generated invoice has already registered its billed "
             "amount on the linked revenue contract; kept so a reset and "
             "repost cannot double-count the billing.")

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        for move in posted:
            move._eh_register_revenue_contract_billing()
        return posted

    def _eh_register_revenue_contract_billing(self):
        """Register a posted, template-generated customer invoice as billing
        on its linked revenue contract, replicating the contract's Record
        Billing split (credit the contract asset first, then the contract
        liability). Idempotent per invoice via the registered flag. A credit
        note or manual reversal is not auto-registered; correct the contract
        position through its own billing flow."""
        self.ensure_one()
        if (self.move_type != 'out_invoice'
                or not self.eh_revenue_contract_id
                or not self.eh_recurring_template_id
                or self.eh_revenue_billing_registered):
            return False
        contract = self.eh_revenue_contract_id.sudo()
        currency = contract.currency_id
        amount = currency.round(self.amount_untaxed)
        if currency.compare_amounts(amount, 0.0) <= 0:
            return False
        # The invoice credited the full untaxed amount to the contract
        # liability. Record Billing would have credited the contract asset
        # first (revenue recognised ahead of billing) and only the remainder
        # to the liability, so reclassify the asset-clearing portion for a
        # general ledger identical to the contract's own convention.
        asset_before = max(
            contract.amount_recognised - contract.amount_billed, 0.0)
        clear = currency.round(min(amount, asset_before))
        if currency.compare_amounts(clear, 0.0) > 0:
            contract._post_move([
                (0, 0, {
                    'name': _("Recurring billing reclass %s", self.name),
                    'account_id': contract.contract_liability_account_id.id,
                    'debit': clear, 'credit': 0.0,
                }),
                (0, 0, {
                    'name': _("Contract asset billed %s", self.name),
                    'account_id': contract.contract_asset_account_id.id,
                    'debit': 0.0, 'credit': clear,
                }),
            ])
        contract.amount_billed += amount
        self.sudo().eh_revenue_billing_registered = True
        return True
