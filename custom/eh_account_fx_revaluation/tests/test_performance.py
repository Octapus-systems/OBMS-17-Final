# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
FX revaluation performance regression guards.
"""

from odoo import fields  # noqa: F401
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_fx_revaluation', 'performance',
        'post_install', '-at_install')
class TestFxPerformance(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gain_account = cls._ensure_account(
            cls.env, '4920', 'Unrealised FX Gain', 'income_other',
        )
        cls.loss_account = cls._ensure_account(
            cls.env, '5930', 'Unrealised FX Loss', 'expense',
        )

    def test_query_budget_compute_with_no_balances(self):
        """Computing a run on an empty ledger should be cheap."""
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-12-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        with self.assertQueryCount(__system__=120):
            run.action_compute()
        self.assertEqual(run.state, 'computed')
