# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 23 worked examples for borrowing cost capitalisation.

Each test is a hand-computed worked example in the spirit of the standard's
illustrative material: the inputs are stated in the test, every expected
amount is derived by hand in a comment, and the posted journal entry is
asserted exactly. No expected value is read back from the engine under test.

Day-count convention of the engine (see
eh_account_borrowing_costs/models/borrowing_cost.py, _weighted_average_base):
actual-day count with an inclusive-start / exclusive-end date difference,

    weight = (period_end - expenditure_date).days
             / (period_end - period_start).days

so a full calendar year 2026-01-01 .. 2026-12-31 spans 364 days (not 365),
and expenditure dated 2026-07-01 is outstanding 183/364 of that period. In
the suspended (windowed) case the DENOMINATOR stays the full nominal period;
suspensions only remove days from the numerator (_windowed_base).
"""

from datetime import date

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_borrowing_costs', 'post_install',
        '-at_install')
class TestGoldenIas23(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.asset = cls._ensure_account(
            cls.env, '1680', 'Asset Under Construction', 'asset_non_current')
        cls.interest = cls._ensure_account(
            cls.env, '5680', 'Interest Expense', 'expense')

    def _rec(self, **vals):
        base = {
            'name': '/', 'qualifying_asset': 'New plant',
            'asset_account_id': self.asset.id,
            'borrowing_cost_account_id': self.interest.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.borrowing.cost'].create(base)

    def test_golden_weighted_average_mid_period_spend(self):
        # IAS 23.14 weighted-average accumulated expenditure, general
        # borrowings only, with a mid-period spend.
        #
        # Capitalisation period 2026-01-01 .. 2026-12-31.
        #   total_days = (2026-12-31 - 2026-01-01).days = 364
        #     (inclusive start, exclusive end; 2026 is not a leap year)
        # Expenditure:
        #   1,200,000 on 2026-01-01 -> outstanding 364/364 of the period
        #     contribution = 1,200,000 x 364/364      = 1,200,000.0000
        #     600,000 on 2026-07-01 -> outstanding (2026-12-31 - 2026-07-01)
        #     = 183 days
        #     contribution = 600,000 x 183/364        =   301,648.3516
        #   weighted-average base                     = 1,501,648.3516
        #     rounded to company currency (2dp)       = 1,501,648.35
        # Capitalisation rate 10%:
        #   general = 1,501,648.3516 x 10%            =   150,164.8352
        #     rounded                                 =   150,164.84
        # No specific borrowing, no cap -> capitalisable = 150,164.84.
        rec = self._rec(
            capitalisation_rate=10.0,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            expenditure_line_ids=[
                (0, 0, {'date': date(2026, 1, 1), 'amount': 1200000.0}),
                (0, 0, {'date': date(2026, 7, 1), 'amount': 600000.0}),
            ])
        self.assertAlmostEqual(
            rec.weighted_average_base, 1501648.35, places=2)
        self.assertAlmostEqual(rec.capitalisable, 150164.84, places=2)
        # The posted entry reclassifies exactly that amount from interest
        # expense to the qualifying asset.
        rec.action_capitalise()
        self.assertEqual(rec.state, 'capitalised')
        self.assertMoveLines(rec.move_id, [
            (self.asset, 150164.84, 0.0),
            (self.interest, 0.0, 150164.84),
        ])
        self.assertBalanced(rec.move_id)

    def test_golden_specific_net_of_temporary_investment_income(self):
        # IAS 23.12-13: borrowing costs on specific borrowings are
        # capitalised net of income on the temporary investment of those
        # borrowings.
        #
        #   specific borrowing cost                   = 80,000
        #   temporary investment income               = 12,000
        #   specific capitalisable = 80,000 - 12,000  = 68,000
        rec = self._rec(
            specific_borrowing_cost=80000.0,
            temporary_investment_income=12000.0)
        self.assertAlmostEqual(rec.capitalisable, 68000.0, places=2)
        rec.action_capitalise()
        self.assertMoveLines(rec.move_id, [
            (self.asset, 68000.0, 0.0),
            (self.interest, 0.0, 68000.0),
        ])
        self.assertBalanced(rec.move_id)

        # Floor at zero: income exceeding the specific cost reduces the
        # specific component to nil, never below.
        #   specific net = max(80,000 - 95,000, 0)    = 0 (not -15,000)
        rec_floor = self._rec(
            specific_borrowing_cost=80000.0,
            temporary_investment_income=95000.0)
        self.assertAlmostEqual(rec_floor.capitalisable, 0.0, places=2)

        # And the excess income must NOT spill over and erode the general
        # component (IAS 23.12: the income belongs to the specific
        # borrowing's own capitalisable amount only).
        #   specific net = max(80,000 - 95,000, 0)    = 0
        #   general      = 200,000 x 5%               = 10,000
        #   capitalisable                             = 10,000
        # A spill defect would net the -15,000 excess into the general
        # pool and give -5,000 (or a floored 0).
        rec_spill = self._rec(
            specific_borrowing_cost=80000.0,
            temporary_investment_income=95000.0,
            general_expenditure=200000.0,
            capitalisation_rate=5.0)
        self.assertAlmostEqual(rec_spill.capitalisable, 10000.0, places=2)

    def test_golden_suspension_excludes_three_months(self):
        # IAS 23.20-21: capitalisation is suspended while active development
        # is suspended. Same inputs as the weighted-average example, with
        # development suspended for three full months, 2026-03-01 to
        # 2026-06-01 (Mar 31 + Apr 30 + May 31 = 92 days).
        #
        # Engine convention (_windowed_base): the denominator remains the
        # full nominal period (364 days); the suspension removes days from
        # each line's outstanding-day numerator only.
        #
        # Active spans: (2026-01-01 .. 2026-03-01) and
        #               (2026-06-01 .. 2026-12-31).
        # Line 1: 1,200,000 dated 2026-01-01
        #   span 1 days = (2026-03-01 - 2026-01-01).days = 59
        #   span 2 days = (2026-12-31 - 2026-06-01).days = 213
        #   outstanding = 59 + 213 = 272 (= 364 - 92)
        #   contribution = 1,200,000 x 272/364        =   896,703.2967
        # Line 2: 600,000 dated 2026-07-01 (inside span 2 only)
        #   outstanding = (2026-12-31 - 2026-07-01).days = 183
        #   contribution = 600,000 x 183/364          =   301,648.3516
        # Base = (1,200,000 x 272 + 600,000 x 183) / 364
        #      = 436,200,000 / 364                    = 1,198,351.6484
        #   rounded                                   = 1,198,351.65
        # Capitalisable = 1,198,351.6484 x 10%        =   119,835.1648
        #   rounded                                   =   119,835.16
        rec = self._rec(
            capitalisation_rate=10.0,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            expenditure_line_ids=[
                (0, 0, {'date': date(2026, 1, 1), 'amount': 1200000.0}),
                (0, 0, {'date': date(2026, 7, 1), 'amount': 600000.0}),
            ],
            suspension_line_ids=[
                (0, 0, {'date_start': date(2026, 3, 1),
                        'date_end': date(2026, 6, 1)}),
            ])
        self.assertTrue(rec._has_capitalisation_window())
        self.assertAlmostEqual(
            rec.weighted_average_base, 1198351.65, places=2)
        self.assertAlmostEqual(rec.capitalisable, 119835.16, places=2)
        # Strictly below the unsuspended figure (150,164.84) by the
        # suspended slice of line 1: 1,200,000 x 92/364 x 10% = 30,329.67.
        self.assertNotAlmostEqual(rec.capitalisable, 150164.84, places=2)

    def test_golden_capped_at_actual_borrowing_cost(self):
        # IAS 23.14: the amount capitalised in a period shall not exceed the
        # borrowing costs incurred in that period. Same weighted-average
        # setup as the first example:
        #   uncapped = 1,501,648.3516 x 10%           =   150,164.8352
        #     rounded                                 =   150,164.84
        # Actual borrowing cost incurred              =    50,000
        #   capitalisable = min(150,164.84, 50,000)   =    50,000 exactly
        rec = self._rec(
            capitalisation_rate=10.0,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            actual_borrowing_cost=50000.0,
            expenditure_line_ids=[
                (0, 0, {'date': date(2026, 1, 1), 'amount': 1200000.0}),
                (0, 0, {'date': date(2026, 7, 1), 'amount': 600000.0}),
            ])
        self.assertAlmostEqual(rec.uncapped_amount, 150164.84, places=2)
        self.assertAlmostEqual(rec.capitalisable, 50000.0, places=2)
        # The posted entry reclassifies interest expense to the qualifying
        # asset for exactly the capped amount, not the uncapped figure.
        rec.action_capitalise()
        self.assertEqual(rec.state, 'capitalised')
        self.assertMoveLines(rec.move_id, [
            (self.asset, 50000.0, 0.0),
            (self.interest, 0.0, 50000.0),
        ])
        self.assertBalanced(rec.move_id)
