# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Custom report builder tests.

Covers the model (constraints, lifecycle), the formula evaluator
(security rejections, arithmetic correctness, missing identifiers), the
publish flow (registers a dynamic.report record), and end to end render
through the orchestrator (account aggregates and formula lines compose).
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase
from odoo.addons.eh_account_dynamic_reports_pro.models.report_builder import (
    safe_eval_formula, FormulaError,
)


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestFormulaEvaluator(EhAccountIntegrationTestCase):

    def test_simple_arithmetic(self):
        self.assertAlmostEqual(safe_eval_formula("1 + 2", {}), 3.0)
        self.assertAlmostEqual(safe_eval_formula("10 - 3", {}), 7.0)
        self.assertAlmostEqual(safe_eval_formula("4 * 5", {}), 20.0)
        self.assertAlmostEqual(safe_eval_formula("9 / 2", {}), 4.5)

    def test_parentheses_and_precedence(self):
        self.assertAlmostEqual(
            safe_eval_formula("(1 + 2) * 3", {}), 9.0,
        )
        self.assertAlmostEqual(
            safe_eval_formula("1 + 2 * 3", {}), 7.0,
        )

    def test_unary_negation(self):
        self.assertAlmostEqual(safe_eval_formula("-5 + 10", {}), 5.0)
        self.assertAlmostEqual(safe_eval_formula("-(2 + 3)", {}), -5.0)

    def test_identifier_lookup(self):
        self.assertAlmostEqual(
            safe_eval_formula("a + b", {'a': 10, 'b': 20}), 30.0,
        )

    def test_missing_identifier_resolves_to_zero(self):
        self.assertAlmostEqual(
            safe_eval_formula("a + b", {'a': 10}), 10.0,
        )

    def test_division_by_zero_returns_zero(self):
        self.assertAlmostEqual(safe_eval_formula("10 / 0", {}), 0.0)

    def test_empty_formula_returns_zero(self):
        self.assertAlmostEqual(safe_eval_formula("", {}), 0.0)
        self.assertAlmostEqual(safe_eval_formula("   ", {}), 0.0)

    def test_function_call_rejected(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("abs(-5)", {})

    def test_attribute_access_rejected(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("a.b", {})

    def test_string_constant_rejected(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("'hello'", {})

    def test_boolean_constant_rejected(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("True + 1", {})

    def test_subscript_rejected(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("a[0]", {})

    def test_comprehension_rejected(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("[x for x in range(5)]", {})

    def test_syntax_error_raises(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("a +", {})


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestBuilderModel(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Builder = cls.env['eh.report.builder']
        cls.Line = cls.env['eh.report.builder.line']

    def _make_builder(self, code='my_report', name='My Report', lines=None):
        builder = self.Builder.create({
            'code': code,
            'name': name,
            'line_ids': [(0, 0, line) for line in (lines or [])],
        })
        return builder

    def test_code_format_constraint(self):
        with self.assertRaises(UserError):
            self._make_builder(code='Bad-Code')
        with self.assertRaises(UserError):
            self._make_builder(code='123_starts_with_number')
        # valid codes pass
        self._make_builder(code='good_code_one', name='one')

    def test_unique_code_constraint(self):
        self._make_builder(code='unique_one')
        with self.assertRaises(Exception):
            self._make_builder(code='unique_one', name='dup')

    def test_line_code_format_constraint(self):
        with self.assertRaises(UserError):
            self._make_builder(code='lc_test', lines=[
                {'name': 'L1', 'code': 'BAD-CODE', 'line_type': 'section_header'},
            ])

    def test_duplicate_line_code_within_builder_rejected(self):
        with self.assertRaises(UserError):
            self._make_builder(code='dup_lines', lines=[
                {'name': 'L1', 'code': 'shared', 'line_type': 'section_header'},
                {'name': 'L2', 'code': 'shared', 'line_type': 'section_header'},
            ])

    def test_action_publish_creates_dynamic_report(self):
        builder = self._make_builder(code='pub_one', name='Publishable')
        self.assertFalse(builder.is_published)
        builder.action_publish()
        self.assertTrue(builder.is_published)
        self.assertTrue(builder.published_report_id)
        self.assertEqual(builder.published_report_id.code, 'pub_one')
        self.assertEqual(
            builder.published_report_id.handler_model,
            'eh.account.dynamic.report.handler.builder',
        )

    def test_action_unpublish_deactivates(self):
        builder = self._make_builder(code='pub_two', name='Pub Two')
        builder.action_publish()
        report = builder.published_report_id
        builder.action_unpublish()
        self.assertFalse(builder.is_published)
        self.assertFalse(report.active)

    def test_publish_idempotent_updates_existing_record(self):
        builder = self._make_builder(code='pub_three', name='Original')
        builder.action_publish()
        report_id = builder.published_report_id.id
        builder.write({'name': 'Updated'})
        builder.action_publish()
        # Same report id, name updated.
        self.assertEqual(builder.published_report_id.id, report_id)
        self.assertEqual(builder.published_report_id.name, 'Updated')

    def test_action_open_viewer_requires_published(self):
        builder = self._make_builder(code='not_pub', name='Not Pub')
        with self.assertRaises(UserError):
            builder.action_open_viewer()

    def test_action_open_viewer_returns_client_action(self):
        builder = self._make_builder(code='pub_view', name='Pub View')
        builder.action_publish()
        action = builder.action_open_viewer()
        self.assertEqual(action['type'], 'ir.actions.client')
        self.assertEqual(action['tag'], 'eh_account_dynamic_report')
        self.assertEqual(action['context']['report_code'], 'pub_view')


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestBuilderRender(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Builder = cls.env['eh.report.builder']
        # Seed activity in the period.
        cls.post_balanced_move(
            [
                {'account': cls.account_revenue, 'credit': 1000.0},
                {'account': cls.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )
        cls.post_balanced_move(
            [
                {'account': cls.account_expense, 'debit': 300.0},
                {'account': cls.account_cash, 'credit': 300.0},
            ],
            date=fields.Date.from_string('2026-07-01'),
        )

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _make_pl_builder(self):
        builder = self.Builder.create({
            'code': 'simple_pl',
            'name': 'Simple P&L',
            'line_ids': [
                (0, 0, {
                    'sequence': 10,
                    'name': 'Income',
                    'line_type': 'section_header',
                }),
                (0, 0, {
                    'sequence': 20,
                    'name': 'Revenue',
                    'code': 'revenue',
                    'line_type': 'account_aggregate',
                    'account_scope': 'types',
                    'account_types': 'income,income_other',
                    'sign': '-',  # income is credit, flip to positive
                }),
                (0, 0, {
                    'sequence': 30,
                    'name': 'Expenses',
                    'line_type': 'section_header',
                }),
                (0, 0, {
                    'sequence': 40,
                    'name': 'Operating Expenses',
                    'code': 'opex',
                    'line_type': 'account_aggregate',
                    'account_scope': 'types',
                    'account_types': 'expense,expense_direct_cost',
                    'sign': '+',
                }),
                (0, 0, {
                    'sequence': 50,
                    'name': 'Net Profit',
                    'code': 'net_profit',
                    'line_type': 'formula',
                    'formula': 'revenue - opex',
                    'is_section_total': True,
                    'level': 0,
                }),
            ],
        })
        builder.action_publish()
        return builder

    def test_render_through_orchestrator(self):
        builder = self._make_pl_builder()
        result = builder.published_report_id.render(self.options)
        self.assertFalse(result['from_cache'])
        self.assertGreater(len(result['lines']), 0)

    def test_account_aggregate_values(self):
        builder = self._make_pl_builder()
        result = builder.published_report_id.render(self.options)
        # Find the revenue line by builder line code in meta.
        revenue_line = next(
            (l for l in result['lines']
             if (l.get('meta') or {}).get('builder_line_code') == 'revenue'),
            None,
        )
        self.assertIsNotNone(revenue_line)
        amount_cell = next(
            c for c in revenue_line['columns']
            if c['expression_label'] == 'amount'
        )
        # Revenue 1000 with sign flip = +1000.
        self.assertAlmostEqual(amount_cell['value'], 1000.0, places=2)

    def test_formula_line_combines_others(self):
        builder = self._make_pl_builder()
        result = builder.published_report_id.render(self.options)
        net_profit_line = next(
            (l for l in result['lines']
             if (l.get('meta') or {}).get('builder_line_code') == 'net_profit'),
            None,
        )
        self.assertIsNotNone(net_profit_line)
        amount_cell = next(
            c for c in net_profit_line['columns']
            if c['expression_label'] == 'amount'
        )
        # 1000 revenue - 300 opex = 700.
        self.assertAlmostEqual(amount_cell['value'], 700.0, places=2)

    def test_totals_dict_includes_named_lines(self):
        builder = self._make_pl_builder()
        result = builder.published_report_id.render(self.options)
        totals = result['totals']
        self.assertIn('revenue', totals)
        self.assertIn('opex', totals)
        self.assertIn('net_profit', totals)

    def test_show_zero_hides_zero_aggregates(self):
        # No revenue activity for a different builder.
        empty_builder = self.Builder.create({
            'code': 'empty_pl',
            'name': 'Empty PL',
            'line_ids': [
                (0, 0, {
                    'sequence': 10,
                    'name': 'Empty Account',
                    'code': 'empty',
                    'line_type': 'account_aggregate',
                    'account_scope': 'codes',
                    'account_codes': '9999',
                    'sign': '+',
                }),
            ],
        })
        empty_builder.action_publish()
        result = empty_builder.published_report_id.render({
            **self.options, 'show_zero': False,
        })
        empty_line = [
            l for l in result['lines']
            if (l.get('meta') or {}).get('builder_line_code') == 'empty'
        ]
        self.assertEqual(empty_line, [])
        # With show_zero true the line appears.
        result2 = empty_builder.published_report_id.render({
            **self.options, 'show_zero': True,
        })
        empty_line2 = [
            l for l in result2['lines']
            if (l.get('meta') or {}).get('builder_line_code') == 'empty'
        ]
        self.assertEqual(len(empty_line2), 1)

    def test_section_header_appears(self):
        builder = self._make_pl_builder()
        result = builder.published_report_id.render(self.options)
        headers = [
            l for l in result['lines']
            if (l.get('meta') or {}).get('kind') == 'section_header'
        ]
        self.assertGreaterEqual(len(headers), 2)

    def test_xlsx_export_works(self):
        builder = self._make_pl_builder()
        content = builder.published_report_id.render_xlsx(self.options)
        self.assertEqual(content[:2], b'PK')

    def test_account_codes_aggregate(self):
        builder = self.Builder.create({
            'code': 'codes_test',
            'name': 'Codes Test',
            'line_ids': [
                (0, 0, {
                    'sequence': 10,
                    'name': 'Cash and Receivables',
                    'code': 'cash_ar',
                    'line_type': 'account_aggregate',
                    'account_scope': 'codes',
                    'account_codes': '1',  # both 1000 and 1100 start with 1
                    'sign': '+',
                }),
            ],
        })
        builder.action_publish()
        result = builder.published_report_id.render(self.options)
        line = next(
            l for l in result['lines']
            if (l.get('meta') or {}).get('builder_line_code') == 'cash_ar'
        )
        amount = next(
            c['value'] for c in line['columns']
            if c['expression_label'] == 'amount'
        )
        # Cash debited 1000, then credited 300 = 700 closing on cash account.
        self.assertAlmostEqual(amount, 700.0, places=2)
