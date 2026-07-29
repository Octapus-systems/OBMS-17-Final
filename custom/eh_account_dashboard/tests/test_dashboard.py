# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Dashboard KPI computation tests.

Builds a small set of receivable / payable / income / expense entries
and verifies each KPI computation aggregates them correctly. Also
exercises the period switcher and the open_for_current_user pattern
that the menu action calls.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dashboard', 'integration', 'post_install', '-at_install')
class TestDashboardKpis(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Dashboard = cls.env['eh.account.dashboard']
        cls.today = fields.Date.context_today(cls.env['res.users'])

    def _post_invoice(self, partner, amount, days_ago_due=0):
        """Post a customer invoice with the given residual."""
        post_date = self.today - timedelta(days=days_ago_due + 30)
        due_date = self.today - timedelta(days=days_ago_due)
        return self.post_balanced_move(
            [
                {
                    'account': self.account_receivable,
                    'debit': amount,
                    'partner': partner,
                    'date_maturity': due_date,
                },
                {'account': self.account_revenue, 'credit': amount},
            ],
            date=post_date,
        )

    def _make_dashboard(self, **overrides):
        vals = {
            'name': 'Test dashboard',
            'period_mode': 'mtd',
            'posted_only': True,
        }
        vals.update(overrides)
        return self.Dashboard.create(vals)

    # ---- optional collections KPI ----

    def test_collections_kpi_aggregates_via_read_group(self):
        """The collections KPI is computed with a single SQL aggregation;
        the count and total still match the open cases. Runs only when the
        collections module is installed alongside the dashboard."""
        if 'eh.collections.case' not in self.env:
            self.skipTest("eh_account_collections not installed")
        self.env['eh.collections.case'].create([
            {
                'partner_id': self.partner_a.id,
                'company_id': self.company.id,
                'total_overdue_amount': 300.0,
            },
            {
                'partner_id': self.partner_b.id,
                'company_id': self.company.id,
                'total_overdue_amount': 200.0,
            },
        ])
        dash = self._make_dashboard(company_id=self.company.id)
        dash.invalidate_recordset([
            'active_collections_count', 'active_collections_total',
        ])
        self.assertTrue(dash.has_collections_module)
        self.assertEqual(dash.active_collections_count, 2)
        self.assertAlmostEqual(
            dash.active_collections_total, 500.0, places=2,
        )

    # ---- period dates ----

    def test_period_dates_mtd(self):
        d = self._make_dashboard(period_mode='mtd')
        self.assertEqual(d.period_date_from, self.today.replace(day=1))
        self.assertEqual(d.period_date_to, self.today)

    def test_period_dates_ytd(self):
        d = self._make_dashboard(period_mode='ytd')
        self.assertEqual(d.period_date_from, self.today.replace(month=1, day=1))

    def test_period_dates_last_30(self):
        d = self._make_dashboard(period_mode='last_30')
        self.assertEqual(d.period_date_from, self.today - timedelta(days=30))

    # ---- receivables ----

    def test_receivable_total_aggregates_open_lines(self):
        self._post_invoice(self.partner_a, 100.0)
        self._post_invoice(self.partner_b, 250.0)
        d = self._make_dashboard()
        self.assertAlmostEqual(d.receivable_total, 350.0)

    def test_receivable_overdue_filters_by_due_date(self):
        # 100 due in 30 days (not overdue), 250 due 10 days ago (overdue).
        self._post_invoice(self.partner_a, 100.0, days_ago_due=-30)
        self._post_invoice(self.partner_b, 250.0, days_ago_due=10)
        d = self._make_dashboard()
        self.assertAlmostEqual(d.receivable_overdue, 250.0)
        self.assertEqual(d.receivable_days_overdue_max, 10)

    def test_receivable_zero_when_no_open_lines(self):
        d = self._make_dashboard()
        self.assertEqual(d.receivable_total, 0.0)
        self.assertEqual(d.receivable_overdue, 0.0)
        self.assertEqual(d.receivable_days_overdue_max, 0)

    # ---- period P/L ----

    def test_period_revenue_includes_income_balance(self):
        # The default _post_invoice posts on (today - 30 days), which
        # falls outside the dashboard's default MTD window. Override
        # the period to cover the post date so the test exercises
        # the income aggregation, not the date filter.
        self._post_invoice(self.partner_a, 100.0)
        self._post_invoice(self.partner_b, 250.0)
        d = self._make_dashboard(
            period_mode='custom',
            period_date_from=self.today - timedelta(days=60),
            period_date_to=self.today,
        )
        self.assertGreaterEqual(d.period_revenue, 350.0 - 0.01)

    # ---- optional KPIs ----

    def test_optional_modules_probed_via_registry(self):
        d = self._make_dashboard()
        # Whether the optional modules are present depends on the test
        # install set; we just verify the booleans match the registry.
        self.assertEqual(
            d.has_approval_module,
            'eh.approval.policy' in self.env,
        )
        self.assertEqual(
            d.has_collections_module,
            'eh.collections.case' in self.env,
        )
        self.assertEqual(
            d.has_budget_module,
            'eh.budget.budget' in self.env,
        )

    def test_optional_kpis_zero_when_module_missing(self):
        d = self._make_dashboard()
        if not d.has_approval_module:
            self.assertEqual(d.pending_approval_count, 0)
        if not d.has_collections_module:
            self.assertEqual(d.active_collections_count, 0)
            self.assertEqual(d.active_collections_total, 0.0)
        if not d.has_budget_module:
            self.assertEqual(d.active_budget_count, 0)
            self.assertEqual(d.overrun_budget_count, 0)

    # ---- open_for_current_user ----

    def test_open_for_current_user_creates_singleton(self):
        # The default entry point now returns the Owl client action
        # (tag eh_account_dashboard.board) with the resolved record id
        # passed via context. Calling twice should resolve to the same
        # underlying dashboard record.
        action = self.Dashboard.open_for_current_user()
        self.assertEqual(action['type'], 'ir.actions.client')
        self.assertEqual(action['tag'], 'eh_account_dashboard.board')
        first_id = action['context']['eh_dashboard_id']
        action2 = self.Dashboard.open_for_current_user()
        self.assertEqual(
            action2['context']['eh_dashboard_id'], first_id,
        )

    def test_open_form_for_current_user_returns_form_action(self):
        # The form-view escape hatch still resolves to the act_window
        # form action so power users can edit fields directly.
        action = self.Dashboard.open_form_for_current_user()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], self._dashboard_model())
        self.assertEqual(action['view_mode'], 'form')

    def test_get_dashboard_snapshot_shape(self):
        # The Owl board contract: snapshot must include the record id,
        # the period block, and one entry per KPI section. The values
        # are computed by the existing field computes; only assert the
        # keys here so the test is robust to ledger contents.
        d = self._make_dashboard()
        snap = d.get_dashboard_snapshot()
        self.assertEqual(snap['record_id'], d.id)
        for section in (
            'period', 'currency', 'company',
            'liquidity', 'pnl', 'modules',
            'operations', 'controls', 'cash_trend',
        ):
            self.assertIn(section, snap)
        self.assertIsInstance(snap['cash_trend'], list)

    def test_payable_prior_delta_flat_ap_reports_zero(self):
        # A single open vendor bill posted before the prior window's end
        # stays open (unpaid) across both periods, so accounts payable is
        # flat. The prior-period delta on payable_total must be ~zero.
        #
        # payable_total is displayed absolute while the prior cumulative
        # sum is signed (credit-side negative); before the fix the delta
        # compared abs(current) against a negative prior, roughly doubling
        # the delta and reporting a spurious ~+200% swing.
        amount = 5000.0
        post_date = self.today - timedelta(days=90)
        self.post_balanced_move(
            [
                {'account': self.account_expense, 'debit': amount},
                {'account': self.account_payable, 'credit': amount},
            ],
            date=post_date,
        )
        dash = self._make_dashboard(
            company_id=self.company.id,
            period_mode='last_30',
        )
        self.assertAlmostEqual(dash.payable_total, amount, places=2)
        deltas = dash._eh_compute_prior_period_deltas()
        payable = deltas['payable_total']
        self.assertAlmostEqual(payable['current'], amount, places=2)
        self.assertAlmostEqual(payable['prior'], amount, places=2)
        self.assertAlmostEqual(payable['delta'], 0.0, places=2)
        self.assertAlmostEqual(payable['pct'] or 0.0, 0.0, places=2)

    def _dashboard_model(self):
        return 'eh.account.dashboard'

    # ---- drilldowns ----

    def test_drilldown_receivables_returns_action(self):
        d = self._make_dashboard()
        action = d.action_drilldown_receivables()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'account.move.line')
        self.assertIn(
            ('account_id.account_type', '=', 'asset_receivable'),
            action['domain'],
        )

    def test_drilldown_pending_approvals_when_no_module(self):
        d = self._make_dashboard()
        if not d.has_approval_module:
            action = d.action_drilldown_pending_approvals()
            self.assertFalse(action)


