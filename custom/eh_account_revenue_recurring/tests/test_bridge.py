# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Bridge tests: recurring invoice templates -> IFRS 15 revenue contracts.

A linked template must route generated invoice lines to the contract
liability account (deferred revenue) instead of income, register the billed
amount on the contract on posting (with the Record Billing asset-first
split replicated on the ledger), and leave unlinked templates byte
identical to before. Golden numbers are hand-derived from the inputs
stated in each test.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_revenue_recurring', 'post_install',
        '-at_install')
class TestRevenueRecurringBridge(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.contract_asset_acc = cls._ensure_account(
            cls.env, '1350', 'Contract Asset', 'asset_current')
        cls.contract_liab_acc = cls._ensure_account(
            cls.env, '2350', 'Contract Liability', 'liability_current')
        # Make the invoice receivable account deterministic for the exact
        # journal-entry assertions.
        cls.partner_a.property_account_receivable_id = cls.account_receivable

    def _contract(self, price=1200.0):
        c = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': price,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Support year', 'standalone_price': price,
                'satisfaction': 'over_time',
                'progress_method': 'output_milestones',
                'method_basis': 'Monthly service milestones depict the '
                                'stand-ready transfer (IFRS 15.B15).',
            })],
        })
        return c

    def _template(self, code, **overrides):
        vals = {
            'name': 'Monthly support %s' % code,
            'code': code,
            'partner_id': self.partner_a.id,
            'journal_id': self.journal_sale.id,
            'interval': 1,
            'interval_unit': 'month',
            'start_date': fields.Date.context_today(self.env.user),
            'next_run_date': fields.Date.context_today(self.env.user),
            'auto_post': True,
            'line_ids': [(0, 0, {
                'name': 'Support fee',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        }
        vals.update(overrides)
        return self.env['eh.recurring.invoice.template'].create(vals)

    def test_golden_linked_template_credits_contract_liability(self):
        """Linked template, 100 per month, no tax.

        The generated invoice must post
        Dr trade receivable 100 / Cr contract liability 100
        (NOT income: the line account override routes the 100 to the
        contract's billing flow), and the contract registers 100 billed,
        so the position shows a 100 contract liability.
        """
        contract = self._contract()
        contract.action_activate()
        tpl = self._template('rev_linked', revenue_contract_id=contract.id)
        tpl.action_generate_now()
        inv = tpl.last_generated_move_id
        self.assertEqual(inv.state, 'posted')
        self.assertEqual(inv.move_type, 'out_invoice')
        self.assertEqual(inv.eh_revenue_contract_id, contract)
        self.assertEqual(inv.invoice_line_ids.account_id,
                         self.contract_liab_acc)
        self.assertMoveLines(inv, [
            (self.account_receivable, 100.0, 0.0),
            (self.contract_liab_acc, 0.0, 100.0),
        ])
        self.assertTrue(inv.eh_revenue_billing_registered)
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 100.0, places=2)
        # No income was touched; the ledger carries the deferred credit.
        self.assertAlmostEqual(
            self.posted_balance(self.account_revenue), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.contract_liab_acc), -100.0, places=2)

    def test_unlinked_template_still_posts_to_income(self):
        # Regression: without a contract link the behaviour is byte
        # identical to the recurring module alone.
        tpl = self._template('rev_free')
        tpl.action_generate_now()
        inv = tpl.last_generated_move_id
        self.assertEqual(inv.state, 'posted')
        self.assertEqual(inv.invoice_line_ids.account_id,
                         self.account_revenue)
        self.assertFalse(inv.eh_revenue_contract_id)
        self.assertFalse(inv.eh_revenue_billing_registered)
        self.assertAlmostEqual(
            self.posted_balance(self.account_revenue), -100.0, places=2)

    def test_golden_recognition_releases_bridged_billing(self):
        """End to end: bridge billing 100, then 50% progress on the 1,200
        obligation targets 600 cumulative revenue. The recognition run
        releases the 100 liability first and books the 500 remainder as a
        contract asset:
        Dr contract liability 100 / Dr contract asset 500 / Cr revenue 600.
        """
        contract = self._contract()
        contract.action_activate()
        tpl = self._template('rev_release', revenue_contract_id=contract.id)
        tpl.action_generate_now()
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)
        contract.obligation_ids.percent_complete = 50.0
        contract.action_recognise()
        move = contract.move_ids.sorted('id')[-1]
        self.assertMoveLines(move, [
            (self.contract_liab_acc, 100.0, 0.0),
            (self.contract_asset_acc, 500.0, 0.0),
            (self.account_revenue, 0.0, 600.0),
        ])
        self.assertAlmostEqual(contract.contract_asset, 500.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 0.0, places=2)

    def test_golden_asset_cleared_before_liability(self):
        """Recognition first (600 contract asset), then a bridged invoice
        of 100 must replicate the Record Billing split: the invoice
        credits the liability, and the registration reclassifies the
        asset-clearing portion:
        reclass entry Dr contract liability 100 / Cr contract asset 100,
        leaving asset 500, liability 0 on both the ledger and the stored
        position.
        """
        contract = self._contract()
        contract.action_activate()
        contract.obligation_ids.percent_complete = 50.0
        contract.action_recognise()
        self.assertAlmostEqual(contract.contract_asset, 600.0, places=2)
        tpl = self._template('rev_reclass', revenue_contract_id=contract.id)
        tpl.action_generate_now()
        reclass = contract.move_ids.sorted('id')[-1]
        self.assertMoveLines(reclass, [
            (self.contract_liab_acc, 100.0, 0.0),
            (self.contract_asset_acc, 0.0, 100.0),
        ])
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)
        self.assertAlmostEqual(contract.contract_asset, 500.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.contract_liab_acc), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.contract_asset_acc), 500.0, places=2)

    def test_repost_does_not_double_count(self):
        contract = self._contract()
        contract.action_activate()
        tpl = self._template('rev_repost', revenue_contract_id=contract.id)
        tpl.action_generate_now()
        inv = tpl.last_generated_move_id
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)
        inv.button_draft()
        inv.action_post()
        # The registered flag makes the billing registration idempotent.
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)

    def test_inactive_contract_blocks_generation(self):
        contract = self._contract()  # left in draft
        tpl = self._template('rev_draft', revenue_contract_id=contract.id)
        with self.assertRaises(UserError):
            tpl.action_generate_now()
