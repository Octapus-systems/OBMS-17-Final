# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden presentation tests: structural OCI recycling (IAS 1.82A), the
IAS 1.60 current / non-current completeness guard, NCI prefill from a
covering consolidation run, and the IAS 34 thin interim support.

All expected amounts are hand-derived from the inputs stated in each test;
derivations are in the comments. Convention: worksheet figures are
credit-positive (a normal equity / OCI gain is a positive number), while the
consolidation run lines carry the ledger sign convention (equity
credit-negative), so the run's NCI carve-out of a positive minority interest
is a negative line amount.
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_statements.models import presentation


@tagged('eh_golden', 'eh_account_statements', 'post_install', '-at_install')
class TestGoldenPresentation(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # action_confirm is manager-gated; the acting user must be an EH
        # Accounting Manager for the confirm paths below.
        cls.group_manager = cls.env.ref('eh_account_base.group_eh_manager')
        cls.env.user.groups_id |= cls.group_manager
        cls.tag_recyclable = cls.env.ref(
            'eh_account_statements.tag_eh_oci_recyclable')
        cls.tag_non_recyclable = cls.env.ref(
            'eh_account_statements.tag_eh_oci_non_recyclable')

    # ---- structural OCI recycling (IAS 1.82A) ----

    def test_recycling_tags_drive_soci_sections(self):
        # GOLDEN: profit 100000. CTA reserve account tagged RECYCLABLE
        # carries the +8000 translation line; FVOCI-equity reserve account
        # tagged NON-RECYCLABLE carries the +5000 line. Neither line hands
        # in a manual flag, so the tag governs (IAS 1.82A):
        #   oci_will_reclassify = 8000
        #   oci_no_reclassify   = 5000
        #   total_oci           = 8000 + 5000  = 13000
        #   TCI                 = 100000 + 13000 = 113000
        acc_cta = self._ensure_account(
            self.env, '3910', 'FX Translation Reserve', 'equity')
        acc_fvoci_eq = self._ensure_account(
            self.env, '3920', 'FVOCI Equity Reserve', 'equity')
        acc_cta.tag_ids = [(4, self.tag_recyclable.id)]
        acc_fvoci_eq.tag_ids = [(4, self.tag_non_recyclable.id)]
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'line_ids': [
                (0, 0, {'name': 'CTA movement', 'oci_type': 'translation',
                        'account_id': acc_cta.id, 'amount': 8000.0}),
                (0, 0, {'name': 'FVOCI equity gain', 'oci_type': 'fvoci',
                        'account_id': acc_fvoci_eq.id, 'amount': 5000.0}),
            ],
        })
        cta_line = soci.line_ids.filtered(
            lambda line_item: line_item.account_id == acc_cta)
        eq_line = soci.line_ids.filtered(
            lambda line_item: line_item.account_id == acc_fvoci_eq)
        self.assertTrue(
            cta_line.will_reclassify,
            "CTA line must land in the recyclable section: its source "
            "account carries the EH OCI Recyclable tag (IAS 21.48).")
        self.assertFalse(
            eq_line.will_reclassify,
            "FVOCI-equity line must land in the non-recyclable section: "
            "its source account carries the EH OCI Non-Recyclable tag "
            "(IFRS 9.B5.7.1).")
        self.assertEqual(cta_line.tag_reclassify, 'recyclable')
        self.assertEqual(eq_line.tag_reclassify, 'non_recyclable')
        self.assertFalse(cta_line.reclassify_discrepancy)
        self.assertFalse(eq_line.reclassify_discrepancy)
        self.assertAlmostEqual(soci.oci_will_reclassify, 8000.0, places=2)
        self.assertAlmostEqual(soci.oci_no_reclassify, 5000.0, places=2)
        self.assertAlmostEqual(soci.total_oci, 13000.0, places=2)
        self.assertAlmostEqual(
            soci.total_comprehensive_income, 113000.0, places=2)

    def test_recycling_cta_recyclable_revaluation_non_recyclable(self):
        # DECISIVE GOLDEN (finding a): the reclassification section of each
        # OCI component is driven PURELY by the recycling tag on its source
        # account, with NO manual will_reclassify handed in on either line.
        #   - CTA reserve account carries EH OCI Recyclable (IAS 21.48): its
        #     +12000 translation line lands in the recyclable section.
        #   - Revaluation surplus account carries EH OCI Non-Recyclable
        #     (IAS 16.41): its +7000 line lands in the non-recyclable section.
        # Derivation:
        #   oci_will_reclassify = 12000 (CTA only)
        #   oci_no_reclassify   =  7000 (revaluation surplus only)
        #   total_oci           = 12000 + 7000 = 19000
        #   TCI                 = 90000 + 19000 = 109000
        acc_cta = self._ensure_account(
            self.env, '3911', 'Foreign Translation Reserve', 'equity')
        acc_reval = self._ensure_account(
            self.env, '3931', 'Asset Revaluation Surplus', 'equity')
        acc_cta.tag_ids = [(4, self.tag_recyclable.id)]
        acc_reval.tag_ids = [(4, self.tag_non_recyclable.id)]
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 90000.0,
            'line_ids': [
                # NOTE: neither line sets will_reclassify; the tag governs.
                (0, 0, {'name': 'CTA movement', 'oci_type': 'translation',
                        'account_id': acc_cta.id, 'amount': 12000.0}),
                (0, 0, {'name': 'Revaluation uplift',
                        'oci_type': 'revaluation',
                        'account_id': acc_reval.id, 'amount': 7000.0}),
            ],
        })
        cta_line = soci.line_ids.filtered(lambda line_item: line_item.account_id == acc_cta)
        reval_line = soci.line_ids.filtered(
            lambda line_item: line_item.account_id == acc_reval)
        # Section placement is tag-derived, not from any manual input.
        self.assertEqual(cta_line.tag_reclassify, 'recyclable')
        self.assertEqual(reval_line.tag_reclassify, 'non_recyclable')
        self.assertTrue(
            cta_line.will_reclassify,
            "CTA (recyclable tag) must derive into the recyclable section "
            "with no manual flag (IAS 21.48).")
        self.assertFalse(
            reval_line.will_reclassify,
            "Revaluation surplus (non-recyclable tag) must derive into the "
            "non-recyclable section with no manual flag (IAS 16.41).")
        # No discrepancy: the derived flag agrees with each tag.
        self.assertFalse(cta_line.reclassify_discrepancy)
        self.assertFalse(reval_line.reclassify_discrepancy)
        self.assertEqual(soci.recycling_discrepancy_count, 0)
        # No untagged component: the IAS 1.82A completeness gate is clear.
        self.assertEqual(soci.oci_untagged_count, 0)
        self.assertFalse(soci.oci_recycling_misfit_note)
        # Subtotals split by tag, hand-derived above.
        self.assertAlmostEqual(soci.oci_will_reclassify, 12000.0, places=2)
        self.assertAlmostEqual(soci.oci_no_reclassify, 7000.0, places=2)
        self.assertAlmostEqual(soci.total_oci, 19000.0, places=2)
        self.assertAlmostEqual(
            soci.total_comprehensive_income, 109000.0, places=2)
        # A fully tag-driven statement confirms with no override needed.
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    def test_recycling_tag_contradiction_sets_discrepancy_field(self):
        # DECISIVE GOLDEN (finding c): a manual will_reclassify that
        # CONTRADICTS the tag sets the discrepancy field on the line and the
        # header count, with no other input. The CTA account is tagged
        # RECYCLABLE, so the tag verdict is recyclable (True); the preparer
        # forces will_reclassify=False, contradicting it.
        acc_cta = self._ensure_account(
            self.env, '3912', 'CTA Reserve Contradiction', 'equity')
        acc_cta.tag_ids = [(4, self.tag_recyclable.id)]
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 60000.0,
            'line_ids': [
                # Manual flag False contradicts the recyclable tag.
                (0, 0, {'name': 'CTA movement', 'oci_type': 'translation',
                        'account_id': acc_cta.id, 'amount': 9000.0,
                        'will_reclassify': False}),
            ],
        })
        line = soci.line_ids
        self.assertEqual(line.tag_reclassify, 'recyclable')
        self.assertFalse(
            line.will_reclassify,
            "The manual False flag must be kept (override wins the value).")
        self.assertTrue(
            line.reclassify_discrepancy,
            "A manual flag contradicting the tag must set the discrepancy "
            "field (IAS 1.82A), never silently win.")
        self.assertEqual(soci.recycling_discrepancy_count, 1)
        # The tagged line is NOT an IAS 1.82A completeness misfit (it has a
        # tag); the discrepancy is a separate, softer signal (chatter on
        # confirm, not a block).
        self.assertEqual(soci.oci_untagged_count, 0)
        # Overridden into the non-recyclable bucket: 9000 no-reclass, 0 recl.
        self.assertAlmostEqual(soci.oci_no_reclassify, 9000.0, places=2)
        self.assertAlmostEqual(soci.oci_will_reclassify, 0.0, places=2)

    def test_untagged_oci_component_blocks_confirm_until_override(self):
        # DECISIVE GOLDEN (finding b): an OCI component whose source account
        # carries NO recycling tag has no tag-derived reclassification
        # section, so its placement rests on a manual flag (honour system).
        # It must surface in the IAS 1.82A completeness note and BLOCK confirm
        # until a manager overrides with a reason.
        acc_untagged = self._ensure_account(
            self.env, '3960', 'Untagged OCI Reserve', 'equity')
        # Deliberately NOT tagged with either recycling tag.
        self.assertFalse(acc_untagged.tag_ids & (
            self.tag_recyclable + self.tag_non_recyclable))
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 70000.0,
            'line_ids': [
                (0, 0, {'name': 'Untagged reserve movement',
                        'oci_type': 'other',
                        'account_id': acc_untagged.id, 'amount': 6000.0,
                        'will_reclassify': True}),
            ],
        })
        line = soci.line_ids
        # The line is a recycling misfit: no verdict, surfaced in the note.
        self.assertIsNone(line._eh_tag_verdict())
        self.assertEqual(line.tag_reclassify, 'none')
        self.assertEqual(soci.oci_untagged_count, 1)
        self.assertTrue(soci.oci_recycling_misfit_note)
        self.assertIn('3960', soci.oci_recycling_misfit_note)
        # Blocked without the override.
        with self.assertRaises(UserError):
            soci.action_confirm()
        self.assertEqual(soci.state, 'draft')
        # Override flag alone (no reason) is still blocked.
        soci.oci_tag_override = True
        with self.assertRaises(UserError):
            soci.action_confirm()
        self.assertEqual(soci.state, 'draft')
        # Override WITH a reason confirms, and the override is logged.
        soci.oci_tag_override_reason = (
            'Bespoke reserve; recycling classified manually per note 6.')
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')
        self.assertTrue(
            any('IAS 1.82A' in (m.body or '') and 'OVERRIDDEN' in (m.body or '')
                for m in soci.message_ids),
            "The IAS 1.82A OCI recycling override must be logged to the "
            "chatter.")

    def test_untagged_oci_completeness_ignores_zero_amount_lines(self):
        # A zero-amount OCI line cannot mis-state either subtotal, so it is
        # not a completeness misfit even with an untagged / absent source
        # account: the gate stays silent and the statement confirms.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 50000.0,
            'line_ids': [
                (0, 0, {'name': 'Nil placeholder', 'oci_type': 'other',
                        'amount': 0.0}),
            ],
        })
        self.assertEqual(soci.oci_untagged_count, 0)
        self.assertFalse(soci.oci_recycling_misfit_note)
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    def test_recycling_manual_override_flags_discrepancy(self):
        # A preparer may override the structural classification, but never
        # silently: the discrepancy is flagged on the line, counted on the
        # header, and recorded in the chatter on confirm.
        acc_cta = self._ensure_account(
            self.env, '3910', 'FX Translation Reserve', 'equity')
        acc_cta.tag_ids = [(4, self.tag_recyclable.id)]
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 50000.0,
            'line_ids': [
                (0, 0, {'name': 'CTA movement', 'oci_type': 'translation',
                        'account_id': acc_cta.id, 'amount': 8000.0}),
            ],
        })
        line = soci.line_ids
        self.assertTrue(line.will_reclassify)
        # Manual override against the tag.
        line.will_reclassify = False
        self.assertTrue(
            line.reclassify_discrepancy,
            "Overriding the flag against the account tag must raise the "
            "discrepancy flag.")
        self.assertEqual(soci.recycling_discrepancy_count, 1)
        # The overridden line now sums in the non-recyclable bucket:
        # oci_no_reclassify = 8000, oci_will_reclassify = 0.
        self.assertAlmostEqual(soci.oci_no_reclassify, 8000.0, places=2)
        self.assertAlmostEqual(soci.oci_will_reclassify, 0.0, places=2)
        # Confirm succeeds (warning, not a block) and records the override.
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')
        self.assertTrue(
            any('IAS 1.82A' in (m.body or '') for m in soci.message_ids),
            "Confirming with a recycling override must record the "
            "overridden lines in the chatter.")

    def test_default_tag_applier_sources_and_no_overwrite(self):
        # Heuristic source: an equity account named for the revaluation
        # surplus is tagged NON-RECYCLABLE (IAS 16.41) by the applier, which
        # runs automatically on statement generation.
        acc_surplus = self._ensure_account(
            self.env, '3930', 'Revaluation Surplus', 'equity')
        # Never-overwrite rule: an account already classified (here, hand
        # tagged recyclable against the default) must be left alone.
        acc_pretagged = self._ensure_account(
            self.env, '3940', 'Revaluation Surplus Special', 'equity')
        acc_pretagged.tag_ids = [(4, self.tag_recyclable.id)]
        self.assertFalse(acc_surplus.tag_ids & (
            self.tag_recyclable + self.tag_non_recyclable))
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        self.assertIn(
            self.tag_non_recyclable, acc_surplus.tag_ids,
            "SOCI generation must auto-apply the non-recyclable default "
            "tag to the revaluation surplus reserve (IAS 16.41).")
        self.assertIn(self.tag_recyclable, acc_pretagged.tag_ids)
        self.assertNotIn(
            self.tag_non_recyclable, acc_pretagged.tag_ids,
            "The applier must never re-tag an account that already "
            "carries a recycling tag (manual classification wins).")
        # Config-field source (soft): when the consolidation module is
        # installed, its entity-level CTA account is tagged RECYCLABLE
        # (IAS 21.48) by the runnable action.
        if 'eh.consol.entity' in self.env.registry:
            acc_group_cta = self._ensure_account(
                self.env, '3950', 'Group Translation Adjustment', 'equity')
            self.env['eh.consol.entity'].create({
                'name': 'Tag Source Group', 'code': 'TSG1',
                'cta_account_id': acc_group_cta.id,
            })
            soci.action_apply_oci_recycling_tags()
            self.assertIn(
                self.tag_recyclable, acc_group_cta.tag_ids,
                "The applier must tag the consolidation entity's CTA "
                "reserve recyclable (IAS 21.48).")

    # ---- IAS 1.60 completeness guard ----

    def _post_off_balance_pair(self, date='2026-03-15'):
        """Post a balanced move carried entirely on off-balance accounts,
        the account type outside every recognised IAS 1.60 set. Both
        accounts end with a non-zero balance (500 / -500)."""
        off_one = self._ensure_account(
            self.env, '9901', 'Off Balance Memo One', 'off_balance')
        off_two = self._ensure_account(
            self.env, '9902', 'Off Balance Memo Two', 'off_balance')
        self.post_balanced_move(
            [
                {'account': off_one, 'debit': 500.0},
                {'account': off_two, 'credit': 500.0},
            ],
            date=date,
        )
        return off_one, off_two

    def test_soci_completeness_guard_blocks_until_override(self):
        off_one, _off_two = self._post_off_balance_pair()
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        self.assertTrue(
            soci.classification_misfit_note,
            "Posted balances on unclassifiable accounts must surface in "
            "the misfit note (IAS 1.60).")
        self.assertIn('9901', soci.classification_misfit_note)
        # Blocked without the override.
        with self.assertRaises(UserError):
            soci.action_confirm()
        self.assertEqual(soci.state, 'draft')
        # Override without a reason is still blocked.
        soci.classification_override = True
        with self.assertRaises(UserError):
            soci.action_confirm()
        self.assertEqual(soci.state, 'draft')
        # Override with a reason confirms, and the override is logged.
        soci.classification_override_reason = (
            'Memo accounts only; excluded from the classified statement '
            'of financial position by policy note 4.')
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')
        self.assertTrue(
            any('OVERRIDDEN' in (m.body or '') for m in soci.message_ids),
            "The IAS 1.60 override must be logged to the chatter.")

    def test_soce_completeness_guard_blocks_until_override(self):
        self._post_off_balance_pair()
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        self.assertIn('9902', soce.classification_misfit_note or '')
        with self.assertRaises(UserError):
            soce.action_confirm()
        self.assertEqual(soce.state, 'draft')
        soce.write({
            'classification_override': True,
            'classification_override_reason': 'Memo accounts only.',
        })
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')

    def test_completeness_guard_ignores_recognised_types(self):
        # Balances on recognised types only (cash / revenue / expense):
        # no misfits, and the guard stays silent - prior behaviour intact.
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
            'profit_for_period': 25000.0,
        })
        self.assertFalse(soci.classification_misfit_note)
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    # ---- NCI prefill from a covering consolidation run ----

    def _seed_consol_run(self, nci_ledger_amount=-25000.0):
        """Seed a settled consolidation run for 2026 whose NCI carve-out is
        one line of ``nci_ledger_amount`` in the ledger sign convention
        (credit-negative: -25000 is a positive 25000 minority interest)."""
        entity = self.env['eh.consol.entity'].create({
            'name': 'Statements Group', 'code': 'STGRP',
        })
        run = self.env['eh.consol.run'].create({
            'entity_id': entity.id,
            'period_from': '2026-01-01', 'period_to': '2026-12-31',
        })
        self.env['eh.consol.run.line'].create({
            'run_id': run.id, 'kind': 'nci',
            'amount': nci_ledger_amount,
        })
        # Settle the run so the statements accept it as a covering source.
        run.write({'state': 'computed'})
        return run

    def test_soci_nci_prefill_and_discrepancy(self):
        if 'eh.consol.run' not in self.env.registry:
            self.skipTest('eh_account_consolidation not installed')
        run = self._seed_consol_run(-25000.0)
        # The run's header carve (ledger convention) is the line sum.
        self.assertAlmostEqual(run.nci_amount, -25000.0, places=2)
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
        })
        soci.action_prefill_nci_from_consolidation()
        # Carve = -(-25000) = 25000 credit-positive; the blank NCI
        # attribution is prefilled from it and ties.
        self.assertTrue(soci.consol_nci_available)
        self.assertEqual(soci.consol_run_name, run.name)
        self.assertAlmostEqual(soci.consol_nci_amount, 25000.0, places=2)
        self.assertAlmostEqual(
            soci.attributable_to_nci, 25000.0, places=2,
            msg="A blank NCI attribution must prefill from the run carve.")
        self.assertTrue(soci.nci_consol_tied)
        self.assertAlmostEqual(soci.nci_consol_discrepancy, 0.0, places=2)
        # Manual divergence: 20000 - 25000 = -5000 discrepancy, untied.
        soci.attributable_to_nci = 20000.0
        self.assertAlmostEqual(
            soci.nci_consol_discrepancy, -5000.0, places=2)
        self.assertFalse(soci.nci_consol_tied)

    def test_soci_nci_prefill_keeps_manual_figure(self):
        if 'eh.consol.run' not in self.env.registry:
            self.skipTest('eh_account_consolidation not installed')
        self._seed_consol_run(-25000.0)
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'attributable_to_nci': 20000.0,
        })
        soci.action_prefill_nci_from_consolidation()
        # The manually keyed 20000 is KEPT; the run figure only surfaces
        # as the reference plus a -5000 discrepancy (20000 - 25000).
        self.assertAlmostEqual(
            soci.attributable_to_nci, 20000.0, places=2,
            msg="Prefill must never overwrite a manually keyed NCI figure.")
        self.assertAlmostEqual(soci.consol_nci_amount, 25000.0, places=2)
        self.assertAlmostEqual(
            soci.nci_consol_discrepancy, -5000.0, places=2)
        self.assertFalse(soci.nci_consol_tied)

    def test_soce_nci_prefill_creates_component_and_discrepancy(self):
        if 'eh.consol.run' not in self.env.registry:
            self.skipTest('eh_account_consolidation not installed')
        self._seed_consol_run(-25000.0)
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        soce.action_prefill_nci_from_consolidation()
        nci_line = soce.line_ids.filtered(lambda line_item: line_item.component == 'nci')
        self.assertEqual(
            len(nci_line), 1,
            "Prefill must create the missing NCI component line.")
        # Opening 25000, no movements: closing = 25000, ties to the carve.
        self.assertAlmostEqual(nci_line.opening_balance, 25000.0, places=2)
        self.assertAlmostEqual(nci_line.closing_balance, 25000.0, places=2)
        self.assertAlmostEqual(
            soce.nci_component_closing, 25000.0, places=2)
        self.assertTrue(soce.nci_consol_tied)
        # A manual movement diverges the component from the run:
        # closing = 25000 + 1000 = 26000, discrepancy = 26000 - 25000.
        nci_line.profit = 1000.0
        self.assertAlmostEqual(
            soce.nci_consol_discrepancy, 1000.0, places=2)
        self.assertFalse(soce.nci_consol_tied)

    # ---- IAS 34 thin interim support ----

    def test_interim_labels_and_comparatives(self):
        Soci = self.env['eh.soci']
        annual_2025 = Soci.create({
            'period_start': '2025-01-01', 'period_end': '2025-12-31',
            'profit_for_period': 80000.0,
        })
        # Default preserves prior behaviour: annual, plain heading.
        self.assertEqual(annual_2025.period_type, 'annual')
        self.assertEqual(
            annual_2025.presentation_label,
            'Statement of comprehensive income')
        interim_2025 = Soci.create({
            'period_start': '2025-01-01', 'period_end': '2025-06-30',
            'period_type': 'interim',
            'profit_for_period': 30000.0,
        })
        self.assertEqual(
            interim_2025.presentation_label,
            'Interim statement of comprehensive income (IAS 34)')
        # Current condensed H1-2026 interim: IAS 34.20 comparatives are the
        # comparable prior-year interim plus the preceding annual period.
        interim_2026 = Soci.create({
            'period_start': '2026-01-01', 'period_end': '2026-06-30',
            'period_type': 'interim',
            'condensed': True,
            'comparative_interim_id': interim_2025.id,
            'comparative_annual_id': annual_2025.id,
            'profit_for_period': 40000.0,
        })
        self.assertEqual(
            interim_2026.presentation_label,
            'Condensed interim statement of comprehensive income '
            '(IAS 34.8)')
        self.assertEqual(
            interim_2026.comparative_interim_id, interim_2025)
        self.assertEqual(
            interim_2026.comparative_annual_id, annual_2025)
        # SOCE mirrors the labelling.
        soce_interim = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-06-30',
            'period_type': 'interim', 'condensed': True,
        })
        self.assertEqual(
            soce_interim.presentation_label,
            'Condensed interim statement of changes in equity (IAS 34.8)')

    def test_interim_field_validation(self):
        Soci = self.env['eh.soci']
        annual_2025 = Soci.create({
            'period_start': '2025-01-01', 'period_end': '2025-12-31',
        })
        interim_2025 = Soci.create({
            'period_start': '2025-01-01', 'period_end': '2025-06-30',
            'period_type': 'interim',
        })
        # Condensed / comparatives are interim-only (IAS 34.8 / 34.20).
        with self.assertRaises(ValidationError):
            Soci.create({
                'period_start': '2026-01-01', 'period_end': '2026-12-31',
                'condensed': True,
            })
        # The prior-annual comparative must be an annual statement.
        with self.assertRaises(ValidationError):
            Soci.create({
                'period_start': '2026-01-01', 'period_end': '2026-06-30',
                'period_type': 'interim',
                'comparative_annual_id': interim_2025.id,
            })
        # A comparative must end before the statement period starts.
        with self.assertRaises(ValidationError):
            Soci.create({
                'period_start': '2025-04-01', 'period_end': '2025-09-30',
                'period_type': 'interim',
                'comparative_interim_id': interim_2025.id,
            })
        # And the well-formed pair passes.
        ok = Soci.create({
            'period_start': '2026-01-01', 'period_end': '2026-06-30',
            'period_type': 'interim',
            'comparative_interim_id': interim_2025.id,
            'comparative_annual_id': annual_2025.id,
        })
        self.assertEqual(ok.period_type, 'interim')

    def test_recognised_type_sets_are_disjoint_and_complete(self):
        # The IAS 1.60 sets must stay disjoint (an account type can only
        # classify one way) and must cover every non-off-balance type the
        # framework ships, so the guard never misfires on a stock chart.
        sets = [
            presentation.CURRENT_ACCOUNT_TYPES,
            presentation.NON_CURRENT_ACCOUNT_TYPES,
            presentation.EQUITY_ACCOUNT_TYPES,
            presentation.PL_ACCOUNT_TYPES,
        ]
        union = set()
        total = 0
        for s in sets:
            union |= s
            total += len(s)
        self.assertEqual(
            len(union), total,
            "The IAS 1.60 classification sets must be disjoint.")
        selection = dict(
            self.env['account.account']._fields['account_type'].selection)
        framework_types = set(selection) - {'off_balance'}
        self.assertFalse(
            framework_types - union,
            "Every framework account type except off_balance must belong "
            "to a recognised IAS 1.60 set: missing %s" % sorted(
                framework_types - union))
