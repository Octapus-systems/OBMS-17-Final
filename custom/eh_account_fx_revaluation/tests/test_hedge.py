# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hedge accounting tests (IFRS 9).
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_fx_revaluation', 'integration', 'post_install', '-at_install')
class TestHedgeDesignation(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.oci = cls._ensure_account(
            cls.env, '3500', 'CFH Reserve', 'equity',
        )
        cls.pl = cls._ensure_account(
            cls.env, '5950', 'Hedge P&L', 'expense',
        )
        cls.instrument = cls._ensure_account(
            cls.env, '1700', 'Forward Contract', 'asset_current',
        )

    def _make_hedge(self, **overrides):
        vals = {
            'name': '/',
            'hedge_type': 'cash_flow',
            'hedged_item_description': 'USD revenue Q3 2026',
            'hedging_instrument_description': 'AUD/USD forward 1M notional',
            'notional_amount': 1_000_000.0,
            'hedged_currency_id': self.env.company.currency_id.id,
            'oci_account_id': self.oci.id,
            'pl_account_id': self.pl.id,
            'instrument_account_id': self.instrument.id,
            'journal_id': self.journal_misc.id,
            'notes': 'Risk management strategy: hedge variability of expected USD revenue.',
        }
        vals.update(overrides)
        return self.env['eh.fx.hedge'].create(vals)

    def test_designate_requires_documentation(self):
        h = self._make_hedge(notes=False)
        with self.assertRaises(UserError):
            h.action_designate()

    def test_designate_happy_path(self):
        h = self._make_hedge()
        h.action_designate()
        self.assertEqual(h.state, 'designated')

    def test_dedesignate_only_from_active(self):
        h = self._make_hedge()
        with self.assertRaises(UserError):
            h.action_dedesignate()
        h.action_designate()
        h.action_dedesignate()
        self.assertEqual(h.state, 'dedesignated')
        self.assertTrue(h.termination_date)

    def test_cfh_requires_oci_account(self):
        h = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'hedged_item_description': 'X',
            'hedging_instrument_description': 'Y',
            'pl_account_id': self.pl.id,
            'instrument_account_id': self.instrument.id,
            'journal_id': self.journal_misc.id,
            'notes': 'docs',
        })
        with self.assertRaises(ValidationError):
            h.action_designate()


