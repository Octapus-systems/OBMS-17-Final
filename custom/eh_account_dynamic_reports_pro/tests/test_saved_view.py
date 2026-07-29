# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Saved view tests.

Covers persistence (save_view round trip), telemetry (use_count and
last_used_at on load_view), visibility (list_for_report respects user
ownership and is_shared), uniqueness constraint, and the pin toggle.
"""

import json

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestSavedView(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        DynRep = cls.env['eh.account.dynamic.report']
        cls.report = DynRep.search([('code', '=', 'trial_balance')], limit=1)
        if not cls.report:
            cls.report = DynRep.create({
                'code': 'trial_balance',
                'name': 'Trial Balance',
                'handler_model':
                    'eh.account.dynamic.report.handler.trial_balance',
            })
        cls.SavedView = cls.env['eh.report.saved.view']

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    # ---- save / load ----

    def test_save_view_persists_options(self):
        view = self.SavedView.save_view(
            self.report.id, "My Q1", self.options,
        )
        self.assertTrue(view.exists())
        self.assertEqual(view.name, "My Q1")
        self.assertEqual(view.user_id, self.env.user)
        self.assertEqual(view.report_id, self.report)
        loaded = json.loads(view.options_json)
        self.assertEqual(loaded['posted_only'], True)
        self.assertEqual(loaded['date']['date_from'], '2026-01-01')

    def test_save_view_requires_name(self):
        with self.assertRaises(UserError):
            self.SavedView.save_view(self.report.id, "", self.options)

    def test_save_view_requires_dict_options(self):
        with self.assertRaises(UserError):
            self.SavedView.save_view(
                self.report.id, "Bad", "not a dict",
            )

    def test_load_view_returns_options(self):
        view = self.SavedView.save_view(
            self.report.id, "Round trip", self.options,
        )
        loaded = view.load_view()
        self.assertEqual(loaded['posted_only'], True)
        self.assertEqual(loaded['date']['date_to'], '2026-12-31')

    def test_load_view_increments_use_count_and_timestamp(self):
        view = self.SavedView.save_view(
            self.report.id, "Telemetry", self.options,
        )
        self.assertEqual(view.use_count, 0)
        self.assertFalse(view.last_used_at)
        view.load_view()
        view.load_view()
        view.invalidate_recordset()
        self.assertEqual(view.use_count, 2)
        self.assertTrue(view.last_used_at)

    # ---- visibility ----

    def _make_account_user(self, name, login):
        # Include EH user group: the saved-view model is gated through
        # eh.account.dynamic.report which requires group_eh_user.
        return self.env['res.users'].create({
            'name': name,
            'login': login,
            'groups_id': [
                (4, self.env.ref('account.group_account_user').id),
                (4, self.env.ref('eh_account_base.group_eh_user').id),
            ],
        })

    def test_list_for_report_returns_own_views(self):
        own = self.SavedView.save_view(
            self.report.id, "Mine", self.options,
        )
        other_user = self._make_account_user('Other', 'other_user_test')
        self.SavedView.with_user(other_user).save_view(
            self.report.id, "Theirs", self.options,
        )
        listing = self.SavedView.list_for_report(self.report.id)
        ids = [r['id'] for r in listing]
        self.assertIn(own.id, ids)

    def test_list_for_report_includes_shared(self):
        other_user = self._make_account_user('Sharer', 'shared_user_test')
        shared = self.SavedView.with_user(other_user).save_view(
            self.report.id, "Shared View", self.options, is_shared=True,
        )
        private = self.SavedView.with_user(other_user).save_view(
            self.report.id, "Private View", self.options, is_shared=False,
        )
        listing = self.SavedView.list_for_report(self.report.id)
        ids = [r['id'] for r in listing]
        self.assertIn(shared.id, ids)
        self.assertNotIn(private.id, ids)

    def test_list_for_report_excludes_shared_when_requested(self):
        other_user = self._make_account_user('Sharer2', 'shared_user2_test')
        shared = self.SavedView.with_user(other_user).save_view(
            self.report.id, "Shared 2", self.options, is_shared=True,
        )
        listing = self.SavedView.list_for_report(
            self.report.id, include_shared=False,
        )
        ids = [r['id'] for r in listing]
        self.assertNotIn(shared.id, ids)

    # ---- uniqueness and pin toggle ----

    def test_unique_per_user_per_report(self):
        self.SavedView.save_view(self.report.id, "Dup", self.options)
        with self.assertRaises(Exception):
            # SQL constraint violation expected
            self.SavedView.save_view(self.report.id, "Dup", self.options)

    def test_action_toggle_pinned(self):
        view = self.SavedView.save_view(
            self.report.id, "Toggle", self.options,
        )
        self.assertFalse(view.pinned)
        view.action_toggle_pinned()
        self.assertTrue(view.pinned)
        view.action_toggle_pinned()
        self.assertFalse(view.pinned)