@tagged('eh_account_dashboard', 'integration', 'post_install', '-at_install')
class TestDashboardDocumentCounts(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Dashboard = cls.env['eh.account.dashboard']
        cls.today = fields.Date.context_today(cls.env['res.users'])

    def _dash(self):
        return self.Dashboard.create({
            'name': 'Doc counts', 'period_mode': 'mtd', 'posted_only': True,
            'company_id': self.company.id,
        })

    def _invoice(self, move_type, account, invoice_date, post=False):
        move = self.env['account.move'].create({
            'move_type': move_type,
            'partner_id': self.partner_a.id,
            'invoice_date': invoice_date,
            'invoice_line_ids': [(0, 0, {
                'name': 'Line', 'account_id': account.id,
                'quantity': 1.0, 'price_unit': 100.0,
                'tax_ids': [(6, 0, [])],
            })],
        })
        if post:
            move.action_post()
        return move

    def test_document_counts(self):
        dash = self._dash()
        counts = ['draft_invoice_count', 'late_invoice_count',
                  'draft_bill_count', 'late_bill_count']
        dash.invalidate_recordset(counts)
        base = {k: dash[k] for k in counts}

        # Draft customer invoice.
        self._invoice('out_invoice', self.account_revenue, self.today)
        # Posted, overdue customer invoice (due in the past, unpaid).
        late_move = self._invoice('out_invoice', self.account_revenue,
                                  self.today - timedelta(days=40), post=True)
        late_move.invoice_date_due = self.today - timedelta(days=10)
        # Draft vendor bill.
        self._invoice('in_invoice', self.account_expense, self.today)

        dash.invalidate_recordset(counts)
        self.assertEqual(dash.draft_invoice_count - base['draft_invoice_count'], 1)
        self.assertEqual(
            dash.late_invoice_count - base['late_invoice_count'], 1,
            msg="due=%s state=%s pay=%s" % (
                late_move.invoice_date_due, late_move.state,
                late_move.payment_state))
        self.assertEqual(dash.draft_bill_count - base['draft_bill_count'], 1)
        self.assertEqual(dash.late_bill_count - base['late_bill_count'], 0)

    def test_document_counts_in_snapshot(self):
        dash = self._dash()
        snap = dash.get_dashboard_snapshot()
        self.assertIn('documents', snap)
        self.assertIn('draft_invoice_count', snap['documents'])
        self.assertIn('integrity', snap)
        self.assertIn('sequence_hole_count', snap['integrity'])

    def test_sequence_hole_detection(self):
        journal = self.env['account.journal'].create({
            'name': 'Seq Journal', 'code': 'SEQJ', 'type': 'sale',
            'company_id': self.company.id,
        })
        dash = self._dash()
        dash.invalidate_recordset(['sequence_hole_count'])
        base = dash.sequence_hole_count

        moves = self.env['account.move']
        for _i in range(3):
            move = self.env['account.move'].create({
                'move_type': 'out_invoice', 'journal_id': journal.id,
                'partner_id': self.partner_a.id, 'invoice_date': self.today,
                'invoice_line_ids': [(0, 0, {
                    'name': 'L', 'account_id': self.account_revenue.id,
                    'quantity': 1.0, 'price_unit': 10.0,
                    'tax_ids': [(6, 0, [])],
                })],
            })
            move.action_post()
            moves += move

        dash.invalidate_recordset(['sequence_hole_count'])
        self.assertEqual(dash.sequence_hole_count - base, 0)  # contiguous

        # Reset the middle posting to draft: leaves a gap among posted.
        moves[1].button_draft()
        dash.invalidate_recordset(['sequence_hole_count'])
        self.assertGreaterEqual(dash.sequence_hole_count - base, 1)

    def test_unhashed_zero_without_hash_journal(self):
        dash = self._dash()
        dash.invalidate_recordset(['unhashed_entry_count'])
        self.assertEqual(dash.unhashed_entry_count, 0)

    def test_to_reconcile_count(self):
        bank = self.env['account.journal'].create({
            'name': 'Dash Bank', 'code': 'BNKD', 'type': 'bank',
            'company_id': self.company.id,
        })
        dash = self._dash()
        dash.invalidate_recordset(['to_reconcile_count'])
        base = dash.to_reconcile_count
        self.env['account.bank.statement.line'].create({
            'journal_id': bank.id, 'date': self.today,
            'amount': 100.0, 'payment_ref': 'unreconciled',
        })
        dash.invalidate_recordset(['to_reconcile_count'])
        self.assertEqual(dash.to_reconcile_count - base, 1)

    def test_bank_ops_in_snapshot(self):
        dash = self._dash()
        snap = dash.get_dashboard_snapshot()
        self.assertIn('bank_ops', snap)
        self.assertIn('to_reconcile_count', snap['bank_ops'])
        self.assertGreaterEqual(snap['bank_ops']['to_check_count'], 0)
