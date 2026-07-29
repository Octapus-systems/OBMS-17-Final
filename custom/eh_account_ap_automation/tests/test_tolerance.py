# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Tolerance profile tests: defaulting, partner override, single default
per company constraint.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhApTestCase


@tagged('eh_account_ap_automation', 'integration', 'post_install', '-at_install')
class TestTolerance(EhApTestCase):

    def test_default_profile_loaded(self):
        # Loaded by data file or by setUp.
        self.assertTrue(self.default_profile)
        self.assertTrue(self.default_profile.is_default)

    def test_get_default_returns_default(self):
        prof = self.env['eh.ap.tolerance.profile'].get_default(self.company)
        self.assertEqual(prof, self.default_profile)

    def test_only_one_default_per_company(self):
        with self.assertRaises(UserError):
            self.env['eh.ap.tolerance.profile'].create({
                'name': 'Second Default',
                'is_default': True,
                'company_id': self.company.id,
            })

    def test_partner_override_takes_priority(self):
        custom = self.env['eh.ap.tolerance.profile'].create({
            'name': 'Strict',
            'qty_tolerance_pct': 0.0,
            'price_tolerance_pct': 0.0,
        })
        self.partner_a.eh_ap_tolerance_profile_id = custom
        prof = self.env['eh.ap.tolerance.profile'].resolve_for_partner(
            self.partner_a, company=self.company,
        )
        self.assertEqual(prof, custom)

    def test_partner_without_override_falls_back(self):
        prof = self.env['eh.ap.tolerance.profile'].resolve_for_partner(
            self.partner_b, company=self.company,
        )
        self.assertEqual(prof, self.default_profile)

    def test_negative_tolerance_blocked(self):
        with self.assertRaises(Exception):
            self.env['eh.ap.tolerance.profile'].create({
                'name': 'Negative',
                'qty_tolerance_pct': -1.0,
            })
