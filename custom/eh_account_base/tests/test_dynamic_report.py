# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Tests for the orchestrator (eh.account.dynamic.report).

Covers:

* render() cache miss path: starts execution, calls handler, persists payload,
  marks done.
* render() cache hit path: skips handler invocation, returns stored payload
  with from_cache=True.
* Cache invalidation: bumping the move version counter forces a recompute.
* Error path: handler exceptions mark execution 'error' and re raise.
* Constraint: handler_model must reference an installed model.

Implementation note: Odoo 19 does not load AbstractModel classes defined
inside a tests/ folder into the registry early enough for env[handler]
lookups during setUpClass. We therefore drive these tests through the
real installed 'aged_payable' handler and use unittest.mock.patch to
control its compute() return value.
"""

import unittest
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAccountUnitTestCase


# Handler we patch. Real, installed (provided by eh_account_dynamic_reports
# which is a guaranteed dependency in the suite test run).
_REAL_HANDLER = 'eh.account.dynamic.report.handler.aged_payable'


def _fake_compute_payload():
    """The standard payload returned by the patched compute() method."""
    return {
        'columns': [
            {'expression_label': 'value', 'name': "Value",
             'figure_type': 'monetary'},
        ],
        'lines': [
            {
                'id': 'line-1',
                'name': "Test Line",
                'level': 1,
                'columns': [
                    {'expression_label': 'value', 'value': 42.0},
                ],
            },
        ],
        'totals': {'value': 42.0},
        'generated_at': '2026-04-30T00:00:00',
    }


class _CallLog:
    calls = []

    @classmethod
    def reset(cls):
        cls.calls = []

    @classmethod
    def fake_compute(cls, *args, **kwargs):
        # Args: (self, options) on bound method, or (options,) when patched
        # with autospec=False on the class. Capture options shape.
        if args:
            options = args[-1] if not isinstance(args[-1], dict) else args[-1]
        else:
            options = kwargs.get('options') or {}
        try:
            cls.calls.append(dict(options))
        except Exception:
            cls.calls.append({})
        return _fake_compute_payload()

    @classmethod
    def failing_compute(cls, *args, **kwargs):
        raise RuntimeError("synthetic failure for tests")


@tagged('eh_account_base', 'integration', 'post_install', '-at_install')
class TestOrchestratorCacheBehaviour(EhAccountUnitTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if _REAL_HANDLER not in cls.env.registry.models:
            raise unittest.SkipTest(
                f"{_REAL_HANDLER} not registered; install "
                f"eh_account_dynamic_reports for these tests."
            )
        # Use a real installed handler. The compute() call will be patched
        # in each test method, so the actual aged_payable logic does not run.
        cls.report = cls.env['eh.account.dynamic.report'].create({
            'code': 'orch_cache_test',
            'name': 'Orchestrator Cache Test',
            'handler_model': _REAL_HANDLER,
        })

    def setUp(self):
        super().setUp()
        _CallLog.reset()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.env.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _patch_compute(self):
        """Return a context manager that swaps the handler's compute()."""
        return patch.object(
            type(self.env[_REAL_HANDLER]),
            'compute',
            _CallLog.fake_compute,
        )

    def test_first_render_miss_invokes_handler(self):
        with self._patch_compute():
            result = self.report.render(self.options)
        self.assertFalse(result['from_cache'])
        self.assertEqual(len(_CallLog.calls), 1)
        self.assertIn('execution_id', result)
        self.assertEqual(len(result['lines']), 1)

    def test_second_render_hit_skips_handler(self):
        with self._patch_compute():
            self.report.render(self.options)
            _CallLog.reset()
            result = self.report.render(self.options)
        self.assertTrue(result['from_cache'])
        self.assertEqual(
            len(_CallLog.calls), 0,
            "Handler should not run on cache hit",
        )
        self.assertEqual(len(result['lines']), 1)

    def test_use_cache_false_forces_recompute(self):
        with self._patch_compute():
            self.report.render(self.options)
            _CallLog.reset()
            result = self.report.render(self.options, use_cache=False)
        self.assertFalse(result['from_cache'])
        self.assertEqual(len(_CallLog.calls), 1)

    def test_move_version_bump_invalidates_cache(self):
        with self._patch_compute():
            self.report.render(self.options)
            _CallLog.reset()
            self.env['res.company']._eh_bump_move_version(
                [self.env.company.id]
            )
            result = self.report.render(self.options)
        self.assertFalse(result['from_cache'])
        self.assertEqual(len(_CallLog.calls), 1)

    def test_different_options_produce_different_keys(self):
        with self._patch_compute():
            result_a = self.report.render(self.options)
            options_b = dict(self.options)
            options_b['posted_only'] = False
            result_b = self.report.render(options_b)
        self.assertFalse(result_b['from_cache'])
        self.assertNotEqual(
            result_a['execution_id'], result_b['execution_id'],
        )

    def test_canonicalisation_makes_key_order_insensitive(self):
        with self._patch_compute():
            self.report.render(self.options)
            _CallLog.reset()
            reordered = {
                'show_zero': False,
                'company_ids': [self.env.company.id],
                'date': {'date_to': '2026-12-31',
                         'date_from': '2026-01-01'},
                'posted_only': True,
            }
            result = self.report.render(reordered)
        self.assertTrue(result['from_cache'])
        self.assertEqual(len(_CallLog.calls), 0)


