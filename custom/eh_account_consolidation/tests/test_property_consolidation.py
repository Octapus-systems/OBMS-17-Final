# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Combinatorial scenario matrix for the consolidation engine.

Consolidation is one of the three riskiest engines in the program plan, so
the matrix runs the FULL cartesian product (not just all-pairs) over

    method     x  {full, proportional, equity}
    currency   x  {same, foreign}
    nci_basis  x  {prop, fv}
    ownership  x  {100, 75, 51}

with invariant oracles per case:

* every computed run nets to exactly zero (the CTA plug closes it);
* NCI lines appear exactly when they must (full method below 100%
  ownership) and never otherwise (proportional and equity methods are
  structurally NCI-free; 100% ownership has no minority);
* goodwill-kind lines appear exactly for the fair-value-basis cases (the
  only ones configured for the IFRS 3 elimination here) and never
  otherwise;
* equity-method members produce exactly the two balanced IAS 28 pick-up
  legs and no line-by-line rollup.

Case normalisation mirrors the model constraints: the fair-value NCI basis
exists only on a full-method member with a genuine minority, so all other
combinations collapse to the proportionate basis exactly as the constraint
set forces them to.

Books seeded once per test in the shared subsidiary company (reads are
non-destructive, so every case consolidates the same underlying trial
balance): cash 1,000 debit, share capital 600 credit, revenue 400 credit.
Foreign cases translate at closing 0.90 / day-weighted average 0.85 (rates
0.80 from 2026-01-01, 0.90 from 2026-01-06, period 2026-01-01..2026-01-10).
"""

from odoo import fields
from odoo.tests import tagged
from odoo.tools import float_is_zero

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import full_product


def _acc_company_field(env):
    return (
        'company_ids'
        if 'company_ids' in env['account.account']._fields
        else 'company_id'
    )


def _make_account(env, company, code, name, account_type):
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


AXES = {
    'method': ['full', 'proportional', 'equity'],
    'currency': ['same', 'foreign'],
    'nci_basis': ['prop', 'fv'],
    'ownership': [100.0, 75.0, 51.0],
}

PERIOD_FROM = '2026-01-01'
PERIOD_TO = '2026-01-10'
BOOK_DATE = '2026-01-05'


@tagged('eh_golden', 'eh_account_consolidation', 'post_install',
        '-at_install')
class TestConsolScenarioMatrix(EhGoldenTestCase):

    def setUp(self):
        super().setUp()
        # Foreign presentation currency with a genuine average/closing gap.
        self.fx_ccy = self.env['res.currency'].create({
            'name': 'TPW', 'symbol': 'W', 'rounding': 0.01, 'active': True,
        })
        self.fx_ccy.rate_ids.unlink()
        self._set_rate(self.fx_ccy, '2026-01-01', 0.80)
        self._set_rate(self.fx_ccy, '2026-01-06', 0.90)
        self.same_ccy = self.company.currency_id
        # One parent company per presentation currency (the member cannot be
        # the parent, and the ledger-company currency must match).
        self.parents = {}
        for key, ccy in (('same', self.same_ccy), ('foreign', self.fx_ccy)):
            parent = self.env['res.company'].create({
                'name': 'PW Parent %s' % key,
                'currency_id': ccy.id,
            })
            self.env.user.write({'company_id': parent.id})
            self.parents[key] = parent
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[
                self.company.id,
                self.parents['same'].id,
                self.parents['foreign'].id,
            ],
        ))
        # Consolidated-chart accounts per parent.
        self.charts = {}
        for key, parent in self.parents.items():
            self.charts[key] = {
                'cta': _make_account(
                    self.env, parent, '3900', 'CTA Reserve', 'equity'),
                'nci': _make_account(
                    self.env, parent, '3200', 'NCI', 'equity'),
                're': _make_account(
                    self.env, parent, '3100', 'Retained Earnings',
                    'equity_unaffected'),
                'investment': _make_account(
                    self.env, parent, '1500', 'Investment in Sub',
                    'asset_non_current'),
                'equity_elim': _make_account(
                    self.env, parent, '3150', 'Pre-Acq Equity Elim',
                    'equity'),
                'goodwill': _make_account(
                    self.env, parent, '1600', 'Goodwill',
                    'asset_non_current'),
                'sop': _make_account(
                    self.env, parent, '4100', 'Share of Profit', 'income'),
            }
        # Shared subsidiary books, read (never mutated) by every case.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_equity, 'credit': 600.0},
                {'account': self.account_revenue, 'credit': 400.0},
            ],
            date=fields.Date.from_string(BOOK_DATE),
        )

    def _build_case(self, idx, case):
        """Create entity + member + run for one scenario, normalised the way
        the model constraints force it, and return (run, expectations)."""
        currency_key = case['currency']
        parent = self.parents[currency_key]
        chart = self.charts[currency_key]
        pres_ccy = parent.currency_id
        ownership = case['ownership']
        method = case['method']
        # The fair-value NCI basis exists only on a full member with a real
        # minority (constraints _check_fair_value_nci_config /
        # _check_proportional_no_nci); every other combination collapses to
        # proportionate.
        fair_value = (
            case['nci_basis'] == 'fv'
            and method == 'full'
            and ownership < 100.0
        )
        entity = self.env['eh.consol.entity'].create({
            'name': 'PW Group %d' % idx,
            'code': 'pw_group_%d' % idx,
            'parent_company_id': parent.id,
            'presentation_currency_id': pres_ccy.id,
            'cta_account_id': chart['cta'].id,
            'nci_account_id': chart['nci'].id,
        })
        member_vals = {
            'entity_id': entity.id,
            'company_id': self.company.id,
            'ownership_pct': ownership,
            'method': method,
        }
        if method == 'equity':
            # Mandatory IAS 28 pick-up configuration.
            member_vals.update({
                'investment_account_id': chart['investment'].id,
                'share_of_profit_account_id': chart['sop'].id,
            })
        if fair_value:
            # Full IFRS 3 elimination configuration so the fair-value
            # acquisition NCI and the full-goodwill residual are booked.
            member_vals.update({
                'investment_account_id': chart['investment'].id,
                'investment_amount': 800.0,
                'acquisition_equity': 600.0,
                'equity_elimination_account_id': chart['equity_elim'].id,
                'goodwill_account_id': chart['goodwill'].id,
                'nci_account_id': chart['nci'].id,
                'nci_basis': 'fair_value',
                'nci_fair_value': 150.0,
            })
        self.env['eh.consol.member'].create(member_vals)
        run = self.env['eh.consol.run'].create({
            'entity_id': entity.id,
            'period_from': PERIOD_FROM,
            'period_to': PERIOD_TO,
        })
        run.action_compute()
        expectations = {
            'pres_ccy': pres_ccy,
            'expect_nci': method == 'full' and ownership < 100.0,
            # Only the fair-value cases are configured for the IFRS 3
            # elimination here, and their goodwill residual
            # (800 + 150 - 600 = 350) is structurally non-zero.
            'expect_goodwill': fair_value,
            'method': method,
        }
        return run, expectations

    def test_full_matrix_invariants(self):
        for idx, case in enumerate(full_product(AXES)):
            with self.subTest(case=case):
                run, exp = self._build_case(idx, case)
                pres_ccy = exp['pres_ccy']
                # Invariant 1: the computed run nets to exactly zero in the
                # presentation currency once the CTA plug is included.
                total = sum(run.line_ids.mapped('amount'))
                self.assertTrue(
                    float_is_zero(
                        total, precision_rounding=pres_ccy.rounding),
                    "case %r: run must net to zero, got %r" % (case, total))
                # Invariant 2: NCI lines appear exactly when a full-method
                # member carries a genuine minority.
                nci_lines = run.line_ids.filtered(
                    lambda l: l.kind == 'nci' and not float_is_zero(
                        l.amount, precision_rounding=pres_ccy.rounding))
                self.assertEqual(
                    bool(nci_lines), exp['expect_nci'],
                    "case %r: NCI presence mismatch" % (case,))
                # Invariant 3: goodwill-kind lines appear exactly for the
                # configured fair-value acquisitions.
                goodwill_lines = run.line_ids.filtered(
                    lambda l: l.kind == 'goodwill')
                self.assertEqual(
                    bool(goodwill_lines), exp['expect_goodwill'],
                    "case %r: goodwill presence mismatch" % (case,))
                # Invariant 4: method shape.
                sub_lines = run.line_ids.filtered(
                    lambda l: l.kind == 'subsidiary_balance')
                pickup_lines = run.line_ids.filtered(
                    lambda l: l.kind == 'equity_pickup')
                if exp['method'] == 'equity':
                    self.assertFalse(
                        sub_lines,
                        "case %r: equity member must not roll up" % (case,))
                    self.assertEqual(
                        len(pickup_lines), 2,
                        "case %r: exactly two pick-up legs" % (case,))
                    self.assertTrue(
                        float_is_zero(
                            sum(pickup_lines.mapped('amount')),
                            precision_rounding=pres_ccy.rounding),
                        "case %r: pick-up pair must balance" % (case,))
                else:
                    self.assertTrue(
                        sub_lines,
                        "case %r: rolled-up member must produce "
                        "subsidiary lines" % (case,))
                    self.assertFalse(
                        pickup_lines,
                        "case %r: no pick-up outside the equity "
                        "method" % (case,))
