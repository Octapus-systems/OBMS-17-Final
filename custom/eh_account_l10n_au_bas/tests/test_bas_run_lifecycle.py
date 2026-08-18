# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
BAS run lifecycle: draft -> computed -> lodged. Lodged is read-only.
"""

from datetime import date

from odoo.exceptions import UserError, ValidationError  # noqa: F401

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


class BasRunLifecycleTest(EhAccountIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.run = self.env['eh.bas.run'].create({
            'company_id': self.env.company.id,
            'date_from': date(2026, 4, 1),
            'date_to': date(2026, 6, 30),
            'name': 'BAS Q4-2026',
        })

    def test_initial_state_is_draft(self):
        self.assertEqual(self.run.state, 'draft')

    def test_compute_advances_to_computed(self):
        self.run.action_compute()
        self.assertEqual(self.run.state, 'computed')

    def test_recompute_on_lodged_raises(self):
        self.run.action_compute()
        self.run.action_mark_lodged()
        self.assertEqual(self.run.state, 'lodged')
        with self.assertRaises(UserError):
            self.run.action_compute()

    def test_unique_per_company_period(self):
        # second run for the exact same period in the same company:
        # the schema enforces uniqueness; constraint must fire.
        with self.assertRaises(Exception):
            self.env['eh.bas.run'].create({
                'company_id': self.env.company.id,
                'date_from': date(2026, 4, 1),
                'date_to': date(2026, 6, 30),
                'name': 'BAS Q4-2026 duplicate',
            })

    def test_audit_row_written_on_compute(self):
        before = self.env['eh.account.report.execution'].search_count([])
        self.run.action_compute()
        after = self.env['eh.account.report.execution'].search_count([])
        self.assertGreater(
            after, before,
            "Compute did not write a report.execution audit row",
        )

    def test_accruals_basis_computes(self):
        # The default (accruals) basis must still compute normally: the
        # cash-basis guard must not block the supported path.
        self.assertEqual(self.run.reporting_basis, 'accruals')
        self.run.action_compute()
        self.assertEqual(self.run.state, 'computed')

    def test_cash_basis_computes_without_refusing(self):
        # Cash basis recognises GST on the payment date, apportioned by the
        # paid fraction of each invoice. It must compute (never refuse) and
        # advance to computed. With no posted taxable activity on these
        # books the labels are zero rather than accrual figures under a
        # cash label.
        self.run.reporting_basis = 'cash'
        self.run.action_compute()
        self.assertEqual(self.run.state, 'computed')
        line_1a = self.env['eh.bas.run.line'].search([
            ('run_id', '=', self.run.id),
            ('label_id.code', '=', '1A'),
        ], limit=1)
        self.assertEqual(line_1a.amount, 0.0)
