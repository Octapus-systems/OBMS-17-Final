# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Live connector cron isolation.

cron_fetch_due_profiles runs every active profile through the shared
eh.cron.batch.mixin so one bank's failure rolls back only that profile
and the run continues. Before the mixin the bare savepoint still let an
unexpected error propagate and abort the whole cron.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged('eh_account_bank_statement_import', 'integration', 'post_install',
        '-at_install')
class TestLiveConnectorCronIsolation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Profile = cls.env['eh.bank.live.connector.profile']

    def _make_profile(self, name, code):
        # One profile per journal (unique_journal constraint), so each
        # profile gets its own fresh bank journal.
        journal = self.env['account.journal'].create({
            'name': name + ' Bank',
            'code': code,
            'type': 'bank',
            'company_id': self.env.company.id,
        })
        return self.Profile.create({
            'name': name,
            'journal_id': journal.id,
            'company_id': self.env.company.id,
            'connector_key': 'manual',
            'active': True,
        })

    def test_credential_fields_build_json(self):
        import json
        p = self._make_profile('Creds Plaid', 'LVCJ')
        p.connector_key = 'plaid'
        p.write({
            'cred_client_id': 'cid-1', 'cred_secret': 'sec-1',
            'cred_access_token': 'tok-1', 'cred_account_id': 'acc-1',
            'cred_environment': 'sandbox',
        })
        data = json.loads(p.credentials_json)
        self.assertEqual(data['client_id'], 'cid-1')
        self.assertEqual(data['secret'], 'sec-1')
        self.assertEqual(data['access_token'], 'tok-1')
        self.assertEqual(data['account_id'], 'acc-1')
        self.assertEqual(data['environment'], 'sandbox')

    def test_credential_fields_parse_from_json(self):
        p = self._make_profile('Creds Basiq', 'LVCB')
        p.connector_key = 'basiq'
        p.credentials_json = '{"api_key": "k-9", "user_id": "u-9", "account_id": "a-9"}'
        p.invalidate_recordset()
        self.assertEqual(p.cred_api_key, 'k-9')
        self.assertEqual(p.cred_user_id, 'u-9')
        self.assertEqual(p.cred_account_id, 'a-9')

    def test_clearing_credential_field_drops_key(self):
        import json
        p = self._make_profile('Creds Clear', 'LVCC')
        p.connector_key = 'plaid'
        p.write({'cred_client_id': 'x', 'cred_secret': 'y'})
        p.write({'cred_secret': False})
        data = json.loads(p.credentials_json)
        self.assertIn('client_id', data)
        self.assertNotIn('secret', data)

    def test_cron_fetch_isolates_one_bad_profile(self):
        p1 = self._make_profile('Profile A', 'LVB1')
        p2 = self._make_profile('Profile B', 'LVB2')
        Klass = type(self.Profile)
        calls = []

        def flaky(profile):
            calls.append(profile.id)
            if profile.id == p1.id:
                raise ValueError("forced failure")
            # p2: succeed without reaching a real connector.

        with patch.object(Klass, '_run_fetch', flaky):
            self.Profile.cron_fetch_due_profiles()

        self.assertIn(p1.id, calls)
        self.assertIn(
            p2.id, calls,
            "second profile must be reached despite the first failing",
        )
