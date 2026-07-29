# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Per-user fold-state model tests.
"""

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('eh_account_base', 'unit')
class TestReportFoldState(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Model = self.env['eh.account.report.fold.state']
        self.user = self.env.user

    def test_set_then_get_round_trip(self):
        self.Model.set_for_user(
            'balance_sheet', 'section-assets-group-12', True,
        )
        result = self.Model.get_for_user('balance_sheet')
        self.assertEqual(
            result.get('section-assets-group-12'), True,
        )

    def test_set_overwrites_existing(self):
        self.Model.set_for_user('balance_sheet', 'l1', True)
        self.Model.set_for_user('balance_sheet', 'l1', False)
        result = self.Model.get_for_user('balance_sheet')
        self.assertEqual(result.get('l1'), False)

    def test_get_returns_empty_when_no_state(self):
        result = self.Model.get_for_user('not_a_real_report')
        self.assertEqual(result, {})

    def test_reset_clears_for_report_only(self):
        self.Model.set_for_user('balance_sheet', 'l1', True)
        self.Model.set_for_user('profit_and_loss', 'l2', True)
        self.Model.reset_for_user('balance_sheet')
        bs = self.Model.get_for_user('balance_sheet')
        pl = self.Model.get_for_user('profit_and_loss')
        self.assertEqual(bs, {})
        self.assertEqual(pl.get('l2'), True)

    def test_unique_per_user_report_line(self):
        from psycopg2.errors import UniqueViolation
        # Two creates with the same key should violate the constraint;
        # Odoo wraps it as IntegrityError.
        self.Model.create({
            'user_id': self.user.id,
            'report_code': 'balance_sheet',
            'line_id': 'unique-line',
            'is_unfolded': True,
        })
        with self.assertRaises(Exception):
            self.Model.create({
                'user_id': self.user.id,
                'report_code': 'balance_sheet',
                'line_id': 'unique-line',
                'is_unfolded': False,
            })

    def test_per_user_isolation(self):
        other = self.env['res.users'].create({
            'name': 'Other Test User',
            'login': 'fold_test_other',
            'email': 'fold_test_other@example.com',
        })
        self.Model.set_for_user('balance_sheet', 'l1', True)
        self.Model.set_for_user('balance_sheet', 'l2', True, user=other)
        mine = self.Model.get_for_user('balance_sheet')
        theirs = self.Model.get_for_user('balance_sheet', user=other)
        self.assertEqual(mine, {'l1': True})
        self.assertEqual(theirs, {'l2': True})
