# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Idempotency of the SEPA Direct Debit export.

Generating the PAIN.008 consumes every mandate in the batch (advancing
its FRST -> RCUR sequence counter and its last collection date) and cuts
the collection file the bank debits against debtor accounts. Re-running
the export on a batch that was already exported would consume every
mandate a second time and produce a duplicate file, so the bank could
debit each debtor twice. These tests prove a second export attempt is
refused, the mandate counter is not advanced again, and no second export
row is written.
"""

from datetime import date
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_sepa_dd', 'integration', 'post_install', '-at_install')
class TestSepaDdReexportIdempotency(EhAccountIntegrationTestCase):

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

        existing = cls.env['eh.sepa.creditor'].search(
            [('journal_id', '=', cls.bank_journal.id)], limit=1,
        )
        if existing:
            cls.creditor = existing
        else:
            cls.creditor = cls.env['eh.sepa.creditor'].create({
                'name': 'Demo creditor',
                'journal_id': cls.bank_journal.id,
                'creditor_identifier': 'DE98ZZZ09999999999',
                'creditor_name': 'Demo Co',
                'iban': 'DE89370400440532013000',
                'pre_notification_days': 0,
            })

        cls.mandate = cls.env['eh.sepa.mandate'].create({
            'mandate_id': 'MNDT-DDIDEM-001',
            'creditor_id': cls.creditor.id,
            'partner_id': cls.partner_a.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 15),
            'state': 'active',
            'local_instrument': 'CORE',
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

    def _make_user(self, login):
        """A plain EH accounting user (NOT a manager). Read/write on the
        export model but no manager group, so the void gate must refuse."""
        return self.env['res.users'].create({
            'name': login,
            'login': login,
            'email': '%s@example.com' % login,
            'groups_id': [(4, self.env.ref(
                'eh_account_base.group_eh_user',
            ).id)],
        })

    def _posted_batch(self, poster):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'inbound',
            'payment_date': '2026-04-30',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'customer',
            'payment_type': 'inbound',
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

    def test_reexport_is_refused_and_does_not_reconsume(self):
        """A second export attempt on an already-exported batch is refused,
        the mandate sequence counter is not advanced again, and no second
        export row is written."""
        poster = self._make_manager('eh_dd_idem_poster')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_idem_exporter')

        # First export: consumes the mandate (FRST), writes one export row.
        batch.with_user(exporter).action_export_sepa_dd()
        self.assertEqual(len(batch.sepa_dd_export_ids), 1)
        self.mandate.invalidate_recordset(['collection_count'])
        self.assertEqual(self.mandate.collection_count, 1)

        # Second export on the SAME batch must be refused before any
        # mandate is re-consumed and before a second file is cut.
        exporter2 = self._make_manager('eh_dd_idem_exporter2')
        with self.assertRaises(UserError):
            batch.with_user(exporter2).action_export_sepa_dd()

        # The mandate counter stayed at 1 (no double FRST -> RCUR advance)
        # and no second export row exists.
        self.mandate.invalidate_recordset(['collection_count'])
        self.assertEqual(self.mandate.collection_count, 1)
        self.assertEqual(len(batch.sepa_dd_export_ids), 1)

    def test_guard_keys_on_live_exports_only(self):
        """The guard blocks on a LIVE (generated/downloaded) export, not on
        a superseded audit row: a batch whose only export rows are
        superseded is not permanently locked out of the export action."""
        poster = self._make_manager('eh_dd_idem_poster2')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_idem_exporter3')

        batch.with_user(exporter).action_export_sepa_dd()
        first = batch.sepa_dd_export_ids
        self.assertEqual(len(first), 1)

        # A live export blocks re-export.
        exporter2 = self._make_manager('eh_dd_idem_exporter4')
        with self.assertRaises(UserError):
            batch.with_user(exporter2).action_export_sepa_dd()

        # Retire the prior export (server-side supersede runs under sudo);
        # the batch now has no LIVE export, so the idempotency guard no
        # longer trips. (Re-export then proceeds through the normal SoD /
        # mandate path, which is out of scope for this guard test.)
        first.sudo().write({'state': 'superseded'})
        live = batch.sepa_dd_export_ids.filtered(
            lambda e: e.state in ('generated', 'downloaded'),
        )
        self.assertFalse(live)

    def test_void_for_recut_reopens_the_batch_for_a_legitimate_recut(self):
        """The escape hatch the refuse message promises: a manager voids the
        live export via action_void_for_recut (the audit row flips to
        superseded), and the batch can then be re-collected. Proves the
        re-cut path the guard's error text advertises actually exists and
        works end to end - the fix for the over-restriction."""
        poster = self._make_manager('eh_dd_void_poster')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_void_exporter')

        batch.with_user(exporter).action_export_sepa_dd()
        first = batch.sepa_dd_export_ids
        self.assertEqual(len(first), 1)
        self.assertIn(first.state, ('generated', 'downloaded'))
        self.mandate.invalidate_recordset(['collection_count'])
        self.assertEqual(self.mandate.collection_count, 1)

        # Manager voids the un-transmitted file for re-cut.
        voider = self._make_manager('eh_dd_voider')
        first.with_user(voider).action_void_for_recut()
        self.assertEqual(first.state, 'superseded')
        self.assertFalse(batch.sepa_dd_export_ids.filtered(
            lambda e: e.state in ('generated', 'downloaded'),
        ))

        # With no live export the idempotency guard no longer trips, so a
        # fresh export succeeds: the mandate is re-consumed (counter 2) and
        # exactly one live export exists again.
        exporter2 = self._make_manager('eh_dd_void_exporter2')
        batch.with_user(exporter2).action_export_sepa_dd()
        self.mandate.invalidate_recordset(['collection_count'])
        self.assertEqual(self.mandate.collection_count, 2)
        live = batch.sepa_dd_export_ids.filtered(
            lambda e: e.state in ('generated', 'downloaded'),
        )
        self.assertEqual(len(live), 1)
        # And the retired file stays retired: never two live files at once.
        self.assertEqual(first.state, 'superseded')

    def test_void_for_recut_refused_for_non_manager(self):
        """The void is a money-file control and stays manager-gated: a plain
        EH user cannot void an export. Proves the escape hatch did not open a
        privilege hole - the four-eyes intent behind the export guard is
        preserved."""
        poster = self._make_manager('eh_dd_void_poster3')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_void_exporter3')
        batch.with_user(exporter).action_export_sepa_dd()
        export = batch.sepa_dd_export_ids
        self.assertEqual(len(export), 1)

        try:
            plain = self._make_user('eh_dd_plain_user')
        except Exception:
            self.skipTest("cannot provision a non-manager EH user here")

        with self.assertRaises(UserError):
            export.with_user(plain).action_void_for_recut()
        # The export stayed live: the non-manager attempt changed nothing.
        self.assertIn(export.state, ('generated', 'downloaded'))

    def test_state_guard_still_blocks_direct_supersede_write(self):
        """The original protection is intact: superseding an export is a
        guarded state transition that only the record's own sudo action may
        perform. A non-superuser cannot forge the void by RPC-writing
        state='superseded' directly - so the audit trail cannot be doctored
        and the double-file guard cannot be side-stepped."""
        poster = self._make_manager('eh_dd_void_poster4')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_void_exporter4')
        batch.with_user(exporter).action_export_sepa_dd()
        export = batch.sepa_dd_export_ids
        self.assertEqual(len(export), 1)

        # Even a manager writing the guarded field DIRECTLY (not via the
        # action) is refused; state moves only through the sudo action.
        with self.assertRaises(AccessError):
            export.with_user(exporter).write({'state': 'superseded'})
        self.assertIn(export.state, ('generated', 'downloaded'))
