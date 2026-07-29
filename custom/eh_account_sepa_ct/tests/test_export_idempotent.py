# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Idempotency of the SEPA Credit Transfer export.

The PAIN.001 IS the instruction the bank executes. Regenerating it for the
same posted batch mints a fresh MsgId but keeps the SAME EndToEndIds and
amounts, so the bank treats the two files as independent instructions and,
if both are submitted, pays every supplier twice. These tests prove that a
second export of an already-exported batch is refused, that a genuine re-cut
is only reachable through the explicit, audited void action, and that voiding
requires the manager group.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_sepa_ct', 'integration', 'post_install', '-at_install')
class TestSepaExportIdempotent(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Batch = cls.env['eh.batch.payment']

        cls.bank_journal = cls.env['account.journal'].search(
            [('type', '=', 'bank'),
             ('company_id', '=', cls.env.company.id)],
            limit=1,
        )
        if not cls.bank_journal:
            cls.bank_journal = cls.env['account.journal'].create({
                'name': 'Test Bank',
                'code': 'TBK',
                'type': 'bank',
                'company_id': cls.env.company.id,
            })

        cls.env['eh.sepa.originator'].create({
            'journal_id': cls.bank_journal.id,
            'initiating_party_name': 'Demo Co',
            'iban': 'DE89370400440532013000',
            'bic': 'DEUTDEFFXXX',
        })

        cls.env['res.partner.bank'].create({
            'acc_number': 'FR1420041010050500013M02606',
            'partner_id': cls.partner_a.id,
        })

        cls.eur = cls.env.ref('base.EUR')
        cls.eur.active = True
        cls.env['res.currency.rate'].create({
            'currency_id': cls.eur.id,
            'company_id': cls.company.id,
            'rate': 1.0,
            'name': '2026-01-01',
        })

        # The class user is the confirmer (maker); promote to manager so
        # confirm/post pass their own guards.
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )

    def _make_manager(self, login):
        return self.env['res.users'].create({
            'name': login,
            'login': login,
            'email': '%s@example.com' % login,
            'groups_id': [(4, self.env.ref(
                'eh_account_base.group_eh_manager',
            ).id)],
        })

    def _posted_batch(self, poster):
        """Confirm (as the class user) then post (as `poster`) a one-line
        outbound batch, returning it posted and ready to export."""
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
            'ref': 'INV-001',
            'currency_id': self.eur.id,
        })
        batch.action_confirm()

        PaymentModel = type(self.env['account.payment'])

        def _fake_post(payment):
            payment.state = 'posted'

        with patch.object(PaymentModel, 'action_post', _fake_post):
            batch.with_user(poster).action_post()
        self.assertEqual(batch.state, 'posted')
        return batch

    def test_reexport_is_blocked(self):
        """A second Export click on an already-exported batch is refused,
        so no duplicate PAIN.001 (a second bank instruction) is minted."""
        poster = self._make_manager('eh_ct_idem_poster')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_ct_idem_exporter')

        batch.with_user(exporter).action_export_sepa_ct()
        self.assertEqual(len(batch.sepa_export_ids), 1)
        first = batch.sepa_export_ids
        self.assertEqual(first.state, 'generated')

        # Second export attempt on the same posted batch: blocked.
        with self.assertRaises(UserError):
            batch.with_user(exporter).action_export_sepa_ct()
        # No new export row was created; the first is untouched.
        self.assertEqual(len(batch.sepa_export_ids), 1)
        self.assertEqual(batch.sepa_export_ids.state, 'generated')

    def test_reexport_blocked_even_after_download(self):
        """Downloading the file does not unlock a re-export: a downloaded
        file is an even stronger 'already sent' signal."""
        poster = self._make_manager('eh_ct_idem_poster2')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_ct_idem_exporter2')

        batch.with_user(exporter).action_export_sepa_ct()
        batch.sepa_export_ids.with_user(exporter).action_download()
        self.assertEqual(batch.sepa_export_ids.state, 'downloaded')

        with self.assertRaises(UserError):
            batch.with_user(exporter).action_export_sepa_ct()
        self.assertEqual(len(batch.sepa_export_ids), 1)

    def test_recut_after_explicit_void(self):
        """The legitimate re-cut path: void the prior (never-sent) file, then
        export again. The prior row is superseded and exactly one file is
        active afterwards."""
        poster = self._make_manager('eh_ct_idem_poster3')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_ct_idem_exporter3')

        batch.with_user(exporter).action_export_sepa_ct()
        first = batch.sepa_export_ids
        self.assertEqual(len(first), 1)

        # Explicitly void the prior file (it was never submitted).
        first.with_user(exporter).action_void_for_recut()
        self.assertEqual(first.state, 'superseded')

        # Re-export now succeeds because no active file remains.
        batch.with_user(exporter).action_export_sepa_ct()
        active = batch.sepa_export_ids.filtered(
            lambda e: e.state in ('generated', 'downloaded'),
        )
        self.assertEqual(len(batch.sepa_export_ids), 2)
        self.assertEqual(len(active), 1)
        self.assertNotEqual(active, first)

    def test_void_requires_manager(self):
        """Voiding a SEPA export (which unlocks a re-cut) is a manager-only
        act; an ordinary accounting user is refused."""
        poster = self._make_manager('eh_ct_idem_poster4')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_ct_idem_exporter4')
        batch.with_user(exporter).action_export_sepa_ct()
        export = batch.sepa_export_ids

        member = self.env['res.users'].create({
            'name': 'eh_ct_idem_member',
            'login': 'eh_ct_idem_member',
            'email': 'eh_ct_idem_member@example.com',
            'groups_id': [(4, self.env.ref(
                'eh_account_base.group_eh_user',
            ).id)],
        })
        if not member:
            self.skipTest("Could not provision a non-manager user.")
        with self.assertRaises(UserError):
            export.with_user(member).action_void_for_recut()
        self.assertEqual(export.state, 'generated')
