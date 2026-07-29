# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
The shipped GST labels (G1 total sales, 1A GST on sales, 1B GST on
purchases) must auto-compute non-zero figures on a fresh install for a
company that carries standard AU GST taxes, and those figures must tie to
the posted tax. This proves _aggregate is reachable in the shipped config
rather than being unreachable dead code that returns 0.0 for every label.
"""

from datetime import date

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


class BasGstComputeTest(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A tax group is required and is not provisioned on a demo-less
        # company; create one search-first.
        # account.tax.group.company_id is 17+ only (absent on 16), so gate
        # the company scoping on the field being present.
        Group = cls.env['account.tax.group']
        group_has_company = 'company_id' in Group._fields
        group = Group.search(
            [('company_id', '=', cls.company.id)] if group_has_company else [],
            limit=1,
        )
        if not group:
            group_vals = {'name': 'GST'}
            if group_has_company:
                group_vals['company_id'] = cls.company.id
            group = Group.create(group_vals)
        # Standard AU-style GST taxes: 10% on sales and 10% on purchases.
        # Minimal create leaves the repartition lines to their computed
        # defaults, which is what a real GST tax carries.
        # The tax country must match the company fiscal country, else posting
        # is refused. Resolve the company's fiscal country (setting it to
        # Australia when a demo-less company left it blank).
        au = cls.env.ref('base.au')
        fiscal_country = cls.company.account_fiscal_country_id
        if not fiscal_country:
            if not cls.company.country_id:
                cls.company.sudo().country_id = au.id
            fiscal_country = cls.company.account_fiscal_country_id or au
        cls.tax_sale = cls.env['account.tax'].create({
            'name': 'GST 10% Sale',
            'amount': 10.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
            'tax_group_id': group.id,
            'country_id': fiscal_country.id,
        })
        cls.tax_purchase = cls.env['account.tax'].create({
            'name': 'GST 10% Purchase',
            'amount': 10.0,
            'amount_type': 'percent',
            'type_tax_use': 'purchase',
            'company_id': cls.company.id,
            'tax_group_id': group.id,
            'country_id': fiscal_country.id,
        })
        # Pin the GST tax journal items to a dedicated control account on
        # every tax repartition line (invoice and refund). The GST control
        # reconciliation walks these accounts, so it needs them configured;
        # without an account the tax line lands on a default and the recon
        # sees no movement to net against the labels.
        cls.gst_control = cls._ensure_account(
            cls.env, '2200', 'GST Control', 'liability_current',
        )
        for tax in (cls.tax_sale, cls.tax_purchase):
            tax_rep = (
                tax.invoice_repartition_line_ids
                + tax.refund_repartition_line_ids
            ).filtered(lambda r: r.repartition_type == 'tax')
            tax_rep.account_id = cls.gst_control.id
        # Payment registration needs a bank journal; a demo-less company has
        # none, so provision one search-first for the cash-basis tests.
        cls.bank_journal = cls._ensure_journal(
            cls.env, cls.company, 'bank', 'BNK', 'Bank',
            default_account=cls.account_cash,
        )
        # Outstanding receipt/payment account. On Odoo 19 the outstanding
        # account is resolved from the journal's payment method lines rather
        # than a company field, so pin it on both the inbound and outbound
        # method lines (reconcilable, as the framework requires).
        outstanding = cls._ensure_account(
            cls.env, '1097', 'BAS Outstanding', 'asset_current',
        )
        if not outstanding.reconcile:
            outstanding.sudo().reconcile = True
        method_lines = (
            cls.bank_journal.inbound_payment_method_line_ids
            + cls.bank_journal.outbound_payment_method_line_ids
        )
        for line in method_lines:
            if not line.payment_account_id:
                line.payment_account_id = outstanding.id

    def _post_sale(self, amount):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-05-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Taxable sale',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, self.tax_sale.ids)],
            })],
        })
        move.action_post()
        return move

    def _post_bill(self, amount):
        move = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_b.id,
            'invoice_date': '2026-05-20',
            'invoice_line_ids': [(0, 0, {
                'name': 'Taxable acquisition',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.account_expense.id,
                'tax_ids': [(6, 0, self.tax_purchase.ids)],
            })],
        })
        move.action_post()
        return move

    def _line(self, run, code):
        return self.env['eh.bas.run.line'].search([
            ('run_id', '=', run.id),
            ('label_id.code', '=', code),
        ], limit=1)

    def test_g1_1a_1b_auto_compute_and_tie_to_posted_tax(self):
        # Sale of 1000 (+100 GST) and a purchase of 400 (+40 GST) inside
        # Q4 FY2025-26 (Apr-Jun 2026).
        self._post_sale(1000.0)
        self._post_bill(400.0)

        run = self.env['eh.bas.run'].create({
            'company_id': self.company.id,
            'fy_label': '2025-26',
            'quarter': 'q4',
        })
        self.assertEqual(run.date_from, date(2026, 4, 1))
        self.assertEqual(run.date_to, date(2026, 6, 30))
        run.action_compute()
        self.assertEqual(run.state, 'computed')

        line_g1 = self._line(run, 'G1')
        line_1a = self._line(run, '1A')
        line_1b = self._line(run, '1B')

        # Every shipped GST label must produce a non-zero figure now, not the
        # blanket 0.0 the manual-only seed returned.
        self.assertNotEqual(line_1a.amount, 0.0, "1A did not auto-compute")
        self.assertNotEqual(line_1b.amount, 0.0, "1B did not auto-compute")
        self.assertNotEqual(line_g1.amount, 0.0, "G1 did not auto-compute")

        # Figures tie to the posted tax exactly.
        self.assertAlmostEqual(line_1a.amount, 100.0, places=2)
        self.assertAlmostEqual(line_1b.amount, 40.0, places=2)
        # G1 = tax-inclusive sales = 1000 base + 100 GST.
        self.assertAlmostEqual(line_g1.amount, 1100.0, places=2)

    def _credit_note_for_sale(self, invoice, amount):
        # A refund/credit note posts the tax reversal on the opposite ledger
        # side of the original sale. It must reduce 1A and G1, not be ignored.
        move = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-05-25',
            'invoice_line_ids': [(0, 0, {
                'name': 'Sale credit note',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, self.tax_sale.ids)],
            })],
        })
        move.action_post()
        return move

    def test_credit_note_nets_1a_g1_and_no_false_recon_alarm(self):
        # A sale of 1000 (+100 GST) fully credited by a 400 (+40 GST) credit
        # note in the same quarter must net to 1A = 60 and G1 = 660, not the
        # gross 100 / 1100 a one-sided sum would leave. Without netting both
        # ledger sides the labels overstate the return and the GST control
        # reconciliation (which nets credit-debit) fires a false alarm.
        sale = self._post_sale(1000.0)
        self._credit_note_for_sale(sale, 400.0)

        run = self.env['eh.bas.run'].create({
            'company_id': self.company.id,
            'fy_label': '2025-26',
            'quarter': 'q4',
        })
        run.action_compute()

        line_1a = self._line(run, '1A')
        line_g1 = self._line(run, 'G1')
        # 100 GST collected minus 40 GST reversed = 60.
        self.assertAlmostEqual(line_1a.amount, 60.0, places=2)
        # (1000 base - 400 base) + (100 GST - 40 GST) = 600 + 60 = 660.
        self.assertAlmostEqual(line_g1.amount, 660.0, places=2)

        # The GST control reconciliation nets credit-debit on the control
        # accounts; with the labels now netted too, the collected movement
        # must tie to 1A and raise no false variance on these clean books.
        report = run.compute_gst_control_reconciliation()
        self.assertAlmostEqual(report['label_1a'], 60.0, places=2)
        self.assertAlmostEqual(report['collected_diff'], 0.0, places=2)

    def _register_payment(self, invoice, date_str, amount=None):
        """Register a customer/vendor payment against an invoice.

        Uses account.payment.register, the cross-version wizard. Pins the
        payment date so the reconciliation max_date lands in a known
        period. When amount is given a partial payment is registered.
        """
        ctx = {
            'active_model': 'account.move',
            'active_ids': invoice.ids,
        }
        wiz_vals = {
            'payment_date': date_str,
            'journal_id': self.bank_journal.id,
        }
        if amount is not None:
            wiz_vals['amount'] = amount
        wizard = self.env['account.payment.register'].with_context(
            **ctx
        ).create(wiz_vals)
        wizard.action_create_payments()

    def test_cash_basis_recognises_gst_on_payment_date_not_accrual(self):
        # Sale of 1000 (+100 GST) invoiced 2026-05-15 (Q4 FY2025-26). Under
        # cash basis the GST is recognised on the PAYMENT date, not the
        # invoice date. Paying it in Q1 FY2026-27 (Jul-Sep 2026) must:
        #   * exclude the 100 GST from the Q4 cash-basis BAS (unpaid then),
        #   * include it in the Q1 cash-basis BAS (paid then),
        # while the accrual BAS still recognises it in Q4. This fails
        # against the old code, which hard-refused any cash-basis compute.
        invoice = self._post_sale(1000.0)

        # Cash-basis Q4 run: invoice posted but unpaid -> 1A must be zero.
        run_q4_cash = self.env['eh.bas.run'].create({
            'company_id': self.company.id,
            'fy_label': '2025-26',
            'quarter': 'q4',
            'reporting_basis': 'cash',
        })
        run_q4_cash.action_compute()
        self.assertEqual(run_q4_cash.state, 'computed')
        self.assertAlmostEqual(
            self._line(run_q4_cash, '1A').amount, 0.0, places=2,
            msg="Unpaid invoice must not appear on the cash-basis BAS",
        )
        self.assertAlmostEqual(
            self._line(run_q4_cash, 'G1').amount, 0.0, places=2,
        )

        # Pay the invoice in Q1 FY2026-27 (2026-08-15).
        self._register_payment(invoice, '2026-08-15')

        run_q1_cash = self.env['eh.bas.run'].create({
            'company_id': self.company.id,
            'fy_label': '2026-27',
            'quarter': 'q1',
            'reporting_basis': 'cash',
        })
        run_q1_cash.action_compute()
        self.assertAlmostEqual(
            self._line(run_q1_cash, '1A').amount, 100.0, places=2,
            msg="Paid invoice must appear on the cash-basis BAS of the "
                "payment period",
        )
        self.assertAlmostEqual(
            self._line(run_q1_cash, 'G1').amount, 1100.0, places=2,
        )

        # Re-running the earlier Q4 cash run still shows zero: the payment
        # belongs to Q1, not Q4.
        run_q4_cash.action_reset_draft()
        run_q4_cash.action_compute()
        self.assertAlmostEqual(
            self._line(run_q4_cash, '1A').amount, 0.0, places=2,
        )

    def test_cash_basis_credit_note_reduces_paid_gst(self):
        # Sale of 1000 (+100 GST) paid in full in Q1 FY2026-27, then a
        # credit note of 400 (+40 GST) issued and settled (refund) in the
        # same Q1. Cash basis for Q1 must net to 1A = 100 - 40 = 60.
        invoice = self._post_sale(1000.0)
        self._register_payment(invoice, '2026-08-15')

        credit = self._credit_note_for_sale(invoice, 400.0)
        # Move the credit note into the payment quarter and pay/refund it
        # there so its GST reversal is recognised in Q1 on a cash basis.
        # (Credit note invoice_date was 2026-05-25 for the accrual test; the
        # cash recognition date is the settlement date, set here to Q1.)
        self._register_payment(credit, '2026-08-20')

        run_q1_cash = self.env['eh.bas.run'].create({
            'company_id': self.company.id,
            'fy_label': '2026-27',
            'quarter': 'q1',
            'reporting_basis': 'cash',
        })
        run_q1_cash.action_compute()
        self.assertAlmostEqual(
            self._line(run_q1_cash, '1A').amount, 60.0, places=2,
            msg="Cash-basis credit note settled in the period must reduce 1A",
        )
        # G1 = (1000 - 400) base + (100 - 40) GST = 660.
        self.assertAlmostEqual(
            self._line(run_q1_cash, 'G1').amount, 660.0, places=2,
        )

    def test_no_gst_activity_is_zero_not_crash(self):
        # A period with no posted taxable activity must compute cleanly to
        # zero rather than raise; the auto-map path must be reachable but
        # tolerate an empty ledger.
        run = self.env['eh.bas.run'].create({
            'company_id': self.company.id,
            'fy_label': '2024-25',
            'quarter': 'q1',
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        self.assertEqual(self._line(run, '1A').amount, 0.0)
        self.assertEqual(self._line(run, '1B').amount, 0.0)
