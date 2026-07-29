# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
The seed data ships the canonical BAS label set. Verify the rows landed.
"""

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


class BasLabelSeedTest(EhAccountIntegrationTestCase):

    def test_g1_through_g3_present(self):
        for code in ('G1', 'G2', 'G3'):
            self.assertTrue(
                self.env['eh.bas.label'].search_count([('code', '=', code)]),
                "BAS label %s missing from seed" % code,
            )

    def test_gst_collected_paid_present(self):
        self.assertTrue(
            self.env['eh.bas.label'].search_count([('code', '=', '1A')]),
            "BAS label 1A (GST collected) missing",
        )
        self.assertTrue(
            self.env['eh.bas.label'].search_count([('code', '=', '1B')]),
            "BAS label 1B (GST paid) missing",
        )

    def test_payg_w1_w2_present(self):
        for code in ('W1', 'W2'):
            self.assertTrue(
                self.env['eh.bas.label'].search_count([('code', '=', code)]),
                "BAS label %s missing" % code,
            )

    def test_unique_codes(self):
        all_codes = self.env['eh.bas.label'].search([]).mapped('code')
        self.assertEqual(
            len(all_codes), len(set(all_codes)),
            "Duplicate BAS label codes in the seed data: %r" % (all_codes,),
        )

    def test_aggregation_values_are_valid(self):
        valid = {'manual', 'base_credit', 'base_debit',
                 'tax_credit', 'tax_debit',
                 'gst_on_sales', 'gst_on_purchases', 'total_sales_incl'}
        for label in self.env['eh.bas.label'].search([]):
            self.assertIn(
                label.aggregation, valid,
                "Label %s has invalid aggregation %s" % (
                    label.code, label.aggregation,
                ),
            )