@tagged('eh_account_fx_revaluation', 'integration', 'post_install', '-at_install')
class TestHedgeEffectiveness(EhAccountIntegrationTestCase):

    def test_dollar_offset_within_band_passes(self):
        h = self._make_minimal_hedge()
        test = self.env['eh.fx.hedge.test'].create({
            'hedge_id': h.id,
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1000.0,
            'cumulative_hedged_change': -1100.0,
        })
        # 1000 / 1100 = 0.909 -> effective
        self.assertAlmostEqual(test.offset_ratio, 0.9091, places=4)
        self.assertTrue(test.is_effective)

    def test_movement_cannot_be_created_posted(self):
        """A hedge movement is born in draft and only reaches a posted /
        reclassified state through Post / Reclassify, which attach its GL
        move. Creating one directly posted would fabricate a finalised figure
        with no journal entry behind it, so it is refused at the ORM layer."""
        h = self._make_minimal_hedge()
        with self.assertRaises(UserError):
            self.env['eh.fx.hedge.movement'].create({
                'hedge_id': h.id, 'state': 'posted'})
        with self.assertRaises(UserError):
            self.env['eh.fx.hedge.movement'].create({
                'hedge_id': h.id, 'state': 'reclassified'})

    def test_dollar_offset_below_band_fails(self):
        h = self._make_minimal_hedge()
        test = self.env['eh.fx.hedge.test'].create({
            'hedge_id': h.id,
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1000.0,
            'cumulative_hedged_change': -2000.0,
        })
        # 1000 / 2000 = 0.5 -> below 0.80 floor
        self.assertFalse(test.is_effective)

    def test_dollar_offset_above_band_fails(self):
        h = self._make_minimal_hedge()
        test = self.env['eh.fx.hedge.test'].create({
            'hedge_id': h.id,
            'method': 'dollar_offset',
            'cumulative_instrument_change': 2000.0,
            'cumulative_hedged_change': -1000.0,
        })
        # 2000 / 1000 = 2.0 -> above 1.25 cap
        self.assertFalse(test.is_effective)

    def test_regression_method_high_correlation_passes(self):
        h = self._make_minimal_hedge(test_method='regression')
        # Pairs with strong negative linear relationship -> r-squared near 1.
        pairs_text = "\n".join([
            "100,-100", "200,-205", "300,-295", "400,-410", "500,-500",
        ])
        test = self.env['eh.fx.hedge.test'].create({
            'hedge_id': h.id,
            'method': 'regression',
            'regression_pairs': pairs_text,
        })
        self.assertGreater(test.rsquared, 0.95)
        self.assertTrue(test.is_effective)

    def test_regression_method_low_correlation_fails(self):
        h = self._make_minimal_hedge(test_method='regression')
        pairs_text = "\n".join([
            "100,-50", "200,300", "300,-100", "400,200", "500,-400",
        ])
        test = self.env['eh.fx.hedge.test'].create({
            'hedge_id': h.id,
            'method': 'regression',
            'regression_pairs': pairs_text,
        })
        self.assertLess(test.rsquared, 0.80)
        self.assertFalse(test.is_effective)

    def test_regression_positive_slope_rejected(self):
        """A positively correlated pair (instrument moving WITH the hedged
        item, not against it) has R-squared near 1 but is not a hedge. The
        positive slope must make it ineffective; R-squared alone would
        wrongly pass it. This is the IFRS 9 sign-blindness defect."""
        h = self._make_minimal_hedge(test_method='regression')
        pairs_text = "\n".join([
            "100,100", "200,205", "300,295", "400,410", "500,500",
        ])
        test = self.env['eh.fx.hedge.test'].create({
            'hedge_id': h.id,
            'method': 'regression',
            'regression_pairs': pairs_text,
        })
        self.assertGreater(test.rsquared, 0.95)
        self.assertGreater(test.slope, 0.0)
        self.assertFalse(test.is_effective)

    def test_regression_under_hedge_slope_rejected(self):
        """A negative but shallow slope (the instrument under-offsets the
        hedged item) is ineffective even at R-squared 1, mirroring the
        dollar-offset lower band."""
        h = self._make_minimal_hedge(test_method='regression')
        pairs_text = "\n".join([
            "100,-50", "200,-100", "300,-150", "400,-200", "500,-250",
        ])
        test = self.env['eh.fx.hedge.test'].create({
            'hedge_id': h.id,
            'method': 'regression',
            'regression_pairs': pairs_text,
        })
        self.assertGreater(test.rsquared, 0.99)
        self.assertAlmostEqual(test.slope, -0.5, places=2)
        self.assertFalse(test.is_effective)

    def test_regression_records_negative_slope_when_effective(self):
        """The effective case records a negative slope close to -1."""
        h = self._make_minimal_hedge(test_method='regression')
        pairs_text = "\n".join([
            "100,-100", "200,-205", "300,-295", "400,-410", "500,-500",
        ])
        test = self.env['eh.fx.hedge.test'].create({
            'hedge_id': h.id,
            'method': 'regression',
            'regression_pairs': pairs_text,
        })
        self.assertTrue(test.is_effective)
        self.assertLess(test.slope, 0.0)
        self.assertAlmostEqual(abs(test.slope), 1.0, delta=0.1)

    def _make_minimal_hedge(self, **overrides):
        vals = {
            'name': '/',
            'hedge_type': 'cash_flow',
            'hedged_item_description': 'X',
            'hedging_instrument_description': 'Y',
            'hedged_currency_id': self.env.company.currency_id.id,
            'pl_account_id': self.account_expense.id,
            'oci_account_id': self.account_equity.id,
            'instrument_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'notes': 'docs',
            'rsquared_threshold': 0.80,
        }
        vals.update(overrides)
        return self.env['eh.fx.hedge'].create(vals)


