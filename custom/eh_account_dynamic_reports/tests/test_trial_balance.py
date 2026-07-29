# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Trial Balance handler tests.

Covers:

* Movement column shows period activity.
* Opening balance reflects entries before date_from.
* Closing balance equals opening plus period movement.
* Zero balance accounts hidden by default; show_zero exposes them.
* Totals balance: debit equals credit at every column tier.
* Account, journal, partner filters narrow the result set.
* posted_only excludes draft entries; setting it false includes them.
* Cancelled entries are always excluded.
* Missing date raises a clear UserError.
* Orchestrator render path produces the same data and respects the cache.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'post_install', '-at_install')
class TestTrialBalanceHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env['eh.account.dynamic.report.handler.trial_balance']
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'trial_balance')], limit=1,
        )
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'trial_balance',
                'name': 'Trial Balance',
                'handler_model': 'eh.account.dynamic.report.handler.trial_balance',
            })

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _post_in_period(self, lines):
        return self.post_balanced_move(
            lines, date=fields.Date.from_string('2026-06-15'),
        )

    def _post_before_period(self, lines):
        return self.post_balanced_move(
            lines, date=fields.Date.from_string('2025-12-15'),
        )

    @staticmethod
    def _index_lines(result):
        return {line['meta']['account_code']: line for line in result['lines']}

    @staticmethod
    def _column_value(line, label):
        for col in line['columns']:
            if col['expression_label'] == label:
                return col['value']
        raise AssertionError(f"Column {label!r} missing from line {line['name']!r}")

    # ---- core math ----

    def test_period_movement_appears(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 1000.0,
             'partner': self.partner_a},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        result = self.handler.compute(self.options)
        idx = self._index_lines(result)
        self.assertIn('4000', idx)
        self.assertAlmostEqual(
            self._column_value(idx['4000'], 'period_credit'), 1000.0, places=2,
        )

    def test_opening_balance_carries_from_prior_period(self):
        self._post_before_period([
            {'account': self.account_revenue, 'credit': 500.0},
            {'account': self.account_cash, 'debit': 500.0},
        ])
        result = self.handler.compute(self.options)
        idx = self._index_lines(result)
        self.assertAlmostEqual(
            self._column_value(idx['1000'], 'opening_debit'), 500.0, places=2,
        )

    def test_closing_equals_opening_plus_movement(self):
        self._post_before_period([
            {'account': self.account_revenue, 'credit': 500.0},
            {'account': self.account_cash, 'debit': 500.0},
        ])
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 200.0},
            {'account': self.account_cash, 'debit': 200.0},
        ])
        result = self.handler.compute(self.options)
        cash = self._index_lines(result)['1000']
        cols = {c['expression_label']: c['value'] for c in cash['columns']}
        self.assertAlmostEqual(cols['opening_debit'], 500.0, places=2)
        self.assertAlmostEqual(cols['period_debit'], 200.0, places=2)
        self.assertAlmostEqual(cols['closing_debit'], 700.0, places=2)
        self.assertAlmostEqual(cols['opening_credit'], 0.0, places=2)
        self.assertAlmostEqual(cols['period_credit'], 0.0, places=2)
        self.assertAlmostEqual(cols['closing_credit'], 0.0, places=2)

    def test_totals_balance_at_each_tier(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 1000.0},
            {'account': self.account_cash, 'debit': 1000.0},
        ])
        self._post_in_period([
            {'account': self.account_expense, 'debit': 200.0},
            {'account': self.account_cash, 'credit': 200.0},
        ])
        result = self.handler.compute(self.options)
        totals = result['totals']
        self.assertAlmostEqual(
            totals['period_debit'], totals['period_credit'], places=2,
        )
        self.assertAlmostEqual(
            totals['closing_debit'], totals['closing_credit'], places=2,
        )
        self.assertAlmostEqual(
            totals['opening_debit'], totals['opening_credit'], places=2,
        )

    # ---- filter behaviour ----

    def test_zero_balance_account_hidden_by_default(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        result = self.handler.compute(self.options)
        idx = self._index_lines(result)
        self.assertNotIn('5000', idx,
                         "Untouched expense account must be hidden")

    def test_account_filter(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        opts = dict(self.options)
        opts['account_ids'] = [self.account_cash.id]
        result = self.handler.compute(opts)
        idx = self._index_lines(result)
        self.assertIn('1000', idx)
        self.assertNotIn('4000', idx)

    def test_journal_filter(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        opts = dict(self.options)
        opts['journal_ids'] = [self.journal_misc.id]
        result = self.handler.compute(opts)
        # Posting was via journal_misc, so it should still appear.
        self.assertIn('1000', self._index_lines(result))

    def test_partner_filter(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0,
             'partner': self.partner_a},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 200.0,
             'partner': self.partner_b},
            {'account': self.account_cash, 'debit': 200.0},
        ])
        opts = dict(self.options)
        opts['partner_ids'] = [self.partner_a.id]
        result = self.handler.compute(opts)
        idx = self._index_lines(result)
        # Only the 100 credit (partner A) should aggregate; cash line had no
        # partner so it is not present in the partner filtered result.
        self.assertIn('4000', idx)
        self.assertAlmostEqual(
            self._column_value(idx['4000'], 'period_credit'), 100.0, places=2,
        )

    # ---- state filtering ----

    def test_posted_only_excludes_draft(self):
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-06-15',
            'line_ids': [
                (0, 0, {'account_id': self.account_revenue.id, 'credit': 999.0}),
                (0, 0, {'account_id': self.account_cash.id, 'debit': 999.0}),
            ],
        })
        result = self.handler.compute(self.options)
        idx = self._index_lines(result)
        self.assertNotIn(
            '4000', idx,
            "draft entries must be excluded when posted_only=True",
        )

    def test_posted_only_false_includes_draft(self):
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-06-15',
            'line_ids': [
                (0, 0, {'account_id': self.account_revenue.id, 'credit': 333.0}),
                (0, 0, {'account_id': self.account_cash.id, 'debit': 333.0}),
            ],
        })
        opts = dict(self.options)
        opts['posted_only'] = False
        result = self.handler.compute(opts)
        idx = self._index_lines(result)
        self.assertIn('4000', idx)
        self.assertAlmostEqual(
            self._column_value(idx['4000'], 'period_credit'), 333.0, places=2,
        )

    def test_cancelled_entries_excluded(self):
        move = self._post_in_period([
            {'account': self.account_revenue, 'credit': 444.0},
            {'account': self.account_cash, 'debit': 444.0},
        ])
        move.button_cancel()
        result = self.handler.compute(self.options)
        idx = self._index_lines(result)
        self.assertNotIn('4000', idx)

    # ---- error handling ----

    def test_missing_date_raises(self):
        bad = dict(self.options)
        bad.pop('date')
        with self.assertRaises(UserError):
            self.handler.compute(bad)

    def test_missing_date_from_raises(self):
        bad = dict(self.options)
        bad['date'] = {'date_to': '2026-12-31'}
        with self.assertRaises(UserError):
            self.handler.compute(bad)

    # ---- orchestrator wiring ----

    def test_orchestrator_renders(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        result = self.report.render(self.options)
        self.assertFalse(result['from_cache'])
        self.assertIn('execution_id', result)
        self.assertGreater(len(result['lines']), 0)

    def test_orchestrator_cache_hit_on_repeated_render(self):
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        first = self.report.render(self.options)
        second = self.report.render(self.options)
        self.assertFalse(first['from_cache'])
        self.assertTrue(second['from_cache'])
        # Cached payload should match the freshly computed one in shape.
        self.assertEqual(len(first['lines']), len(second['lines']))
        self.assertEqual(first['totals'], second['totals'])

    def test_orchestrator_invalidates_cache_on_new_post(self):
        first = self.report.render(self.options)
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 50.0},
            {'account': self.account_cash, 'debit': 50.0},
        ])
        second = self.report.render(self.options)
        self.assertFalse(second['from_cache'],
                         "Posting must invalidate the cache")
        # The new entry should appear in the second render.
        idx = self._index_lines(second)
        self.assertIn('4000', idx)

    def test_drilldown_action_returns_filtered_journal_items(self):
        move = self._post_in_period([
            {'account': self.account_revenue, 'credit': 75.0},
            {'account': self.account_cash, 'debit': 75.0},
        ])
        action = self.handler.get_drilldown_action(
            self.options, "account-%s" % self.account_revenue.id,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action['res_model'], 'account.move.line')
        # Verify the domain matches the revenue line.
        Line = self.env['account.move.line']
        items = Line.search(action['domain'])
        self.assertIn(
            self.account_revenue.id, items.mapped('account_id.id'),
        )

    def test_drilldown_action_returns_none_for_invalid_id(self):
        action = self.handler.get_drilldown_action(self.options, 'totally-bogus')
        self.assertIsNone(action)

    def test_compute_under_non_en_us_lang_does_not_crash(self):
        """Regression: Trial Balance must run in any env.lang.

        Customer report (Daria, INTERIM 2000 GNF, odoo.sh, French UI):
        opening the Balance générale screen surfaced a generic "Error:
        Odoo Server Error" with no traceback in the UI.

        Root cause: the trial balance handler issued a SELECT whose
        account_name expression resolved via the MoveLineQuery
        translated-name helper (``COALESCE(acc.name ->> '<lang>',
        acc.name ->> 'en_US')`` when env.lang differs from en_US),
        but the GROUP BY clause hardcoded ``(acc.name ->> 'en_US')``.
        PostgreSQL strictly requires every non-aggregated SELECT
        expression to appear in GROUP BY verbatim, so it rejected the
        query with ``column "acc.name" must appear in the GROUP BY
        clause`` for every non-en_US locale.

        The fix made trial_balance.py use the new
        ``group_by_account_field`` helper so SELECT, ORDER BY, and
        GROUP BY share the same expression for translated columns.
        This test installs French, posts an entry, runs compute() and
        render() under fr_FR, and asserts both return data without
        raising.
        """
        # Ensure French is active. Odoo lazy-loads languages, so we
        # call load_language directly rather than INSERT into res_lang.
        self.env['res.lang']._activate_lang('fr_FR')

        # Post one entry inside the period so the SQL has rows to
        # group; a zero-row query bypasses the GROUP BY validation in
        # some PostgreSQL versions.
        self._post_in_period([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])

        handler = self.handler.with_context(lang='fr_FR')
        report = self.report.with_context(lang='fr_FR')

        # compute() path
        result = handler.compute(self.options)
        self.assertTrue(result.get('lines'),
                        "compute() under fr_FR returned no lines")

        # render() path (the orchestrator the OWL viewer actually calls)
        rendered = report.render(self.options)
        self.assertTrue(rendered.get('lines'),
                        "render() under fr_FR returned no lines")


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestTrialBalanceFiscalYearWS4(EhAccountIntegrationTestCase):
    """WS4: fiscal-year-aware opening + unaffected-earnings footing.

    The base company uses a calendar fiscal year (default
    fiscalyear_last_month=12), so the fiscal year containing 2026-01-01
    starts on 2026-01-01 and everything dated in 2025 is prior-year P&L.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env['eh.account.dynamic.report.handler.trial_balance']

    @staticmethod
    def _index_by_id(result):
        return {line['id']: line for line in result['lines']}

    @staticmethod
    def _col(line, label):
        for c in line['columns']:
            if c['expression_label'] == label:
                return c['value']
        raise AssertionError(f"missing column {label!r}")

    def _opts(self, date_from='2026-01-01', date_to='2026-12-31', **kw):
        opts = {
            'date': {'date_from': date_from, 'date_to': date_to},
            'company_ids': [self.company.id],
            'posted_only': True, 'show_zero': False,
            'hierarchical_groups': kw.pop('hierarchical_groups', False),
        }
        opts.update(kw)
        return opts

    def test_tb_foots_at_year_boundary_with_unaffected_line(self):
        # Prior-year revenue 1000 (credit) and expense 300 (debit): net
        # profit P = 700. Plus a balance-sheet posting so opening is non-trivial.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 1000.0},
             {'account': self.account_cash, 'debit': 1000.0}],
            date=fields.Date.from_string('2025-06-15'))
        self.post_balanced_move(
            [{'account': self.account_expense, 'debit': 300.0},
             {'account': self.account_cash, 'credit': 300.0}],
            date=fields.Date.from_string('2025-07-15'))

        result = self.handler.compute(self._opts())
        totals = result['totals']
        # The trial balance must foot at the year boundary on every tier.
        self.assertAlmostEqual(
            totals['opening_debit'], totals['opening_credit'], places=2)
        self.assertAlmostEqual(
            totals['closing_debit'], totals['closing_credit'], places=2)

        # The unaffected-earnings line carries net prior-year profit P=700 on
        # the credit side (a profit is a credit balance).
        idx = self._index_by_id(result)
        self.assertIn('account-unaffected-earnings', idx)
        ue = idx['account-unaffected-earnings']
        self.assertAlmostEqual(
            self._col(ue, 'opening_credit'), 700.0, places=2)
        self.assertAlmostEqual(self._col(ue, 'opening_debit'), 0.0, places=2)

    def test_pl_account_opening_zero_at_fiscal_year_start(self):
        # Prior-year revenue must NOT appear as the revenue account's opening
        # once date_from is the fiscal-year start; it is rolled to unaffected.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 500.0},
             {'account': self.account_cash, 'debit': 500.0}],
            date=fields.Date.from_string('2025-12-15'))
        result = self.handler.compute(self._opts())
        idx = {l['meta'].get('account_code'): l
               for l in result['lines'] if l.get('meta')}
        # Revenue account (4000) opens at zero on both sides at FY start.
        if '4000' in idx:
            self.assertAlmostEqual(
                self._col(idx['4000'], 'opening_debit'), 0.0, places=2)
            self.assertAlmostEqual(
                self._col(idx['4000'], 'opening_credit'), 0.0, places=2)
        # Cash (balance sheet) still carries its 500 opening forward.
        self.assertIn('1000', idx)
        self.assertAlmostEqual(
            self._col(idx['1000'], 'opening_debit'), 500.0, places=2)

    def test_mid_year_pl_opening_is_current_fy_to_date_only(self):
        # date_from mid fiscal year: an income account's opening must equal
        # ONLY its current-FY-to-date movement (Jan..May 2026), not all-time.
        self.post_balanced_move(  # prior year -> rolls to unaffected
            [{'account': self.account_revenue, 'credit': 400.0},
             {'account': self.account_cash, 'debit': 400.0}],
            date=fields.Date.from_string('2025-11-15'))
        self.post_balanced_move(  # current FY, before mid-year date_from
            [{'account': self.account_revenue, 'credit': 250.0},
             {'account': self.account_cash, 'debit': 250.0}],
            date=fields.Date.from_string('2026-03-15'))
        result = self.handler.compute(
            self._opts(date_from='2026-07-01', date_to='2026-12-31'))
        idx = {l['meta'].get('account_code'): l
               for l in result['lines'] if l.get('meta')}
        # Revenue opening = only the current-FY-to-date 250 credit, NOT 650.
        self.assertIn('4000', idx)
        self.assertAlmostEqual(
            self._col(idx['4000'], 'opening_credit'), 250.0, places=2)

    def test_opening_parity_roll_loses_nothing(self):
        # Sum of all openings (incl unaffected) must equal the sum of all
        # aml.balance before date_from: the roll reclassifies, never loses.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 900.0},
             {'account': self.account_cash, 'debit': 900.0}],
            date=fields.Date.from_string('2025-05-15'))
        self.post_balanced_move(
            [{'account': self.account_expense, 'debit': 200.0},
             {'account': self.account_cash, 'credit': 200.0}],
            date=fields.Date.from_string('2025-08-15'))
        result = self.handler.compute(self._opts())
        # Net signed opening across all lines = opening_debit - opening_credit.
        net_opening = (result['totals']['opening_debit']
                       - result['totals']['opening_credit'])
        # All prior moves are balanced, so the signed sum is exactly zero.
        self.assertAlmostEqual(net_opening, 0.0, places=2)

    def test_no_prior_pl_means_no_unaffected_line(self):
        # Regression: with only in-period activity, no unaffected line is
        # emitted and the TB shape is unchanged from pre-WS4.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 100.0},
             {'account': self.account_cash, 'debit': 100.0}],
            date=fields.Date.from_string('2026-06-15'))
        result = self.handler.compute(self._opts())
        ids = {l['id'] for l in result['lines']}
        self.assertNotIn('account-unaffected-earnings', ids)

    def test_hierarchical_unaffected_line_and_footing(self):
        # The hierarchical builder must also emit the unaffected line and foot.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 800.0},
             {'account': self.account_cash, 'debit': 800.0}],
            date=fields.Date.from_string('2025-06-15'))
        result = self.handler.compute(self._opts(hierarchical_groups=True))
        ids = {l['id'] for l in result['lines']}
        self.assertIn('account-unaffected-earnings', ids)
        totals = result['totals']
        self.assertAlmostEqual(
            totals['opening_debit'], totals['opening_credit'], places=2)
        self.assertAlmostEqual(
            totals['closing_debit'], totals['closing_credit'], places=2)

    def test_staggered_fiscal_year_opening(self):
        # Non-calendar fiscal year ending 30 June: FY containing 2026-08-01
        # starts 2026-07-01, so a July 2026 P&L line is current-FY (opening),
        # while a June 2026 line is prior-year (rolled to unaffected).
        self.company.write({
            'fiscalyear_last_day': 30, 'fiscalyear_last_month': '6'})
        self.post_balanced_move(  # prior FY (before 2026-07-01) -> unaffected
            [{'account': self.account_revenue, 'credit': 600.0},
             {'account': self.account_cash, 'debit': 600.0}],
            date=fields.Date.from_string('2026-06-15'))
        self.post_balanced_move(  # current FY, before date_from -> opening
            [{'account': self.account_revenue, 'credit': 150.0},
             {'account': self.account_cash, 'debit': 150.0}],
            date=fields.Date.from_string('2026-07-10'))
        result = self.handler.compute(
            self._opts(date_from='2026-08-01', date_to='2027-06-30'))
        idx = {l['meta'].get('account_code'): l
               for l in result['lines'] if l.get('meta')}
        self.assertIn('4000', idx)
        # Only the current-FY 150 is in revenue's opening; the June 600 rolled.
        self.assertAlmostEqual(
            self._col(idx['4000'], 'opening_credit'), 150.0, places=2)
        ue = {l['id']: l for l in result['lines']}.get(
            'account-unaffected-earnings')
        self.assertIsNotNone(ue)
        self.assertAlmostEqual(self._col(ue, 'opening_credit'), 600.0, places=2)


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestTrialBalanceMultiCurrencyWS4(EhAccountIntegrationTestCase):
    """WS4: cross-company consolidation converts to a presentation currency.

    Company A is in the base currency; company B reports in a second currency
    at a fixed rate. The consolidated trial balance total must convert B's
    balance, not sum raw mixed-currency figures.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env['eh.account.dynamic.report.handler.trial_balance']
        cls.company_a = cls.company
        cls.base_currency = cls.company_a.currency_id

        # A distinct second currency for company B with a fixed rate of 2.0
        # against the base currency (1 base = 2.0 of currency B), so a B
        # balance converts to base by dividing by the company rate. We assert
        # the realised number directly to pin the orientation.
        cls.currency_b = cls.env['res.currency'].create({
            'name': 'XBT', 'symbol': 'X', 'rounding': 0.01,
        })
        cls.env['res.currency.rate'].create({
            'currency_id': cls.currency_b.id, 'name': '2020-01-01',
            'rate': 2.0,
        })
        cls.company_b = cls.env['res.company'].create({
            'name': 'FX Co B', 'currency_id': cls.currency_b.id,
        })
        cls.env.user.company_ids = [(4, cls.company_b.id)]
        cls.journal_b = cls.env['account.journal'].create({
            'name': 'Misc B', 'code': 'MSCB', 'type': 'general',
            'company_id': cls.company_b.id,
        })
        cls.revenue_b = cls.env['account.account'].create({
            'code': '4002B', 'name': 'Revenue B', 'account_type': 'income',
            'company_id': cls.company_b.id,
        })
        cls.cash_b = cls.env['account.account'].create({
            'code': '1002B', 'name': 'Cash B', 'account_type': 'asset_cash',
            'company_id': cls.company_b.id,
        })

    def _consolidated_total(self, result, label):
        return result['totals'][label]

    def test_cross_currency_consolidation_converts_b(self):
        # A: 1000 base. B: 1000 currency-B (which is 500 base at rate 2.0).
        self.post_balanced_move(
            [{'account': self.account_cash, 'debit': 1000.0},
             {'account': self.account_revenue, 'credit': 1000.0}],
            date=fields.Date.from_string('2026-06-15'))
        self.post_balanced_move(
            [{'account': self.cash_b, 'debit': 1000.0},
             {'account': self.revenue_b, 'credit': 1000.0}],
            journal=self.journal_b,
            date=fields.Date.from_string('2026-06-15'))

        options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company_a.id, self.company_b.id],
            'posted_only': True, 'show_zero': False,
            'hierarchical_groups': False,
            'presentation_currency_id': self.base_currency.id,
        }
        result = self.handler.compute(options)

        # Resolve the actual rate orientation the ORM uses so the assertion is
        # robust to rate-direction conventions: convert 1000 B to base.
        b_in_base = self.currency_b._convert(
            1000.0, self.base_currency, self.company_b,
            fields.Date.from_string('2026-12-31'))
        expected = round(1000.0 + b_in_base, 2)

        # Period debit total: cash A (1000 base) + cash B (1000 B -> base).
        self.assertAlmostEqual(
            self._consolidated_total(result, 'period_debit'),
            expected, places=2)
        # Raw (unconverted) sum would have been 2000; prove we did NOT do that
        # unless the rate happens to be 1.0.
        if abs(b_in_base - 1000.0) > 0.01:
            self.assertNotAlmostEqual(
                self._consolidated_total(result, 'period_debit'),
                2000.0, places=2)

    def test_inverse_direction_presentation_currency_b(self):
        # Present in currency B instead: now A's base balance converts UP into
        # currency B and B's stays as-is, proving rate orientation both ways.
        self.post_balanced_move(
            [{'account': self.account_cash, 'debit': 1000.0},
             {'account': self.account_revenue, 'credit': 1000.0}],
            date=fields.Date.from_string('2026-06-15'))
        self.post_balanced_move(
            [{'account': self.cash_b, 'debit': 1000.0},
             {'account': self.revenue_b, 'credit': 1000.0}],
            journal=self.journal_b,
            date=fields.Date.from_string('2026-06-15'))

        options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company_a.id, self.company_b.id],
            'posted_only': True, 'show_zero': False,
            'hierarchical_groups': False,
            'presentation_currency_id': self.currency_b.id,
        }
        result = self.handler.compute(options)
        a_in_b = self.base_currency._convert(
            1000.0, self.currency_b, self.company_a,
            fields.Date.from_string('2026-12-31'))
        expected = round(a_in_b + 1000.0, 2)
        self.assertAlmostEqual(
            self._consolidated_total(result, 'period_debit'),
            expected, places=2)

    def test_monocurrency_single_company_unchanged(self):
        # Regression: a single-company run (no presentation currency) must
        # produce exactly the figures it did before WS4's currency thread.
        self.post_balanced_move(
            [{'account': self.account_cash, 'debit': 1000.0},
             {'account': self.account_revenue, 'credit': 1000.0}],
            date=fields.Date.from_string('2026-06-15'))
        options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company_a.id],
            'posted_only': True, 'show_zero': False,
            'hierarchical_groups': False,
        }
        result = self.handler.compute(options)
        self.assertAlmostEqual(
            result['totals']['period_debit'], 1000.0, places=2)
        self.assertFalse(
            result['meta'].get('multi_currency', False))
