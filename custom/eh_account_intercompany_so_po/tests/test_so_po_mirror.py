# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Inter-company SO/PO mirroring tests.

Two companies; a partner that represents company B. Confirming a sales
order in company A drafts a purchase order in B, and confirming a
purchase order in A drafts a sales order in B. A mirror is never
mirrored back, and non-inter-company partners never trigger a mirror.
"""

from odoo.tests import TransactionCase, tagged


@tagged('eh_account_intercompany_so_po', 'integration', 'post_install',
        '-at_install')
class TestSoPoMirror(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'SoPo Sister B',
            'currency_id': cls.company_a.currency_id.id,
        })
        cls.env.user.company_ids = [(4, cls.company_b.id)]
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')

        cls.partner_b = cls.env['res.partner'].create({
            'name': 'SoPo Sister B partner',
            'company_id': False,
            'eh_represented_company_id': cls.company_b.id,
        })
        cls.normal_partner = cls.env['res.partner'].create({
            'name': 'Ordinary customer',
        })
        cls.env['eh.intercompany.config'].sudo().create({
            'company_id': cls.company_b.id,
            'enabled': True,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'IC product',
            'list_price': 100.0, 'standard_price': 80.0,
        })

    def _sale_order(self, partner):
        return self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': partner.id,
            'company_id': self.company_a.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'name': 'IC sale line',
                'product_uom_qty': 3.0,
                'price_unit': 120.0,
            })],
        })

    def _purchase_order(self, partner):
        return self.env['purchase.order'].with_company(self.company_a).create({
            'partner_id': partner.id,
            'company_id': self.company_a.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'name': 'IC purchase line',
                'product_qty': 5.0,
                'price_unit': 90.0,
                'product_uom': self.product.uom_id.id,
            })],
        })

    # ---- SO -> PO ----

    def test_so_confirm_creates_mirror_po(self):
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        po = sale.eh_ic_mirror_po_id
        self.assertTrue(po)
        self.assertEqual(po.company_id, self.company_b)
        self.assertEqual(po.partner_id, self.company_a.partner_id)
        self.assertEqual(po.eh_ic_origin_so_id, sale)
        self.assertEqual(po.state, 'draft')
        self.assertEqual(len(po.order_line), 1)
        self.assertEqual(po.order_line.product_qty, 3.0)
        self.assertEqual(po.order_line.price_unit, 120.0)

    def test_mirror_po_carries_source_uom_and_converted_price(self):
        """A source line in a non-default UoM and a cross-currency order
        must produce a mirror PO line that keeps the SOURCE line UoM (not
        the product default) and a price converted into the destination
        company currency."""
        # A second company/currency for the mirror so a raw price copy
        # would be visibly wrong.
        gbp = self.env.ref('base.GBP')
        eur = self.env.ref('base.EUR')
        cross_company = self.env['res.company'].create({
            'name': 'SoPo Cross-currency B',
            'currency_id': gbp.id,
        })
        self.env.user.company_ids = [(4, cross_company.id)]
        self.company_a.currency_id = eur
        # A deterministic EUR->GBP rate on the order date.
        self.env['res.currency.rate'].create({
            'name': '2026-01-01',
            'currency_id': gbp.id,
            'company_id': cross_company.id,
            'rate': 2.0,
        })
        partner = self.env['res.partner'].create({
            'name': 'SoPo Cross partner',
            'company_id': False,
            'eh_represented_company_id': cross_company.id,
        })
        self.env['eh.intercompany.config'].sudo().create({
            'company_id': cross_company.id,
            'enabled': True,
        })
        # A UoM that is NOT the product default (dozen vs unit).
        dozen = self.env.ref('uom.product_uom_dozen')
        self.assertNotEqual(dozen, self.product.uom_id)
        sale = self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': partner.id,
            'company_id': self.company_a.id,
            'date_order': '2026-06-01',
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'name': 'IC cross line',
                'product_uom_qty': 2.0,
                'product_uom': dozen.id,
                'price_unit': 50.0,
            })],
        })
        source_uom = (getattr(sale.order_line, 'product_uom_id', False)
                      or getattr(sale.order_line, 'product_uom', False))
        self.assertEqual(source_uom, dozen)
        sale.action_confirm()
        po = sale.eh_ic_mirror_po_id
        self.assertTrue(po)
        self.assertEqual(po.company_id, cross_company)
        po_uom = (getattr(po.order_line, 'product_uom_id', False)
                  or getattr(po.order_line, 'product_uom', False))
        # Mirror keeps the SOURCE line UoM, not the product default.
        self.assertEqual(po_uom, dozen)
        # 50.0 EUR at rate 2.0 -> 100.0 GBP, not the raw 50.0.
        self.assertAlmostEqual(po.order_line.price_unit, 100.0, places=2)

    def test_no_mirror_for_ordinary_partner(self):
        sale = self._sale_order(self.normal_partner)
        sale.action_confirm()
        self.assertFalse(sale.eh_ic_mirror_po_id)

    def test_mirror_po_uom_write_key_is_version_correct(self):
        """The mirror WRITE side must set the UoM under the field name the
        TARGET line model actually exposes: product_uom_id on 18/19,
        product_uom on 16/17. If the write key were hardcoded to
        product_uom_id the create() would break (or silently drop the UoM)
        on 16/17. Confirming a sale must therefore produce a PO whose line
        carries the source UoM read through the version-correct field."""
        # A UoM that is NOT the product default so a dropped/ignored write
        # would leave the mirror on the product default and fail the check.
        dozen = self.env.ref('uom.product_uom_dozen')
        self.assertNotEqual(dozen, self.product.uom_id)
        pol_key = ('product_uom_id'
                   if 'product_uom_id'
                   in self.env['purchase.order.line']._fields
                   else 'product_uom')
        so_line_key = ('product_uom_id'
                       if 'product_uom_id'
                       in self.env['sale.order.line']._fields
                       else 'product_uom')
        sale = self.env['sale.order'].with_company(self.company_a).create({
            'partner_id': self.partner_b.id,
            'company_id': self.company_a.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'name': 'IC uom line',
                'product_uom_qty': 4.0,
                'price_unit': 30.0,
                so_line_key: dozen.id,
            })],
        })
        sale.action_confirm()
        po = sale.eh_ic_mirror_po_id
        self.assertTrue(po)
        # Read the mirror UoM through the target field name and confirm it
        # equals the source UoM: proves the write key resolved correctly.
        mirror_uom = getattr(po.order_line, pol_key)
        self.assertEqual(mirror_uom, dozen)

    def test_mirror_so_uom_write_key_is_version_correct(self):
        """Symmetric to the SO->PO case: confirming a purchase must set the
        mirror sale line UoM under the field the sale.order.line model
        actually exposes (product_uom_id on 18/19, product_uom on 16/17),
        not a hardcoded product_uom_id that would break on 16/17."""
        dozen = self.env.ref('uom.product_uom_dozen')
        self.assertNotEqual(dozen, self.product.uom_id)
        pol_key = ('product_uom_id'
                   if 'product_uom_id'
                   in self.env['purchase.order.line']._fields
                   else 'product_uom')
        purchase = self.env['purchase.order'].with_company(
            self.company_a).create({
                'partner_id': self.partner_b.id,
                'company_id': self.company_a.id,
                'order_line': [(0, 0, {
                    'product_id': self.product.id,
                    'name': 'IC uom purchase line',
                    'product_qty': 6.0,
                    'price_unit': 20.0,
                    pol_key: dozen.id,
                })],
            })
        purchase.button_confirm()
        sale = purchase.eh_ic_mirror_so_id
        self.assertTrue(sale)
        so_line_key = ('product_uom_id'
                       if 'product_uom_id'
                       in self.env['sale.order.line']._fields
                       else 'product_uom')
        mirror_uom = getattr(sale.order_line, so_line_key)
        self.assertEqual(mirror_uom, dozen)

    # ---- PO -> SO ----

    def test_po_confirm_creates_mirror_so(self):
        purchase = self._purchase_order(self.partner_b)
        purchase.button_confirm()
        sale = purchase.eh_ic_mirror_so_id
        self.assertTrue(sale)
        self.assertEqual(sale.company_id, self.company_b)
        self.assertEqual(sale.partner_id, self.company_a.partner_id)
        self.assertEqual(sale.eh_ic_origin_po_id, purchase)
        self.assertEqual(len(sale.order_line), 1)
        self.assertEqual(sale.order_line.product_uom_qty, 5.0)

    # ---- no mirror-of-a-mirror ----

    def test_mirror_po_not_remirrored_on_confirm(self):
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        po = sale.eh_ic_mirror_po_id
        # Confirming the generated PO must not create a back-mirror SO.
        po.with_company(self.company_b).button_confirm()
        self.assertFalse(po.eh_ic_mirror_so_id)

    # ---- concurrency guard ----

    def test_unique_constraint_blocks_duplicate_mirror_po(self):
        """The unique(origin_so, company) constraint forbids a second
        purchase order claiming the same source sales order in the same
        company, closing the concurrent double-mirror race."""
        sale = self._sale_order(self.partner_b)
        sale.action_confirm()
        self.assertTrue(sale.eh_ic_mirror_po_id)
        # A second PO in company B trying to claim the same source SO
        # (already taken by the real mirror) must fail the DB constraint.
        other = self.env['purchase.order'].sudo().with_company(
            self.company_b).create({
                'company_id': self.company_b.id,
                'partner_id': self.company_a.partner_id.id,
            })
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                other.eh_ic_origin_so_id = sale.id
                other.flush_recordset(['eh_ic_origin_so_id'])

    def test_unique_constraint_blocks_duplicate_mirror_so(self):
        """The unique(origin_po, company) constraint forbids a second
        sales order claiming the same source purchase order in the same
        company, closing the concurrent double-mirror race."""
        purchase = self._purchase_order(self.partner_b)
        purchase.button_confirm()
        self.assertTrue(purchase.eh_ic_mirror_so_id)
        # A second SO in company B trying to claim the same source PO
        # (already taken by the real mirror) must fail the DB constraint.
        other = self.env['sale.order'].sudo().with_company(
            self.company_b).create({
                'company_id': self.company_b.id,
                'partner_id': self.company_a.partner_id.id,
            })
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                other.eh_ic_origin_po_id = purchase.id
                other.flush_recordset(['eh_ic_origin_po_id'])