@tagged('eh_account_fx_revaluation', 'integration', 'post_install', '-at_install')
class TestHedgeMovements(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Posting/reclassifying a hedge movement is manager-gated (SoD);
        # these happy-path tests act as a manager.
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.oci = cls._ensure_account(
            cls.env, '3501', 'CFH Reserve 2', 'equity',
        )
        cls.pl = cls._ensure_account(
            cls.env, '5951', 'Hedge P&L 2', 'expense',
        )
        cls.instrument = cls._ensure_account(
            cls.env, '1701', 'Forward Contract 2', 'asset_current',
        )
        cls.hedged_item = cls._ensure_account(
            cls.env, '1702', 'Hedged Item', 'asset_current',
        )

    def _make_active_hedge(self, hedge_type='cash_flow'):
        vals = {
            'name': '/',
            'hedge_type': hedge_type,
            'hedged_item_description': 'X',
            'hedging_instrument_description': 'Y',
            'hedged_currency_id': self.env.company.currency_id.id,
            'oci_account_id': self.oci.id,
            'pl_account_id': self.pl.id,
            'instrument_account_id': self.instrument.id,
            'journal_id': self.journal_misc.id,
            'notes': 'docs',
        }
        if hedge_type == 'fair_value':
            vals['hedged_item_account_id'] = self.hedged_item.id
        h = self.env['eh.fx.hedge'].create(vals)
        h.action_designate()
        return h

    def _make_effective(self, hedge):
        """Record a passing effectiveness test so the hedge qualifies for
        hedge accounting (flips state to 'effective'). Required before a
        movement can defer its effective portion to OCI or the hedged item."""
        self.env['eh.fx.hedge.test'].create({
            'hedge_id': hedge.id,
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1000.0,
            'cumulative_hedged_change': -1000.0,
        })
        self.assertEqual(hedge.state, 'effective')

    def test_cfh_effective_to_oci_ineffective_to_pl(self):
        h = self._make_active_hedge('cash_flow')
        self._make_effective(h)
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': h.id,
            'total_change': 1000.0,
            'effective_portion': 900.0,
        })
        self.assertAlmostEqual(mvt.ineffective_portion, 100.0, places=2)
        mvt.action_post()
        self.assertEqual(mvt.state, 'posted')
        self.assertTrue(mvt.move_id)
        # Verify the OCI leg was credited 900 and the P&L leg credited 100.
        oci_lines = mvt.move_id.line_ids.filtered(
            lambda line_item: line_item.account_id == self.oci,
        )
        pl_lines = mvt.move_id.line_ids.filtered(
            lambda line_item: line_item.account_id == self.pl,
        )
        self.assertAlmostEqual(sum(oci_lines.mapped('credit')), 900.0, places=2)
        self.assertAlmostEqual(sum(pl_lines.mapped('credit')), 100.0, places=2)

    def test_fvh_effective_to_hedged_item_ineffective_to_pl(self):
        h = self._make_active_hedge('fair_value')
        self._make_effective(h)
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': h.id,
            'total_change': 500.0,
            'effective_portion': 450.0,
        })
        mvt.action_post()
        oci_lines = mvt.move_id.line_ids.filtered(
            lambda line_item: line_item.account_id == self.oci,
        )
        hedged_lines = mvt.move_id.line_ids.filtered(
            lambda line_item: line_item.account_id == self.hedged_item,
        )
        pl_lines = mvt.move_id.line_ids.filtered(
            lambda line_item: line_item.account_id == self.pl,
        )
        # FVH never touches OCI.
        self.assertFalse(oci_lines)
        # Effective portion adjusts the hedged item's carrying amount...
        self.assertAlmostEqual(
            sum(hedged_lines.mapped('credit')), 450.0, places=2)
        # ...and only the ineffective portion reaches P&L.
        self.assertAlmostEqual(sum(pl_lines.mapped('credit')), 50.0, places=2)

    def test_effective_cannot_exceed_total(self):
        h = self._make_active_hedge('cash_flow')
        with self.assertRaises(ValidationError):
            self.env['eh.fx.hedge.movement'].create({
                'hedge_id': h.id,
                'total_change': 100.0,
                'effective_portion': 200.0,
            })

    def test_reclassify_oci_to_pl(self):
        h = self._make_active_hedge('cash_flow')
        self._make_effective(h)
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': h.id,
            'total_change': 1000.0,
            'effective_portion': 900.0,
        })
        mvt.action_post()
        mvt.action_reclassify_to_pl()
        self.assertEqual(mvt.state, 'reclassified')
        self.assertTrue(mvt.reclassification_move_id)
        # Reclass entry: Dr OCI 900, Cr P&L 900.
        recl = mvt.reclassification_move_id
        oci_lines = recl.line_ids.filtered(
            lambda line_item: line_item.account_id == self.oci,
        )
        pl_lines = recl.line_ids.filtered(
            lambda line_item: line_item.account_id == self.pl,
        )
        self.assertAlmostEqual(sum(oci_lines.mapped('debit')), 900.0, places=2)
        self.assertAlmostEqual(sum(pl_lines.mapped('credit')), 900.0, places=2)

    def test_reclassify_blocked_for_fvh(self):
        h = self._make_active_hedge('fair_value')
        self._make_effective(h)
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': h.id,
            'total_change': 500.0,
            'effective_portion': 450.0,
        })
        mvt.action_post()
        with self.assertRaises(UserError):
            mvt.action_reclassify_to_pl()

    def test_deferral_blocked_without_passing_test(self):
        """A non-zero effective portion cannot be deferred to OCI without a
        qualifying effectiveness test; zeroing it lets the full change post
        to P&L (the de-designated treatment)."""
        h = self._make_active_hedge('cash_flow')  # designated, no test yet
        self.assertEqual(h.state, 'designated')
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': h.id,
            'total_change': 1000.0,
            'effective_portion': 900.0,
        })
        with self.assertRaises(UserError):
            mvt.action_post()
        mvt.effective_portion = 0.0
        mvt.action_post()
        self.assertEqual(mvt.state, 'posted')
        # Full 1000 in P&L, nothing deferred to OCI.
        pl_lines = mvt.move_id.line_ids.filtered(
            lambda line_item: line_item.account_id == self.pl,
        )
        self.assertAlmostEqual(sum(pl_lines.mapped('credit')), 1000.0, places=2)

    def test_posted_movement_measurement_inputs_frozen(self):
        """Freeze-after-post: once a movement is posted its measurement
        inputs drive a live journal entry, so a direct write to
        effective_portion (or total_change) must be blocked. Otherwise a
        posted audit figure could be edited while its JE stays posted."""
        h = self._make_active_hedge('cash_flow')
        self._make_effective(h)
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': h.id,
            'total_change': 1000.0,
            'effective_portion': 900.0,
        })
        mvt.action_post()
        self.assertEqual(mvt.state, 'posted')
        # Editing the effective portion on a posted movement is blocked.
        with self.assertRaises(UserError):
            mvt.effective_portion = 400.0
        # total_change is equally frozen.
        with self.assertRaises(UserError):
            mvt.write({'total_change': 1500.0})
        # The frozen figures are unchanged after the blocked writes.
        self.assertAlmostEqual(mvt.effective_portion, 900.0, places=2)
        self.assertAlmostEqual(mvt.total_change, 1000.0, places=2)
        # Bookkeeping-only writes (e.g. notes) are still permitted.
        mvt.write({'notes': 'reviewed'})
        self.assertEqual(mvt.notes, 'reviewed')
        # A reclassified movement is likewise frozen.
        mvt.action_reclassify_to_pl()
        self.assertEqual(mvt.state, 'reclassified')
        with self.assertRaises(UserError):
            mvt.effective_portion = 100.0


