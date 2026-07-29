# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 21 worked examples for the consolidation translation engine.

Each test is a hand-computed worked example: the inputs are stated in the
test, every expected amount is derived by hand in a comment, and the run
lines the engine writes are asserted exactly. No expected value is read back
from the engine under test.

Rate convention of the engine (eh_account_consolidation/models/consol_run.py):

* _fetch_balances MULTIPLIES each subsidiary balance (Odoo signed balance,
  debit positive, in the subsidiary's functional currency) by a conversion
  factor obtained as

      source_currency._convert(1.0, presentation_currency, sub_company, date)

  so the factor is quoted as PRESENTATION UNITS PER ONE SUBSIDIARY UNIT.
* Balance-sheet accounts use the factor at period_to (the closing rate).
* P&L accounts use _period_average_rate: the day-weighted average of that
  factor across [period_from, period_to]; each spot rate is weighted by the
  number of days it is in force, and the final segment runs through
  period_to inclusive (seg_end = period_to + 1 day).
* Rate records follow the recipe of tests/test_consolidation.py: they are
  created on the presentation currency with company_id set to the subsidiary
  company, so an Odoo rate of R means 1 subsidiary-currency unit buys R
  presentation units.

Worked-example story: the subsidiary keeps its books in EUR and the group
presents in USD. Here the seeded fixture company plays the EUR subsidiary
(its posted balances are the EUR figures) and a synthetic presentation
currency plays the USD. A quote of the form "1 USD = 0.80 EUR" is the
INVERSE of the factor the engine multiplies by, so the rates are pinned in
the engine's own multiply direction:

    2026-01-01 .. 2026-01-05: 1 EUR = 0.80 USD (opening)
    2026-01-06 onwards:       1 EUR = 0.90 USD (closing)

The run period is 2026-01-01 .. 2026-01-10 with the single rate change on
2026-01-06, so the day-weighted average splits into two equal 5-day
segments and is exactly

    average = (5 days x 0.80 + 5 days x 0.90) / 10 days = 0.85 USD per EUR.
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


def _acc_company_field(env):
    """account.account is multi-company (company_ids m2m) from Odoo 18."""
    return (
        'company_ids'
        if 'company_ids' in env['account.account']._fields
        else 'company_id'
    )


def _make_account(env, company, code, name, account_type):
    """Create/return an account owned by `company`, cross-version safe."""
    Account = env['account.account'].with_company(company)
    field = _acc_company_field(env)
    value = [(6, 0, company.ids)] if field == 'company_ids' else company.id
    existing = Account.search([
        ('code', '=', code), (field, 'in', company.ids)], limit=1)
    if existing:
        return existing
    return Account.create({
        'code': code, 'name': name, 'account_type': account_type,
        field: value,
    })


@tagged('eh_golden', 'eh_account_consolidation', 'post_install',
        '-at_install')
class TestGoldenIas21Consol(EhGoldenTestCase):
    """IAS 21 closing/average translation, the CTA plug, the NCI carve."""

    PERIOD_FROM = '2026-01-01'
    PERIOD_TO = '2026-01-10'
    BOOK_DATE = '2026-01-05'

    def setUp(self):
        super().setUp()
        Currency = self.env['res.currency']
        # Presentation currency (the USD of the worked example), distinct
        # from the subsidiary's functional currency. Freshly created, so its
        # rate table holds exactly the two records pinned below and the
        # average / closing lookups are fully deterministic.
        self.pres_ccy = Currency.create({
            'name': 'TGD',
            'symbol': 'G',
            'rounding': 0.01,
            'active': True,
        })
        self.pres_ccy.rate_ids.unlink()
        # Recipe of tests/test_consolidation.py: rate records live on the
        # presentation currency with company_id = the subsidiary company, so
        # rate = presentation units per 1 subsidiary unit. _set_rate
        # (golden_common) creates exactly that record (company defaults to
        # cls.company, the subsidiary).
        #   2026-01-01 .. 2026-01-05: 1 sub unit = 0.80 pres units
        #   2026-01-06 onwards:       1 sub unit = 0.90 pres units
        self._set_rate(self.pres_ccy, self.PERIOD_FROM, 0.80)
        self._set_rate(self.pres_ccy, '2026-01-06', 0.90)
        # A separate parent company (a member cannot be the parent), and the
        # multi-company access dance so the module's record rules do not
        # hide the consolidation records from the test user.
        self.parent_company = self.env['res.company'].create({
            'name': 'Golden IAS21 Parent Co',
            'currency_id': self.pres_ccy.id,
        })
        self.env.user.write({'company_id': self.parent_company.id})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[self.company.id, self.parent_company.id],
        ))
        # Explicit CTA + NCI equity accounts so the run resolves them by
        # configuration rather than the name heuristic, plus a retained
        # earnings account so the NCI reclass leg resolves deterministically
        # (_nci_reclass_account preference 2: the sole equity_unaffected
        # account on the parent chart).
        self.cta_account = _make_account(
            self.env, self.parent_company, '3900',
            'Currency Translation Reserve', 'equity')
        self.nci_account = _make_account(
            self.env, self.parent_company, '3200',
            'Non-Controlling Interest', 'equity')
        self.re_account = _make_account(
            self.env, self.parent_company, '3100',
            'Consolidated Retained Earnings', 'equity_unaffected')
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'Golden IAS21 Group',
            'code': 'golden_ias21_group',
            'parent_company_id': self.parent_company.id,
            'presentation_currency_id': self.pres_ccy.id,
            'cta_account_id': self.cta_account.id,
            'nci_account_id': self.nci_account.id,
        })

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _member(self, pct):
        return self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': pct,
            'method': 'full',
        })

    def _computed_run(self):
        run = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': self.PERIOD_FROM,
            'period_to': self.PERIOD_TO,
        })
        run.action_compute()
        return run

    def _seed_capital_and_revenue(self):
        """Subsidiary books, in the subsidiary's functional currency (the
        EUR of the worked example): share capital 100,000 and revenue
        50,000, balanced by a cash asset of 150,000."""
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 150000.0},
                {'account': self.account_equity, 'credit': 100000.0},
                {'account': self.account_revenue, 'credit': 50000.0},
            ],
            date=fields.Date.from_string(self.BOOK_DATE),
        )

    # ------------------------------------------------------------------
    # golden examples
    # ------------------------------------------------------------------
    def test_golden_closing_vs_average_rate_translation(self):
        # IAS 21.39(a)-(b): assets and liabilities (and equity, which the
        # engine treats as a balance-sheet item) translate at the closing
        # rate; income and expenses translate at rates approximating the
        # transaction dates, here the period average.
        #
        # Inputs (subsidiary currency, the EUR of the story; Odoo signed
        # balances, debit positive):
        #   share capital 100,000 credit -> balance -100,000
        #   revenue        50,000 credit -> balance  -50,000
        #   cash (balancing asset) 150,000 debit -> balance +150,000
        #
        # Rates (presentation units per 1 subsidiary unit; the engine
        # MULTIPLIES the subsidiary balance by these, consol_run.py
        # _fetch_balances):
        #   closing = factor in force at period_to 2026-01-10 = 0.90
        #             (the 2026-01-06 rate record is the last one <= it)
        #   average = day-weighted across 2026-01-01 .. 2026-01-10
        #           = (5 days x 0.80 + 5 days x 0.90) / 10 days = 0.85
        #
        # Hand-derived translated lines:
        #   cash          +150,000 x 0.90 = +135,000.00 (closing, B/S)
        #   share capital -100,000 x 0.90 =  -90,000.00 (closing, B/S)
        #   revenue        -50,000 x 0.85 =  -42,500.00 (average, P&L)
        self._member(100.0)
        self._seed_capital_and_revenue()
        run = self._computed_run()
        sub_lines = run.line_ids.filtered(
            lambda l: l.kind == 'subsidiary_balance')
        self.assertEqual(
            len(sub_lines), 3,
            "exactly one translated line per seeded account expected")
        cash_line = sub_lines.filtered(
            lambda l: l.account_id == self.account_cash)
        equity_line = sub_lines.filtered(
            lambda l: l.account_id == self.account_equity)
        revenue_line = sub_lines.filtered(
            lambda l: l.account_id == self.account_revenue)
        self.assertAlmostEqual(cash_line.amount, 135000.00, places=2)
        self.assertAlmostEqual(equity_line.amount, -90000.00, places=2)
        self.assertAlmostEqual(revenue_line.amount, -42500.00, places=2)

    def test_golden_cta_balances_the_translated_trial_balance(self):
        # Same books as the translation example. The subsidiary's own trial
        # balance nets to zero, but the translated one no longer does,
        # because revenue is translated at 0.85 while every balance-sheet
        # item uses 0.90:
        #
        #   +135,000.00 (cash) - 90,000.00 (capital) - 42,500.00 (revenue)
        #   = +2,500.00
        #
        # Cross-check: revenue at the closing rate would have been
        # -50,000 x 0.90 = -45,000.00; the average-rate figure of
        # -42,500.00 credits exactly 2,500.00 less, which is the residue.
        #
        # The engine books the CTA as the NEGATED sum of every other line
        # (consol_run.py, _compute_cta), so
        #
        #   CTA = -(+2,500.00) = -2,500.00
        #
        # a credit to the translation reserve, after which the run sums to
        # zero.
        self._member(100.0)
        self._seed_capital_and_revenue()
        run = self._computed_run()
        cta_lines = run.line_ids.filtered(lambda l: l.kind == 'cta')
        self.assertEqual(len(cta_lines), 1, "exactly one CTA line expected")
        self.assertEqual(cta_lines.account_id, self.cta_account)
        self.assertAlmostEqual(cta_lines.amount, -2500.00, places=2)
        total = sum(run.line_ids.filtered(
            lambda l: l.kind in (
                'subsidiary_balance', 'parent_balance', 'elimination',
                'equity_pickup', 'nci', 'cta',
            )).mapped('amount'))
        self.assertAlmostEqual(
            total, 0.00, places=2,
            msg="the run must balance once the CTA plug is included")

    def test_golden_nci_carve_at_80_percent(self):
        # 80%-owned subsidiary whose only balances are equity against cash,
        # so every line is a balance-sheet item at the closing rate and no
        # CTA arises:
        #
        #   share capital 100,000 credit -> -100,000 x 0.90 = -90,000.00
        #   cash          100,000 debit  -> +100,000 x 0.90 = +90,000.00
        #
        # Translated subsidiary equity (signed, credit-negative):
        #
        #   E = -90,000.00
        #
        # NCI carve (consol_run.py, _build_nci_vals): a balanced two-leg
        # reclass within equity,
        #
        #   NCI line (kind 'nci', credit)  = 0.20 x E
        #                                  = 0.20 x -90,000.00 = -18,000.00
        #   reclass leg (kind 'elimination', debit to consolidated
        #   retained earnings)             = -(-18,000.00)     = +18,000.00
        #
        # Parent share of the translated equity = 0.80 x E = -72,000.00,
        # which is what remains once the reclass debit is netted against
        # the full translated equity line:
        #
        #   -90,000.00 + 18,000.00 = -72,000.00
        self._member(80.0)
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 100000.0},
                {'account': self.account_equity, 'credit': 100000.0},
            ],
            date=fields.Date.from_string(self.BOOK_DATE),
        )
        run = self._computed_run()
        nci_lines = run.line_ids.filtered(lambda l: l.kind == 'nci')
        self.assertEqual(len(nci_lines), 1, "exactly one NCI line expected")
        self.assertEqual(nci_lines.account_id, self.nci_account)
        self.assertAlmostEqual(nci_lines.amount, -18000.00, places=2)
        reclass_lines = run.line_ids.filtered(
            lambda l: l.kind == 'elimination')
        self.assertEqual(
            len(reclass_lines), 1,
            "exactly one reclass leg for the NCI carve expected")
        self.assertEqual(reclass_lines.account_id, self.re_account)
        self.assertAlmostEqual(reclass_lines.amount, 18000.00, places=2)
        equity_line = run.line_ids.filtered(
            lambda l: l.kind == 'subsidiary_balance'
            and l.account_id == self.account_equity)
        self.assertAlmostEqual(
            equity_line.amount + reclass_lines.amount, -72000.00, places=2,
            msg="parent share must be 0.80 x the translated equity")
        # Everything translated at the one closing rate and the NCI pair
        # nets to zero, so no CTA line may be booked.
        self.assertFalse(
            run.line_ids.filtered(lambda l: l.kind == 'cta'),
            "no CTA when the whole trial balance translates at one rate")
