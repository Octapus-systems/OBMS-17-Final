# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Producer/consumer seam: eh.deferred.tax.run.action_gather_from_engines pulls
temporary differences from any model implementing eh_deferred_tax_temp_diffs
(IFRS 9 ECL, IAS 37 provisions, ...), so the category hooks are driven by a
real producer instead of being hand-keyed. Exercised here with a stub
provider so the test needs no cross-module install; the real ECL and
provision producers are validated when the full suite installs them together.
"""

from unittest.mock import patch

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


def _stub_provider(records, reporting_date):
    return [{
        'name': 'Stub warranty provision',
        'category': 'provision',
        'nature': 'liability',
        'carrying_amount': 10000.0,
        'tax_base': 0.0,
        'through_oci': False,
    }]


@tagged('eh_account_deferred_tax', 'post_install', '-at_install')
class TestDeferredTaxGatherSeam(EhGoldenTestCase):

    def _run(self):
        return self.env['eh.deferred.tax.run'].create({
            'statutory_rate': 25.0,
            'period_end': '2026-12-31',
            'company_id': self.company.id,
        })

    def _gather_with_stub(self, run):
        Company = type(self.env['res.company'])
        with patch.object(type(run), '_eh_deferred_tax_providers',
                          return_value=['res.company']), \
                patch.object(Company, 'eh_deferred_tax_temp_diffs',
                             _stub_provider, create=True):
            run.action_gather_from_engines()

    def test_gather_pulls_producer_diffs_as_dta(self):
        """A deductible temporary difference from a producer becomes a
        gathered line with the right category and a DTA (deductible_diff =
        carrying for a liability with a nil tax base)."""
        run = self._run()
        self._gather_with_stub(run)
        gathered = run.line_ids.filtered('eh_auto_gathered')
        self.assertEqual(len(gathered), 1)
        self.assertEqual(gathered.category, 'provision')
        self.assertEqual(gathered.nature, 'liability')
        # A liability carried at 10,000 with a nil tax base is a DEDUCTIBLE
        # temporary difference (-> DTA). The rate is resolved later on compute;
        # the DTA = rate x diff arithmetic is covered by the golden tests.
        self.assertAlmostEqual(gathered.deductible_diff, 10000.0, places=2)
        self.assertAlmostEqual(gathered.taxable_diff, 0.0, places=2)

    def test_gather_is_idempotent_and_spares_manual_lines(self):
        """Re-gathering replaces the auto lines (no duplication) and never
        touches hand-keyed lines."""
        run = self._run()
        manual = self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Hand-keyed depreciation',
            'category': 'depreciation', 'nature': 'asset',
            'carrying_amount': 5000.0, 'tax_base': 3000.0,
        })
        self._gather_with_stub(run)
        self._gather_with_stub(run)  # second gather must not duplicate
        self.assertEqual(
            len(run.line_ids.filtered('eh_auto_gathered')), 1)
        self.assertIn(manual, run.line_ids)
        self.assertFalse(manual.eh_auto_gathered)

    def test_gather_no_providers_is_clean(self):
        """With no producers installed the gather is a clean no-op."""
        run = self._run()
        with patch.object(type(run), '_eh_deferred_tax_providers',
                          return_value=[]):
            run.action_gather_from_engines()
        self.assertFalse(run.line_ids.filtered('eh_auto_gathered'))