@tagged('eh_account_fx_revaluation', 'integration', 'post_install', '-at_install')
class TestHedgeMovementSoD(EhAccountIntegrationTestCase):
    """Segregation-of-duties: only EH accounting managers can post or
    reclassify hedge movements. The CSV grants group_eh_user write on the
    movement, so the gate must live in the action, not only in ACLs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The base acting user must be a manager so it can post the
        # movement in the reclassify/manager control tests.
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.oci = cls._ensure_account(
            cls.env, '3502', 'CFH Reserve SoD', 'equity',
        )
        cls.pl = cls._ensure_account(
            cls.env, '5952', 'Hedge P&L SoD', 'expense',
        )
        cls.instrument = cls._ensure_account(
            cls.env, '1703', 'Forward Contract SoD', 'asset_current',
        )
        # A non-manager operator: EH user role only (no manager rights).
        cls.operator = cls.env['res.users'].create({
            'name': 'FX Operator',
            'login': 'fx_operator_sod',
            'email': 'fx_operator_sod@example.com',
            'groups_id': [
                (6, 0, [cls.env.ref('eh_account_base.group_eh_user').id]),
            ],
        })

    def _make_effective_hedge(self):
        h = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'hedged_item_description': 'X',
            'hedging_instrument_description': 'Y',
            'hedged_currency_id': self.env.company.currency_id.id,
            'oci_account_id': self.oci.id,
            'pl_account_id': self.pl.id,
            'instrument_account_id': self.instrument.id,
            'journal_id': self.journal_misc.id,
            'notes': 'docs',
        })
        h.action_designate()
        self.env['eh.fx.hedge.test'].create({
            'hedge_id': h.id,
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1000.0,
            'cumulative_hedged_change': -1000.0,
        })
        self.assertEqual(h.state, 'effective')
        return h

    def test_non_manager_cannot_post_movement(self):
        h = self._make_effective_hedge()
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': h.id,
            'total_change': 1000.0,
            'effective_portion': 900.0,
        })
        self.assertFalse(
            self.operator.has_group('eh_account_base.group_eh_manager'))
        with self.assertRaises(UserError):
            mvt.with_user(self.operator).action_post()
        self.assertEqual(mvt.state, 'draft')

    def test_non_manager_cannot_reclassify_movement(self):
        h = self._make_effective_hedge()
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': h.id,
            'total_change': 1000.0,
            'effective_portion': 900.0,
        })
        # Manager posts it first.
        mvt.action_post()
        self.assertEqual(mvt.state, 'posted')
        # A non-manager cannot reclassify the OCI portion to P&L.
        with self.assertRaises(UserError):
            mvt.with_user(self.operator).action_reclassify_to_pl()
        self.assertEqual(mvt.state, 'posted')

    def test_manager_can_post_movement(self):
        """Control: a manager (the base admin user) still posts fine."""
        h = self._make_effective_hedge()
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': h.id,
            'total_change': 1000.0,
            'effective_portion': 900.0,
        })
        mvt.action_post()
        self.assertEqual(mvt.state, 'posted')


@tagged('eh_account_fx_revaluation', 'integration', 'post_install', '-at_install')
class TestHedgeMovementIntegrity(EhAccountIntegrationTestCase):
    """Posted-figure integrity controls on eh.fx.hedge.movement: a posted
    movement carries a live GL entry, so its measurement inputs are frozen,
    it cannot be deleted, and a plain user cannot raw-reset its state out of
    posted to lift the freeze. The sanctioned action flow still works."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The base acting user is a manager so it can post movements; the
        # raw-reset control is proven with a separate non-manager operator.
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.oci = cls._ensure_account(
            cls.env, '3503', 'CFH Reserve Integ', 'equity',
        )
        cls.pl = cls._ensure_account(
            cls.env, '5953', 'Hedge P&L Integ', 'expense',
        )
        cls.instrument = cls._ensure_account(
            cls.env, '1704', 'Forward Contract Integ', 'asset_current',
        )
        cls.operator = cls.env['res.users'].create({
            'name': 'FX Operator Integ',
            'login': 'fx_operator_integ',
            'email': 'fx_operator_integ@example.com',
            'groups_id': [
                (6, 0, [cls.env.ref('eh_account_base.group_eh_user').id]),
            ],
        })

    def _posted_movement(self):
        h = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'hedged_item_description': 'X',
            'hedging_instrument_description': 'Y',
            'hedged_currency_id': self.env.company.currency_id.id,
            'oci_account_id': self.oci.id,
            'pl_account_id': self.pl.id,
            'instrument_account_id': self.instrument.id,
            'journal_id': self.journal_misc.id,
            'notes': 'docs',
        })
        h.action_designate()
        self.env['eh.fx.hedge.test'].create({
            'hedge_id': h.id,
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1000.0,
            'cumulative_hedged_change': -1000.0,
        })
        self.assertEqual(h.state, 'effective')
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': h.id,
            'total_change': 1000.0,
            'effective_portion': 900.0,
        })
        mvt.action_post()
        self.assertEqual(mvt.state, 'posted')
        return mvt

    def test_posted_input_frozen_at_write(self):
        mvt = self._posted_movement()
        with self.assertRaises(UserError):
            mvt.write({'effective_portion': 400.0})
        with self.assertRaises(UserError):
            mvt.write({'total_change': 1500.0})
        # Figures unchanged; a bookkeeping-only write still passes.
        self.assertAlmostEqual(mvt.effective_portion, 900.0, places=2)
        self.assertAlmostEqual(mvt.total_change, 1000.0, places=2)
        mvt.write({'notes': 'reviewed'})
        self.assertEqual(mvt.notes, 'reviewed')

    def test_posted_movement_cannot_be_unlinked(self):
        mvt = self._posted_movement()
        with self.assertRaises(UserError):
            mvt.unlink()
        self.assertTrue(mvt.exists())

    def test_plain_user_cannot_raw_reset_state(self):
        """A non-manager cannot raw-write state out of posted to draft; the
        write-layer state gate blocks it even though the CSV grants the user
        write on the movement. This stops lifting the figure freeze."""
        mvt = self._posted_movement()
        with self.assertRaises(UserError):
            mvt.with_user(self.operator).write({'state': 'draft'})
        self.assertEqual(mvt.state, 'posted')
        # The sanctioned flag is not available to a plain user by writing it
        # directly: even the manager control path uses with_context internally.
        with self.assertRaises(UserError):
            mvt.with_user(self.operator).write({'state': 'reclassified'})
        self.assertEqual(mvt.state, 'posted')

    def test_sanctioned_post_and_reclassify_flow_still_works(self):
        """The normal manager action flow (post then reclassify OCI to P&L)
        is unaffected by the integrity guards."""
        mvt = self._posted_movement()
        self.assertTrue(mvt.move_id)
        mvt.action_reclassify_to_pl()
        self.assertEqual(mvt.state, 'reclassified')
        self.assertTrue(mvt.reclassification_move_id)
