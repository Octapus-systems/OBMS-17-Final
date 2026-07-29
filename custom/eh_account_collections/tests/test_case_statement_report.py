# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Render guard for the collections case-statement PDF.

The template used ``fields.Date.context_today`` / ``fields.Datetime.now``,
but ``fields`` is not in the QWeb render context, so printing a case
statement failed with KeyError: 'fields'. This test renders the report the
way the Print button does and fails if that context regression returns.
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_collections', 'integration', 'post_install',
        '-at_install')
class TestCaseStatementReport(EhAccountIntegrationTestCase):

    def test_case_statement_report_renders(self):
        case = self.env['eh.collections.case'].create({
            'partner_id': self.partner_a.id})
        report = self.env.ref(
            'eh_account_collections.action_report_case_statement')
        html, ftype = report._render_qweb_html(report.report_name, case.ids)
        self.assertEqual(ftype, 'html')
        self.assertTrue(html)
        self.assertIn(b'Statement of Account', html)
