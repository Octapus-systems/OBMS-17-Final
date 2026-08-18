# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
FX revaluation tests.

Set up a foreign currency receivable, change the closing rate, run
revaluation. Verify line aggregation, gain/loss classification, posted
move balance and reversal.
"""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_fx_revaluation', 'integration', 'post_install', '-at_install')
class TestFxRevaluation(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref('account.group_account_manager')
        cls.env.user.groups_id |= cls.env.ref('eh_account_base.group_eh_manager')

        cls.eur = cls.env.ref('base.EUR')
        cls.eur.active = True
        cls.usd = cls.env.ref('base.USD')
        cls.usd.active = True

        # Force company currency to USD by setting the company.
        # Using whatever is set already works too; we test with the
        # foreign side being EUR regardless.
        cls.gain_account = cls._ensure_account(
            cls.env, '4920', 'Unrealised FX Gain', 'income_other',
        )
        cls.loss_account = cls._ensure_account(
            cls.env, '5930', 'Unrealised FX Loss', 'expense',
        )

        cls.account_receivable.eh_fx_revalue = True

        # Seed an opening EUR rate.
        Rate = cls.env['res.currency.rate']
        Rate.search([('currency_id', '=', cls.eur.id)]).unlink()
        Rate.create({
            'currency_id': cls.eur.id,
            'name': '2026-01-01',
            'rate': 1.0,
            'company_id': cls.company.id,
        })

    def _post_eur_invoice_balance(self, amount_eur, amount_company, partner=None):
        """Post a balanced manual entry that mimics a foreign currency
        invoice: Dr Receivable (EUR), Cr Revenue (company currency).
        """
        partner = partner or self.partner_a
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-02-15',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'EUR receivable',
                    'account_id': self.account_receivable.id,
                    'partner_id': partner.id,
                    'currency_id': self.eur.id,
                    'amount_currency': amount_eur,
                    'debit': amount_company,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'EUR revenue (in company ccy)',
                    'account_id': self.account_revenue.id,
                    'debit': 0.0,
                    'credit': amount_company,
                }),
            ],
        })
        move.action_post()
        return move

    def _set_closing_rate(self, date_str, rate):
        """Set the EUR rate at `date_str`. Odoo convention: rate is units
        of foreign currency per 1 unit of company currency. So if 1 USD =
        0.5 EUR, rate = 0.5.
        """
        Rate = self.env['res.currency.rate']
        Rate.create({
            'currency_id': self.eur.id,
            'name': date_str,
            'rate': rate,
            'company_id': self.company.id,
        })

    # ---- run lifecycle ----

    def test_create_run_assigns_sequence(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        self.assertNotEqual(run.name, '/')
        self.assertTrue(run.name.startswith('FXR/'))

    def test_unique_date_per_company(self):
        self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        with self.assertRaises(Exception):
            self.env['eh.fx.revaluation.run'].create({
                'revaluation_date': '2026-03-31',
                'journal_id': self.journal_misc.id,
                'gain_account_id': self.gain_account.id,
                'loss_account_id': self.loss_account.id,
            })

    def test_compute_with_no_open_balances(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        self.assertEqual(len(run.line_ids), 0)

    def test_post_blocks_without_lines(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        run.action_compute()
        with self.assertRaises(UserError):
            run.action_post()

    def test_compute_blocked_after_posted(self):
        # Post one line worth of activity so the run can be posted.
        self._post_eur_invoice_balance(1000.0, 1000.0)
        # Default rate is 1.0; keep it for the closing date so adjustment is 0
        # but we still get a line. To produce a non zero adjustment, change rate.
        self._set_closing_rate('2026-03-31', 1.25)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        run.action_post()
        with self.assertRaises(UserError):
            run.action_compute()

    # ---- nature classification ----

    def test_classify_nature_asset_gain(self):
        from odoo.addons.eh_account_fx_revaluation.models.fx_revaluation_run import (
            EhFxRevaluationRun,
        )
        nature = EhFxRevaluationRun._classify_nature(
            self.account_receivable, 100.0,
        )
        self.assertEqual(nature, 'gain')

    def test_classify_nature_asset_loss(self):
        from odoo.addons.eh_account_fx_revaluation.models.fx_revaluation_run import (
            EhFxRevaluationRun,
        )
        nature = EhFxRevaluationRun._classify_nature(
            self.account_receivable, -100.0,
        )
        self.assertEqual(nature, 'loss')

    def test_classify_nature_liability_loss(self):
        # Liability balance is signed-negative in Odoo. A more-negative
        # adjustment means the debt grew, which is a loss. -100 means
        # the payable went from -X to -(X+100).
        from odoo.addons.eh_account_fx_revaluation.models.fx_revaluation_run import (
            EhFxRevaluationRun,
        )
        nature = EhFxRevaluationRun._classify_nature(
            self.account_payable, -100.0,
        )
        self.assertEqual(nature, 'loss')

    def test_classify_nature_flat(self):
        from odoo.addons.eh_account_fx_revaluation.models.fx_revaluation_run import (
            EhFxRevaluationRun,
        )
        nature = EhFxRevaluationRun._classify_nature(
            self.account_receivable, 0.0,
        )
        self.assertEqual(nature, 'flat')

    # ---- account flag ----

    def test_account_account_default_revalue_flag(self):
        receivable = self.env['account.account'].create({
            'code': '1199',
            'name': 'Test Receivable',
            'account_type': 'asset_receivable',
            'company_id': self.company.id,
            'reconcile': True,
        })
        self.assertTrue(receivable.eh_fx_revalue)

    def test_account_account_non_monetary_default_off(self):
        equity = self.env['account.account'].create({
            'code': '3199',
            'name': 'Test Equity',
            'account_type': 'equity',
            'company_id': self.company.id,
        })
        self.assertFalse(equity.eh_fx_revalue)

    def test_account_account_non_monetary_flag_rejected(self):
        fixed = self.env['account.account'].create({
            'code': '1500',
            'name': 'Test Fixed Asset',
            'account_type': 'asset_fixed',
            'company_id': self.company.id,
        })
        with self.assertRaises(ValidationError):
            fixed.eh_fx_revalue = True

    # ---- cancel / set to draft ----

    def test_cancel_and_reset(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        run.action_cancel()
        self.assertEqual(run.state, 'cancelled')
        run.action_set_to_draft()
        self.assertEqual(run.state, 'draft')

    def test_cancel_blocked_when_posted(self):
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 1.25)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        run.action_post()
        with self.assertRaises(UserError):
            run.action_cancel()

    # ---- SoD: manager-only transitions ----

    def _plain_user(self):
        """A user with only eh_account_base.group_eh_user (no manager).

        Segregation of duties: a plain accounting user must never be
        able to post, reverse or cancel an FX revaluation run.
        """
        User = self.env['res.users']
        group_user = self.env.ref('eh_account_base.group_eh_user')
        user = User.create({
            'name': 'FX Plain User',
            'login': 'fx_plain_user',
            'email': 'fx_plain_user@example.com',
            'company_id': self.company.id,  # noqa: F601
            'company_id': self.company.id,  # noqa: F601
            'groups_id': [(6, 0, group_user.ids)],
        })
        self.assertFalse(
            user.has_group('eh_account_base.group_eh_manager'),
            "Fixture user must NOT be a manager for the SoD test to mean "
            "anything.",
        )
        return user

    def test_sod_post_blocked_for_plain_user(self):
        """A plain accounting user cannot post a computed run."""
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        with self.assertRaises(UserError):
            run.with_user(self._plain_user()).action_post()
        # State is unchanged: the block happened before any transition.
        self.assertEqual(run.state, 'computed')

    def test_sod_reverse_blocked_for_plain_user(self):
        """A plain accounting user cannot reverse a posted run."""
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        with self.assertRaises(UserError):
            run.with_user(self._plain_user()).action_reverse()
        self.assertEqual(run.state, 'posted')
        self.assertFalse(run.reversal_move_id)

    def test_sod_cancel_blocked_for_plain_user(self):
        """A plain accounting user cannot cancel a computed run.

        This is the core regression: action_cancel previously had no
        group check at all, so any user could cancel a computed run.
        """
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        with self.assertRaises(UserError):
            run.with_user(self._plain_user()).action_cancel()
        # The run was not cancelled by the unauthorised user.
        self.assertEqual(run.state, 'computed')

    # ---- journal balance regression ----

    def _post_eur_payable_balance(self, amount_eur, amount_company, partner=None):
        partner = partner or self.partner_a
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-02-15',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'EUR payable',
                    'account_id': self.account_payable.id,
                    'partner_id': partner.id,
                    'currency_id': self.eur.id,
                    'amount_currency': -amount_eur,
                    'debit': 0.0,
                    'credit': amount_company,
                }),
                (0, 0, {
                    'name': 'EUR expense (in company ccy)',
                    'account_id': self.account_expense.id,
                    'debit': amount_company,
                    'credit': 0.0,
                }),
            ],
        })
        move.action_post()
        return move

    def test_posted_move_balances_for_asset_gain(self):
        """Asset receivable strengthens: DR receivable, CR FX gain.

        Regression for the bug where the leg followed adjustment sign
        only and produced unbalanced entries when the natural side and
        the adjustment sign disagreed.

        Odoo rate convention: rate stored on res.currency.rate is
        "units of foreign per 1 unit of company". Setting EUR rate=0.8
        means 1 USD = 0.8 EUR, so 1 EUR = 1.25 USD. A 1000 EUR
        receivable is now worth 1250 USD (asset strengthened) -> gain.
        """
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertTrue(run.line_ids)
        self.assertEqual(run.line_ids[:1].nature, 'gain')
        run.action_post()
        move = run.move_id
        self.assertTrue(move and move.state == 'posted')
        debit = sum(move.line_ids.mapped('debit'))
        credit = sum(move.line_ids.mapped('credit'))
        self.assertAlmostEqual(debit, credit)
        receivable_leg = move.line_ids.filtered(
            lambda line_item: line_item.account_id == self.account_receivable,
        )
        self.assertEqual(len(receivable_leg), 1)
        self.assertGreater(receivable_leg.debit, 0)
        self.assertEqual(receivable_leg.credit, 0)
        gain_leg = move.line_ids.filtered(
            lambda line_item: line_item.account_id == self.gain_account,
        )
        self.assertEqual(len(gain_leg), 1)
        self.assertGreater(gain_leg.credit, 0)
        self.assertEqual(gain_leg.debit, 0)

    def test_posted_move_balances_for_liability_loss(self):
        """Liability payable strengthens: CR payable, DR FX loss.

        EUR rate=0.8 means 1 USD = 0.8 EUR, so 1 EUR = 1.25 USD.
        A 2000 EUR payable was originally worth 2000 USD; it is now
        worth 2500 USD owed. Liability up = LOSS.
        """
        self.account_payable.eh_fx_revalue = True
        self._post_eur_payable_balance(2000.0, 2000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertTrue(run.line_ids)
        self.assertEqual(run.line_ids[:1].nature, 'loss')
        run.action_post()
        move = run.move_id
        self.assertTrue(move and move.state == 'posted')
        debit = sum(move.line_ids.mapped('debit'))
        credit = sum(move.line_ids.mapped('credit'))
        self.assertAlmostEqual(debit, credit)
        payable_leg = move.line_ids.filtered(
            lambda line_item: line_item.account_id == self.account_payable,
        )
        self.assertEqual(len(payable_leg), 1)
        self.assertGreater(payable_leg.credit, 0)
        self.assertEqual(payable_leg.debit, 0)
        loss_leg = move.line_ids.filtered(
            lambda line_item: line_item.account_id == self.loss_account,
        )
        self.assertEqual(len(loss_leg), 1)
        self.assertGreater(loss_leg.debit, 0)
        self.assertEqual(loss_leg.credit, 0)

    def test_revaluation_leg_carries_no_foreign_currency(self):
        """The translation adjustment is functional currency only.

        The original code set currency_id=foreign and amount_currency=0,
        which violates the move-line invariant. Verify the leg has no
        currency_id so amount_currency stays consistent.
        """
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 1.25)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        run.action_post()
        company_ccy = self.company.currency_id
        for leg in run.move_id.line_ids:
            self.assertIn(leg.currency_id, (False, company_ccy))

    # ---- B3: point-in-time residual reconstruction ----

    def test_residual_at_date_ignores_later_settlement(self):
        inv = self._post_eur_invoice_balance(1000.0, 1000.0)
        ar_line = inv.line_ids.filtered(
            lambda line_item: line_item.account_id == self.account_receivable)
        # A partial settlement dated AFTER the revaluation date.
        pay = self.env['account.move'].create({
            'move_type': 'entry', 'date': '2026-05-10',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'pmt',
                        'account_id': self.account_receivable.id,
                        'partner_id': self.partner_a.id,
                        'currency_id': self.eur.id,
                        'amount_currency': -400.0,
                        'debit': 0.0, 'credit': 400.0}),
                (0, 0, {'name': 'bank',
                        'account_id': self.account_revenue.id,
                        'debit': 400.0, 'credit': 0.0}),
            ],
        })
        pay.action_post()
        pay_ar = pay.line_ids.filtered(
            lambda line_item: line_item.account_id == self.account_receivable)
        (ar_line + pay_ar).reconcile()
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        # Before the settlement: full 1000 EUR was open.
        _comp, foreign = run._residual_at_date(
            ar_line, fields.Date.from_string('2026-03-31'))
        self.assertEqual(foreign, 1000.0)
        # After the settlement: residual drops to 600 EUR.
        _comp2, foreign2 = run._residual_at_date(
            ar_line, fields.Date.from_string('2026-06-30'))
        self.assertEqual(foreign2, 600.0)

    # ---- B3: fetch-frequency scheduler ----

    def test_fetch_frequency_due_logic(self):
        Config = self.env['eh.fx.rate.config']
        cfg = Config.search([('company_id', '=', self.company.id)], limit=1)
        if not cfg:
            cfg = Config.create({'company_id': self.company.id})
        now = fields.Datetime.now()

        cfg.fetch_frequency = 'manual'
        cfg.last_fetched_at = False
        self.assertFalse(cfg._is_fetch_due())

        cfg.fetch_frequency = 'daily'
        cfg.last_fetched_at = False
        self.assertTrue(cfg._is_fetch_due(), "never fetched -> due")
        cfg.last_fetched_at = now - timedelta(hours=1)
        self.assertFalse(cfg._is_fetch_due())
        cfg.last_fetched_at = now - timedelta(hours=25)
        self.assertTrue(cfg._is_fetch_due())

        cfg.fetch_frequency = 'weekly'
        cfg.last_fetched_at = now - timedelta(days=3)
        self.assertFalse(cfg._is_fetch_due())
        cfg.last_fetched_at = now - timedelta(days=8)
        self.assertTrue(cfg._is_fetch_due())

    # ---- line freeze on post ----

    def _posted_run_with_line(self):
        """Compute and post a run that produces at least one line, so the
        line-freeze behaviour can be exercised."""
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertTrue(run.line_ids)
        run.action_post()
        self.assertEqual(run.state, 'posted')
        return run

    def test_posted_line_write_blocked(self):
        """The model docstring promises lines are frozen on post; editing a
        line whose run has posted must raise."""
        run = self._posted_run_with_line()
        line = run.line_ids[:1]
        with self.assertRaises(UserError):
            line.adjustment = 12345.0

    def test_posted_line_unlink_blocked(self):
        """Deleting a line whose run has posted must raise."""
        run = self._posted_run_with_line()
        line = run.line_ids[:1]
        with self.assertRaises(UserError):
            line.unlink()

    # ---- run freeze on post ----

    def test_posted_run_input_field_write_blocked(self):
        """A posted run's measurement/input field is frozen at the ORM
        write layer; touching it must raise even outside the action
        methods."""
        run = self._posted_run_with_line()
        with self.assertRaises(UserError):
            run.write({'revaluation_date': '2026-04-30'})

    def test_posted_run_unlink_blocked(self):
        """A posted run cannot be unlinked; it is the record behind a
        posted journal entry."""
        run = self._posted_run_with_line()
        with self.assertRaises(UserError):
            run.unlink()

    def test_post_and_reverse_flow_still_works(self):
        """The normal action_post -> action_reverse flow is unaffected by
        the freeze guard: both write only state + audit stamps + move
        links, which are never frozen fields."""
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        self.assertTrue(run.move_id)
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        self.assertTrue(run.reversal_move_id)

    def test_draft_run_input_field_write_allowed(self):
        """Editing input fields while still draft/computed must NOT be
        blocked, so recompute and normal editing stay possible."""
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        # A computed run is not frozen: input edits pass.
        run.write({'description': 'March 2026 month end'})
        self.assertEqual(run.description, 'March 2026 month end')

    def test_draft_line_write_allowed(self):
        """Recomputing (which unlinks and rebuilds lines) and editing lines
        while the run is still computed must NOT be blocked."""
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        line = run.line_ids[:1]
        # Editing a computed (not yet posted) line is allowed.
        line.source_line_count = 99
        self.assertEqual(line.source_line_count, 99)
        # Recompute rebuilds lines (unlink of computed lines is allowed).
        run.action_compute()
        self.assertTrue(run.line_ids)
