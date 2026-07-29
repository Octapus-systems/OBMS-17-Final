# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
B5 collections depth tests: SMS follow-up dispatch, configurable
activity responsible, and the manual reminder wizard.
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_collections', 'integration', 'post_install', '-at_install')
class TestCollectionsB5(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Case = cls.env['eh.collections.case']
        cls.Level = cls.env['eh.collections.followup.level']
        cls.Sms = cls.env['sms.sms']
        cls.partner_a.write({'phone': '+15550001111'})
        cls.salesperson = cls.env['res.users'].create({
            'name': 'Sales Rep', 'login': 'b5_sales@test',
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.collector = cls.env['res.users'].create({
            'name': 'Collector', 'login': 'b5_collector@test',
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.sms_tpl = cls.env['sms.template'].create({
            'name': 'Dunning SMS',
            'model_id': cls.env['ir.model']._get('eh.collections.case').id,
            'body': 'Your account is overdue. Please pay.',
        })

    def _sms_count(self, partner):
        return self.Sms.search_count([('partner_id', '=', partner.id)])

    def test_followup_sends_sms(self):
        level = self.Level.create({
            'name': 'SMS level', 'sequence': 10, 'days_overdue': 7,
            'action_type': 'activity', 'also_send_sms': True,
            'sms_template_id': self.sms_tpl.id,
            'company_id': self.env.company.id,
        })
        case = self.Case.create({
            'partner_id': self.partner_a.id,
            'collector_id': self.collector.id,
        })
        before = self._sms_count(self.partner_a)
        case._apply_followup_level(level)
        self.assertEqual(self._sms_count(self.partner_a), before + 1)

    def test_no_sms_without_mobile(self):
        self.partner_b.write({'phone': False})
        level = self.Level.create({
            'name': 'SMS level2', 'sequence': 10, 'days_overdue': 7,
            'action_type': 'activity', 'also_send_sms': True,
            'sms_template_id': self.sms_tpl.id,
            'company_id': self.env.company.id,
        })
        case = self.Case.create({'partner_id': self.partner_b.id})
        before = self._sms_count(self.partner_b)
        case._apply_followup_level(level)
        self.assertEqual(self._sms_count(self.partner_b), before)

    def test_responsible_resolution(self):
        case = self.Case.create({
            'partner_id': self.partner_a.id,
            'collector_id': self.collector.id,
        })
        lvl_collector = self.Level.create({
            'name': 'Lc', 'sequence': 10, 'days_overdue': 7,
            'action_type': 'activity', 'responsible_type': 'collector',
            'company_id': self.env.company.id,
        })
        self.assertEqual(
            case._eh_resolve_responsible(lvl_collector), self.collector)

        self.partner_a.write({'user_id': self.salesperson.id})
        lvl_sales = self.Level.create({
            'name': 'Ls', 'sequence': 20, 'days_overdue': 30,
            'action_type': 'activity', 'responsible_type': 'salesperson',
            'company_id': self.env.company.id,
        })
        self.assertEqual(
            case._eh_resolve_responsible(lvl_sales), self.salesperson)

    def test_manual_reminder_sms(self):
        case = self.Case.create({'partner_id': self.partner_a.id})
        before = self._sms_count(self.partner_a)
        before_actions = len(case.action_ids)
        wizard = self.env['eh.collections.reminder.wizard'].create({
            'case_id': case.id,
            'partner_id': self.partner_a.id,
            'channel': 'sms',
            'body': 'Final notice before legal.',
        })
        wizard.action_send()
        self.assertEqual(self._sms_count(self.partner_a), before + 1)
        self.assertEqual(len(case.action_ids), before_actions + 1)

    def test_manual_reminder_email(self):
        case = self.Case.create({'partner_id': self.partner_a.id})
        before_msgs = len(case.message_ids)
        wizard = self.env['eh.collections.reminder.wizard'].create({
            'case_id': case.id,
            'partner_id': self.partner_a.id,
            'channel': 'email',
            'subject': 'Reminder',
            'body': 'Please settle your overdue balance.',
        })
        wizard.action_send()
        self.assertGreater(len(case.message_ids), before_msgs)
        self.assertTrue(case.action_ids)
