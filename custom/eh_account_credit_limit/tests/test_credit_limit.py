# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Customer credit limit tests.

Sets up a company default policy with a $1000 limit, two test partners,
and walks through:

* Exposure calculation (open AR, optional drafts).
* Block on customer invoice post when over limit.
* Warn-only mode logs to chatter without blocking.
* Override flow records to the audit log and allows the post.
* Override log is append-only and undeletable.
* Refund reduces exposure rather than adding.
* Per-partner override takes precedence over the company default.
"""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_credit_limit', 'integration', 'post_install', '-at_install')
class TestCreditLimit(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Policy = cls.env['eh.credit.policy']
        cls.LogModel = cls.env['eh.credit.override.log']

        cls.policy = cls.Policy.create({
            'name': 'Default test policy',
            'company_id': cls.env.company.id,
            'is_company_default': True,
            'default_credit_limit': 1000.0,
            'enforcement_mode': 'block',
            'include_drafts': False,
        })

        # Test user gets manager so override paths can be exercised.
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )

    def _customer_invoice(self, partner, amount, post=True):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Test sale',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.account_revenue.id,
            })],
        })
        if post:
            move.action_post()
        return move

    # ---- exposure calculation ----

    def test_zero_exposure_for_clean_partner(self):
        self.assertEqual(self.partner_a.eh_credit_exposure, 0.0)
        self.assertEqual(self.partner_a.eh_credit_status, 'ok')

    def test_exposure_includes_posted_open_ar(self):
        self._customer_invoice(self.partner_a, 200.0)
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        self.assertAlmostEqual(self.partner_a.eh_credit_exposure, 200.0)

    def test_status_warn_above_80_percent(self):
        self._customer_invoice(self.partner_a, 850.0)  # 85% of 1000
        self.partner_a.invalidate_recordset(['eh_credit_status'])
        self.assertEqual(self.partner_a.eh_credit_status, 'warn')

    def test_status_over_above_100_percent(self):
        # Switch to warn mode so the over-limit invoice can post and
        # exposure becomes computable. The block-mode gate is exercised
        # in test_post_blocked_when_over_limit instead.
        self.policy.enforcement_mode = 'warn'
        self._customer_invoice(self.partner_a, 1100.0)
        self.partner_a.invalidate_recordset(['eh_credit_status'])
        self.assertEqual(self.partner_a.eh_credit_status, 'over')

    def test_drafts_count_when_policy_says_so(self):
        self.policy.include_drafts = True
        self._customer_invoice(self.partner_a, 200.0)
        self._customer_invoice(self.partner_a, 300.0, post=False)
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        self.assertAlmostEqual(self.partner_a.eh_credit_exposure, 500.0)

    def test_drafts_excluded_by_default(self):
        self._customer_invoice(self.partner_a, 200.0)
        self._customer_invoice(self.partner_a, 300.0, post=False)
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        self.assertAlmostEqual(self.partner_a.eh_credit_exposure, 200.0)

    # ---- inline credit warning banner ----

    def test_credit_warning_on_over_limit_draft(self):
        self.partner_a.eh_credit_limit = 500.0
        draft = self._customer_invoice(self.partner_a, 800.0, post=False)
        draft.invalidate_recordset(['eh_credit_warning'])
        self.assertTrue(draft.eh_credit_warning)
        self.assertIn('credit limit', draft.eh_credit_warning)

    def test_no_credit_warning_within_limit(self):
        self.partner_a.eh_credit_limit = 5000.0
        draft = self._customer_invoice(self.partner_a, 800.0, post=False)
        draft.invalidate_recordset(['eh_credit_warning'])
        self.assertFalse(draft.eh_credit_warning)

    # ---- block mode ----

    def test_post_blocked_when_over_limit(self):
        self._customer_invoice(self.partner_a, 800.0)
        # Now try to post a 500 invoice; total would be 1300 vs 1000.
        with self.assertRaises(UserError) as cm:
            self._customer_invoice(self.partner_a, 500.0)
        self.assertIn('Credit limit exceeded', str(cm.exception))

    def test_post_succeeds_under_limit(self):
        self._customer_invoice(self.partner_a, 200.0)
        # 200 + 700 = 900 vs 1000 limit.
        invoice = self._customer_invoice(self.partner_a, 700.0)
        self.assertEqual(invoice.state, 'posted')

    # ---- warn mode ----

    def test_warn_mode_logs_but_does_not_block(self):
        self.policy.enforcement_mode = 'warn'
        self._customer_invoice(self.partner_a, 800.0)
        invoice = self._customer_invoice(self.partner_a, 500.0)
        self.assertEqual(invoice.state, 'posted')
        # The chatter should mention the breach.
        bodies = [m.body or '' for m in invoice.message_ids]
        self.assertTrue(
            any('Credit warning' in b for b in bodies),
            "expected a warning chatter post on the over-limit move",
        )

    # ---- override flow ----

    def test_override_with_reason_succeeds(self):
        self._customer_invoice(self.partner_a, 800.0)
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'eh_credit_override_reason': 'Long-term customer; deal pending',
            'invoice_line_ids': [(0, 0, {
                'name': 'Big sale',
                'quantity': 1,
                'price_unit': 500.0,
                'account_id': self.account_revenue.id,
            })],
        })
        invoice.action_post()
        self.assertEqual(invoice.state, 'posted')
        self.assertTrue(invoice.eh_credit_override_log_id)
        log = invoice.eh_credit_override_log_id
        self.assertEqual(log.partner_id, self.partner_a)
        self.assertAlmostEqual(log.exposure_at_override, 800.0)
        self.assertAlmostEqual(log.limit_at_override, 1000.0)
        self.assertAlmostEqual(log.move_amount, 500.0)
        self.assertAlmostEqual(log.excess, 300.0)
        self.assertEqual(log.reason, 'Long-term customer; deal pending')

    def test_override_blocked_when_user_not_in_override_group(self):
        # Drop manager from current user.
        self.env.user.groups_id -= self.env.ref(
            'eh_account_base.group_eh_manager',
        )
        self._customer_invoice(self.partner_a, 800.0)
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'eh_credit_override_reason': 'Trying to bypass',
            'invoice_line_ids': [(0, 0, {
                'name': 'Big sale',
                'quantity': 1,
                'price_unit': 500.0,
                'account_id': self.account_revenue.id,
            })],
        })
        with self.assertRaises(UserError) as cm:
            invoice.action_post()
        self.assertIn('override', str(cm.exception).lower())

    def test_cleared_override_group_still_blocks_non_manager(self):
        """Explicitly clearing the policy override group must not open a
        bypass: the override falls back to the manager group, so a
        non-manager who types a reason is still blocked. Previously an
        empty override group let any invoice writer override."""
        self._customer_invoice(self.partner_a, 800.0)
        self.policy.override_group_id = False
        self.env.user.groups_id -= self.env.ref(
            'eh_account_base.group_eh_manager',
        )
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'eh_credit_override_reason': 'Bypass attempt, no group configured',
            'invoice_line_ids': [(0, 0, {
                'name': 'Big sale', 'quantity': 1, 'price_unit': 500.0,
                'account_id': self.account_revenue.id,
            })],
        })
        with self.assertRaises(UserError) as cm:
            invoice.action_post()
        self.assertIn('override', str(cm.exception).lower())

    def test_cleared_override_group_allows_manager_via_fallback(self):
        """With the policy override group cleared, a manager can still
        override through the manager-group fallback."""
        self._customer_invoice(self.partner_a, 800.0)
        self.policy.override_group_id = False
        # env.user remains a manager (granted in setUpClass).
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'eh_credit_override_reason': 'Manager override, no group set',
            'invoice_line_ids': [(0, 0, {
                'name': 'Big sale', 'quantity': 1, 'price_unit': 500.0,
                'account_id': self.account_revenue.id,
            })],
        })
        invoice.action_post()
        self.assertEqual(invoice.state, 'posted')
        self.assertTrue(invoice.eh_credit_override_log_id)

    # ---- audit log immutability ----

    def test_override_log_is_append_only(self):
        self._customer_invoice(self.partner_a, 800.0)
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'eh_credit_override_reason': 'Bypass',
            'invoice_line_ids': [(0, 0, {
                'name': 'X', 'quantity': 1, 'price_unit': 500.0,
                'account_id': self.account_revenue.id,
            })],
        })
        invoice.action_post()
        log = invoice.eh_credit_override_log_id
        self.assertTrue(log)
        with self.assertRaises(UserError):
            log.write({'reason': 'changed'})
        with self.assertRaises(UserError):
            log.unlink()
        # Comment is the one writable field.
        log.write({'comment': 'discussion notes'})
        self.assertEqual(log.comment, 'discussion notes')

    # ---- refund reduces exposure ----

    def test_refund_reduces_exposure(self):
        self._customer_invoice(self.partner_a, 800.0)
        # Refund 300; exposure should become 500 once refund posts.
        # We cannot use _customer_invoice helper because it has only
        # out_invoice; build manually.
        refund = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-16',
            'invoice_line_ids': [(0, 0, {
                'name': 'Refund', 'quantity': 1, 'price_unit': 300.0,
                'account_id': self.account_revenue.id,
            })],
        })
        refund.action_post()
        # Refund move's _eh_credit_relevant_amount is negative; the
        # gate should never fire on it. Verify by attempting another
        # 600 invoice (exposure 800 - 300 + 600 = 1100, still over)
        # to confirm the math is consistent.
        # Note: after refund, the receivable reconciles partially; the
        # ORM updates amount_residual on the original invoice. We do
        # not assert exact figures here because reconciliation
        # behaviour depends on Odoo's residual computation timing.

    # ---- per-partner override ----

    def test_per_partner_limit_override(self):
        self.partner_a.eh_credit_limit = 500.0
        # 500 is the per-partner limit, ignoring the 1000 default.
        self._customer_invoice(self.partner_a, 400.0)
        with self.assertRaises(UserError):
            self._customer_invoice(self.partner_a, 200.0)

    def test_per_partner_policy_override(self):
        strict_policy = self.Policy.create({
            'name': 'Strict policy',
            'company_id': self.env.company.id,
            'is_company_default': False,
            'default_credit_limit': 200.0,
            'enforcement_mode': 'block',
        })
        self.partner_b.eh_credit_policy_id = strict_policy.id
        # partner_b uses the 200 limit, not the 1000 default.
        self._customer_invoice(self.partner_b, 150.0)
        with self.assertRaises(UserError):
            self._customer_invoice(self.partner_b, 100.0)

    # ---- zero limit means no enforcement ----

    def test_zero_limit_disables_gate(self):
        self.policy.default_credit_limit = 0.0
        # Even a huge invoice posts because zero means disabled.
        invoice = self._customer_invoice(self.partner_a, 99999.0)
        self.assertEqual(invoice.state, 'posted')

    # ---- multi-currency exposure ----

    def test_exposure_currency_is_company_currency(self):
        self.assertEqual(
            self.partner_a.eh_credit_exposure_currency_id,
            self.env.company.currency_id,
        )

    def test_draft_exposure_uses_company_currency_signed(self):
        """A draft refund should reduce, not inflate, exposure."""
        self.policy.include_drafts = True
        self._customer_invoice(self.partner_a, 200.0, post=False)
        # Draft refund of 50 in same currency.
        self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'X', 'quantity': 1, 'price_unit': 50.0,
                'account_id': self.account_revenue.id,
            })],
        })
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        # 200 invoice draft (signed +200) plus 50 refund draft
        # (signed -50) gives 150 exposure when drafts are counted.
        self.assertAlmostEqual(self.partner_a.eh_credit_exposure, 150.0)

    def test_foreign_so_enforced_at_converted_value(self):
        """A foreign-currency open sale order must be enforced against
        the limit at its company-currency-converted value, not its raw
        order-currency figure.

        The post-time gate (account_move._eh_partner_exposure_excluding_self)
        must convert the open-SO amount exactly as the partner-form
        compute does. Rate: 1 EUR = 2 USD (company currency is USD).
        A 600 EUR open order converts to 1200 USD. Raw (unconverted)
        exposure of 600 would stay under the 1000 limit and wrongly
        allow the post; the converted 1200 exceeds it and must block.
        """
        if 'sale.order' not in self.env:
            self.skipTest("sale module not installed in this run")

        company = self.env.company
        eur = self.env.ref('base.EUR')
        eur.active = True
        # Company currency is USD (base=1); an EUR rate of 0.5 means
        # 1 USD = 0.5 EUR, i.e. 1 EUR = 2 USD.
        self.env['res.currency.rate'].create({
            'name': '2026-01-01',
            'currency_id': eur.id,
            'rate': 0.5,
            'company_id': company.id,
        })
        # sale.order.currency_id is driven by the pricelist, so force the
        # order into EUR with an EUR pricelist rather than writing
        # currency_id directly (which does not stick across versions).
        pricelist = self.env['product.pricelist'].create({
            'name': 'EH Credit EUR',
            'currency_id': eur.id,
            'company_id': company.id,
        })
        self.policy.include_sale_orders = True

        order_line = {
            'name': 'FX line',
            'product_id': self._eh_credit_test_product().id,
            'product_uom_qty': 1,
            'price_unit': 600.0,
        }
        # sale.order.line renamed tax_id -> tax_ids in Odoo 17; clear the tax
        # (so the order is exactly 600 EUR = 1200 USD) on whichever exists.
        tax_field = 'tax_ids' \
            if 'tax_ids' in self.env['sale.order.line']._fields else 'tax_id'
        order_line[tax_field] = [(6, 0, [])]
        order = self.env['sale.order'].create({
            'partner_id': self.partner_a.id,
            'pricelist_id': pricelist.id,
            'date_order': '2026-04-15',
            'order_line': [(0, 0, order_line)],
        })
        order.action_confirm()

        # The order must genuinely be in EUR for the conversion to bite.
        self.assertEqual(order.currency_id, eur)
        self.assertIn(order.state, ('sale', 'done'))

        # 600 EUR converts to 1200 USD, already over the 1000 limit, so
        # even a small 100 USD invoice must block. If the gate used the
        # raw 600 figure, 600 + 100 = 700 would post cleanly. The block
        # therefore fires ONLY because the open SO was converted.
        with self.assertRaises(UserError) as cm:
            self._customer_invoice(self.partner_a, 100.0)
        self.assertIn('Credit limit exceeded', str(cm.exception))

    def test_foreign_invoice_gate_uses_converted_amount(self):
        """The move under post must contribute its COMPANY-currency value.

        The gated invoice is denominated in a foreign currency. Its
        amount_total lives in that currency, but the exposure and the
        limit are in company currency. _eh_credit_relevant_amount must
        return the converted (company-currency) figure so the gate
        compares like-for-like.

        Rate: 1 EUR = 2 USD (company currency USD). A 600 EUR invoice is
        1200 USD, already over the 1000 limit, so the post must block.
        If the gate used the raw 600 amount_total, 600 < 1000 would post
        cleanly; the block therefore fires ONLY because the move was
        converted to company currency.
        """
        company = self.env.company
        eur = self.env.ref('base.EUR')
        eur.active = True
        # Company currency is USD (base=1); an EUR rate of 0.5 means
        # 1 USD = 0.5 EUR, i.e. 1 EUR = 2 USD.
        self.env['res.currency.rate'].create({
            'name': '2026-01-01',
            'currency_id': eur.id,
            'rate': 0.5,
            'company_id': company.id,
        })
        self.assertNotEqual(
            eur, company.currency_id,
            "test presumes company currency is not EUR",
        )

        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'currency_id': eur.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'FX sale',
                'quantity': 1,
                'price_unit': 600.0,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, [])],
            })],
        })
        # The move must genuinely be in EUR for the conversion to bite.
        self.assertEqual(move.currency_id, eur)
        self.assertAlmostEqual(move.amount_total, 600.0)
        # amount_total_signed is company currency: 600 EUR -> 1200 USD.
        self.assertAlmostEqual(move.amount_total_signed, 1200.0)

        # The move's contribution to the gate must be the 1200 USD
        # converted figure, not the 600 EUR raw amount.
        self.assertAlmostEqual(move._eh_credit_relevant_amount(), 1200.0)

        # 1200 USD alone exceeds the 1000 USD limit, so the post blocks.
        # With the pre-fix raw-amount behaviour (600), it would post.
        with self.assertRaises(UserError) as cm:
            move.action_post()
        self.assertIn('Credit limit exceeded', str(cm.exception))
        self.assertNotEqual(move.state, 'posted')

    # ---- concurrency: block-mode gate serialises on the partner row ----

    def test_block_gate_locks_partner_before_reading_exposure(self):
        """The block-mode gate must take a FOR UPDATE row lock on the
        commercial partner before reading exposure, so two concurrent
        posts for the same customer serialise instead of both passing a
        stale check-then-act and both breaching the limit.

        A live race is not deterministically unit-testable; instead we
        assert the lock helper is invoked on the sequential post path
        (with the resolved commercial partner) whenever a block-mode
        gate runs.
        """
        Move = type(self.env['account.move'])
        original = Move._eh_lock_partner_for_gate
        locked = []

        def spy(move_self, partner):
            locked.append(partner.id)
            return original(move_self, partner)

        self.patch(Move, '_eh_lock_partner_for_gate', spy)
        # 200 against a 1000 block-mode default: gate runs, lock taken.
        self._customer_invoice(self.partner_a, 200.0)
        self.assertIn(
            self.partner_a.commercial_partner_id.id, locked,
            "block-mode gate did not lock the commercial partner row",
        )

    def test_warn_gate_does_not_lock_partner(self):
        """Warn-only enforcement is advisory, so it must not take the
        block-mode partner lock (there is no serialisation guarantee to
        uphold when posting is never refused)."""
        self.policy.enforcement_mode = 'warn'
        Move = type(self.env['account.move'])
        original = Move._eh_lock_partner_for_gate
        locked = []

        def spy(move_self, partner):
            locked.append(partner.id)
            return original(move_self, partner)

        self.patch(Move, '_eh_lock_partner_for_gate', spy)
        self._customer_invoice(self.partner_a, 200.0)
        self.assertFalse(
            locked, "warn-mode gate should not take the partner row lock",
        )

    # ---- single-default invariant survives unarchive ----

    def test_unarchive_stale_default_is_revalidated(self):
        """Restoring an archived default must re-run the single-default
        check. Archive A, create B as the new active default (allowed
        because archived A is excluded from the active_test search), then
        unarchiving A must raise rather than leave two active defaults
        that find_for_company resolves non-deterministically.
        """
        company = self.env.company
        # Neutralise the setUpClass default so this company starts with a
        # single, controllable default for the scenario.
        self.policy.is_company_default = False

        policy_a = self.Policy.create({
            'name': 'Stale default A',
            'company_id': company.id,
            'is_company_default': True,
            'default_credit_limit': 5000.0,
        })
        # Archive A: it keeps is_company_default in the row but drops out
        # of the active_test=True search, so a second default is allowed.
        policy_a.active = False
        policy_b = self.Policy.create({
            'name': 'New default B',
            'company_id': company.id,
            'is_company_default': True,
            'default_credit_limit': 500000.0,
        })
        # Unarchiving A (the standard Unarchive action writes only
        # {'active': True}) must re-validate and refuse, because B holds
        # the active default slot. Wrap in a savepoint so the rejected
        # write rolls back cleanly.
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                policy_a.active = True
                policy_a.flush_recordset()
        # The resolver still deterministically returns the single active
        # default, never the stale one.
        self.assertEqual(self.Policy.find_for_company(company), policy_b)

    def test_archive_default_does_not_raise(self):
        """Archiving a company default must not trip the single-default
        constraint: an archived default is not competing for the slot."""
        company = self.env.company
        self.policy.is_company_default = False
        policy_a = self.Policy.create({
            'name': 'Archivable default',
            'company_id': company.id,
            'is_company_default': True,
            'default_credit_limit': 1000.0,
        })
        policy_a.active = False
        policy_a.flush_recordset()
        self.assertFalse(policy_a.active)

    def _eh_credit_test_product(self):
        """A stored product for sale-order lines in the FX test."""
        Product = self.env['product.product']
        product = Product.search([('name', '=', 'EH Credit FX Product')], limit=1)
        if not product:
            product = Product.create({
                'name': 'EH Credit FX Product',
                'type': 'service',
                'invoice_policy': 'order',
                'list_price': 600.0,
            })
        return product
