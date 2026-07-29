# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for eh.account.report.execution.

Covers:

* Lifecycle: start_execution -> complete_execution / fail_execution.
* Options canonicalisation: deterministic across input orderings.
* Hash stability: same canonical form yields same hash.
* find_cached: matches by (report_code, options_hash, version) and misses
  when any component drifts.
* Move version snapshotting: the audit row records version at start.
"""

import json

from odoo.tests import tagged

from odoo.addons.eh_account_base.models.report_execution import (
    EhAccountReportExecution,
)
from .common import EhAccountUnitTestCase


@tagged('eh_account_base', 'unit')
class TestOptionsCanonicalisation(EhAccountUnitTestCase):

    def test_dict_keys_sorted(self):
        a = EhAccountReportExecution._canonicalise_options({'b': 1, 'a': 2})
        b = EhAccountReportExecution._canonicalise_options({'a': 2, 'b': 1})
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))

    def test_nested_dict_canonicalised(self):
        a = EhAccountReportExecution._canonicalise_options({
            'filters': {'b': 1, 'a': 2},
            'meta': {'y': 'z', 'x': 'w'},
        })
        b = EhAccountReportExecution._canonicalise_options({
            'meta': {'x': 'w', 'y': 'z'},
            'filters': {'a': 2, 'b': 1},
        })
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))

    def test_list_order_preserved(self):
        # Lists may carry meaningful order; do not sort them.
        result = EhAccountReportExecution._canonicalise_options([3, 1, 2])
        self.assertEqual(result, [3, 1, 2])

    def test_set_is_sorted(self):
        result = EhAccountReportExecution._canonicalise_options({3, 1, 2})
        self.assertEqual(result, [1, 2, 3])

    def test_scalar_passthrough(self):
        for value in (1, 1.5, 'hello', True, False, None):
            self.assertEqual(
                EhAccountReportExecution._canonicalise_options(value), value,
            )

    def test_hash_is_stable_across_dict_orderings(self):
        h1 = EhAccountReportExecution._hash_string(
            json.dumps(
                EhAccountReportExecution._canonicalise_options({'b': 1, 'a': 2}),
                sort_keys=True,
            )
        )
        h2 = EhAccountReportExecution._hash_string(
            json.dumps(
                EhAccountReportExecution._canonicalise_options({'a': 2, 'b': 1}),
                sort_keys=True,
            )
        )
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_hash_differs_on_value_change(self):
        h1 = EhAccountReportExecution._hash_string(json.dumps({'a': 1}, sort_keys=True))
        h2 = EhAccountReportExecution._hash_string(json.dumps({'a': 2}, sort_keys=True))
        self.assertNotEqual(h1, h2)


@tagged('eh_account_base', 'integration')
class TestReportExecutionLifecycle(EhAccountUnitTestCase):

    def setUp(self):
        super().setUp()
        self.Execution = self.env['eh.account.report.execution']
        self.company = self.env.company

    def test_start_execution_creates_running_record(self):
        execution = self.Execution.start_execution(
            report_code='profit_loss',
            name='Profit and Loss',
            options={'date': {'from': '2026-01-01', 'to': '2026-12-31'}},
            company_ids=[self.company.id],
            result_format='json',
        )
        self.assertEqual(execution.state, 'running')
        self.assertEqual(execution.report_code, 'profit_loss')
        self.assertEqual(execution.executed_by, self.env.user)
        self.assertEqual(len(execution.options_hash), 64)
        self.assertIn('2026-01-01', execution.options_snapshot)

    def test_start_execution_requires_companies(self):
        with self.assertRaises(ValueError):
            self.Execution.start_execution(
                report_code='profit_loss',
                name='PL',
                options={},
                company_ids=[],
            )

    def test_start_execution_records_move_version_snapshot(self):
        # Bump the company version directly.
        self.env['res.company']._eh_bump_move_version([self.company.id])
        version_before = self.company.eh_move_version
        execution = self.Execution.start_execution(
            report_code='profit_loss',
            name='PL',
            options={},
            company_ids=[self.company.id],
        )
        self.assertEqual(execution.move_version_at_start, version_before)

    def test_complete_execution_marks_done(self):
        execution = self.Execution.start_execution(
            report_code='profit_loss',
            name='PL',
            options={},
            company_ids=[self.company.id],
        )
        execution.complete_execution(row_count=42, result_hash='abc' * 21 + 'd')
        self.assertEqual(execution.state, 'done')
        self.assertEqual(execution.row_count, 42)
        self.assertGreaterEqual(execution.duration_ms, 0)

    def test_fail_execution_marks_error(self):
        execution = self.Execution.start_execution(
            report_code='profit_loss',
            name='PL',
            options={},
            company_ids=[self.company.id],
        )
        execution.fail_execution("boom: SQL syntax error at line 42")
        self.assertEqual(execution.state, 'error')
        self.assertIn('SQL syntax error', execution.error_message)

    def test_fail_execution_truncates_long_error_message(self):
        execution = self.Execution.start_execution(
            report_code='pl',
            name='pl',
            options={},
            company_ids=[self.company.id],
        )
        long_msg = 'x' * 20000
        execution.fail_execution(long_msg)
        self.assertEqual(len(execution.error_message), 8000)

    def test_find_cached_matches_recent_done_execution(self):
        options = {'date': '2026-04-30'}
        execution = self.Execution.start_execution(
            report_code='trial_balance',
            name='TB',
            options=options,
            company_ids=[self.company.id],
        )
        execution.complete_execution(row_count=10)

        found = self.Execution.find_cached(
            report_code='trial_balance',
            options_hash=execution.options_hash,
            company_ids=[self.company.id],
        )
        self.assertEqual(found, execution)

    def test_find_cached_misses_when_version_changed(self):
        options = {'date': '2026-04-30'}
        execution = self.Execution.start_execution(
            report_code='trial_balance',
            name='TB',
            options=options,
            company_ids=[self.company.id],
        )
        execution.complete_execution(row_count=10)

        # Bump the version so the cached version no longer matches.
        self.env['res.company']._eh_bump_move_version([self.company.id])

        found = self.Execution.find_cached(
            report_code='trial_balance',
            options_hash=execution.options_hash,
            company_ids=[self.company.id],
        )
        self.assertFalse(found)

    def test_find_cached_misses_when_options_differ(self):
        execution = self.Execution.start_execution(
            report_code='trial_balance',
            name='TB',
            options={'date': '2026-04-30'},
            company_ids=[self.company.id],
        )
        execution.complete_execution(row_count=10)

        found = self.Execution.find_cached(
            report_code='trial_balance',
            options_hash='deadbeef' * 8,
            company_ids=[self.company.id],
        )
        self.assertFalse(found)

    def test_find_cached_skips_running_executions(self):
        execution = self.Execution.start_execution(
            report_code='trial_balance',
            name='TB',
            options={'date': '2026-04-30'},
            company_ids=[self.company.id],
        )
        # Do NOT call complete_execution: state stays 'running'.
        found = self.Execution.find_cached(
            report_code='trial_balance',
            options_hash=execution.options_hash,
            company_ids=[self.company.id],
        )
        self.assertFalse(found)

    def test_find_cached_strict_company_scope(self):
        """Cache lookup must require an exact match on company_ids.

        Regression: the previous implementation used ('company_ids',
        'in', X) which matches overlapping sets, so a render against
        [c1, c2] could pick up a cached row computed for [c1] alone.
        With company_ids_key the lookup is strict equality on the
        sorted-comma representation.
        """
        with self.env.cr.savepoint():
            try:
                company2 = self.env['res.company'].with_context(
                    default_group_rfq='default',
                ).create({
                    'name': 'Cache Scope Test Co 2',
                })
            except Exception as exc:
                # Stock + Enterprise interaction occasionally strips
                # not-null defaults on per-company auto-records
                # (warehouse picking types). When that happens the
                # test environment cannot produce a second company.
                self.skipTest(
                    f"environment cannot create a second company: {exc}"
                )
                return
        options = {'date': '2026-05-31'}
        # Cache an execution scoped to a single company.
        single = self.Execution.start_execution(
            report_code='trial_balance',
            name='TB',
            options=options,
            company_ids=[self.company.id],
        )
        single.complete_execution(row_count=42)

        # A render request for a wider scope must not pick up the
        # narrow-scope cache, even though the overlap is non empty.
        found = self.Execution.find_cached(
            report_code='trial_balance',
            options_hash=single.options_hash,
            company_ids=[self.company.id, company2.id],
        )
        self.assertFalse(found, "wide scope must not match narrow cache")

        # Original scope still hits.
        found_same = self.Execution.find_cached(
            report_code='trial_balance',
            options_hash=single.options_hash,
            company_ids=[self.company.id],
        )
        self.assertEqual(found_same, single)

    def test_company_ids_key_canonical(self):
        """The key is the sorted-comma representation regardless of input
        order; the strict cache match relies on this canonicalisation.
        """
        self.assertEqual(
            self.Execution._company_ids_key_for([3, 1, 2]),
            "1,2,3",
        )
        self.assertEqual(
            self.Execution._company_ids_key_for([1, 1, 2]),
            "1,2",
        )
        self.assertEqual(self.Execution._company_ids_key_for([]), "")
