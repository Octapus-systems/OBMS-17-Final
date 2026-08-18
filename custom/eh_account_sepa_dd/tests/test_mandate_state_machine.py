# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Mandate state machine tests.

Covers FRST -> RCUR transition on first consume, OOFF immediate
completion, the 36-month dormancy expiry both at consume time and
via the cron, validation guards, and atomic counter increment.
"""

from datetime import date, timedelta  # noqa: F401
from dateutil.relativedelta import relativedelta

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_sepa_dd', 'integration', 'post_install', '-at_install')
class TestMandateStateMachine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Mandate = cls.env['eh.sepa.mandate']
        cls.Creditor = cls.env['eh.sepa.creditor']

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

        # Other tests in the same database may have already created a
        # creditor for this journal (the unique constraint is on
        # journal_id alone). Reuse if present.
        existing = cls.Creditor.search(
            [('journal_id', '=', cls.bank_journal.id)], limit=1,
        )
        if existing:
            cls.creditor = existing
        else:
            cls.creditor = cls.Creditor.create({
                'name': 'Demo creditor',
                'journal_id': cls.bank_journal.id,
                'creditor_identifier': 'DE98ZZZ09999999999',
                'creditor_name': 'Demo Co',
                'iban': 'DE89370400440532013000',
            })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Demo customer',
        })

    def _make_mandate(self, **overrides):
        vals = {
            'mandate_id': 'MNDT-TEST-001',
            'creditor_id': self.creditor.id,
            'partner_id': self.partner.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 15),
            'state': 'active',
            'local_instrument': 'CORE',
        }
        vals.update(overrides)
        return self.Mandate.create(vals)

    # ---- next_sequence_type ----

    def test_active_first_use_yields_frst(self):
        m = self._make_mandate()
        self.assertEqual(m.next_sequence_type, 'FRST')

    def test_one_off_yields_ooff(self):
        m = self._make_mandate(is_one_off=True)
        self.assertEqual(m.next_sequence_type, 'OOFF')

    def test_draft_has_no_next_sequence(self):
        m = self._make_mandate(state='draft')
        self.assertFalse(m.next_sequence_type)

    # ---- consume_for_collection ----

    def test_first_consume_returns_frst_and_advances(self):
        m = self._make_mandate()
        seq = m.consume_for_collection(date(2026, 5, 1))
        self.assertEqual(seq, 'FRST')
        m.invalidate_recordset()
        self.assertEqual(m.collection_count, 1)
        self.assertEqual(m.last_collection_date, date(2026, 5, 1))
        self.assertEqual(m.next_sequence_type, 'RCUR')

    def test_second_consume_returns_rcur(self):
        m = self._make_mandate()
        m.consume_for_collection(date(2026, 5, 1))
        seq = m.consume_for_collection(date(2026, 6, 1))
        self.assertEqual(seq, 'RCUR')
        m.invalidate_recordset()
        self.assertEqual(m.collection_count, 2)

    def test_one_off_consume_returns_ooff_and_completes(self):
        m = self._make_mandate(is_one_off=True)
        seq = m.consume_for_collection(date(2026, 5, 1))
        self.assertEqual(seq, 'OOFF')
        m.invalidate_recordset()
        self.assertEqual(m.state, 'completed')

    def test_mark_final_returns_fnal_and_completes(self):
        m = self._make_mandate()
        m.consume_for_collection(date(2026, 5, 1))  # FRST
        seq = m.consume_for_collection(date(2026, 6, 1), mark_final=True)
        self.assertEqual(seq, 'FNAL')
        m.invalidate_recordset()
        self.assertEqual(m.state, 'completed')

    def test_consume_on_draft_raises(self):
        m = self._make_mandate(state='draft')
        with self.assertRaises(UserError):
            m.consume_for_collection(date(2026, 5, 1))

    def test_consume_on_revoked_raises(self):
        m = self._make_mandate()
        m.action_revoke()
        with self.assertRaises(UserError):
            m.consume_for_collection(date(2026, 5, 1))

    # ---- 36-month expiry ----

    def test_consume_on_dormant_mandate_expires_and_raises(self):
        m = self._make_mandate()
        m.consume_for_collection(date(2023, 1, 1))  # FRST in the past
        # try/except instead of assertRaises so the implicit savepoint
        # does not roll back the SQL UPDATE that flips state=expired.
        raised_msg = ''
        try:
            m.consume_for_collection(date(2027, 6, 1))
        except UserError as exc:
            raised_msg = str(exc)
        self.assertIn('dormant', raised_msg.lower())
        m.invalidate_recordset(['state'])
        self.assertEqual(m.state, 'expired')

    def test_consume_just_before_expiry_succeeds(self):
        m = self._make_mandate()
        first = date(2024, 1, 1)
        m.consume_for_collection(first)
        # 35 months later: still inside the window.
        almost_cutoff = first + relativedelta(months=35)
        seq = m.consume_for_collection(almost_cutoff)
        self.assertEqual(seq, 'RCUR')

    def test_cron_expires_dormant(self):
        m = self._make_mandate()
        # Backdate last collection to far past.
        m.consume_for_collection(date(2020, 1, 1))
        self.Mandate._cron_expire_dormant()
        m.invalidate_recordset()
        self.assertEqual(m.state, 'expired')

    # ---- validation ----

    def test_invalid_iban_rejected(self):
        with self.assertRaises(ValidationError):
            self._make_mandate(debtor_iban='DE99370400440532013000')

    def test_too_long_mandate_id_rejected(self):
        with self.assertRaises(ValidationError):
            self._make_mandate(mandate_id='X' * 36)

    def test_unique_mandate_id_per_company(self):
        self._make_mandate()
        with self.assertRaises(Exception):
            self._make_mandate()

    # ---- transitions ----

    def test_activate_from_draft(self):
        m = self._make_mandate(state='draft')
        m.action_activate()
        self.assertEqual(m.state, 'active')

    def test_activate_from_active_raises(self):
        m = self._make_mandate()
        with self.assertRaises(UserError):
            m.action_activate()

    def test_revoke_clears_active(self):
        m = self._make_mandate()
        m.action_revoke()
        self.assertEqual(m.state, 'revoked')

    def test_set_to_draft_resets_counter(self):
        m = self._make_mandate()
        m.consume_for_collection(date(2026, 5, 1))
        m.action_revoke()
        m.action_set_to_draft()
        self.assertEqual(m.state, 'draft')
        self.assertEqual(m.collection_count, 0)
        self.assertFalse(m.last_collection_date)

    # ---- amendment trail ----

    def test_iban_change_on_active_mandate_creates_amendment(self):
        m = self._make_mandate()
        old_iban = m.debtor_iban
        self.assertFalse(m.amendment_ids)
        new_iban = 'DE89370400440532013000'
        m.write({'debtor_iban': new_iban})
        self.assertEqual(len(m.amendment_ids), 1)
        amendment = m.amendment_ids
        self.assertEqual(amendment.amendment_type, 'iban')
        self.assertEqual(amendment.old_value, old_iban)
        self.assertEqual(amendment.new_value, m.debtor_iban)
        # The rendering helper must expose the original IBAN so the next
        # collection can emit OrgnlDbtrAcct under AmdmntInfDtls.
        details = m._eh_latest_amendment_for_rendering()
        self.assertEqual(details.get('original_iban'), old_iban)

    def test_scheme_change_on_active_mandate_creates_amendment(self):
        m = self._make_mandate()
        m.write({'local_instrument': 'B2B'})
        self.assertEqual(len(m.amendment_ids), 1)
        self.assertEqual(m.amendment_ids.amendment_type, 'scheme')
        self.assertEqual(m.amendment_ids.old_value, 'CORE')
        self.assertEqual(m.amendment_ids.new_value, 'B2B')

    def test_debtor_agent_change_creates_amendment_and_renders_smnda(self):
        # A debtor bank move changes the debtor BIC (agent). The scheme
        # requires the next collection to carry OrgnlDbtrAgt/Othr/Id=SMNDA
        # (same mandate, new debtor agent). Before the fix this change was
        # not recorded and the rendering helper never set
        # same_mandate_new_debtor_agent, so SMNDA was dead code.
        m = self._make_mandate(debtor_bic='BNPAFRPP')
        self.assertFalse(m.amendment_ids)
        m.write({'debtor_bic': 'DEUTDEFF'})
        self.assertEqual(len(m.amendment_ids), 1)
        amendment = m.amendment_ids
        self.assertEqual(amendment.amendment_type, 'agent')
        details = m._eh_latest_amendment_for_rendering()
        self.assertTrue(details.get('same_mandate_new_debtor_agent'))

    def test_noop_iban_write_creates_no_amendment(self):
        m = self._make_mandate()
        m.write({'debtor_iban': m.debtor_iban})
        self.assertFalse(m.amendment_ids)

    def test_iban_change_on_draft_mandate_creates_no_amendment(self):
        m = self._make_mandate(state='draft')
        m.write({'debtor_iban': 'DE89370400440532013000'})
        self.assertFalse(m.amendment_ids)
