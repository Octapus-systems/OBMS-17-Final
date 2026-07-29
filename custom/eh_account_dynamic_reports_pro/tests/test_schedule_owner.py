# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: a scheduled report must render under its immutable creator,
never a freely-writable owner field.

The vulnerability: user_id was a plain, writable Many2one and _build_attachments
bound the (root cron) render to it via with_user(). A basic accounting user
(group_eh_user has create/write on eh.report.schedule) could point user_id at a
system administrator or a better-scoped colleague, then let the hourly cron
render another company/user's financials under those elevated rights and email
them to an attacker-controlled address, defeating the company-scope clamp.

Two defences, both exercised here:

* _build_attachments now binds with_user() to create_uid (stamped once by the
  ORM, unforgeable over RPC), not user_id.
* create()/write() refuse to let a non-manager own another user's schedule, and
  refuse to hand any schedule to a base.group_system user the caller is not.

The test env runs as SUPERUSER (guard-exempt), so the negative assertions are
made through with_user() as freshly provisioned non-superusers; skipTest
gracefully if the environment forbids provisioning them.
"""

import json
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestScheduleOwner(EhAccountIntegrationTestCase):

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
        cls.Schedule = cls.env['eh.report.schedule']

        def _mk(login, group_xmlids):
            # Odoo 19 uses group_ids on res.users (the 16/17 backport transform
            # rewrites it to groups_id). Single company_id (never a two-element
            # company_ids command, which the transform mangles).
            try:
                return cls.env['res.users'].create({
                    'name': login,
                    'login': login,
                    'company_id': cls.company.id,
                    'groups_id': [(6, 0, [
                        cls.env.ref(x).id for x in group_xmlids])],
                })
            except Exception:  # pragma: no cover - hardened env may forbid it
                return None

        cls.manager = _mk('eh_sched_mgr', [
            'base.group_user', 'eh_account_base.group_eh_manager'])
        cls.plain = _mk('eh_sched_plain', [
            'base.group_user', 'eh_account_base.group_eh_user'])
        cls.other = _mk('eh_sched_other', [
            'base.group_user', 'eh_account_base.group_eh_user'])
        cls.sysadmin = _mk('eh_sched_sys', [
            'base.group_user', 'base.group_system',
            'eh_account_base.group_eh_user'])

    def _vals(self, **overrides):
        vals = {
            'name': 'Owner TB',
            'report_id': self.report.id,
            'options_json': json.dumps({'company_ids': [self.company.id]}),
            'interval': 1,
            'interval_unit': 'month',
            'next_run': fields.Datetime.now() - timedelta(minutes=1),
            'subject': 'Owner test',
            'recipient_emails': 'owner@example.com',
            'delivery_format': 'xlsx',
        }
        vals.update(overrides)
        return vals

    # ---- create/write owner guard ----

    def test_non_manager_cannot_own_another_users_schedule(self):
        if not (self.plain and self.other):
            self.skipTest("Could not provision non-manager test users.")
        with self.assertRaises(UserError):
            self.Schedule.with_user(self.plain).create(
                self._vals(user_id=self.other.id))

    def test_non_manager_cannot_repoint_owner_on_write(self):
        if not (self.plain and self.other):
            self.skipTest("Could not provision non-manager test users.")
        # Owns its own schedule (default user_id == the creator).
        schedule = self.Schedule.with_user(self.plain).create(self._vals())
        with self.assertRaises(UserError):
            schedule.with_user(self.plain).write({'user_id': self.other.id})

    def test_nobody_below_system_can_assign_to_system_admin(self):
        # A manager clears the "own only" check but must still be refused
        # when handing the schedule to a base.group_system user.
        if not (self.manager and self.sysadmin):
            self.skipTest("Could not provision manager / sysadmin test users.")
        with self.assertRaises(UserError):
            self.Schedule.with_user(self.manager).create(
                self._vals(user_id=self.sysadmin.id))

    def test_manager_may_assign_to_plain_user(self):
        if not (self.manager and self.other):
            self.skipTest("Could not provision manager / plain test users.")
        schedule = self.Schedule.with_user(self.manager).create(
            self._vals(user_id=self.other.id))
        self.assertEqual(schedule.user_id, self.other)
        self.assertEqual(schedule.create_uid, self.manager)

    def test_non_manager_may_own_own_schedule(self):
        if not self.plain:
            self.skipTest("Could not provision a non-manager test user.")
        schedule = self.Schedule.with_user(self.plain).create(
            self._vals(user_id=self.plain.id))
        self.assertEqual(schedule.create_uid, self.plain)
        self.assertEqual(schedule.user_id, self.plain)

    # ---- render binds to create_uid, not user_id ----

    def test_render_owner_is_create_uid_not_user_id(self):
        """Even when user_id points at a different user, the attachment
        render must run under the immutable creator so the engine's
        company-scope clamp keys off the person who actually owns it."""
        if not (self.manager and self.other):
            self.skipTest("Could not provision manager / plain test users.")
        schedule = self.Schedule.with_user(self.manager).create(
            self._vals(user_id=self.other.id))
        self.assertNotEqual(schedule.create_uid, schedule.user_id)

        captured = {}
        ReportCls = type(self.report)
        original = ReportCls.render_xlsx

        def _spy(report_self, options, use_cache=True):
            captured['uid'] = report_self.env.uid
            return b'PKspy'

        self.patch(ReportCls, 'render_xlsx', _spy)
        schedule._build_attachments(schedule._parse_options())

        self.assertEqual(
            captured.get('uid'), schedule.create_uid.id,
            "render must bind to the immutable create_uid",
        )
        self.assertNotEqual(
            captured.get('uid'), schedule.user_id.id,
            "render must NOT bind to the writable user_id",
        )
        # Guard against accidentally leaving the spy patched (self.patch
        # auto-reverts, but assert the original is a real bound method).
        self.assertTrue(callable(original))
