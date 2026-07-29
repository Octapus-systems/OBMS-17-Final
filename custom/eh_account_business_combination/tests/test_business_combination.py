# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 3 goodwill and IAS 28 equity method tests."""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_business_combination', 'integration', 'post_install',
        '-at_install')
class TestBusinessCombination(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.goodwill_acc = cls._ensure_account(
            cls.env, '1800', 'Goodwill', 'asset_non_current')
        cls.clearing = cls._ensure_account(
            cls.env, '1801', 'Acquisition Clearing', 'asset_current')
        cls.gain = cls._ensure_account(
            cls.env, '4800', 'Bargain Purchase Gain', 'income_other')
        cls.nci_equity = cls._ensure_account(
            cls.env, '3800', 'Non-controlling Interest', 'equity')
        cls.ppa_ppe = cls._ensure_account(
            cls.env, '1600', 'Property Plant Equipment', 'asset_fixed')
        cls.ppa_payable = cls._ensure_account(
            cls.env, '2600', 'Assumed Payables', 'liability_current')
        cls.deferred_tax = cls._ensure_account(
            cls.env, '2700', 'Deferred Tax Liability', 'liability_non_current')
        cls.investment = cls._ensure_account(
            cls.env, '1820', 'Investment in Associate', 'asset_non_current')
        cls.share_profit = cls._ensure_account(
            cls.env, '4820', 'Share of Profit of Associates', 'income_other')
        cls.impairment = cls._ensure_account(
            cls.env, '5820', 'Impairment of Associate', 'expense')
        cls.disposal_gain = cls._ensure_account(
            cls.env, '4830', 'Gain on Disposal of Associate', 'income_other')
        cls.disposal_loss = cls._ensure_account(
            cls.env, '5830', 'Loss on Disposal of Associate', 'expense')

    def _bal(self, account):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', account.id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    # ---- IFRS 3 ----

    def _combination(self, **vals):
        base = {
            'name': '/', 'acquiree_name': 'Target Ltd',
            'consideration_transferred': 1000.0, 'nci_amount': 200.0,
            'fv_identifiable_net_assets': 900.0,
            'goodwill_account_id': self.goodwill_acc.id,
            'clearing_account_id': self.clearing.id,
            'gain_account_id': self.gain.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.business.combination'].create(base)

    def test_goodwill_computed(self):
        # 1000 + 200 + 0 - 900 = 300 goodwill.
        c = self._combination()
        self.assertAlmostEqual(c.goodwill, 300.0, places=2)
        self.assertAlmostEqual(c.bargain_purchase_gain, 0.0, places=2)

    def test_goodwill_posted(self):
        c = self._combination()
        c.action_recognise()
        self.assertEqual(c.state, 'recognised')
        self.assertAlmostEqual(self._bal(self.goodwill_acc), 300.0, places=2)
        self.assertAlmostEqual(self._bal(self.clearing), -300.0, places=2)

    def test_bargain_purchase(self):
        # 1000 + 0 - 1300 = -300 -> bargain gain 300.
        c = self._combination(nci_amount=0.0,
                              fv_identifiable_net_assets=1300.0)
        self.assertAlmostEqual(c.goodwill, 0.0, places=2)
        self.assertAlmostEqual(c.bargain_purchase_gain, 300.0, places=2)
        c.action_recognise()
        self.assertAlmostEqual(self._bal(self.gain), -300.0, places=2)

    def test_step_acquisition_prior_interest(self):
        # 1000 + 200 + 150 - 900 = 450.
        c = self._combination(previously_held_interest_fv=150.0)
        self.assertAlmostEqual(c.goodwill, 450.0, places=2)

    # ---- IFRS 3 full purchase price allocation ----

    def _combination_ppa(self, **vals):
        # Assets 1500, liability 600 -> identifiable net assets 900.
        base = {
            'asset_line_ids': [
                (0, 0, {'name': 'PPE', 'account_id': self.ppa_ppe.id,
                        'fair_value': 1500.0, 'is_liability': False}),
                (0, 0, {'name': 'Payables',
                        'account_id': self.ppa_payable.id,
                        'fair_value': 600.0, 'is_liability': True}),
            ],
            'nci_account_id': self.nci_equity.id,
        }
        base.update(vals)
        return self._combination(**base)

    def test_ppa_identifiable_net_assets_from_lines(self):
        c = self._combination_ppa()
        self.assertAlmostEqual(c.identifiable_net_assets, 900.0, places=2)
        # Lines drive goodwill: 1000 + 200 + 0 - 900 = 300.
        self.assertAlmostEqual(c.goodwill, 300.0, places=2)

    def test_ppa_full_entry_posts_balanced(self):
        c = self._combination_ppa()
        c.action_recognise_ppa()
        self.assertEqual(c.state, 'recognised')
        move = c.move_id
        self.assertTrue(move)
        # Balanced by construction.
        self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                               sum(move.line_ids.mapped('credit')), places=2)
        # Assets at fair value, goodwill, consideration, NCI.
        self.assertAlmostEqual(self._bal(self.ppa_ppe), 1500.0, places=2)
        self.assertAlmostEqual(self._bal(self.ppa_payable), -600.0, places=2)
        self.assertAlmostEqual(self._bal(self.goodwill_acc), 300.0, places=2)
        self.assertAlmostEqual(self._bal(self.clearing), -1000.0, places=2)
        self.assertAlmostEqual(self._bal(self.nci_equity), -200.0, places=2)

    def test_ppa_bargain_full_entry_balanced(self):
        # Consideration 500, nci 0 -> 500 - 900 = -400 bargain gain.
        c = self._combination_ppa(consideration_transferred=500.0,
                                   nci_amount=0.0)
        self.assertAlmostEqual(c.bargain_purchase_gain, 400.0, places=2)
        c.action_recognise_ppa()
        move = c.move_id
        self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                               sum(move.line_ids.mapped('credit')), places=2)
        self.assertAlmostEqual(self._bal(self.gain), -400.0, places=2)

    def test_ppa_prior_interest_balances(self):
        # Prior interest is carried via clearing so the entry still balances.
        c = self._combination_ppa(previously_held_interest_fv=150.0)
        c.action_recognise_ppa()
        move = c.move_id
        self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                               sum(move.line_ids.mapped('credit')), places=2)
        # Clearing carries consideration 1000 + prior 150 = 1150.
        self.assertAlmostEqual(self._bal(self.clearing), -1150.0, places=2)

    def test_nci_proportionate_computed(self):
        # IFRS 3.19 proportionate basis: NCI = minority% x FV of identifiable
        # net assets, computed, not a manual figure. 20% x 900 = 180.
        c = self._combination(nci_measurement='proportionate', nci_pct=20.0,
                              nci_amount=999.0)
        self.assertAlmostEqual(c.nci_amount, 180.0, places=2)
        # Goodwill uses the computed NCI: 1000 + 180 + 0 - 900 = 280.
        self.assertAlmostEqual(c.goodwill, 280.0, places=2)

    def test_nci_proportionate_net_of_deferred_tax(self):
        # IFRS 3.19 / IAS 12.66: proportionate NCI is a share of the
        # *recognised* identifiable net assets, which are net of the deferred
        # tax on the fair-value step-up, not the pre-tax fair value. PPE FV
        # 1500 over a 1000 tax base is a 500 taxable difference; at 20% the
        # deferred tax liability is 100, so recognised net assets are
        # 900 - 100 = 800. NCI at 20% is 160 (the pre-tax base would wrongly
        # give 180), and goodwill = 1000 + 160 - 900 + 100 = 360 (not 380).
        c = self._combination_ppa(
            nci_measurement='proportionate', nci_pct=20.0, nci_amount=999.0,
            tax_rate=20.0, deferred_tax_account_id=self.deferred_tax.id,
            asset_line_ids=[
                (0, 0, {'name': 'PPE', 'account_id': self.ppa_ppe.id,
                        'fair_value': 1500.0, 'tax_base': 1000.0,
                        'is_liability': False}),
                (0, 0, {'name': 'Payables',
                        'account_id': self.ppa_payable.id,
                        'fair_value': 600.0, 'tax_base': 600.0,
                        'is_liability': True}),
            ])
        self.assertAlmostEqual(c.deferred_tax, 100.0, places=2)
        self.assertAlmostEqual(c.identifiable_net_assets, 900.0, places=2)
        # Post-tax base drives NCI: (900 - 100) x 20% = 160.
        self.assertAlmostEqual(c.nci_amount, 160.0, places=2)
        self.assertAlmostEqual(c.goodwill, 360.0, places=2)
        c.action_recognise_ppa()
        move = c.move_id
        self.assertTrue(move)
        # Balanced by construction, with NCI and goodwill at the post-tax
        # figures.
        self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                               sum(move.line_ids.mapped('credit')), places=2)
        self.assertAlmostEqual(self._bal(self.nci_equity), -160.0, places=2)
        self.assertAlmostEqual(self._bal(self.goodwill_acc), 360.0, places=2)

    def test_nci_fair_value_kept(self):
        # Fair-value basis keeps the entered amount (byte-identical default).
        c = self._combination(nci_measurement='fair_value', nci_amount=200.0)
        self.assertAlmostEqual(c.nci_amount, 200.0, places=2)

    def test_recognised_combination_frozen(self):
        # IFRS 3: a recognised, posted combination cannot be retro-edited out
        # from under its move.
        c = self._combination()
        c.action_recognise()
        self.assertEqual(c.state, 'recognised')
        with self.assertRaises(UserError):
            c.consideration_transferred = 2000.0
        with self.assertRaises(UserError):
            c.unlink()

    def test_no_lines_still_goodwill_plug(self):
        # Regression: no asset lines -> byte-identical goodwill plug.
        c = self._combination()
        self.assertFalse(c.asset_line_ids)
        c.action_recognise()
        self.assertEqual(c.state, 'recognised')
        self.assertAlmostEqual(self._bal(self.goodwill_acc), 300.0, places=2)
        self.assertAlmostEqual(self._bal(self.clearing), -300.0, places=2)

    # ---- IAS 12 deferred tax on the fair-value step-up ----

    def test_deferred_tax_default_off(self):
        # Regression: no tax rate -> no deferred tax, goodwill unchanged.
        c = self._combination_ppa()
        self.assertAlmostEqual(c.deferred_tax, 0.0, places=2)
        self.assertAlmostEqual(c.goodwill, 300.0, places=2)

    def test_deferred_tax_raises_goodwill_and_books_dtl(self):
        # IAS 12.19/24/.66: PPE stepped up 1500 over a 1000 tax base is a 500
        # taxable temporary difference; the payable's fair value equals its tax
        # base so it adds nothing. At 20% the deferred tax liability is 100,
        # which raises goodwill from 300 to 400.
        c = self._combination_ppa(
            tax_rate=20.0,
            deferred_tax_account_id=self.deferred_tax.id,
            asset_line_ids=[
                (0, 0, {'name': 'PPE', 'account_id': self.ppa_ppe.id,
                        'fair_value': 1500.0, 'tax_base': 1000.0,
                        'is_liability': False}),
                (0, 0, {'name': 'Payables',
                        'account_id': self.ppa_payable.id,
                        'fair_value': 600.0, 'tax_base': 600.0,
                        'is_liability': True}),
            ])
        self.assertAlmostEqual(c.fair_value_step_up, 500.0, places=2)
        self.assertAlmostEqual(c.deferred_tax, 100.0, places=2)
        # Goodwill lifted by the DTL: 300 base -> 400.
        self.assertAlmostEqual(c.goodwill, 400.0, places=2)
        c.action_recognise_ppa()
        move = c.move_id
        self.assertTrue(move)
        # Balanced by construction.
        self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                               sum(move.line_ids.mapped('credit')), places=2)
        # DTL credited 100, goodwill carries the uplift.
        self.assertAlmostEqual(self._bal(self.deferred_tax), -100.0, places=2)
        self.assertAlmostEqual(self._bal(self.goodwill_acc), 400.0, places=2)

    # ---- IAS 28 ----

    def _investment(self, **vals):
        base = {
            'name': '/', 'investee_name': 'Assoc Ltd', 'ownership_pct': 30.0,
            'cost_of_investment': 1000.0,
            'investment_account_id': self.investment.id,
            'share_of_profit_account_id': self.share_profit.id,
            'cash_account_id': self.account_cash.id,
            'impairment_account_id': self.impairment.id,
            'disposal_gain_loss_account_id': self.disposal_gain.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.equity.investment'].create(base)

    def test_dispose_posts_gain(self):
        # IAS 28.22: proceeds above the carrying amount are a gain to P&L and
        # the carrying amount is derecognised.
        inv = self._investment(cost_of_investment=1000.0)
        inv.action_activate()
        inv.disposal_proceeds = 1250.0
        inv.action_dispose()
        self.assertEqual(inv.state, 'disposed')
        self.assertAlmostEqual(inv.carrying_amount, 0.0, places=2)
        # Acquisition Dr 1000 then derecognition Cr 1000 net the investment
        # account to zero; gain 250 to P&L; cash = 1250 proceeds - 1000 paid.
        self.assertAlmostEqual(self._bal(self.investment), 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.disposal_gain), -250.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_cash), 250.0, places=2)
        for move in inv.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_dispose_posts_loss(self):
        # Proceeds below the carrying amount are a loss to P&L.
        inv = self._investment(cost_of_investment=1000.0,
                               disposal_gain_loss_account_id=self.disposal_loss.id)
        inv.action_activate()
        inv.disposal_proceeds = 700.0
        inv.action_dispose()
        # Acquisition Dr 1000 nets against derecognition Cr 1000 -> zero.
        self.assertAlmostEqual(self._bal(self.investment), 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.disposal_loss), 300.0, places=2)
        for move in inv.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_dispose_requires_active(self):
        inv = self._investment()
        with self.assertRaises(UserError):
            inv.action_dispose()

    def test_equity_method_roll_forward(self):
        inv = self._investment(ownership_pct=30.0, cost_of_investment=1000.0)
        inv.action_activate()
        self.assertAlmostEqual(inv.carrying_amount, 1000.0, places=2)
        # Investee profit 500 x 30% = 150.
        inv.investee_profit = 500.0
        inv.action_pickup_profit()
        self.assertAlmostEqual(inv.carrying_amount, 1150.0, places=2)
        # Acquisition Dr 1000 + share of profit Dr 150 = 1150 on the account.
        self.assertAlmostEqual(self._bal(self.investment), 1150.0, places=2)
        self.assertAlmostEqual(self._bal(self.share_profit), -150.0, places=2)
        # Dividend 60 reduces carrying, is not income.
        inv.dividend_received = 60.0
        inv.action_record_dividend()
        self.assertAlmostEqual(inv.carrying_amount, 1090.0, places=2)
        # Impair 90.
        inv.impairment_amount = 90.0
        inv.action_impair()
        self.assertAlmostEqual(inv.carrying_amount, 1000.0, places=2)
        self.assertAlmostEqual(self._bal(self.impairment), 90.0, places=2)

    def test_equity_method_share_of_loss(self):
        inv = self._investment(ownership_pct=40.0, cost_of_investment=1000.0)
        inv.action_activate()
        inv.investee_profit = -250.0  # loss
        inv.action_pickup_profit()
        # 40% of -250 = -100.
        self.assertAlmostEqual(inv.carrying_amount, 900.0, places=2)

    def test_equity_method_loss_floored_at_zero(self):
        # IAS 28.38: a share of loss exceeding the carrying amount is capped
        # so the carrying amount floors at zero, not negative.
        inv = self._investment(ownership_pct=50.0, cost_of_investment=1000.0)
        inv.action_activate()
        # 50% of -3000 = -1500, which exceeds the 1000 carrying amount.
        inv.investee_profit = -3000.0
        inv.action_pickup_profit()
        self.assertAlmostEqual(inv.carrying_amount, 0.0, places=2)
        # Acquisition Dr 1000 then share-of-loss Cr 1000 (floored at nil) net
        # the investment account to zero.
        self.assertAlmostEqual(self._bal(self.investment), 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.share_profit), 1000.0, places=2)
        # The entry balances by construction.
        for move in inv.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)
        # Once at nil, no further loss can be recognised.
        inv.investee_profit = -500.0
        with self.assertRaises(UserError):
            inv.action_pickup_profit()

    def test_pickup_requires_active(self):
        inv = self._investment()
        inv.investee_profit = 100.0
        with self.assertRaises(UserError):
            inv.action_pickup_profit()

    def test_entries_balance(self):
        inv = self._investment()
        inv.action_activate()
        inv.investee_profit = 500.0
        inv.action_pickup_profit()
        for move in inv.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)
