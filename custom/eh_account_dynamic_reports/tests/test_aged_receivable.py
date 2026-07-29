# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Aged Receivable handler tests.

Covers bucket classification across all five tiers, sign convention
(receivable amounts positive), filter narrowing, posted only, cancelled
exclusion, error handling, orchestrator wiring, drill down, XLSX.
"""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'post_install', '-at_install')
class TestAgedReceivableHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.aged_receivable'
        ]
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'aged_receivable')], limit=1,
        )
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'aged_receivable',
                'name': 'Aged Receivable',
                'handler_model':
                    'eh.account.dynamic.report.handler.aged_receivable',
            })

    def setUp(self):
        super().setUp()
        self.date_to = fields.Date.from_string('2026-12-31')
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _post_receivable(self, partner, amount, days_overdue):
        """Post a receivable with date_maturity at (date_to - days_overdue).

        Positive days_overdue means the line is overdue at date_to.
        Negative means it is not yet due. Zero means it is due exactly on
        date_to.
        """
        date_maturity = self.date_to - timedelta(days=days_overdue)
        # Post the entry on a recent date (within period).
        post_date = fields.Date.from_string('2026-06-15')
        return self.post_balanced_move(
            [
                {'account': self.account_receivable, 'debit': amount,
                 'partner': partner, 'date_maturity': date_maturity},
                {'account': self.account_revenue, 'credit': amount},
            ],
            date=post_date,
        )

    @staticmethod
    def _line_for_partner(result, partner_id):
        for line in result['lines']:
            if (line.get('meta') or {}).get('partner_id') == partner_id:
                return line
        return None

    @staticmethod
    def _bucket(line, key):
        for col in line['columns']:
            if col['expression_label'] == key:
                return col['value']
        return None

    # ---- bucket classification ----

    def test_not_due_bucket(self):
        # 30 days in the future: not due.
        self._post_receivable(self.partner_a, 100.0, days_overdue=-30)
        result = self.handler.compute(self.options)
        line = self._line_for_partner(result, self.partner_a.id)
        self.assertIsNotNone(line)
        self.assertAlmostEqual(self._bucket(line, 'not_due'), 100.0, places=2)
        self.assertAlmostEqual(self._bucket(line, 'bucket_30'), 0.0, places=2)

    def test_0_to_30_bucket(self):
        self._post_receivable(self.partner_a, 100.0, days_overdue=15)
        result = self.handler.compute(self.options)
        line = self._line_for_partner(result, self.partner_a.id)
        self.assertAlmostEqual(self._bucket(line, 'bucket_30'), 100.0, places=2)

    def test_31_to_60_bucket(self):
        self._post_receivable(self.partner_a, 100.0, days_overdue=45)
        result = self.handler.compute(self.options)
        line = self._line_for_partner(result, self.partner_a.id)
        self.assertAlmostEqual(self._bucket(line, 'bucket_60'), 100.0, places=2)

    # ---- configurable aging buckets (C1) ----

    def test_custom_aging_interval(self):
        # 45 days overdue: default 30-day buckets -> bucket_60 (31-60).
        # With a 45-day interval it lands in the first bucket (1-45).
        self._post_receivable(self.partner_a, 100.0, days_overdue=45)
        options = dict(self.options, aging_interval=45,
                       aging_bucket_count=3)
        result = self.handler.compute(options)
        col_keys = [c['expression_label'] for c in result['columns']]
        self.assertIn('bucket_45', col_keys)
        self.assertNotIn('bucket_60', col_keys)
        line = self._line_for_partner(result, self.partner_a.id)
        self.assertAlmostEqual(self._bucket(line, 'bucket_45'), 100.0,
                               places=2)

    def test_aging_basis_invoice_date_vs_maturity(self):
        # Maturity 30 days in the future (not due by maturity), but the
        # invoice was posted on 2026-06-15 (~199 days before date_to).
        self._post_receivable(self.partner_a, 100.0, days_overdue=-30)

        by_maturity = self.handler.compute(
            dict(self.options, aging_basis='maturity'))
        line_m = self._line_for_partner(by_maturity, self.partner_a.id)
        self.assertAlmostEqual(self._bucket(line_m, 'not_due'), 100.0,
                               places=2)

        by_invoice = self.handler.compute(
            dict(self.options, aging_basis='invoice_date'))
        line_i = self._line_for_partner(by_invoice, self.partner_a.id)
        self.assertAlmostEqual(self._bucket(line_i, 'not_due'), 0.0, places=2)
        self.assertAlmostEqual(self._bucket(line_i, 'bucket_older'), 100.0,
                               places=2)

    def test_61_to_90_bucket(self):
        self._post_receivable(self.partner_a, 100.0, days_overdue=75)
        result = self.handler.compute(self.options)
        line = self._line_for_partner(result, self.partner_a.id)
        self.assertAlmostEqual(self._bucket(line, 'bucket_90'), 100.0, places=2)

    def test_91_plus_bucket(self):
        self._post_receivable(self.partner_a, 100.0, days_overdue=200)
        result = self.handler.compute(self.options)
        line = self._line_for_partner(result, self.partner_a.id)
        self.assertAlmostEqual(
            self._bucket(line, 'bucket_older'), 100.0, places=2,
        )

    def test_partner_with_lines_in_multiple_buckets(self):
        self._post_receivable(self.partner_a, 50.0, days_overdue=-10)
        self._post_receivable(self.partner_a, 75.0, days_overdue=20)
        self._post_receivable(self.partner_a, 100.0, days_overdue=120)
        result = self.handler.compute(self.options)
        line = self._line_for_partner(result, self.partner_a.id)
        self.assertAlmostEqual(self._bucket(line, 'not_due'), 50.0, places=2)
        self.assertAlmostEqual(self._bucket(line, 'bucket_30'), 75.0, places=2)
        self.assertAlmostEqual(
            self._bucket(line, 'bucket_older'), 100.0, places=2,
        )
        self.assertAlmostEqual(self._bucket(line, 'total'), 225.0, places=2)

    # ---- sign convention ----

    def test_receivable_amounts_are_positive(self):
        self._post_receivable(self.partner_a, 100.0, days_overdue=10)
        result = self.handler.compute(self.options)
        line = self._line_for_partner(result, self.partner_a.id)
        # All bucket amounts should be non-negative for AR.
        for key in ('not_due', 'bucket_30', 'bucket_60',
                    'bucket_90', 'bucket_older', 'total'):
            self.assertGreaterEqual(self._bucket(line, key), 0.0)

    # ---- totals row ----

    def test_totals_aggregate_across_partners(self):
        self._post_receivable(self.partner_a, 100.0, days_overdue=20)
        self._post_receivable(self.partner_b, 150.0, days_overdue=50)
        result = self.handler.compute(self.options)
        totals = result['totals']
        self.assertAlmostEqual(totals['bucket_30'], 100.0, places=2)
        self.assertAlmostEqual(totals['bucket_60'], 150.0, places=2)
        self.assertAlmostEqual(totals['total'], 250.0, places=2)

    # ---- filter narrowing ----

    def test_partner_filter_narrows(self):
        self._post_receivable(self.partner_a, 100.0, days_overdue=10)
        self._post_receivable(self.partner_b, 200.0, days_overdue=10)
        opts = dict(self.options)
        opts['partner_ids'] = [self.partner_a.id]
        result = self.handler.compute(opts)
        self.assertEqual(len(result['lines']), 1)
        self.assertIsNotNone(self._line_for_partner(result, self.partner_a.id))
        self.assertIsNone(self._line_for_partner(result, self.partner_b.id))

    # ---- state filtering ----

    def test_posted_only_excludes_draft(self):
        date_maturity = self.date_to - timedelta(days=30)
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-06-15',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id, 'debit': 999.0,
                    'date_maturity': date_maturity,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id, 'credit': 999.0,
                }),
            ],
        })
        result = self.handler.compute(self.options)
        self.assertIsNone(self._line_for_partner(result, self.partner_a.id))

    def test_cancelled_excluded(self):
        move = self._post_receivable(self.partner_a, 100.0, days_overdue=10)
        move.button_cancel()
        result = self.handler.compute(self.options)
        self.assertIsNone(self._line_for_partner(result, self.partner_a.id))

    # ---- error handling ----

    def test_missing_date_to_raises(self):
        bad = dict(self.options)
        bad['date'] = {'date_from': '2026-01-01'}
        with self.assertRaises(UserError):
            self.handler.compute(bad)

    # ---- orchestrator wiring ----

    def test_orchestrator_renders(self):
        self._post_receivable(self.partner_a, 100.0, days_overdue=10)
        result = self.report.render(self.options)
        self.assertFalse(result['from_cache'])

    def test_orchestrator_cache_hit_on_second_render(self):
        self._post_receivable(self.partner_a, 100.0, days_overdue=10)
        first = self.report.render(self.options)
        second = self.report.render(self.options)
        self.assertFalse(first['from_cache'])
        self.assertTrue(second['from_cache'])

    # ---- drill down ----

    def test_drilldown_partner_returns_filtered_aml_action(self):
        action = self.handler.get_drilldown_action(
            self.options, "partner-%s" % self.partner_a.id,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action['res_model'], 'account.move.line')
        domain_pairs = [
            tuple(d[:2]) for d in action['domain']
            if isinstance(d, (list, tuple)) and len(d) >= 2
        ]
        self.assertIn(('partner_id', '='), domain_pairs)
        self.assertIn(('amount_residual', '!='), domain_pairs)

    def test_drilldown_unknown_id_returns_none(self):
        self.assertIsNone(self.handler.get_drilldown_action(
            self.options, 'totally-bogus',
        ))
        self.assertIsNone(self.handler.get_drilldown_action(
            self.options, 'partner-not-a-number',
        ))

    # ---- XLSX export ----

    def test_xlsx_export_renders_workbook(self):
        self._post_receivable(self.partner_a, 100.0, days_overdue=10)
        content = self.report.render_xlsx(self.options)
        self.assertEqual(content[:2], b'PK')
        self.assertGreater(len(content), 1000)
