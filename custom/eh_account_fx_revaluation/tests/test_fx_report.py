# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
FX revaluation report render regression.

The two shipped bugs on the front-end PDF reports were missing render
tests: nothing exercised the QWeb template, so a KeyError or bad
expression in the template only surfaced when a user clicked Print.
This proves the FX revaluation report renders to HTML for a real run.
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_fx_revaluation', 'integration', 'post_install', '-at_install')
class TestFxReport(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref('account.group_account_manager')
        cls.env.user.groups_id |= cls.env.ref('eh_account_base.group_eh_manager')

        cls.gain_account = cls._ensure_account(
            cls.env, '4920', 'Unrealised FX Gain', 'income_other',
        )
        cls.loss_account = cls._ensure_account(
            cls.env, '5930', 'Unrealised FX Loss', 'expense',
        )

    def test_report_renders_html(self):
        """action_report_fx_revaluation renders non-empty HTML for a run.

        Seeds a minimal valid eh.fx.revaluation.run (same fields the
        sibling model tests use), renders via _render_qweb_html so there
        is no wkhtmltopdf dependency, and asserts the QWeb template
        produced output. A non-empty body proves the template evaluated
        without a KeyError or render failure; the stable title string
        confirms the run's page was actually emitted.
        """
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        report = self.env.ref(
            'eh_account_fx_revaluation.action_report_fx_revaluation')
        html, ftype = report._render_qweb_html(report.report_name, run.ids)
        self.assertEqual(ftype, 'html')
        self.assertTrue(html)
        self.assertIn(b'FX Revaluation', html)
