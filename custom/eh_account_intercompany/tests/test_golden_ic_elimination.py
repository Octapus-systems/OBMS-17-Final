# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden worked examples for the inter-company elimination pair engine
(IFRS 10.B86) in eh_account_intercompany.

Every expected amount is hand-derivable from the inputs stated in the
test, with the derivation in a comment; assertions are exact to 2dp and
nothing is read back from the engine to build an expectation.

Conventions exercised (read from models/ic_elimination_batch.py):

* A pair is one posted source move plus its posted mirror, both dated in
  the batch period. The OUT side (customer invoice) carries the
  receivable and revenue; the IN side carries the payable and expense.
* Each elimination leg NEGATES the recognised balance, so an invoice
  pair yields Cr receivable / Dr payable and Dr revenue / Cr expense.
* Totals diverging beyond one cent flag the pair on the mismatch tab
  and eliminate at the COMMON (lower) amount only.
* The margin on an unrealised-profit line is invoice line subtotal less
  product standard cost (selling company) times quantity; the
  unrealised amount is margin times the remaining fraction, rounded to
  the elimination currency (2dp).
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_intercompany', 'post_install', '-at_install')
class TestGoldenIcElimination(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Move = cls.env['account.move']
        cls.Batch = cls.env['eh.ic.elimination.batch']
        cls.Config = cls.env['eh.intercompany.config']

        cls.company_a = cls.env.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'Elim Sister B',
            'currency_id': cls.company_a.currency_id.id,
        })
        if 'account_fiscal_country_id' in cls.company_a._fields:
            us = cls.env.ref('base.us')
            for comp in (cls.company_a, cls.company_b):
                if not comp.account_fiscal_country_id:
                    comp.sudo().account_fiscal_country_id = us.id
        cls.env.user.company_ids = [(4, cls.company_b.id)]
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')

        # Shared partner representing company B from company A's view.
        cls.partner_sub = cls.env['res.partner'].create({
            'name': 'Elim Sister B',
            'company_id': False,
            'eh_represented_company_id': cls.company_b.id,
        })

        # Company B journal for the bill mirror.
        cls.journal_purchase_b = cls.env['account.journal'].sudo().create({
            'name': 'Purchase B', 'code': 'PURB',
            'type': 'purchase', 'company_id': cls.company_b.id,
        })

        # Company B accounts with the SAME CODES as the company A chart
        # seeded by EhAccountIntegrationTestCase (2100 Trade Payables,
        # 5000 Cost of Sales), so posting the elimination move in
        # company A resolves them by code.
        Account = cls.env['account.account'].sudo().with_company(
            cls.company_b)
        cls.expense_b = Account.create({
            'code': '5000',
            'name': 'IC Expense B',
            'account_type': 'expense',
            'company_id': cls.company_b.id,
        })
        cls.payable_b = Account.create({
            'code': '2100',
            'name': 'IC Payable B',
            'account_type': 'liability_payable',
            'company_id': cls.company_b.id,
            'reconcile': True,
        })

        # Partner properties so the receivable/payable auto-lines
        # resolve deterministic accounts on both sides.
        cls.partner_sub.with_company(cls.company_a).write({
            'property_account_receivable_id': cls.account_receivable.id,
        })
        cls.company_a.partner_id.with_company(cls.company_b).write({
            'property_account_payable_id': cls.payable_b.id,
        })

        # Config on B: destination of the mirror when A sells to the
        # partner representing B. Elimination books in the parent (A).
        cls.config_b = cls.Config.sudo().create({
            'company_id': cls.company_b.id,
            'enabled': True,
            'auto_post_mirror': True,
            'purchase_journal_id': cls.journal_purchase_b.id,
            'fallback_expense_account_id': cls.expense_b.id,
            'elimination_company_id': cls.company_a.id,
        })
        # Config on A: carries the elimination company so the auto
        # created elimination journal is stored here on first use.
        cls.config_a = cls.Config.sudo().create({
            'company_id': cls.company_a.id,
            'enabled': True,
            'elimination_company_id': cls.company_a.id,
        })

        cls.product = cls.env['product.product'].create({
            'name': 'Elim product',
            'list_price': 1000.0,
            'standard_price': 600.0,
        })

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _make_sale_invoice(self, price=1000.0, qty=1.0,
                           invoice_date='2026-03-15', partner=None):
        """Customer invoice in A towards the partner representing B.
        Taxes are cleared explicitly so amount_total equals the untaxed
        amount and every expectation stays hand-derivable."""
        return self.Move.with_company(self.company_a).create({
            'move_type': 'out_invoice',
            'partner_id': (partner or self.partner_sub).id,
            'journal_id': self.journal_sale.id,
            'company_id': self.company_a.id,
            'invoice_date': invoice_date,
            'invoice_line_ids': [(0, 0, {
                'name': 'IC sale',
                'quantity': qty,
                'price_unit': price,
                'product_id': self.product.id,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, [])],
            })],
        })

    def _make_batch(self):
        return self.Batch.create({
            'company_a_id': self.company_a.id,
            'company_b_id': self.company_b.id,
            'period_from': '2026-03-01',
            'period_to': '2026-03-31',
            'elimination_company_id': self.company_a.id,
        })

    # ------------------------------------------------------------------
    # golden: matched pair, full elimination
    # ------------------------------------------------------------------

    def test_golden_matched_pair_elimination(self):
        """Invoice 1,000 A -> B, mirrored and auto-posted.

        Ledger recognised by the pair (no tax):
          A: Dr receivable 1100 1,000 / Cr revenue 4000 1,000
          B: Dr expense 5000 1,000 / Cr payable 2100 1,000

        The batch negates each recognised balance, so the elimination
        entry booked in A (accounts resolved by code) is exactly:
          Dr payable 2100 1,000.00
          Cr receivable 1100 1,000.00
          Dr revenue 4000 1,000.00
          Cr expense 5000 1,000.00
        """
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertTrue(mirror)
        self.assertEqual(mirror.state, 'posted')

        batch = self._make_batch()
        batch.action_compute()
        self.assertEqual(batch.state, 'computed')
        self.assertFalse(batch.mismatch_ids)
        self.assertEqual(len(batch.line_ids), 4)
        by_kind = {
            line.kind: line for line in batch.line_ids
        }
        self.assertEqual(
            set(by_kind), {'receivable', 'payable', 'revenue', 'expense'})
        # Orientation and amounts, leg by leg.
        self.assertAlmostEqual(by_kind['receivable'].credit, 1000.00, places=2)
        self.assertAlmostEqual(by_kind['receivable'].debit, 0.00, places=2)
        self.assertAlmostEqual(by_kind['payable'].debit, 1000.00, places=2)
        self.assertAlmostEqual(by_kind['revenue'].debit, 1000.00, places=2)
        self.assertAlmostEqual(by_kind['expense'].credit, 1000.00, places=2)
        # Gross totals feeding the consolidation hook.
        self.assertAlmostEqual(batch.receivable_total, 1000.00, places=2)
        self.assertAlmostEqual(batch.payable_total, 1000.00, places=2)
        self.assertAlmostEqual(batch.revenue_total, 1000.00, places=2)
        self.assertAlmostEqual(batch.expense_total, 1000.00, places=2)

        batch.action_post()
        self.assertEqual(batch.state, 'posted')
        move = batch.move_id
        self.assertTrue(move)
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.company_id, self.company_a)
        self.assertMoveLines(move, [
            ('2100', 1000.00, 0.00),
            ('1100', 0.00, 1000.00),
            ('4000', 1000.00, 0.00),
            ('5000', 0.00, 1000.00),
        ])
        self.assertBalanced(move)
        # Journal auto-created on first use, in the elimination company,
        # and stored on that company's inter-company configuration.
        self.assertEqual(move.journal_id.code, 'ICEL')
        self.assertEqual(move.journal_id.type, 'general')
        self.assertEqual(move.journal_id.company_id, self.company_a)
        self.assertEqual(self.config_a.elimination_journal_id,
                         move.journal_id)
        # Sealed: the posted counterpart of the batch cannot be unposted
        # outside the sanctioned reset path.
        self.assertTrue(move.eh_sealed)
        with self.assertRaises(UserError):
            move.button_draft()

        # Consolidation hook totals for the period.
        summary = self.Batch.eh_ic_elimination_summary(
            '2026-03-01', '2026-03-31',
            [self.company_a.id, self.company_b.id])
        self.assertAlmostEqual(
            summary['receivable_eliminated'], 1000.00, places=2)
        self.assertAlmostEqual(
            summary['payable_eliminated'], 1000.00, places=2)
        self.assertAlmostEqual(
            summary['revenue_eliminated'], 1000.00, places=2)
        self.assertAlmostEqual(
            summary['expense_eliminated'], 1000.00, places=2)
        self.assertEqual(summary['mismatch_count'], 0)
        self.assertEqual(len(summary['batches']), 1)

    # ------------------------------------------------------------------
    # golden: reset unwinds the sealed move
    # ------------------------------------------------------------------

    def test_golden_reset_reverses_sealed_move(self):
        """Reset on a posted batch reverses and removes the sealed
        entry, leaving the elimination ledger flat (payable 2100 posted
        balance back to 0 in company A from this batch)."""
        sale = self._make_sale_invoice()
        sale.action_post()
        batch = self._make_batch()
        batch.action_compute()
        batch.action_post()
        move = batch.move_id
        self.assertTrue(move)
        batch.action_reset_to_draft()
        self.assertEqual(batch.state, 'draft')
        self.assertFalse(batch.move_id)
        self.assertFalse(move.exists())

    # ------------------------------------------------------------------
    # golden: amount mismatch, block, override
    # ------------------------------------------------------------------

    def test_golden_mismatch_blocks_then_override_posts_common_amount(self):
        """Mirror edited to 900 before posting.

        Source total 1,000 vs mirror total 900: difference 100.00 on
        the mismatch tab. The pair is eliminated at the COMMON amount
        min(1000, 900) = 900 per bucket. Posting is refused while
        block_on_mismatch holds; clearing the flag (the audited
        override) posts:
          Dr payable 2100 900.00 / Cr receivable 1100 900.00
          Dr revenue 4000 900.00 / Cr expense 5000 900.00
        """
        self.config_b.auto_post_mirror = False
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertEqual(mirror.state, 'draft')
        mirror.sudo().invoice_line_ids.write({'price_unit': 900.0})
        mirror.sudo().with_company(self.company_b).action_post()
        self.assertAlmostEqual(mirror.amount_total, 900.00, places=2)

        batch = self._make_batch()
        batch.action_compute()
        self.assertEqual(len(batch.mismatch_ids), 1)
        mismatch = batch.mismatch_ids
        self.assertEqual(mismatch.kind, 'amount')
        self.assertAlmostEqual(mismatch.source_amount, 1000.00, places=2)
        self.assertAlmostEqual(mismatch.mirror_amount, 900.00, places=2)
        self.assertAlmostEqual(mismatch.difference, 100.00, places=2)
        self.assertTrue(mismatch.reason)

        # Blocked by default.
        self.assertTrue(batch.block_on_mismatch)
        with self.assertRaises(UserError):
            batch.action_post()
        self.assertEqual(batch.state, 'computed')

        # Override: clear the flag, post the common amount.
        batch.block_on_mismatch = False
        batch.action_post()
        self.assertMoveLines(batch.move_id, [
            ('2100', 900.00, 0.00),
            ('1100', 0.00, 900.00),
            ('4000', 900.00, 0.00),
            ('5000', 0.00, 900.00),
        ])
        self.assertBalanced(batch.move_id)
        # The mismatch stays listed for follow-up after the override.
        self.assertEqual(len(batch.mismatch_ids), 1)

    def test_golden_unmatched_source_flagged_no_legs(self):
        """With mirroring disabled the posted source has no mirror at
        all: the batch lists it as 'no mirror' with zero legs, and
        posting is refused (nothing to post AND mismatch present)."""
        self.config_b.enabled = False
        sale = self._make_sale_invoice()
        sale.action_post()
        self.assertFalse(sale.eh_intercompany_mirror_id)
        self.config_b.enabled = True

        batch = self._make_batch()
        batch.action_compute()
        self.assertFalse(batch.line_ids)
        self.assertEqual(len(batch.mismatch_ids), 1)
        self.assertEqual(batch.mismatch_ids.kind, 'no_mirror')
        with self.assertRaises(UserError):
            batch.action_post()

    # ------------------------------------------------------------------
    # golden: idempotency
    # ------------------------------------------------------------------

    def test_golden_recompute_replaces_lines_once(self):
        """Recompute is idempotent: two computes leave exactly one set
        of four legs (the engine wipes and rebuilds), and duplicate
        batches per pair + period are refused in both pair orders."""
        sale = self._make_sale_invoice()
        sale.action_post()
        batch = self._make_batch()
        batch.action_compute()
        first_ids = set(batch.line_ids.ids)
        self.assertEqual(len(first_ids), 4)
        batch.action_compute()
        self.assertEqual(len(batch.line_ids), 4)
        # Replaced, not appended.
        self.assertFalse(first_ids & set(batch.line_ids.ids))
        self.assertAlmostEqual(batch.receivable_total, 1000.00, places=2)

        # Same pair + period: DB unique constraint.
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                dup = self.Batch.create({
                    'company_a_id': self.company_a.id,
                    'company_b_id': self.company_b.id,
                    'period_from': '2026-03-01',
                    'period_to': '2026-03-31',
                    'elimination_company_id': self.company_a.id,
                })
                dup.flush_recordset()

        # Reversed pair, same period: python constraint.
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                self.Batch.create({
                    'company_a_id': self.company_b.id,
                    'company_b_id': self.company_a.id,
                    'period_from': '2026-03-01',
                    'period_to': '2026-03-31',
                    'elimination_company_id': self.company_a.id,
                })

    # ------------------------------------------------------------------
    # golden: unrealised profit from source documents
    # ------------------------------------------------------------------

    def test_golden_unrealised_profit_margin_times_fraction(self):
        """Invoice 1,000, product standard cost 600 (selling company):

          margin = 1,000.00 - 600.00 x 1 = 400.00 (engine-derived)

        Remaining fraction 0.5 entered per line (stock fraction is not
        available for a non-storable product):

          unrealised = 400.00 x 0.5 = 200.00

        The summary hook reports it; the elimination move does NOT book
        it (the consolidation run owns the inventory restatement).
        """
        self.product.with_company(self.company_a).standard_price = 600.0
        sale = self._make_sale_invoice(price=1000.0, qty=1.0)
        sale.action_post()
        batch = self._make_batch()
        batch.action_compute()

        self.assertEqual(len(batch.unrealised_line_ids), 1)
        line = batch.unrealised_line_ids
        self.assertEqual(line.product_id, self.product)
        self.assertAlmostEqual(line.price_subtotal, 1000.00, places=2)
        self.assertAlmostEqual(line.unit_cost, 600.00, places=2)
        self.assertAlmostEqual(line.margin, 400.00, places=2)
        # The product is not storable, so the stock-quant path never
        # fires: the fraction starts manual at 0 and nothing is
        # unrealised until it is entered.
        self.assertEqual(line.fraction_source, 'manual')
        self.assertAlmostEqual(line.remaining_fraction, 0.0, places=4)
        self.assertAlmostEqual(line.unrealised_amount, 0.00, places=2)

        line.remaining_fraction = 0.5
        self.assertAlmostEqual(line.unrealised_amount, 200.00, places=2)
        self.assertAlmostEqual(batch.unrealised_total, 200.00, places=2)

        summary = self.Batch.eh_ic_elimination_summary(
            '2026-03-01', '2026-03-31',
            [self.company_a.id, self.company_b.id])
        self.assertAlmostEqual(summary['unrealised_profit'], 200.00,
                               places=2)

        # The margin is engine-derived and cannot be hand-typed.
        with self.assertRaises(UserError):
            line.margin = 999.0
        # The elimination move never books the unrealised legs.
        batch.action_post()
        kinds = set()
        for mline in batch.move_id.line_ids:
            kinds.add(mline.account_id.code)
        self.assertEqual(kinds, {'1100', '2100', '4000', '5000'})

    # ------------------------------------------------------------------
    # golden: restrict_ic_partners posting guard
    # ------------------------------------------------------------------

    def test_golden_restrict_flag_blocks_unflagged_group_partner(self):
        """With restrict_ic_partners on company A's config, posting an
        invoice towards company B's RAW company partner (no Represented
        Company flag) is refused before anything books; with the flag
        off the same post succeeds (old semantics preserved); and a
        properly flagged partner posts fine with the flag on."""
        group_partner = self.company_b.partner_id
        group_partner.with_company(self.company_a).write({
            'property_account_receivable_id': self.account_receivable.id,
        })

        def make_invoice():
            return self.Move.with_company(self.company_a).create({
                'move_type': 'out_invoice',
                'partner_id': group_partner.id,
                'journal_id': self.journal_sale.id,
                'company_id': self.company_a.id,
                'invoice_date': '2026-03-20',
                'invoice_line_ids': [(0, 0, {
                    'name': 'Mis-keyed IC sale',
                    'quantity': 1.0,
                    'price_unit': 500.0,
                    'account_id': self.account_revenue.id,
                    'tax_ids': [(6, 0, [])],
                })],
            })

        self.config_a.restrict_ic_partners = True
        blocked = make_invoice()
        with self.assertRaises(UserError):
            blocked.action_post()
        self.assertNotEqual(blocked.state, 'posted')

        # Flag off: default semantics, the post goes through.
        self.config_a.restrict_ic_partners = False
        allowed = make_invoice()
        allowed.action_post()
        self.assertEqual(allowed.state, 'posted')

        # Flag on but the partner is properly flagged for mirroring:
        # the sanctioned IC path stays open.
        self.config_a.restrict_ic_partners = True
        flagged = self._make_sale_invoice(invoice_date='2026-03-21')
        flagged.action_post()
        self.assertEqual(flagged.state, 'posted')
        self.assertTrue(flagged.eh_intercompany_mirror_id)
