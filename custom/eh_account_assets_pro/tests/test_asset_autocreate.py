# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Automatic asset creation from posted vendor bills."""

from odoo.tests import tagged

from odoo.addons.eh_account_assets_pro.tests.common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestAssetAutoCreate(EhAssetTestCase):

    def _bill(self, amount):
        return self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-03-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Capital purchase',
                'account_id': self.account_fixed.id,
                'price_unit': amount,
                'quantity': 1,
                'tax_ids': [(6, 0, [])],
            })],
        })

    def test_autocreate_draft_asset_from_bill(self):
        self.account_fixed.eh_asset_category_id = self.asset_category.id
        self.account_fixed.eh_asset_auto = 'draft'
        bill = self._bill(12000.0)

        bill.action_post()

        line = bill.invoice_line_ids
        self.assertTrue(line.eh_asset_id)
        asset = line.eh_asset_id
        self.assertEqual(asset.state, 'draft')
        self.assertEqual(asset.category_id, self.asset_category)
        self.assertEqual(asset.invoice_id, bill)
        self.assertAlmostEqual(asset.acquisition_cost, 12000.0, places=2)

    def test_autocreate_validate_starts_asset(self):
        self.account_fixed.eh_asset_category_id = self.asset_category.id
        self.account_fixed.eh_asset_auto = 'validate'
        bill = self._bill(6000.0)

        bill.action_post()

        asset = bill.invoice_line_ids.eh_asset_id
        self.assertTrue(asset)
        self.assertEqual(asset.state, 'running')
        self.assertTrue(asset.depreciation_line_ids)

    def test_no_asset_when_account_untagged(self):
        bill = self._bill(5000.0)

        bill.action_post()

        self.assertFalse(bill.invoice_line_ids.eh_asset_id)

    def test_repost_does_not_duplicate(self):
        self.account_fixed.eh_asset_category_id = self.asset_category.id
        self.account_fixed.eh_asset_auto = 'draft'
        bill = self._bill(9000.0)
        bill.action_post()
        bill.button_draft()
        bill.action_post()

        assets = self.env['eh.asset'].search([('invoice_id', '=', bill.id)])
        self.assertEqual(len(assets), 1)
