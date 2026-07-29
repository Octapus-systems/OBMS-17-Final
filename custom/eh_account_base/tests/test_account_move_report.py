# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Render regression test for the branded Journal Entry PDF report.

The report (action ``eh_account_base.action_report_eh_account_move``,
template ``eh_account_base.report_eh_account_move``, model
``account.move``) has shipped rendering bugs twice, both caused by the
absence of a render test. This test drives the QWeb HTML render path
(no wkhtmltopdf dependency) against a real POSTED journal entry and
proves the template renders to non-empty HTML: a missing field, bad
attribute access, or template KeyError would surface here as a render
failure rather than as a broken print button in production.

There is no reliably stable title string in the rendered output across
Odoo series and localisations, so this asserts only on the render
mechanics: a non-empty ``html`` body and a ``ftype`` of ``'html'``.
"""

from odoo.tests import tagged

from .common import EhAccountIntegrationTestCase


@tagged('eh_account_base', 'integration', 'post_install', '-at_install')
class TestAccountMoveReportRender(EhAccountIntegrationTestCase):
    """Prove the Journal Entry report renders for a posted move."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A minimal, balanced, POSTED entry: one debit line and one credit
        # line, with a partner on the receivable leg so the template's
        # partner-facing branches are exercised.
        cls.move = cls.post_balanced_move([
            {
                'account': cls.account_receivable,
                'debit': 150.0,
                'partner': cls.partner_a,
                'name': 'Journal entry render test line',
            },
            {
                'account': cls.account_revenue,
                'credit': 150.0,
                'name': 'Journal entry render test counter-leg',
            },
        ])

    def test_journal_entry_report_renders(self):
        """The report renders to non-empty HTML for a posted move."""
        self.assertEqual(
            self.move.state, 'posted',
            'Fixture move must be posted before rendering the report.',
        )
        report = self.env.ref(
            'eh_account_base.action_report_eh_account_move')
        html, ftype = report._render_qweb_html(
            report.report_name, self.move.ids)
        self.assertEqual(ftype, 'html')
        # Non-empty HTML proves the template compiled and rendered without a
        # KeyError / attribute error / missing-field failure.
        self.assertTrue(html)
