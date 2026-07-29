# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Integration tests for the cache invalidation hook on account.move.

Covers:

* Posting a draft entry bumps the version counter.
* Cancelling a posted entry bumps the counter.
* Drafting a posted entry bumps the counter.
* An unrelated field write does not bump the counter.
* Multi company posts bump only the affected company's counter.
"""

from odoo.tests import tagged

from .common import EhAccountIntegrationTestCase


@tagged('eh_account_base', 'integration', 'post_install', '-at_install')
class TestCacheInvalidationHook(EhAccountIntegrationTestCase):

    def test_post_bumps_version(self):
        before = self.company.eh_move_version
        self.post_balanced_move([
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': self.account_cash, 'debit': 100.0},
        ])
        self.company.invalidate_recordset(['eh_move_version'])
        self.assertGreater(
            self.company.eh_move_version,
            before,
            "Posting a balanced entry should bump eh_move_version",
        )

    def test_cancel_bumps_version(self):
        move = self.post_balanced_move([
            {'account': self.account_revenue, 'credit': 50.0},
            {'account': self.account_cash, 'debit': 50.0},
        ])
        self.company.invalidate_recordset(['eh_move_version'])
        post_version = self.company.eh_move_version

        move.button_cancel()
        self.company.invalidate_recordset(['eh_move_version'])
        self.assertGreater(
            self.company.eh_move_version,
            post_version,
            "Cancelling a posted entry should bump eh_move_version",
        )

    def test_draft_bumps_version(self):
        move = self.post_balanced_move([
            {'account': self.account_revenue, 'credit': 75.0},
            {'account': self.account_cash, 'debit': 75.0},
        ])
        self.company.invalidate_recordset(['eh_move_version'])
        post_version = self.company.eh_move_version

        move.button_draft()
        self.company.invalidate_recordset(['eh_move_version'])
        self.assertGreater(
            self.company.eh_move_version,
            post_version,
            "Returning a posted entry to draft should bump eh_move_version",
        )

    def test_unrelated_write_does_not_bump(self):
        move = self.post_balanced_move([
            {'account': self.account_revenue, 'credit': 60.0},
            {'account': self.account_cash, 'debit': 60.0},
        ])
        self.company.invalidate_recordset(['eh_move_version'])
        baseline = self.company.eh_move_version

        # Write a non state field. Should not bump.
        move.write({'ref': 'NEW REFERENCE'})
        self.company.invalidate_recordset(['eh_move_version'])
        self.assertEqual(
            self.company.eh_move_version,
            baseline,
            "Writing a non state field must not bump eh_move_version",
        )

    def test_post_bumps_each_company_independently(self):
        other_company = self.env['res.company'].create({
            'name': 'Second Company',
            'currency_id': self.company.currency_id.id,
        })

        # Post in our default company.
        self.post_balanced_move([
            {'account': self.account_revenue, 'credit': 10.0},
            {'account': self.account_cash, 'debit': 10.0},
        ])
        self.env['res.company'].invalidate_model(['eh_move_version'])
        v_self = self.company.eh_move_version
        v_other = other_company.eh_move_version
        self.assertGreater(v_self, 0)
        self.assertEqual(v_other, 0,
                         "Other company's counter must not be bumped")
