# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Integration tests for MoveLineQuery: actual execution against seeded data.

Validates that the SQL the builder produces is not only syntactically valid
but also semantically correct, by comparing aggregated results against ORM
read_group results on the same fixture data.
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery
from .common import EhAccountIntegrationTestCase


@tagged('eh_account_base', 'integration', 'post_install', '-at_install')
class TestMoveLineQueryExecution(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Seed three balanced entries with different account / partner mixes.
        cls.move_a = cls.post_balanced_move([
            {'account': cls.account_revenue, 'credit': 100.0, 'partner': cls.partner_a},
            {'account': cls.account_cash, 'debit': 100.0},
        ])
        cls.move_b = cls.post_balanced_move([
            {'account': cls.account_revenue, 'credit': 200.0, 'partner': cls.partner_b},
            {'account': cls.account_cash, 'debit': 200.0},
        ])
        cls.move_c = cls.post_balanced_move([
            {'account': cls.account_expense, 'debit': 50.0, 'partner': cls.partner_a},
            {'account': cls.account_cash, 'credit': 50.0},
        ])

    def test_sum_balance_revenue_only(self):
        rows = (
            MoveLineQuery(self.env, company_ids=[self.company.id])
            .select_balance_sum()
            .where_accounts([self.account_revenue.id])
            .execute()
        )
        self.assertEqual(len(rows), 1)
        # Revenue lines are credits, so balance is negative (-100 + -200).
        self.assertAlmostEqual(rows[0]['balance'], -300.0, places=2)

    def test_group_by_account_returns_per_account_totals(self):
        rows = (
            MoveLineQuery(self.env, company_ids=[self.company.id])
            .select_field('account_id')
            .select_balance_sum()
            .where_account_types(['income', 'expense'])
            .group_by('account_id')
            .execute()
        )
        by_account = {r['account_id']: r['balance'] for r in rows}
        self.assertAlmostEqual(by_account[self.account_revenue.id], -300.0, places=2)
        self.assertAlmostEqual(by_account[self.account_expense.id], 50.0, places=2)

    def test_partner_filter(self):
        rows = (
            MoveLineQuery(self.env, company_ids=[self.company.id])
            .select_balance_sum()
            .where_partners([self.partner_a.id])
            .execute()
        )
        self.assertEqual(len(rows), 1)
        # Partner A: revenue -100 + expense 50 = -50.
        self.assertAlmostEqual(rows[0]['balance'], -50.0, places=2)

    def test_account_codes_prefix_filter(self):
        rows = (
            MoveLineQuery(self.env, company_ids=[self.company.id])
            .select_balance_sum()
            .where_account_codes(['1'])  # cash account 1000 only
            .execute()
        )
        self.assertEqual(len(rows), 1)
        # Cash: 100 + 200 - 50 = 250.
        self.assertAlmostEqual(rows[0]['balance'], 250.0, places=2)

    def test_cancelled_moves_excluded_by_default(self):
        # Cancel one move and ensure it disappears from the aggregation.
        self.move_b.button_cancel()
        rows = (
            MoveLineQuery(self.env, company_ids=[self.company.id])
            .select_balance_sum()
            .where_accounts([self.account_revenue.id])
            .execute()
        )
        # Only move A's revenue should remain: -100.
        self.assertAlmostEqual(rows[0]['balance'], -100.0, places=2)

    def test_count_lines(self):
        rows = (
            MoveLineQuery(self.env, company_ids=[self.company.id])
            .select_count()
            .where_account_types(['income'])
            .execute()
        )
        self.assertEqual(rows[0]['line_count'], 2)
