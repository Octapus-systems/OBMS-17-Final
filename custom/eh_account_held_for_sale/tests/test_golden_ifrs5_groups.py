# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IFRS 5 disposal-group worked examples for eh_account_held_for_sale.

Each test is a hand-computed worked example in the shape of the IFRS 5 /
IAS 36 illustrative material (numbers only, recomputed by hand from the
inputs stated in the test). The exact journal entry the engine posts is
asserted line by line against literal amounts; nothing is read back from
the engine under test to build an expected value.

Allocation convention implemented by eh.disposal.group (read from
models/disposal_group.py):

* A group write-down (carrying amount less fair value less costs to sell,
  IFRS 5.15) is allocated goodwill-first (IAS 36.104), then pro rata by
  carrying amount over the in-scope non-goodwill members, never below a
  member's fair-value floor (IAS 36.105); a blocked excess re-prorates
  over the remaining scope members. Liability members and out-of-scope
  members (IFRS 5.5) receive no allocation.
* One journal entry per measurement event, with a per-member leg pair
  (impairment expense against the member account). Amounts are rounded
  to company currency (2dp) per member; the rounding residue lands on
  the first member with headroom.
* Reversals are pro rata to cumulative member write-downs, capped at the
  cumulative NON-goodwill write-down (IFRS 5.22; goodwill write-downs
  are never reversed, IAS 36.124).
