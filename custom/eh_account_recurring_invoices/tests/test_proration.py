# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression tests for the proration wizard.

Covers the previously-broken `tpl.cadence` reference (the field is
`interval_unit`), all five interval_units, and the days_remaining=0
guard.
"""

from datetime import date

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_recurring_invoices', 'integration', 'post_install', '-at_install')
class TestProrationWizard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Template = cls.env['eh.recurring.invoice.template']
        cls.Wizard = cls.env['eh.recurring.invoice.proration.wizard']
        cls.sale_journal = cls.env['account.journal'].search(
            [('company_id', '=', cls.company.id), ('type', '=', 'sale')],
            limit=1,
        )
        if not cls.sale_journal:
            cls.sale_journal = cls.env['account.journal'].create({
                'name': 'Sales', 'code': 'SALE', 'type': 'sale',
                'company_id': cls.company.id,
            })

    def _make_template(self, interval_unit='month', interval=1, price=100.0,
                       next_run='2026-04-01'):
        return self.Template.create({
            'name': 'T_%s_%d' % (interval_unit, interval),
            'code': 't_%s_%d' % (interval_unit, interval),
            'partner_id': self.partner_a.id,
            'journal_id': self.sale_journal.id,
            'interval': interval,
            'interval_unit': interval_unit,
            'start_date': fields.Date.from_string(next_run),
            'next_run_date': fields.Date.from_string(next_run),
            'line_ids': [(0, 0, {
                'name': 'Service',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': price,
            })],
        })

    def _open_wizard(self, tpl, change_date, new_amount):
        return self.Wizard.create({
            'template_id': tpl.id,
            'change_date': fields.Date.from_string(change_date),
            'new_amount': new_amount,
        })

    # --- regression: ensure no AttributeError on any interval_unit ---

    def test_no_attribute_error_on_monthly(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        wiz = self._open_wizard(tpl, '2026-04-15', 200.0)
        # Forcing compute. If tpl.cadence regressed, this raises.
        self.assertEqual(wiz.period_end_date, date(2026, 4, 30))
        self.assertEqual(wiz.period_length_days, 30)
        self.assertEqual(wiz.days_remaining, 16)

    def test_no_attribute_error_on_quarterly(self):
        tpl = self._make_template('quarter', 1, 300.0, '2026-04-01')
        wiz = self._open_wizard(tpl, '2026-05-01', 600.0)
        # Q2 = Apr-Jun, period_end = Jun 30
        self.assertEqual(wiz.period_end_date, date(2026, 6, 30))
        self.assertEqual(wiz.period_length_days, 91)

    def test_no_attribute_error_on_yearly(self):
        tpl = self._make_template('year', 1, 1200.0, '2026-01-01')
        wiz = self._open_wizard(tpl, '2026-04-01', 2400.0)
        self.assertEqual(wiz.period_end_date, date(2026, 12, 31))
        self.assertEqual(wiz.period_length_days, 365)

    def test_no_attribute_error_on_weekly(self):
        tpl = self._make_template('week', 2, 50.0, '2026-04-06')
        wiz = self._open_wizard(tpl, '2026-04-08', 80.0)
        # 2 weeks from 2026-04-06 -> 2026-04-19
        self.assertEqual(wiz.period_end_date, date(2026, 4, 19))
        self.assertEqual(wiz.period_length_days, 14)

    def test_no_attribute_error_on_daily(self):
        tpl = self._make_template('day', 30, 100.0, '2026-04-01')
        wiz = self._open_wizard(tpl, '2026-04-15', 200.0)
        # 30 days from Apr 1 -> Apr 30
        self.assertEqual(wiz.period_end_date, date(2026, 4, 30))
        self.assertEqual(wiz.period_length_days, 30)

    # --- proration math sanity ---

    def test_credit_amount_half_period(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        # 15 days remaining of 30 = half. Credit = 50, new_period = 100,
        # net = 50.
        self.assertEqual(wiz.days_remaining, 15)
        self.assertEqual(wiz.credit_amount, 50.0)
        self.assertEqual(wiz.new_period_amount, 100.0)
        self.assertEqual(wiz.net_amount, 50.0)

    def test_apply_posts_both_moves(self):
        """The proration credit and catch-up invoice must be POSTED, not left
        in draft where they never reach AR or revenue."""
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        result = wiz.action_apply()
        move_ids = result['domain'][0][2]
        moves = self.env['account.move'].browse(move_ids)
        self.assertEqual(len(moves), 2)
        self.assertTrue(
            all(m.state == 'posted' for m in moves),
            "Proration moves must be posted, got %s"
            % moves.mapped('state'))
        # The template price advanced to the new amount.
        self.assertEqual(tpl.line_ids[0].price_unit, 200.0)

    # --- guards ---

    def test_apply_blocks_when_no_days_remaining(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        # Change date AFTER period end -> 0 days remaining.
        wiz = self._open_wizard(tpl, '2026-05-15', 200.0)
        self.assertEqual(wiz.days_remaining, 0)
        with self.assertRaises(UserError):
            wiz.action_apply()

    def test_apply_blocks_on_multi_line_template(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        tpl.write({'line_ids': [(0, 0, {
            'name': 'Addon',
            'account_id': self.account_revenue.id,
            'quantity': 1.0,
            'price_unit': 50.0,
        })]})
        wiz = self._open_wizard(tpl, '2026-04-15', 200.0)
        with self.assertRaises(UserError):
            wiz.action_apply()