@tagged('eh_account_base', 'integration', 'post_install', '-at_install')
class TestOrchestratorErrorPath(EhAccountUnitTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if _REAL_HANDLER not in cls.env.registry.models:
            raise unittest.SkipTest(
                f"{_REAL_HANDLER} not registered; install "
                f"eh_account_dynamic_reports for these tests."
            )
        cls.report = cls.env['eh.account.dynamic.report'].create({
            'code': 'orch_error_test',
            'name': 'Orchestrator Error Test',
            'handler_model': _REAL_HANDLER,
        })

    def test_handler_exception_marks_execution_error(self):
        # Manually catch the exception rather than using assertRaises,
        # because Odoo 19's _assertRaises wraps the block in a savepoint
        # that rolls back on exception. That rollback would also undo
        # the fail_execution() write we want to inspect.
        Execution = self.env['eh.account.report.execution']
        raised = False
        with patch.object(
            type(self.env[_REAL_HANDLER]),
            'compute',
            _CallLog.failing_compute,
        ):
            try:
                self.report.render({
                    'date': {'date_from': '2026-01-01',
                             'date_to': '2026-12-31'},
                    'company_ids': [self.env.company.id],
                })
            except RuntimeError as exc:
                raised = True
                self.assertIn('synthetic failure', str(exc))
        self.assertTrue(raised, "render should have re raised RuntimeError")
        self.env.flush_all()
        Execution.invalidate_model()
        last = Execution.search(
            [('report_code', '=', 'orch_error_test')],
            limit=1, order='executed_at desc',
        )
        self.assertTrue(last)
        self.assertEqual(last.state, 'error')
        self.assertIn('synthetic failure', last.error_message)


@tagged('eh_account_base', 'integration', 'post_install', '-at_install')
class TestOrchestratorConstraints(EhAccountUnitTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._handler_available = (
            _REAL_HANDLER in cls.env.registry.models
        )

    def _require_handler(self):
        if not self._handler_available:
            self.skipTest(
                f"{_REAL_HANDLER} not registered; install "
                f"eh_account_dynamic_reports for these tests."
            )

    def test_unknown_handler_model_rejected(self):
        with self.assertRaises(UserError):
            self.env['eh.account.dynamic.report'].create({
                'code': 'bad_test',
                'name': 'Bad',
                'handler_model': 'eh.does.not.exist',
            })

    def test_duplicate_code_rejected(self):
        self._require_handler()
        first = self.env['eh.account.dynamic.report'].create({
            'code': 'unique_test',
            'name': 'First',
            'handler_model': _REAL_HANDLER,
        })
        with self.assertRaises(Exception):
            self.env['eh.account.dynamic.report'].create({
                'code': 'unique_test',
                'name': 'Second',
                'handler_model': _REAL_HANDLER,
            })
        self.assertTrue(first.exists())

    def test_get_default_options_returns_dict(self):
        self._require_handler()
        report = self.env['eh.account.dynamic.report'].create({
            'code': 'defaults_test',
            'name': 'Defaults',
            'handler_model': _REAL_HANDLER,
        })
        opts = report.get_default_options()
        self.assertIn('date', opts)
        self.assertIn('company_ids', opts)
        self.assertEqual(opts['posted_only'], True)
