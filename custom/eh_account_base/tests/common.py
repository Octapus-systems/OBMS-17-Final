# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Shared test fixtures for the ERP Heritage accounting suite.

Two base classes:

* EhAccountUnitTestCase: pure unit tests, no DB seeding beyond what TransactionCase
  already provides. Use for SQL builder shape assertions, cache layer tests,
  options canonicalisation tests.
* EhAccountIntegrationTestCase: integration tests that need a chart of accounts
  and a posted journal entry. The setUpClass seeds a minimal CoA and a
  reusable balanced entry.
"""

from odoo import fields
from odoo.tests import TransactionCase


class EhAccountUnitTestCase(TransactionCase):
    """Lightweight base class. No accounting fixtures.

    Suitable for tests that exercise pure Python helpers (SQL builder, cache,
    canonicalisation) and only need self.env.cr to be available.
    """


class EhAccountIntegrationTestCase(TransactionCase):
    """Integration base class with a seeded chart of accounts and partners."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        # Odoo 16 refuses to post a mail message when the author has no email
        # ("Unable to send message, please configure the sender's email
        # address"); 17+ tolerate it. Workflows here post chatter/activities,
        # so give the acting user and company a sender address.
        if not cls.env.user.email:
            cls.env.user.email = 'eh-tester@example.com'
        if not cls.company.email:
            cls.company.email = 'eh-company@example.com'
        # Pin the company currency to USD so currency-sensitive tests behave
        # identically across series: Odoo 16 defaults the company currency to
        # EUR while 17/18/19 default to USD, which otherwise makes a test that
        # writes an EUR rate skip it (a company never rates its own currency).
        # Safe here because no journal entry exists yet.
        usd = cls.env.ref('base.USD')
        if not usd.active:
            usd.sudo().write({'active': True})
        if cls.company.currency_id != usd:
            cls.company.sudo().write({'currency_id': usd.id})
        # Bank journals inherit their suspense and outstanding-payment
        # accounts from these company defaults. On a demo-less Odoo 16
        # company they are unset, so bank statement lines and payments are
        # refused ("no Suspense Account configured"). Provision them so any
        # bank journal a test creates works; 17/18/19 already have them.
        suspense = cls._ensure_account(
            cls.env, '1099', 'Bank Suspense', 'asset_current')
        # A bank journal's suspense account is reconcilable in real Odoo;
        # reclassifying a statement line's residual clears the counter-leg
        # against the original suspense line, which requires reconciliation.
        # Provision it as such here so fixtures mirror a correctly
        # configured chart of accounts.
        if not suspense.reconcile:
            suspense.sudo().reconcile = True
        outstanding = cls._ensure_account(
            cls.env, '1098', 'Outstanding Payments', 'asset_current')
        # Outstanding receipt/payment accounts must be reconcilable: Odoo
        # validates that a company payment debit/credit account is a
        # reconcilable account.
        if not outstanding.reconcile:
            outstanding.sudo().reconcile = True
        comp_vals = {}
        comp_fields = cls.company._fields
        if ('account_journal_suspense_account_id' in comp_fields
                and not cls.company.account_journal_suspense_account_id):
            comp_vals['account_journal_suspense_account_id'] = suspense.id
        for fname in ('account_journal_payment_debit_account_id',
                      'account_journal_payment_credit_account_id'):
            if fname in comp_fields and not cls.company[fname]:
                comp_vals[fname] = outstanding.id
        if comp_vals:
            cls.company.sudo().write(comp_vals)

        cls.account_receivable = cls._ensure_account(
            cls.env, '1100', 'Trade Receivables', 'asset_receivable',
        )
        cls.account_payable = cls._ensure_account(
            cls.env, '2100', 'Trade Payables', 'liability_payable',
        )
        cls.account_revenue = cls._ensure_account(
            cls.env, '4000', 'Sales Revenue', 'income',
        )
        cls.account_expense = cls._ensure_account(
            cls.env, '5000', 'Cost of Sales', 'expense',
        )
        cls.account_cash = cls._ensure_account(
            cls.env, '1000', 'Cash on Hand', 'asset_cash',
        )
        cls.account_equity = cls._ensure_account(
            cls.env, '3000', 'Owner Equity', 'equity',
        )

        cls.journal_misc = cls._ensure_journal(
            cls.env, cls.company, 'general', 'MISC', 'Miscellaneous',
        )
        # Odoo 17/18/19 auto-provision sale/purchase journals on the company;
        # Odoo 16 with --without-demo does not, so tests that create customer
        # invoices or vendor bills fail at posting with "No journal ... for
        # sale/purchase". Provision them explicitly (search-first, so nothing
        # changes on versions that already have them).
        cls.journal_sale = cls._ensure_journal(
            cls.env, cls.company, 'sale', 'INV', 'Customer Invoices',
            default_account=cls.account_revenue,
        )
        cls.journal_purchase = cls._ensure_journal(
            cls.env, cls.company, 'purchase', 'BILL', 'Vendor Bills',
            default_account=cls.account_expense,
        )

        # On a demo-less Odoo 16/17 company there is no chart-of-accounts
        # template, so partners have no default receivable/payable account
        # ("Partner X has no receivable account configured"). Set the
        # company-wide property defaults so every partner (existing and new)
        # resolves to these accounts. Odoo 18 removed ir.property (replaced by
        # company-dependent fields) and provisions these differently, so this
        # only runs where ir.property exists.
        if 'ir.property' in cls.env.registry:
            IrProperty = cls.env['ir.property'].sudo()
            for prop_name, account in (
                ('property_account_receivable_id', cls.account_receivable),
                ('property_account_payable_id', cls.account_payable),
            ):
                if not IrProperty._get(prop_name, 'res.partner'):
                    IrProperty._set_default(
                        prop_name, 'res.partner', account, company=cls.company)
            # Product category income/expense defaults, so an invoice line
            # with a product resolves an account on a demo-less 16 company.
            for prop_name, account in (
                ('property_account_income_categ_id', cls.account_revenue),
                ('property_account_expense_categ_id', cls.account_expense),
            ):
                if not IrProperty._get(prop_name, 'product.category'):
                    IrProperty._set_default(
                        prop_name, 'product.category', account,
                        company=cls.company)

        cls.partner_a = cls.env['res.partner'].create({'name': 'Test Partner A'})
        cls.partner_b = cls.env['res.partner'].create({'name': 'Test Partner B'})

    @staticmethod
    def _ensure_account(env, code, name, account_type):
        # account.account became multi-company (company_ids, Many2many) in
        # Odoo 18; before that it carries a single company_id. Resolve the
        # field at runtime so the helper works across series.
        Account = env['account.account']
        multi = 'company_ids' in Account._fields
        company_field = 'company_ids' if multi else 'company_id'
        company_value = (
            [(6, 0, env.company.ids)] if multi else env.company.id)
        existing = Account.search(
            [
                ('code', '=', code),
                (company_field, 'in', env.company.ids),
            ],
            limit=1,
        )
        if existing:
            return existing
        vals = {
            'code': code,
            'name': name,
            'account_type': account_type,
            company_field: company_value,
        }
        # Reconcilable types must carry reconcile=True for amount_residual to
        # compute correctly. Aged receivable/payable tests rely on this.
        if account_type in (
            'asset_receivable', 'liability_payable', 'liability_credit_card',
        ):
            vals['reconcile'] = True
        return env['account.account'].create(vals)

    @staticmethod
    def _ensure_journal(env, company, jtype, code, name, default_account=None):
        """Return the company's journal of the given type, creating a minimal
        one if the framework did not provision it (Odoo 16 without demo)."""
        Journal = env['account.journal']
        journal = Journal.search(
            [('company_id', '=', company.id), ('type', '=', jtype)], limit=1,
        )
        if journal:
            return journal
        vals = {
            'name': name, 'code': code, 'type': jtype,
            'company_id': company.id,
        }
        if default_account is not None:
            vals['default_account_id'] = default_account.id
        return Journal.create(vals)

    @classmethod
    def post_balanced_move(cls, lines, journal=None, date=None):
        """Helper: create and post a balanced journal entry.

        :param lines: list of dicts with keys: account (required), debit,
            credit, partner, name, date_maturity.
        :param journal: optional account.journal record (defaults to misc).
        :param date: optional date (defaults to today).
        :return: the posted account.move record.
        """
        journal = journal or cls.journal_misc
        date = date or fields.Date.today()
        line_vals = []
        for line in lines:
            vals = {
                'account_id': line['account'].id,
                'debit': line.get('debit', 0.0),
                'credit': line.get('credit', 0.0),
                'partner_id': line['partner'].id if line.get('partner') else False,
                'name': line.get('name', '/'),
            }
            if 'date_maturity' in line:
                vals['date_maturity'] = line['date_maturity']
            line_vals.append((0, 0, vals))
        move = cls.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'date': date,
            'line_ids': line_vals,
        })
        move.action_post()
        return move
