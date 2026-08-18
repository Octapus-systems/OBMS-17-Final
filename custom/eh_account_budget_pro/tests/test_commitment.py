# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Encumbrance / commitment tests.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestCommitmentBasics(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.Line = cls.env['eh.budget.line']
        cls.Commitment = cls.env['eh.budget.commitment']

    def _make_budget_line(
        self, budgeted=10000.0, account=None, policy='block',
    ):
        budget = self.Budget.create({
            'code': 'test_commit_%d' % self.env['ir.sequence'].next_by_code(
                'eh.budget.budget',
            ) if False else 'test_commit_basic',
            'name': 'Test Commitment Budget',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'overrun_policy': policy,
            'line_ids': [(0, 0, {
                'account_id': (account or self.account_expense).id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': budgeted,
            })],
        })
        budget.action_confirm()
        return budget.line_ids[0]

    def test_committed_starts_zero(self):
        line = self._make_budget_line()
        self.assertEqual(line.committed_amount, 0.0)
        self.assertEqual(line.available_amount, line.budgeted_amount)

    def test_reserved_commitment_deducts_availability(self):
        line = self._make_budget_line(budgeted=10000.0)
        self.Commitment.create({
            'budget_line_id': line.id,
            'amount': 3000.0,
            'state': 'reserved',
            'source_model': 'manual',
            'source_id': 0,
        })
        line.invalidate_recordset(['committed_amount', 'available_amount'])
        self.assertAlmostEqual(line.committed_amount, 3000.0, places=2)
        self.assertAlmostEqual(line.available_amount, 7000.0, places=2)

    def test_draft_commitment_does_not_deduct(self):
        line = self._make_budget_line(budgeted=10000.0)
        self.Commitment.create({
            'budget_line_id': line.id,
            'amount': 5000.0,
            'state': 'draft',
            'source_model': 'manual',
            'source_id': 0,
        })
        line.invalidate_recordset(['committed_amount', 'available_amount'])
        self.assertEqual(line.committed_amount, 0.0)
        self.assertEqual(line.available_amount, 10000.0)

    def test_released_commitment_does_not_deduct(self):
        line = self._make_budget_line(budgeted=10000.0)
        c = self.Commitment.create({
            'budget_line_id': line.id,
            'amount': 5000.0,
            'state': 'reserved',
            'source_model': 'manual',
            'source_id': 0,
        })
        c.action_release()
        line.invalidate_recordset(['committed_amount', 'available_amount'])
        self.assertEqual(line.committed_amount, 0.0)
        self.assertEqual(line.available_amount, 10000.0)

    def test_released_cannot_re_reserve(self):
        line = self._make_budget_line()
        c = self.Commitment.create({
            'budget_line_id': line.id,
            'amount': 100.0,
            'state': 'reserved',
            'source_model': 'manual',
            'source_id': 0,
        })
        c.action_release()
        with self.assertRaises(UserError):
            c.action_reserve()

    def test_resolve_picks_matching_budget_line(self):
        line = self._make_budget_line(account=self.account_expense)
        match = self.Commitment._resolve_budget_line(
            self.account_expense,
            fields.Date.from_string('2026-06-01'),
            self.company,
        )
        self.assertEqual(match, line)

    def test_resolve_returns_empty_when_no_match(self):
        self._make_budget_line(account=self.account_expense)
        match = self.Commitment._resolve_budget_line(
            self.account_revenue,  # different account
            fields.Date.from_string('2026-06-01'),
            self.company,
        )
        self.assertFalse(match)

    def test_resolve_skips_draft_budgets(self):
        # Draft (unconfirmed) budget should not be reachable.
        budget = self.Budget.create({  # noqa: F841
            'code': 'draft_budget',
            'name': 'Draft',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 1000.0,
            })],
        })
        match = self.Commitment._resolve_budget_line(
            self.account_expense,
            fields.Date.from_string('2026-06-01'),
            self.company,
        )
        self.assertFalse(match,
                        "Draft budgets should not be reachable for commitments")  # noqa: E128

    def test_amount_must_be_non_negative(self):
        line = self._make_budget_line()
        with self.assertRaises(Exception):
            self.Commitment.create({
                'budget_line_id': line.id,
                'amount': -100.0,
                'state': 'reserved',
                'source_model': 'manual',
                'source_id': 0,
            })


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestCommitmentPoLineKey(EhAccountIntegrationTestCase):
    """Two PO lines that resolve to the same budget line must each keep
    their own commitment, and the block gate must accumulate their need.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.Commitment = cls.env['eh.budget.commitment']
        cls.product = cls.env['product.product'].create({
            'name': 'Commitment Test Product',
            'type': 'consu',
            'purchase_ok': True,
            'property_account_expense_id': cls.account_expense.id,
        })

    def _make_budget(self, budgeted, policy='block', code='po_key_budget'):
        budget = self.Budget.create({
            'code': code,
            'name': 'PO Key Budget',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'overrun_policy': policy,
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': budgeted,
            })],
        })
        budget.action_confirm()
        return budget.line_ids[0]

    def _make_po(self, amounts):
        po_form = Form(self.env['purchase.order'])
        po_form.partner_id = self.partner_a
        for amount in amounts:
            with po_form.order_line.new() as line:
                line.product_id = self.product
                line.product_qty = 1
                line.price_unit = amount
        return po_form.save()

    def test_two_po_lines_same_budget_line_keep_separate_commitments(self):
        # Both PO lines hit the same expense account, period and (no)
        # analytic, so they resolve to the same budget line. The fix
        # keys the commitment on the PO line id, so each line keeps its
        # own row instead of the second overwriting the first.
        line = self._make_budget(budgeted=20000.0)
        po = self._make_po([3000.0, 4000.0])
        po.button_confirm()
        commits = self.Commitment.search([
            ('source_model', '=', 'purchase.order'),
            ('source_id', '=', po.id),
        ])
        self.assertEqual(len(commits), 2,
                         "each PO line must keep its own commitment")
        self.assertEqual(
            set(commits.mapped('source_line_id')),
            set(po.order_line.ids),
            "commitments must carry their originating PO line id",
        )
        self.assertAlmostEqual(sum(commits.mapped('amount')), 7000.0, 2)
        line.invalidate_recordset(['committed_amount', 'available_amount'])
        self.assertAlmostEqual(line.committed_amount, 7000.0, 2)
        self.assertAlmostEqual(line.available_amount, 13000.0, 2)

    def test_reconfirm_does_not_duplicate_commitments(self):
        # Re-running creation for the same PO lines updates the existing
        # rows rather than adding new ones.
        line = self._make_budget(budgeted=20000.0, code='po_key_reconfirm')  # noqa: F841
        po = self._make_po([3000.0, 4000.0])
        po.button_confirm()
        po._eh_create_commitments()
        commits = self.Commitment.search([
            ('source_model', '=', 'purchase.order'),
            ('source_id', '=', po.id),
        ])
        self.assertEqual(len(commits), 2,
                         "re-running creation must not duplicate rows")

    def test_block_policy_accumulates_across_lines(self):
        # 3000 + 4000 = 7000 exceeds the 6000 budget. Each line on its
        # own fits, so the gate only fires when need is accumulated per
        # budget line.
        self._make_budget(budgeted=6000.0, policy='block',
                          code='po_key_block')
        po = self._make_po([3000.0, 4000.0])
        with self.assertRaises(UserError):
            po.button_confirm()


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestCommitmentPartialBilling(EhAccountIntegrationTestCase):
    """Sequential partial vendor bills must release the encumbrance to
    exactly zero. Regression guard for the release-math defect where the
    per-bill ratio was applied to an already-reduced reserved amount, so
    the second partial bill under-released and left a phantom residual
    reserved forever.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.Commitment = cls.env['eh.budget.commitment']
        cls.product = cls.env['product.product'].create({
            'name': 'Partial Billing Product',
            'type': 'consu',
            'purchase_ok': True,
            'property_account_expense_id': cls.account_expense.id,
        })

    def _make_budget_line(self, budgeted, code):
        budget = self.Budget.create({
            'code': code,
            'name': 'Partial Billing Budget',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'overrun_policy': 'warn',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': budgeted,
            })],
        })
        budget.action_confirm()
        return budget.line_ids[0]

    def _make_po(self, qty, price_unit):
        po_form = Form(self.env['purchase.order'])
        po_form.partner_id = self.partner_a
        with po_form.order_line.new() as line:
            line.product_id = self.product
            line.product_qty = qty
            line.price_unit = price_unit
        po = po_form.save()
        po.button_confirm()
        return po

    def _bill_partial(self, po, amount):
        """Post an in_invoice vendor bill of `amount` linked to the PO.

        Built explicitly (not via action_create_invoice) so the bill is
        unambiguously an in_invoice for a positive subtotal and every
        line carries purchase_line_id back to the PO line. This keeps
        the release path under test deterministic and free of the
        refund-conversion heuristics in the PO invoicing wizard.
        """
        po_line = po.order_line[0]
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'name': self.product.name,
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.account_expense.id,
                'purchase_line_id': po_line.id,
                'tax_ids': [(6, 0, [])],
            })],
        })
        bill.action_post()
        return bill

    def _reserved_amount(self, po):
        rows = self.Commitment.search([
            ('source_model', '=', 'purchase.order'),
            ('source_id', '=', po.id),
            ('state', '=', 'reserved'),
        ])
        return sum(rows.mapped('amount'))

    def test_two_partial_bills_release_encumbrance_to_zero(self):
        # PO for 10 units at 1000 = 10000 committed. Bill 6 units then
        # 4 units. After both bills the reserved encumbrance must be
        # exactly zero, not the residual the old ratio math left behind.
        line = self._make_budget_line(budgeted=50000.0,
                                      code='partial_bill_zero')
        po = self._make_po(qty=10, price_unit=1000.0)
        self.assertAlmostEqual(self._reserved_amount(po), 10000.0, 2)

        # First partial bill: 6000 of the 10000 PO.
        self._bill_partial(po, amount=6000.0)
        self.assertAlmostEqual(
            self._reserved_amount(po), 4000.0, 2,
            "first partial bill (6000 of 10000) must leave 4000 reserved")

        # Second partial bill: remaining 4000. This is the case the old
        # math got wrong: it applied 4000/10000 to the already-reduced
        # 4000 row and left ~2400 reserved forever.
        self._bill_partial(po, amount=4000.0)
        self.assertAlmostEqual(
            self._reserved_amount(po), 0.0, 2,
            "two partial bills totalling the PO must release the "
            "encumbrance to zero")

        line.invalidate_recordset(['committed_amount', 'available_amount'])
        self.assertAlmostEqual(line.committed_amount, 0.0, 2)
