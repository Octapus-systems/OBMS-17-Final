# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 1 primary statement tests."""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_statements', 'integration', 'post_install', '-at_install')
class TestStatements(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # action_confirm / action_set_to_draft on both primary statements are
        # manager-gated (segregation of duties). The acting test user must be
        # an EH Accounting Manager for the positive-path tests to confirm.
        cls.group_manager = cls.env.ref('eh_account_base.group_eh_manager')
        cls.env.user.groups_id |= cls.group_manager
        # A plain user with no manager rights, used for the negative gate
        # tests. group_eh_user grants read access without the manager gate.
        cls.group_user = cls.env.ref('eh_account_base.group_eh_user')
        cls.user_plain = cls.env['res.users'].create({
            'name': 'Statements Plain User',
            'login': 'eh_stmt_plain@test',
            'email': 'eh_stmt_plain@test',
            'groups_id': [(6, 0, [cls.group_user.id])],
        })

    def test_soce_closing_reconciles(self):
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'share_capital',
                        'opening_balance': 100000.0, 'issue_of_shares': 20000.0}),
                (0, 0, {'component': 'retained_earnings',
                        'opening_balance': 50000.0, 'profit': 30000.0,
                        'dividends': 10000.0}),
                (0, 0, {'component': 'revaluation_reserve',
                        'opening_balance': 0.0, 'oci_movement': 5000.0}),
            ],
        })
        # Share capital 100000 + 20000 = 120000.
        sc = soce.line_ids.filtered(lambda line_item: line_item.component == 'share_capital')
        self.assertAlmostEqual(sc.closing_balance, 120000.0, places=2)
        # Retained 50000 + 30000 - 10000 = 70000.
        re = soce.line_ids.filtered(
            lambda line_item: line_item.component == 'retained_earnings')
        self.assertAlmostEqual(re.closing_balance, 70000.0, places=2)
        # Totals: opening 150000, closing 120000 + 70000 + 5000 = 195000.
        self.assertAlmostEqual(soce.total_opening, 150000.0, places=2)
        self.assertAlmostEqual(soce.total_closing, 195000.0, places=2)
        self.assertAlmostEqual(soce.total_profit, 30000.0, places=2)
        self.assertAlmostEqual(soce.total_dividends, 10000.0, places=2)

    def test_soci_total_comprehensive_income(self):
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'line_ids': [
                (0, 0, {'name': 'Revaluation', 'oci_type': 'revaluation',
                        'amount': 15000.0, 'will_reclassify': False}),
                (0, 0, {'name': 'Hedge', 'oci_type': 'cashflow_hedge',
                        'amount': -5000.0, 'will_reclassify': True}),
            ],
        })
        self.assertAlmostEqual(soci.oci_no_reclassify, 15000.0, places=2)
        self.assertAlmostEqual(soci.oci_will_reclassify, -5000.0, places=2)
        self.assertAlmostEqual(soci.total_oci, 10000.0, places=2)
        self.assertAlmostEqual(soci.total_comprehensive_income, 110000.0,
                               places=2)

    def test_soci_attribution_residual(self):
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'attributable_to_owners': 90000.0,
            'attributable_to_nci': 10000.0,
            'line_ids': [],
        })
        # 100000 profit, no OCI; attribution 90000 + 10000 = 100000, residual 0.
        self.assertAlmostEqual(soci.total_comprehensive_income, 100000.0,
                               places=2)
        self.assertAlmostEqual(soci.attribution_residual, 0.0, places=2)

    def test_confirm(self):
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31'})
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')

    def test_soci_confirm_blocks_when_attribution_untied(self):
        # Attribution amounts set but they do not sum to total comprehensive
        # income: residual != 0, so confirm must raise.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'attributable_to_owners': 80000.0,
            'attributable_to_nci': 10000.0,
        })
        self.assertFalse(soci.attribution_tied)
        self.assertAlmostEqual(soci.attribution_residual, 10000.0, places=2)
        with self.assertRaises(UserError):
            soci.action_confirm()
        self.assertEqual(soci.state, 'draft')
        # A tied statement (residual 0) confirms without error.
        soci.attributable_to_owners = 90000.0
        self.assertTrue(soci.attribution_tied)
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    def test_soci_derive_profit_from_ledger(self):
        # Post a P&L move: 40000 revenue (credit) vs 15000 expense (debit),
        # cash balancing. Net profit = 40000 - 15000 = 25000.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25000.0},
                {'account': self.account_expense, 'debit': 15000.0},
                {'account': self.account_revenue, 'credit': 40000.0},
            ],
            date='2026-06-30',
        )
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        soci.action_derive_profit_from_ledger()
        self.assertAlmostEqual(soci.profit_for_period, 25000.0, places=2)

    def test_soce_profit_ties_and_derivation(self):
        # Post a P&L move dated within 2026: revenue 40000 (credit), expense
        # 15000 (debit), cash balancing. Net profit = 40000 - 15000 = 25000.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25000.0},
                {'account': self.account_expense, 'debit': 15000.0},
                {'account': self.account_revenue, 'credit': 40000.0},
            ],
            date='2026-06-30',
        )
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'retained_earnings', 'profit': 25000.0}),
            ],
        })
        # Before derivation reported_profit is 0, so components (25000) do not
        # tie to reported (0).
        self.assertAlmostEqual(soce.total_profit, 25000.0, places=2)
        self.assertFalse(soce.profit_ties)
        # Deriving reported_profit from the ledger yields 25000 and ties.
        soce.action_derive_profit_from_ledger()
        self.assertAlmostEqual(soce.reported_profit, 25000.0, places=2)
        self.assertAlmostEqual(soce.profit_movement_tie_out, 0.0, places=2)
        self.assertTrue(soce.profit_ties)
        # Advisory only: confirmation is never blocked by the profit tie-out.
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')

    def test_soce_confirm_blocks_when_closing_disagrees_with_ledger(self):
        # A ledger equity figure IS derivable (a posted equity move exists),
        # so the closing tie-out is now blocking, not advisory. Post 200000
        # to owner equity at period end: ledger closing equity = 200000. The
        # worksheet claims a 150000 closing, which disagrees, so confirm must
        # raise and the statement stays draft.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 200000.0},
                {'account': self.account_equity, 'credit': 200000.0},
            ],
            date='2026-06-30',
        )
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'share_capital',
                        'opening_balance': 150000.0}),
            ],
        })
        self.assertTrue(soce.ledger_derivable)
        self.assertAlmostEqual(soce.ledger_closing, 200000.0, places=2)
        self.assertAlmostEqual(soce.total_closing, 150000.0, places=2)
        self.assertFalse(soce.tied)
        with self.assertRaises(UserError):
            soce.action_confirm()
        self.assertEqual(soce.state, 'draft')
        # Reconciling the worksheet to the ledger (closing 200000) lets it
        # confirm.
        soce.line_ids[0].opening_balance = 200000.0
        self.assertTrue(soce.tied)
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')

    def test_soce_confirm_advisory_when_ledger_not_derivable(self):
        # No equity postings exist, so no ledger closing figure is derivable:
        # the closing tie-out stays advisory. A worksheet with a non-zero
        # closing that would "disagree" with the nil ledger read must still
        # confirm, and a chatter warning is posted instead of blocking.
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'share_capital',
                        'opening_balance': 150000.0}),
            ],
        })
        self.assertFalse(soce.ledger_derivable)
        self.assertFalse(soce.tied)
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')

    def test_soce_derive_from_ledger(self):
        # Post equity: credit 200000 to owner equity, debit cash. Equity is
        # credit-positive, so ledger equity = 200000.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 200000.0},
                {'account': self.account_equity, 'credit': 200000.0},
            ],
            date='2025-12-31',
        )
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'share_capital'}),
                (0, 0, {'component': 'retained_earnings'}),
            ],
        })
        soce.action_derive_from_ledger()
        # No per-component account mapping, so the equity opening (200000,
        # dated 2025-12-31 <= period_start-1) lands on the first line; the
        # second line is zeroed (backward-compatible path).
        self.assertAlmostEqual(soce.total_opening, 200000.0, places=2)
        first = soce.line_ids[0]
        self.assertAlmostEqual(first.opening_balance, 200000.0, places=2)

    def test_soce_derive_opening_lands_on_correct_component(self):
        # DEFECT (B): the derive helper used to dump ALL opening equity onto
        # line[0], so every per-component opening roll-forward was individually
        # wrong even though the header total tied. With a per-component account
        # mapping, each component must receive the opening balance of exactly
        # its own equity accounts.
        account_share = self._ensure_account(
            self.env, '3010', 'Share Capital', 'equity')
        account_retained = self._ensure_account(
            self.env, '3020', 'Retained Earnings', 'equity')
        # Opening balances at 2025-12-31: share capital 150000, retained 50000.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 200000.0},
                {'account': account_share, 'credit': 150000.0},
                {'account': account_retained, 'credit': 50000.0},
            ],
            date='2025-12-31',
        )
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'share_capital',
                        'equity_account_ids': [(6, 0, [account_share.id])]}),
                (0, 0, {'component': 'retained_earnings',
                        'equity_account_ids': [(6, 0, [account_retained.id])]}),
            ],
        })
        soce.action_derive_from_ledger()
        share_line = soce.line_ids.filtered(
            lambda line_item: line_item.component == 'share_capital')
        retained_line = soce.line_ids.filtered(
            lambda line_item: line_item.component == 'retained_earnings')
        # Each component gets ITS OWN opening, not the whole 200000 on line[0].
        self.assertAlmostEqual(share_line.opening_balance, 150000.0, places=2)
        self.assertAlmostEqual(
            retained_line.opening_balance, 50000.0, places=2)
        # Header total still equals the ledger figure.
        self.assertAlmostEqual(soce.total_opening, 200000.0, places=2)

    def test_soci_confirm_blocks_when_profit_disagrees_with_ledger(self):
        # DEFECT (A): a mis-keyed profit used to confirm freely because the
        # SoCI confirm never tied profit_for_period to the ledger. A ledger
        # profit figure IS derivable here (posted P&L exists), so the gate
        # must now BLOCK when the reported profit disagrees.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25000.0},
                {'account': self.account_expense, 'debit': 15000.0},
                {'account': self.account_revenue, 'credit': 40000.0},
            ],
            date='2026-06-30',
        )
        # Ledger net profit = 25000, but the statement was mis-keyed to 30000.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 30000.0,
        })
        with self.assertRaises(UserError):
            soci.action_confirm()
        self.assertEqual(soci.state, 'draft')
        # Correcting the profit to match the ledger lets it confirm.
        soci.profit_for_period = 25000.0
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    def test_soci_confirm_advisory_when_ledger_not_derivable(self):
        # Advisory-safe: with NO posted P&L in the period the ledger profit is
        # not derivable, so a hand-keyed profit must still confirm (existing
        # flows on empty-ledger companies are preserved).
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
        })
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    # ---- manager gate (segregation of duties, IAS 1) ----

    def test_soci_confirm_requires_manager(self):
        # A non-manager can neither confirm nor silently reopen a signed
        # statement of comprehensive income; a manager can do both.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
        })
        with self.assertRaises(UserError):
            soci.with_user(self.user_plain).action_confirm()
        self.assertEqual(soci.state, 'draft')
        # Manager confirms.
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')
        # Non-manager cannot reopen a confirmed statement.
        with self.assertRaises(UserError):
            soci.with_user(self.user_plain).action_set_to_draft()
        self.assertEqual(soci.state, 'confirmed')
        # Manager can reopen.
        soci.action_set_to_draft()
        self.assertEqual(soci.state, 'draft')

    def test_soce_confirm_requires_manager(self):
        # Same gate on the statement of changes in equity.
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31'})
        with self.assertRaises(UserError):
            soce.with_user(self.user_plain).action_confirm()
        self.assertEqual(soce.state, 'draft')
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')
        with self.assertRaises(UserError):
            soce.with_user(self.user_plain).action_set_to_draft()
        self.assertEqual(soce.state, 'confirmed')
        soce.action_set_to_draft()
        self.assertEqual(soce.state, 'draft')

    # ---- freeze-after-confirm (IAS 1.106-108) ----

    def test_soci_confirmed_figures_frozen(self):
        # A confirmed statement of comprehensive income must not have its
        # figures edited in place; only a set-to-draft unlocks it.
        # The single OCI line names no source account, so its recycling
        # section is not tag-derived (IAS 1.82A): this test exercises the
        # freeze mechanics, not the recycling gate, so it clears that gate
        # with the OCI recycling override + reason.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'oci_tag_override': True,
            'oci_tag_override_reason': 'Freeze-mechanics fixture only.',
            'line_ids': [
                (0, 0, {'name': 'Revaluation', 'oci_type': 'revaluation',
                        'amount': 15000.0, 'will_reclassify': False}),
            ],
        })
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')
        # Editing the header profit figure is blocked.
        with self.assertRaises(UserError):
            soci.profit_for_period = 250000.0
        self.assertAlmostEqual(soci.profit_for_period, 100000.0, places=2)
        # Editing an OCI line amount is blocked too.
        with self.assertRaises(UserError):
            soci.line_ids[0].amount = 99999.0
        self.assertAlmostEqual(soci.line_ids[0].amount, 15000.0, places=2)
        # Deleting an OCI line is blocked.
        with self.assertRaises(UserError):
            soci.line_ids[0].unlink()
        # Deleting the confirmed statement is blocked.
        with self.assertRaises(UserError):
            soci.unlink()
        # After a manager set-to-draft the figures are editable again.
        soci.action_set_to_draft()
        soci.profit_for_period = 250000.0
        self.assertAlmostEqual(soci.profit_for_period, 250000.0, places=2)

    def test_soci_combined_state_and_figure_write_is_blocked(self):
        # The freeze must not be bypassable by flipping state to draft AND
        # editing a frozen figure in the SAME raw write.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
        })
        soci.action_confirm()
        with self.assertRaises(UserError):
            soci.write({'state': 'draft', 'profit_for_period': 250000.0})
        self.assertEqual(soci.state, 'confirmed')
        self.assertAlmostEqual(soci.profit_for_period, 100000.0, places=2)

    def test_non_manager_cannot_reopen_confirmed_statement_by_raw_write(self):
        # Reopening a confirmed statement (state -> draft) is manager-gated
        # even via a raw ORM write, not just via action_set_to_draft.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
        })
        soci.action_confirm()
        with self.assertRaises(UserError):
            soci.with_user(self.user_plain).write({'state': 'draft'})
        self.assertEqual(soci.state, 'confirmed')

    def test_soce_confirmed_figures_frozen(self):
        # A confirmed statement of changes in equity must not have its
        # figures edited in place; only a set-to-draft unlocks it.
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'retained_earnings',
                        'opening_balance': 50000.0, 'profit': 30000.0}),
            ],
        })
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')
        # Editing a line input figure is blocked.
        with self.assertRaises(UserError):
            soce.line_ids[0].profit = 99999.0
        self.assertAlmostEqual(soce.line_ids[0].profit, 30000.0, places=2)
        # Editing the header reported_profit is blocked.
        with self.assertRaises(UserError):
            soce.reported_profit = 12345.0
        # Deleting a line is blocked.
        with self.assertRaises(UserError):
            soce.line_ids[0].unlink()
        # Deleting the confirmed statement is blocked.
        with self.assertRaises(UserError):
            soce.unlink()
        # After a manager set-to-draft the figures are editable again.
        soce.action_set_to_draft()
        soce.line_ids[0].profit = 99999.0
        self.assertAlmostEqual(soce.line_ids[0].profit, 99999.0, places=2)

    def test_soci_confirmed_line_create_append_blocked(self):
        # A direct create() that appends an OCI line to a CONFIRMED statement
        # would recompute totals and silently move the parent figures, so it
        # must be blocked just like write()/unlink() on the line.
        # The OCI line names no source account, so it clears the IAS 1.82A
        # recycling completeness gate with the override (this test exercises
        # the confirmed-append freeze, not the recycling gate).
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'oci_tag_override': True,
            'oci_tag_override_reason': 'Freeze-mechanics fixture only.',
            'line_ids': [
                (0, 0, {'name': 'Revaluation', 'oci_type': 'revaluation',
                        'amount': 15000.0, 'will_reclassify': False}),
            ],
        })
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')
        before = soci.total_comprehensive_income
        with self.assertRaises(UserError):
            self.env['eh.soci.line'].create({
                'soci_id': soci.id, 'name': 'Injected',
                'oci_type': 'other', 'amount': 88888.0,
            })
        self.assertEqual(len(soci.line_ids), 1)
        self.assertAlmostEqual(
            soci.total_comprehensive_income, before, places=2)
        # After a manager set-to-draft the append is allowed again.
        soci.action_set_to_draft()
        self.env['eh.soci.line'].create({
            'soci_id': soci.id, 'name': 'Allowed',
            'oci_type': 'other', 'amount': 5000.0,
        })
        self.assertEqual(len(soci.line_ids), 2)

    def test_soce_confirmed_line_create_append_blocked(self):
        # A direct create() that appends a line to a CONFIRMED statement of
        # changes in equity would recompute totals and silently move the parent
        # figures, so it must be blocked just like write()/unlink().
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'retained_earnings',
                        'opening_balance': 50000.0, 'profit': 30000.0}),
            ],
        })
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')
        before = soce.total_closing
        with self.assertRaises(UserError):
            self.env['eh.soce.line'].create({
                'soce_id': soce.id, 'component': 'share_capital',
                'opening_balance': 77777.0,
            })
        self.assertEqual(len(soce.line_ids), 1)
        self.assertAlmostEqual(soce.total_closing, before, places=2)
        # After a manager set-to-draft the append is allowed again.
        soce.action_set_to_draft()
        self.env['eh.soce.line'].create({
            'soce_id': soce.id, 'component': 'share_capital',
            'opening_balance': 1000.0,
        })
        self.assertEqual(len(soce.line_ids), 2)
