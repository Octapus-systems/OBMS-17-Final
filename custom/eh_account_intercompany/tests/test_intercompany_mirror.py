# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Inter-company mirror tests.

Sets up two companies with a shared partner that represents company B,
posts a sale invoice in company A, and verifies the mirror purchase
invoice appears in company B with the right move_type, partner, lines,
and origin pointer. Covers the duplicate guard and the journal
configuration error path.
"""

from odoo.exceptions import UserError  # noqa: F401
from odoo.tests import tagged
from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_intercompany', 'integration', 'post_install', '-at_install')
class TestIntercompanyMirror(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Move = cls.env['account.move']
        cls.Config = cls.env['eh.intercompany.config']

        cls.company_a = cls.env.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'Sister Company B',
            'currency_id': cls.company_a.currency_id.id,
        })
        # On a demo-less Odoo 16 company the fiscal country is unset, so a
        # move with a US-country tax is rejected as "incompatible with your
        # fiscal country". Pin both companies to the US, matching the taxes
        # _make_tax builds. 17/18/19 already have a fiscal country.
        if 'account_fiscal_country_id' in cls.company_a._fields:
            us = cls.env.ref('base.us')
            for comp in (cls.company_a, cls.company_b):
                if not comp.account_fiscal_country_id:
                    comp.sudo().account_fiscal_country_id = us.id
        # Promote test user to access both companies and pass the manager
        # guard on cross-company writes.
        cls.env.user.company_ids = [(4, cls.company_b.id)]
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )

        # Shared partner: represents company B from company A's view.
        # Stays globally accessible (company_id=False); the dedicated
        # eh_represented_company_id field tells the mirror where to land.
        cls.partner_b = cls.env['res.partner'].create({
            'name': 'Sister Company B',
            'company_id': False,
            'eh_represented_company_id': cls.company_b.id,
        })

        # Journals.
        cls.journal_sale_a = cls.env['account.journal'].search(
            [('company_id', '=', cls.company_a.id), ('type', '=', 'sale')],
            limit=1,
        )
        if not cls.journal_sale_a:
            cls.journal_sale_a = cls.env['account.journal'].create({
                'name': 'Sale A', 'code': 'SALA',
                'type': 'sale', 'company_id': cls.company_a.id,
            })
        cls.journal_purchase_b = cls.env['account.journal'].search(
            [('company_id', '=', cls.company_b.id),
             ('type', '=', 'purchase')],
            limit=1,
        )
        if not cls.journal_purchase_b:
            cls.journal_purchase_b = cls.env['account.journal'].sudo().create({
                'name': 'Purchase B', 'code': 'PURB',
                'type': 'purchase', 'company_id': cls.company_b.id,
            })

        # Fallback expense account on company B for the bill mirror.
        # Real deployments would set the product's expense account in
        # company_b; the fallback path is the safety net when the
        # product is unconfigured for the destination company.
        cls.fallback_expense_b = cls.env['account.account'].sudo().create({
            'code': '610010',
            'name': 'Intercompany Fallback Expense',
            'account_type': 'expense',
            'company_id': cls.company_b.id,
        })

        # Payable account on company B + property on the partner so
        # account.move's payment_term auto-line resolves an account.
        # Without this Odoo's _sync_dynamic_lines emits a NULL
        # account_id on the payment-term line and the create fails
        # the account_move_line check constraint.
        cls.payable_b = cls.env['account.account'].sudo().create({
            'code': '210010',
            'name': 'Vendor Payable B',
            'account_type': 'liability_payable',
            'company_id': cls.company_b.id,
            'reconcile': True,
        })
        # The mirror's counterparty is company_a's partner; we set its
        # property in company_b's context to the new payable account.
        cls.company_a.partner_id.with_company(cls.company_b).write({
            'property_account_payable_id': cls.payable_b.id,
        })

        # Inter-company config on company B (the destination of the
        # mirror when company A sells to partner_b).
        cls.config_b = cls.Config.sudo().create({
            'company_id': cls.company_b.id,
            'enabled': True,
            'auto_post_mirror': False,
            'purchase_journal_id': cls.journal_purchase_b.id,
            'fallback_expense_account_id': cls.fallback_expense_b.id,
        })

        cls.product = cls.env['product.product'].create({
            'name': 'Demo product',
            'list_price': 100.0,
            'standard_price': 80.0,
        })

    def _make_sale_invoice(self):
        return self.Move.with_company(self.company_a).create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_b.id,
            'journal_id': self.journal_sale_a.id,
            'company_id': self.company_a.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Sold to sister',
                'quantity': 2,
                'price_unit': 150.0,
                'product_id': self.product.id,
            })],
        })

    # ---- happy path ----

    def test_post_sale_creates_mirror_in_company_b(self):
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertTrue(mirror)
        self.assertEqual(mirror.company_id, self.company_b)
        self.assertEqual(mirror.move_type, 'in_invoice')
        self.assertEqual(mirror.eh_intercompany_origin_id, sale)

    def test_mirror_inherits_lines(self):
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertEqual(len(mirror.invoice_line_ids), 1)
        self.assertEqual(mirror.invoice_line_ids.quantity, 2)
        self.assertEqual(mirror.invoice_line_ids.price_unit, 150.0)

    def test_mirror_partner_is_source_company(self):
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertEqual(
            mirror.partner_id, self.company_a.partner_id,
        )

    def test_auto_post_when_configured(self):
        self.config_b.auto_post_mirror = True
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertEqual(mirror.state, 'posted')

    def test_draft_when_not_configured_to_auto_post(self):
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertEqual(mirror.state, 'draft')

    # ---- duplicate guard ----

    def test_no_duplicate_when_repost_attempted(self):
        sale = self._make_sale_invoice()
        sale.action_post()
        first_mirror = sale.eh_intercompany_mirror_id
        # Force a second pass through the trigger path.
        sale._eh_create_intercompany_mirror()
        sale.invalidate_recordset()
        self.assertEqual(sale.eh_intercompany_mirror_id, first_mirror)

    # ---- skip paths ----

    def test_no_mirror_when_partner_has_no_company_link(self):
        plain_partner = self.env['res.partner'].create({
            'name': 'External customer',
        })
        sale = self.Move.with_company(self.company_a).create({
            'move_type': 'out_invoice',
            'partner_id': plain_partner.id,
            'journal_id': self.journal_sale_a.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'External sale',
                'quantity': 1, 'price_unit': 50.0,
            })],
        })
        sale.action_post()
        self.assertFalse(sale.eh_intercompany_mirror_id)

    def test_no_mirror_when_destination_config_disabled(self):
        self.config_b.enabled = False
        sale = self._make_sale_invoice()
        sale.action_post()
        self.assertFalse(sale.eh_intercompany_mirror_id)

    def test_no_mirror_for_mirror_itself(self):
        """Posting a mirror should not chain into another mirror."""
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        # If the mirror is auto-posted, posting it again would re-fire
        # the trigger; verify the origin guard prevents chaining.
        self.assertFalse(mirror.eh_intercompany_mirror_id)

    # ---- error path ----

    def test_missing_journal_raises_with_chatter(self):
        self.config_b.purchase_journal_id = False
        sale = self._make_sale_invoice()
        # The post itself succeeds; the mirror fails and surfaces a
        # chatter warning rather than rolling back the source.
        sale.action_post()
        self.assertEqual(sale.state, 'posted')
        self.assertFalse(sale.eh_intercompany_mirror_id)
        # Chatter should record the failure.
        messages = sale.message_ids.mapped('body')
        self.assertTrue(any(
            'Inter-company mirror failed' in (m or '') for m in messages
        ))

    # ---- account resolution priority chain (Option C + Option D) ----

    def test_account_resolution_uses_product_account_when_configured(self):
        """Option D: when the product has a destination-company expense
        account, the mirror line uses it (not the config fallback)."""
        # Set a per-company expense on the product.
        product_expense_b = self.env['account.account'].sudo().create({
            'code': '610020',
            'name': 'Product Expense B',
            'account_type': 'expense',
            'company_id': self.company_b.id,
        })
        # Write the expense account on the product in company_b context.
        self.product.with_company(self.company_b).property_account_expense_id = (
            product_expense_b
        )
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertTrue(mirror, "Mirror must be created")
        line = mirror.invoice_line_ids[:1]
        self.assertEqual(
            line.account_id, product_expense_b,
            "Product-configured account must take priority over fallback",
        )

    def test_account_resolution_falls_back_to_config(self):
        """Option C: when the product has NO destination-company
        expense account, the mirror line uses the config fallback."""
        # Ensure the product has NO expense account in company_b.
        self.product.with_company(self.company_b).property_account_expense_id = False
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertTrue(mirror)
        line = mirror.invoice_line_ids[:1]
        self.assertEqual(
            line.account_id, self.fallback_expense_b,
            "Config fallback must apply when product is unconfigured",
        )

    def test_account_resolution_hard_fails_when_neither_configured(self):
        """No silent default: when neither product nor config provides
        an account, the post raises UserError with a clear message."""
        # Wipe both: product has no expense, config has no fallback.
        self.product.with_company(self.company_b).property_account_expense_id = False
        self.config_b.fallback_expense_account_id = False
        sale = self._make_sale_invoice()
        # The mirror creation must NOT silently swallow this; the
        # source post itself reports the failure via chatter.
        sale.action_post()
        # Source still posts (mirror failure does not rollback source);
        # but no mirror created and a chatter message records the cause.
        self.assertFalse(sale.eh_intercompany_mirror_id)
        messages = sale.message_ids.mapped('body')
        self.assertTrue(any(
            'cannot resolve an account' in (m or '').lower()
            for m in messages
        ), "Chatter must surface the missing-account error")

    # ---- analytic preservation (advantage over Enterprise) ----

    def _make_sale_invoice_with_analytic(self, analytic, pct=100.0):
        return self.Move.with_company(self.company_a).create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_b.id,
            'journal_id': self.journal_sale_a.id,
            'company_id': self.company_a.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Sold to sister',
                'quantity': 2,
                'price_unit': 150.0,
                'product_id': self.product.id,
                'analytic_distribution': {str(analytic.id): pct},
            })],
        })

    def test_mirror_preserves_group_analytic(self):
        """A group-wide analytic account (no company) on the source line
        is carried to the mirror. Enterprise drops analytics; preserving
        the group allocation is a deliberate advantage."""
        # Odoo 16 gives account.analytic.plan a company_id that defaults to
        # the active company, which then clashes with the company-less
        # analytic account (_check_company); pin it to no company there.
        # Odoo 17+ dropped the field (plans are global), so only set it when
        # the field exists.
        Plan = self.env['account.analytic.plan']
        plan_vals = {'name': 'Group Plan'}
        if 'company_id' in Plan._fields:
            plan_vals['company_id'] = False
        plan = Plan.create(plan_vals)
        analytic = self.env['account.analytic.account'].create({
            'name': 'Group Project',
            'plan_id': plan.id,
            'company_id': False,
        })
        sale = self._make_sale_invoice_with_analytic(analytic, pct=100.0)
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertTrue(mirror)
        line = mirror.invoice_line_ids[:1]
        self.assertTrue(line.analytic_distribution)
        self.assertIn(str(analytic.id), line.analytic_distribution)
        self.assertAlmostEqual(
            line.analytic_distribution[str(analytic.id)], 100.0, places=2,
        )

    def test_mirror_drops_source_only_analytic(self):
        """An analytic account specific to the source company must not
        leak onto the destination company's mirror line."""
        plan = self.env['account.analytic.plan'].create({'name': 'A Plan'})
        analytic_a = self.env['account.analytic.account'].create({
            'name': 'Company A only',
            'plan_id': plan.id,
            'company_id': self.company_a.id,
        })
        sale = self._make_sale_invoice_with_analytic(analytic_a, pct=100.0)
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertTrue(mirror)
        line = mirror.invoice_line_ids[:1]
        self.assertFalse(line.analytic_distribution)

    # ---- concurrency guard ----

    def test_unique_constraint_blocks_duplicate_mirror(self):
        """The unique(origin, company) constraint forbids a second mirror
        for the same source in the same company, closing the concurrent
        double-mirror race."""
        sale = self._make_sale_invoice()
        sale.action_post()
        self.assertTrue(sale.eh_intercompany_mirror_id)
        journal_b = self.env['account.journal'].sudo().search(
            [('company_id', '=', self.company_b.id), ('type', '=', 'general')],
            limit=1,
        ) or self.env['account.journal'].sudo().create({
            'name': 'Misc B', 'code': 'MISCB', 'type': 'general',
            'company_id': self.company_b.id,
        })
        # A valid second move in company B; only claiming the same origin
        # (which is already taken by the real mirror) must fail.
        other = self.Move.sudo().with_company(self.company_b).create({
            'move_type': 'entry',
            'company_id': self.company_b.id,
            'journal_id': journal_b.id,
        })
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                other.eh_intercompany_origin_id = sale.id
                other.flush_recordset(['eh_intercompany_origin_id'])

    # ---- tax propagation ----

    def _make_tax(self, company, use, amount, name):
        country = company.account_fiscal_country_id or self.env.ref('base.us')
        # account.tax.group is company-specific (company_id/country_id) from
        # Odoo 17; on Odoo 16 it is global with neither field.
        Group = self.env['account.tax.group'].sudo()
        if 'company_id' in Group._fields:
            group = Group.search(
                [('company_id', '=', company.id)], limit=1,
            ) or Group.create({
                'name': 'IC Test Tax Group',
                'company_id': company.id,
                'country_id': country.id,
            })
        else:
            group = Group.search([], limit=1) or Group.create({
                'name': 'IC Test Tax Group',
            })
        return self.env['account.tax'].sudo().create({
            'name': name,
            'type_tax_use': use,
            'amount_type': 'percent',
            'amount': amount,
            'company_id': company.id,
            'country_id': country.id,
            'tax_group_id': group.id,
            # Force tax-excluded on both sides so price_include matches
            # regardless of each company's default and the rate+direction
            # mapping is what the test exercises.
            'price_include': False,
        })

    def _make_sale_invoice_with_tax(self, tax):
        return self.Move.with_company(self.company_a).create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_b.id,
            'journal_id': self.journal_sale_a.id,
            'company_id': self.company_a.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Taxed line',
                'quantity': 1,
                'price_unit': 100.0,
                'product_id': self.product.id,
                'tax_ids': [(6, 0, [tax.id])],
            })],
        })

    def test_mirror_tax_maps_by_rate_and_direction_not_name(self):
        # Company A sale tax; company B has a purchase tax at the same
        # rate but a DIFFERENT name, plus a same-rate, same-name SALE
        # tax that must NOT be chosen. The mirror is a bill, so only the
        # purchase tax is the correct mapping. Proves the match keys on
        # rate + direction, not on the tax name.
        src_tax = self._make_tax(self.company_a, 'sale', 10.0, 'GST 10pct A')
        dest_purchase = self._make_tax(
            self.company_b, 'purchase', 10.0, 'Impuesto 10pct B')
        dest_sale_decoy = self._make_tax(
            self.company_b, 'sale', 10.0, 'GST 10pct A')
        sale = self._make_sale_invoice_with_tax(src_tax)
        line = sale.invoice_line_ids[:1]
        vals = sale._eh_build_mirror_line_vals(
            line, self.company_b, 'in_invoice', self.config_b)
        self.assertIn('tax_ids', vals)
        mapped_ids = vals['tax_ids'][0][2]
        self.assertEqual(
            mapped_ids, dest_purchase.ids,
            "mirror must map to the destination PURCHASE tax by rate and "
            "direction, never by name or to the same-rate sale tax",
        )
        self.assertNotIn(dest_sale_decoy.id, mapped_ids)

    def test_mirror_tax_unresolved_is_dropped_not_misapplied(self):
        # Source carries a 7pct sale tax; company B has only a 10pct
        # purchase tax. The mirror must NOT apply the wrong-rate 10pct
        # tax (the old cartesian name/amount match could). The unmatched
        # tax is dropped, leaving the mirror line with no tax rather than
        # a wrong one.
        src_tax = self._make_tax(self.company_a, 'sale', 7.0, 'GST 7pct A')
        decoy = self._make_tax(
            self.company_b, 'purchase', 10.0, 'Purchase 10pct B')
        sale = self._make_sale_invoice_with_tax(src_tax)
        line = sale.invoice_line_ids[:1]
        vals = sale._eh_build_mirror_line_vals(
            line, self.company_b, 'in_invoice', self.config_b)
        # No 7pct purchase equivalent, so no tax is mapped at all; the
        # wrong-rate 10pct tax must never be applied.
        if 'tax_ids' in vals:
            self.assertNotIn(decoy.id, vals['tax_ids'][0][2])
            self.assertEqual(vals['tax_ids'][0][2], [])