"""

from datetime import timedelta

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo import fields
from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_held_for_sale', 'post_install',
        '-at_install')
class TestGoldenIfrs5Groups(EhGoldenTestCase):
    """IFRS 5.23 group write-down allocation, floors, scope exclusions,
    linked-asset lockstep, reversal caps, discontinued tagging and the
    12-month overdue flag."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.imp = cls._ensure_account(
            cls.env, '5175', 'HFS Group Impairment', 'expense')
        cls.gain = cls._ensure_account(
            cls.env, '4790', 'Group Disposal Gain/Loss', 'income_other')
        cls.acc_fallback = cls._ensure_account(
            cls.env, '1789', 'Disposal Group Assets', 'asset_current')
        cls.acc_gw = cls._ensure_account(
            cls.env, '1790', 'Goodwill Held for Sale', 'asset_current')
        cls.acc_a = cls._ensure_account(
            cls.env, '1791', 'HFS Member A', 'asset_current')
        cls.acc_b = cls._ensure_account(
            cls.env, '1792', 'HFS Member B', 'asset_current')
        cls.acc_inv = cls._ensure_account(
            cls.env, '1793', 'HFS Inventory at NRV', 'asset_current')
        cls.acc_lia = cls._ensure_account(
            cls.env, '2790', 'HFS Associated Liabilities',
            'liability_current')
        cls.disc_rev = cls._ensure_account(
            cls.env, '4791', 'Discontinued Revenue', 'income')
        cls.disc_exp = cls._ensure_account(
            cls.env, '5791', 'Discontinued Costs', 'expense')

    def _group(self, lines, **vals):
        base = {
            'name': '/',
            'fair_value_less_costs': 0.0,
            'asset_account_id': self.acc_fallback.id,
            'impairment_account_id': self.imp.id,
            'proceeds_account_id': self.account_cash.id,
            'gain_loss_account_id': self.gain.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, line) for line in lines],
        }
        base.update(vals)
        return self.env['eh.disposal.group'].create(base)

    def _line(self, group, name):
        line = group.line_ids.filtered(lambda line_item: line_item.name == name)
        self.assertEqual(len(line), 1, 'expected one line named %s' % name)
        return line

    def _running_asset(self, cost=36000.0):
        """A running fixed asset (straight line, 36 months, no proration)
        with its first depreciation line posted, so its ledger net book
        value is cost - cost/36."""
        accum = self._ensure_account(
            self.env, '1510', 'Accumulated Depreciation', 'asset_fixed')
        dep_exp = self._ensure_account(
            self.env, '5100', 'Depreciation Expense', 'expense_depreciation')
        fixed = self._ensure_account(
            self.env, '1500', 'Fixed Assets', 'asset_fixed')
        category = self.env['eh.asset.category'].search([
            ('code', '=', 'DGITHW'),
            ('company_id', '=', self.company.id)], limit=1)
        if not category:
            category = self.env['eh.asset.category'].create({
                'name': 'DG IT Hardware', 'code': 'DGITHW',
                'method': 'straight_line', 'useful_life_months': 36,
                'salvage_rate': 0.0, 'prorate_first_period': False,
                'asset_account_id': fixed.id,
                'depreciation_account_id': dep_exp.id,
                'accumulated_depreciation_account_id': accum.id,
                'journal_id': self.journal_misc.id,
            })
        asset = self.env['eh.asset'].create({
            'name': '/', 'category_id': category.id,
            'acquisition_date': '2026-01-01', 'in_service_date': '2026-01-31',
            'acquisition_cost': cost, 'salvage_value': 0.0,
            'method': 'straight_line', 'useful_life_months': 36,
            'prorate_first_period': False,
            'asset_account_id': fixed.id,
            'depreciation_account_id': dep_exp.id,
            'accumulated_depreciation_account_id': accum.id,
            'journal_id': self.journal_misc.id,
        })
        asset.action_activate()
        first = asset.depreciation_line_ids.sorted('depreciation_date')[0]
        first.action_post()
        return asset

    # ------------------------------------------------------------------
    # IFRS 5.23 / IAS 36.104: goodwill first, then pro rata
    # ------------------------------------------------------------------
    def test_golden_group_writedown_goodwill_first_then_prorata(self):
        """Group of goodwill 40,000 + plant A 156,000 + plant B 104,000,
        FVLCTS 220,000.

        Carrying = 40,000 + 156,000 + 104,000 = 300,000.
        Loss = 300,000 - 220,000 = 80,000.
        IAS 36.104(a): goodwill first -> 40,000 (fully consumed).
        Remaining 40,000 pro rata by carrying over A and B:
          A: 156,000 / 260,000 x 40,000 = 24,000
          B: 104,000 / 260,000 x 40,000 = 16,000
        One JE: DR impairment 40,000 + 24,000 + 16,000;
                CR goodwill 40,000, CR A 24,000, CR B 16,000.
        """
        group = self._group([
            {'name': 'GW', 'carrying_amount': 40000.0, 'is_goodwill': True,
             'account_id': self.acc_gw.id},
            {'name': 'A', 'carrying_amount': 156000.0,
             'account_id': self.acc_a.id},
            {'name': 'B', 'carrying_amount': 104000.0,
             'account_id': self.acc_b.id},
        ], fair_value_less_costs=220000.0)
        self.assertAlmostEqual(group.carrying_amount, 300000.0, places=2)
        self.assertAlmostEqual(group.writedown, 80000.0, places=2)
        group.action_classify()
        self.assertEqual(group.state, 'held')
        self.assertTrue(group.depreciation_ceased)
        self.assertEqual(len(group.move_ids), 1)
        self.assertMoveLines(group.move_ids, [
            (self.imp, 40000.0, 0.0),
            (self.imp, 24000.0, 0.0),
            (self.imp, 16000.0, 0.0),
            (self.acc_gw, 0.0, 40000.0),
            (self.acc_a, 0.0, 24000.0),
            (self.acc_b, 0.0, 16000.0),
        ])
        self.assertBalanced(group.move_ids)
        gw, a, b = (self._line(group, n) for n in ('GW', 'A', 'B'))
        self.assertAlmostEqual(gw.allocated_writedown, 40000.0, places=2)
        self.assertAlmostEqual(a.allocated_writedown, 24000.0, places=2)
        self.assertAlmostEqual(b.allocated_writedown, 16000.0, places=2)
        self.assertAlmostEqual(gw.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(a.carrying_amount, 132000.0, places=2)
        self.assertAlmostEqual(b.carrying_amount, 88000.0, places=2)
        # Group lands exactly on FVLCTS (IFRS 5.15 lower-of rule).
        self.assertAlmostEqual(group.carrying_amount, 220000.0, places=2)
        self.assertAlmostEqual(group.cumulative_writedown, 80000.0, places=2)

    # ------------------------------------------------------------------
    # IAS 36.105: fair-value floor stops allocation; excess re-prorates
    # ------------------------------------------------------------------
    def test_golden_floor_blocks_and_excess_reallocates(self):
        """A 156,000 with a fair-value floor of 140,000; B 104,000 with no
        floor. FVLCTS 220,000.

        Carrying = 260,000, loss = 40,000.
        Raw pro rata: A 156/260 x 40,000 = 24,000; B 104/260 x 40,000
        = 16,000. A's floor caps A at 156,000 - 140,000 = 16,000; the
        blocked 8,000 re-prorates onto B (the only remaining member):
        B = 16,000 + 8,000 = 24,000 (checked: B ends at 80,000, above
        zero, no floor breached).
        JE: DR impairment 16,000 + 24,000; CR A 16,000; CR B 24,000.
        """
        group = self._group([
            {'name': 'A', 'carrying_amount': 156000.0,
             'fair_value_floor': 140000.0, 'account_id': self.acc_a.id},
            {'name': 'B', 'carrying_amount': 104000.0,
             'account_id': self.acc_b.id},
        ], fair_value_less_costs=220000.0)
        group.action_classify()
        self.assertEqual(len(group.move_ids), 1)
        self.assertMoveLines(group.move_ids, [
            (self.imp, 16000.0, 0.0),
            (self.imp, 24000.0, 0.0),
            (self.acc_a, 0.0, 16000.0),
            (self.acc_b, 0.0, 24000.0),
        ])
        a, b = self._line(group, 'A'), self._line(group, 'B')
        self.assertAlmostEqual(a.carrying_amount, 140000.0, places=2)
        self.assertAlmostEqual(b.carrying_amount, 80000.0, places=2)
        self.assertAlmostEqual(group.carrying_amount, 220000.0, places=2)

    # ------------------------------------------------------------------
    # IFRS 5.5: out-of-scope members are excluded from allocation
    # ------------------------------------------------------------------
    def test_golden_out_of_scope_member_gets_no_allocation(self):
        """A 100,000 in scope; inventory at NRV 50,000 flagged out of the
        IFRS 5 measurement scope. FVLCTS 120,000.

        Carrying = 150,000, loss = 30,000. The inventory member is
        measured under IAS 2 and receives nothing; the whole 30,000 goes
        to A. JE: DR impairment 30,000; CR A 30,000.
        """
        group = self._group([
            {'name': 'A', 'carrying_amount': 100000.0,
             'account_id': self.acc_a.id},
            {'name': 'INV', 'carrying_amount': 50000.0, 'in_scope': False,
             'account_id': self.acc_inv.id},
        ], fair_value_less_costs=120000.0)
        group.action_classify()
        self.assertEqual(len(group.move_ids), 1)
        self.assertMoveLines(group.move_ids, [
            (self.imp, 30000.0, 0.0),
            (self.acc_a, 0.0, 30000.0),
        ])
        a, inv = self._line(group, 'A'), self._line(group, 'INV')
        self.assertAlmostEqual(a.carrying_amount, 70000.0, places=2)
        self.assertAlmostEqual(inv.allocated_writedown, 0.0, places=2)
        self.assertAlmostEqual(inv.carrying_amount, 50000.0, places=2)
        self.assertAlmostEqual(group.carrying_amount, 120000.0, places=2)

    # ------------------------------------------------------------------
    # IFRS 5.15/5.25: linked assets seed from NBV, one JE, NBV lockstep
    # ------------------------------------------------------------------
    def test_golden_linked_assets_one_je_and_nbv_lockstep(self):
        """Two linked assets, each 36,000 over 36 months with the first
        1,000 charge posted, so each has a ledger NBV of 35,000 (the
        hand-keyed line carrying of 1.0 must be overridden by the seed).

        Group carrying = 70,000; FVLCTS 60,000 -> loss 10,000, pro rata
        over equal carryings: 5,000 each. ONE journal entry:
        DR impairment 5,000 + 5,000; CR member accounts 5,000 each. Each
        asset's subledger falls in lockstep: NBV 30,000, accumulated
        impairment 5,000, and both assets are paused (IFRS 5.25).
        """
        asset1 = self._running_asset(cost=36000.0)
        asset2 = self._running_asset(cost=36000.0)
        self.assertAlmostEqual(asset1.net_book_value, 35000.0, places=2)
        self.assertAlmostEqual(asset2.net_book_value, 35000.0, places=2)
        group = self._group([
            {'name': 'A1', 'carrying_amount': 1.0, 'asset_id': asset1.id,
             'account_id': self.acc_a.id},
            {'name': 'A2', 'carrying_amount': 1.0, 'asset_id': asset2.id,
             'account_id': self.acc_b.id},
        ], fair_value_less_costs=60000.0)
        group.action_classify()
        # One entry for the whole group.
        self.assertEqual(len(group.move_ids), 1)
        self.assertMoveLines(group.move_ids, [
            (self.imp, 5000.0, 0.0),
            (self.imp, 5000.0, 0.0),
            (self.acc_a, 0.0, 5000.0),
            (self.acc_b, 0.0, 5000.0),
        ])
        l1, l2 = self._line(group, 'A1'), self._line(group, 'A2')
        # Seeded from NBV (35,000), then written down by 5,000 each.
        self.assertAlmostEqual(l1.carrying_amount, 30000.0, places=2)
        self.assertAlmostEqual(l2.carrying_amount, 30000.0, places=2)
        self.assertAlmostEqual(group.carrying_amount, 60000.0, places=2)
        # Asset subledgers in lockstep with the member carryings.
        for asset in (asset1, asset2):
            self.assertEqual(asset.state, 'paused')
            self.assertAlmostEqual(asset.net_book_value, 30000.0, places=2)
            self.assertAlmostEqual(
                asset.accumulated_impairment, 5000.0, places=2)
            impairment = asset.impairment_ids
            self.assertEqual(len(impairment), 1)
            self.assertEqual(impairment.state, 'posted')
            self.assertEqual(impairment.move_id, group.move_ids)
        self.assertTrue(l1.asset_paused_by_group)
        self.assertTrue(l2.asset_paused_by_group)

    def test_golden_depreciation_ceases_on_all_members(self):
        """Classification pauses every linked asset, so the depreciation
        cron posts nothing further for any member (IFRS 5.25). FVLCTS
        above carrying: pure classification, no write-down entry."""
        asset1 = self._running_asset(cost=36000.0)
        asset2 = self._running_asset(cost=36000.0)
        group = self._group([
            {'name': 'A1', 'carrying_amount': 1.0, 'asset_id': asset1.id,
             'account_id': self.acc_a.id},
            {'name': 'A2', 'carrying_amount': 1.0, 'asset_id': asset2.id,
             'account_id': self.acc_b.id},
        ], fair_value_less_costs=80000.0)
        group.action_classify()
        self.assertFalse(group.move_ids)
        self.assertTrue(group.depreciation_ceased)
        self.assertEqual(asset1.state, 'paused')
        self.assertEqual(asset2.state, 'paused')
        posted_before = [
            len(a.depreciation_line_ids.filtered(lambda line_item: line_item.is_posted))
            for a in (asset1, asset2)]
        self.env['eh.asset'].with_context(
            allowed_company_ids=self.company.ids)._cron_post_due()
        posted_after = [
            len(a.depreciation_line_ids.filtered(lambda line_item: line_item.is_posted))
            for a in (asset1, asset2)]
        self.assertEqual(posted_after, posted_before,
                         "The cron must not post depreciation for paused "
                         "held-for-sale members (IFRS 5.25).")
        # Cancellation resumes both (cease-to-be-classified, IFRS 5.26).
        group.action_cancel()
        self.assertEqual(asset1.state, 'running')
        self.assertEqual(asset2.state, 'running')

    # ------------------------------------------------------------------
    # IFRS 5.22: reversal capped at cumulative write-down
    # ------------------------------------------------------------------
    def test_golden_reversal_capped_at_cumulative(self):
        """A 100,000 + B 60,000, FVLCTS 120,000: loss 40,000 pro rata
        A 100/160 x 40,000 = 25,000, B 60/160 x 40,000 = 15,000.

        FVLCTS then rises to 200,000: the implied 80,000 gain is capped
        at the cumulative 40,000 write-down (IFRS 5.22), reversed pro
        rata to cumulative: A 25,000, B 15,000. Carrying returns to
        160,000 (never above pre-classification), impairment nets to nil.
        A further remeasure at the same FVLCTS is fully limited and
        refused.
        """
        group = self._group([
            {'name': 'A', 'carrying_amount': 100000.0,
             'account_id': self.acc_a.id},
            {'name': 'B', 'carrying_amount': 60000.0,
             'account_id': self.acc_b.id},
        ], fair_value_less_costs=120000.0)
        group.action_classify()
        self.assertAlmostEqual(group.carrying_amount, 120000.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.imp), 40000.0, places=2)
        group.fair_value_less_costs = 200000.0
        group.action_remeasure()
        reversal = group.move_ids.sorted('id')[-1]
        self.assertMoveLines(reversal, [
            (self.acc_a, 25000.0, 0.0),
            (self.acc_b, 15000.0, 0.0),
            (self.imp, 0.0, 25000.0),
            (self.imp, 0.0, 15000.0),
        ])
        self.assertAlmostEqual(group.carrying_amount, 160000.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.imp), 0.0, places=2)
        a, b = self._line(group, 'A'), self._line(group, 'B')
        self.assertAlmostEqual(a.cumulative_writedown, 0.0, places=2)
        self.assertAlmostEqual(b.cumulative_writedown, 0.0, places=2)
        # Cap now exhausted: any further upward remeasure is refused.
        with self.assertRaises(UserError):
            group.action_remeasure()

    def test_golden_reversal_never_touches_goodwill(self):
        """Goodwill 40,000 + A 60,000, FVLCTS 50,000: loss 50,000 takes
        goodwill 40,000 first, then A 10,000.

        FVLCTS then rises to 150,000. IAS 36.124 forbids reversing the
        goodwill write-down, so the cap is A's cumulative 10,000 only:
        JE DR A 10,000 / CR impairment 10,000; goodwill stays at zero and
        the group carries 60,000 (not 100,000).
        """
        group = self._group([
            {'name': 'GW', 'carrying_amount': 40000.0, 'is_goodwill': True,
             'account_id': self.acc_gw.id},
            {'name': 'A', 'carrying_amount': 60000.0,
             'account_id': self.acc_a.id},
        ], fair_value_less_costs=50000.0)
        group.action_classify()
        self.assertAlmostEqual(group.carrying_amount, 50000.0, places=2)
        group.fair_value_less_costs = 150000.0
        group.action_remeasure()
        reversal = group.move_ids.sorted('id')[-1]
        self.assertMoveLines(reversal, [
            (self.acc_a, 10000.0, 0.0),
            (self.imp, 0.0, 10000.0),
        ])
        gw, a = self._line(group, 'GW'), self._line(group, 'A')
        self.assertAlmostEqual(gw.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(gw.cumulative_writedown, 40000.0, places=2)
        self.assertAlmostEqual(a.carrying_amount, 60000.0, places=2)
        self.assertAlmostEqual(group.carrying_amount, 60000.0, places=2)

    # ------------------------------------------------------------------
    # Group sale: derecognition of members incl. liabilities
    # ------------------------------------------------------------------
    def test_golden_sale_derecognises_members_and_liability(self):
        """A 100,000 asset + 30,000 directly associated liability; group
        carrying 70,000 = FVLCTS (no write-down). Sold for 75,000.

        Gain = 75,000 - 70,000 = 5,000.
        JE: DR cash 75,000; DR liability 30,000 (derecognised);
            CR A 100,000; CR gain 5,000.
        """
        group = self._group([
            {'name': 'A', 'carrying_amount': 100000.0,
             'account_id': self.acc_a.id},
            {'name': 'LIA', 'carrying_amount': 30000.0,
             'is_liability': True, 'account_id': self.acc_lia.id},
        ], fair_value_less_costs=70000.0)
        group.action_classify()
        self.assertAlmostEqual(group.carrying_amount, 70000.0, places=2)
        group.proceeds = 75000.0
        group.action_sell()
        self.assertEqual(group.state, 'sold')
        sale = group.move_ids.sorted('id')[-1]
        self.assertMoveLines(sale, [
            (self.account_cash, 75000.0, 0.0),
            (self.acc_lia, 30000.0, 0.0),
            (self.acc_a, 0.0, 100000.0),
            (self.gain, 0.0, 5000.0),
        ])
        self.assertAlmostEqual(group.carrying_amount, 0.0, places=2)

    # ------------------------------------------------------------------
    # IFRS 5.33: discontinued-operations tag and the statements hook
    # ------------------------------------------------------------------
    def test_golden_discontinued_hook_returns_tagged_pl_total(self):
        """Tagging applies the per-company tag to the member lines' P&L
        accounts; eh_discontinued_pl_amount then returns the tagged
        accounts' posted P&L for the period, profit positive.

        Seeded entries on the tagged accounts:
          2026-03-01: revenue 1,000 (credit disc_rev)
          2026-04-01: costs 400 (debit disc_exp)
          2027-02-01: revenue 999 (outside the period, excluded)
        Expected 2026 total = 1,000 - 400 = 600.00.
        """
        group = self._group([
            {'name': 'A', 'carrying_amount': 10000.0,
             'account_id': self.acc_a.id, 'pl_account_id': self.disc_rev.id},
            {'name': 'B', 'carrying_amount': 5000.0,
             'account_id': self.acc_b.id, 'pl_account_id': self.disc_exp.id},
        ], fair_value_less_costs=15000.0, is_discontinued=True)
        group.action_tag_discontinued()
        tag = group.discontinued_tag_id
        self.assertTrue(tag)
        self.assertIn(tag, self.disc_rev.tag_ids)
        self.assertIn(tag, self.disc_exp.tag_ids)
        self.post_balanced_move([
            {'account': self.account_cash, 'debit': 1000.0},
            {'account': self.disc_rev, 'credit': 1000.0},
        ], date='2026-03-01')
        self.post_balanced_move([
            {'account': self.disc_exp, 'debit': 400.0},
            {'account': self.account_cash, 'credit': 400.0},
        ], date='2026-04-01')
        self.post_balanced_move([
            {'account': self.account_cash, 'debit': 999.0},
            {'account': self.disc_rev, 'credit': 999.0},
        ], date='2027-02-01')
        total = self.env['eh.disposal.group'].eh_discontinued_pl_amount(
            '2026-01-01', '2026-12-31', company=self.company)
        self.assertAlmostEqual(total, 600.0, places=2)
        # Re-running the action is idempotent (no duplicate tags).
        group.action_tag_discontinued()
        self.assertEqual(
            len(self.disc_rev.tag_ids.filtered(lambda t: t == tag)), 1)

    def test_golden_discontinued_tag_on_single_record(self):
        """The single-asset record shares the tag and the hook: picking
        P&L accounts and tagging feeds the same per-company total."""
        item = self.env['eh.held.for.sale'].create({
            'name': '/', 'carrying_amount': 1000.0,
            'fair_value_less_costs': 900.0,
            'is_discontinued': True,
            'discontinued_pl_account_ids': [(6, 0, self.disc_exp.ids)],
            'journal_id': self.journal_misc.id,
        })
        item.action_tag_discontinued()
        self.assertTrue(item.discontinued_tag_id)
        self.assertIn(item.discontinued_tag_id, self.disc_exp.tag_ids)
        self.post_balanced_move([
            {'account': self.disc_exp, 'debit': 250.0},
            {'account': self.account_cash, 'credit': 250.0},
        ], date='2026-05-01')
        total = self.env['eh.disposal.group'].eh_discontinued_pl_amount(
            '2026-01-01', '2026-12-31', company=self.company)
        self.assertAlmostEqual(total, -250.0, places=2)

    # ------------------------------------------------------------------
    # IFRS 5.9: 12-month overdue flag (disclose-first, no auto action)
    # ------------------------------------------------------------------
    def test_golden_overdue_12m_flag_flips(self):
        """A group held for 400 days is overdue; recording the IFRS 5.9
        extension clears the flag; a freshly classified group is not
        overdue. The flag never declassifies anything."""
        old_date = fields.Date.context_today(
            self.env['eh.disposal.group']) - timedelta(days=400)
        overdue = self._group([
            {'name': 'A', 'carrying_amount': 1000.0,
             'account_id': self.acc_a.id},
        ], fair_value_less_costs=2000.0, classification_date=old_date)
        overdue.action_classify()
        fresh = self._group([
            {'name': 'A', 'carrying_amount': 1000.0,
             'account_id': self.acc_a.id},
        ], fair_value_less_costs=2000.0)
        fresh.action_classify()
        self.assertTrue(overdue.overdue_12m)
        self.assertFalse(fresh.overdue_12m)
        self.assertEqual(overdue.state, 'held')  # never auto-declassified
        # The search filter finds exactly the overdue one.
        found = self.env['eh.disposal.group'].search(
            [('overdue_12m', '=', True), ('id', 'in',
              (overdue | fresh).ids)])  # noqa: E128
        self.assertEqual(found, overdue)
        not_overdue = self.env['eh.disposal.group'].search(
            [('overdue_12m', '=', False), ('id', 'in',
              (overdue | fresh).ids)])  # noqa: E128
        self.assertEqual(not_overdue, fresh)
        # The IFRS 5.9 extension suppresses the flag.
        overdue.extension_12m = True
        self.assertFalse(overdue.overdue_12m)
        # Same mechanics on the single-asset model.
        item = self.env['eh.held.for.sale'].create({
            'name': '/', 'carrying_amount': 1000.0,
            'fair_value_less_costs': 2000.0,
            'classification_date': old_date,
            'asset_account_id': self.acc_a.id,
            'impairment_account_id': self.imp.id,
            'journal_id': self.journal_misc.id,
        })
        item.action_classify()
        self.assertTrue(item.overdue_12m)
        item.extension_12m = True
        self.assertFalse(item.overdue_12m)

    # ------------------------------------------------------------------
    # Grouped single record: standalone remeasure blocked
    # ------------------------------------------------------------------
    def test_golden_grouped_single_record_remeasure_blocked(self):
        """A single held-for-sale record that joins a disposal group is
        measured at group level (IFRS 5.15): its standalone Remeasure is
        refused until it leaves the group."""
        group = self._group([
            {'name': 'A', 'carrying_amount': 1000.0,
             'account_id': self.acc_a.id},
        ], fair_value_less_costs=2000.0)
        item = self.env['eh.held.for.sale'].create({
            'name': '/', 'carrying_amount': 1000.0,
            'fair_value_less_costs': 800.0,
            'asset_account_id': self.acc_a.id,
            'impairment_account_id': self.imp.id,
            'journal_id': self.journal_misc.id,
        })
        item.action_classify()  # carrying now 800
        item.group_id = group.id
        item.fair_value_less_costs = 700.0
        with self.assertRaises(UserError):
            item.action_remeasure()
        # Leaving the group restores the standalone path unchanged.
        item.group_id = False
        item.action_remeasure()
        self.assertAlmostEqual(item.carrying_amount, 700.0, places=2)

    # ------------------------------------------------------------------
    # Frozen-after-post guardrails on the member lines
    # ------------------------------------------------------------------
    def test_golden_member_lines_frozen_after_classification(self):
        group = self._group([
            {'name': 'A', 'carrying_amount': 1000.0,
             'account_id': self.acc_a.id},
        ], fair_value_less_costs=800.0)
        group.action_classify()
        line = self._line(group, 'A')
        with self.assertRaises(UserError):
            line.carrying_amount = 5.0
        with self.assertRaises(UserError):
            self.env['eh.disposal.group.line'].create({
                'group_id': group.id, 'name': 'late',
                'carrying_amount': 1.0})
        with self.assertRaises(UserError):
            line.unlink()
        # The allocation stamps are engine output even in draft.
        draft = self._group([
            {'name': 'A', 'carrying_amount': 1000.0,
             'account_id': self.acc_a.id},
        ], fair_value_less_costs=800.0)
        with self.assertRaises(UserError):
            self._line(draft, 'A').cumulative_writedown = 123.0
        # A posted group cannot be deleted; its impairment row cannot be
        # cancelled standalone (it shares the group entry).
        with self.assertRaises(UserError):
            group.unlink()
