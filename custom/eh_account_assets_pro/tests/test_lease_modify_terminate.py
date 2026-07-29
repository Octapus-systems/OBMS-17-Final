# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Lease modification and termination flows.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestLeaseModifyTerminate(EhAssetTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref('account.group_account_manager')

    def test_modify_extends_term(self):
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=24, cadence='monthly',
            payment_amount=1000.0,
        )
        lease.action_activate()
        lease.action_post_due_lines()
        wizard = self.env['eh.lease.modify.wizard'].create({
            'lease_id': lease.id,
            'modification_date': '2026-04-30',
            'new_term_months': 24,
            'new_payment_amount': 800.0,
            'new_ibr': 7.0,
        })
        wizard.action_modify()
        self.assertEqual(lease.state, 'modified')
        self.assertEqual(lease.term_months, 24)
        self.assertEqual(lease.payment_amount, 800.0)
        self.assertEqual(lease.modification_count, 1)
        # Has unposted lines under the new schedule.
        unposted = lease.schedule_line_ids.filtered(lambda l: not l.is_posted)
        self.assertGreater(len(unposted), 0)

    def test_modify_blocked_in_draft(self):
        lease = self._make_lease()
        wizard = self.env['eh.lease.modify.wizard'].create({
            'lease_id': lease.id,
            'modification_date': '2026-04-30',
            'new_term_months': 24,
            'new_payment_amount': 800.0,
            'new_ibr': 7.0,
        })
        with self.assertRaises(UserError):
            wizard.action_modify()

    def test_terminate_records_pl_difference(self):
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=24, cadence='monthly',
            payment_amount=1000.0,
        )
        lease.action_activate()
        lease.action_post_due_lines()
        wizard = self.env['eh.lease.terminate.wizard'].create({
            'lease_id': lease.id,
            'termination_date': '2026-04-30',
            'settlement_amount': 0.0,
            'pl_account_id': self.account_termination_pl.id,
        })
        wizard.action_terminate()
        self.assertEqual(lease.state, 'terminated')
        self.assertTrue(lease.termination_move_id)
        # No unposted schedule lines remain.
        self.assertFalse(lease.schedule_line_ids.filtered(
            lambda l: not l.is_posted,
        ))

    def test_terminate_with_settlement(self):
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=12, cadence='monthly',
            payment_amount=1000.0,
        )
        lease.action_activate()
        wizard = self.env['eh.lease.terminate.wizard'].create({
            'lease_id': lease.id,
            'termination_date': '2026-04-30',
            'settlement_amount': 500.0,
            'settlement_account_id': self.account_cash.id,
            'pl_account_id': self.account_termination_pl.id,
        })
        wizard.action_terminate()
        self.assertEqual(lease.state, 'terminated')
        # Cash credit leg posted.
        cash_lines = lease.termination_move_id.line_ids.filtered(
            lambda l: l.account_id == self.account_cash,
        )
        self.assertEqual(len(cash_lines), 1)
        self.assertAlmostEqual(cash_lines.credit, 500.0, places=2)

    def test_terminate_blocked_when_terminated(self):
        lease = self._make_lease(
            commencement_date='2025-01-31',
            term_months=12, cadence='monthly',
            payment_amount=1000.0,
        )
        lease.action_activate()
        wizard = self.env['eh.lease.terminate.wizard'].create({
            'lease_id': lease.id,
            'pl_account_id': self.account_termination_pl.id,
        })
        wizard.action_terminate()
        wizard2 = self.env['eh.lease.terminate.wizard'].create({
            'lease_id': lease.id,
            'pl_account_id': self.account_termination_pl.id,
        })
        with self.assertRaises(UserError):
            wizard2.action_terminate()
