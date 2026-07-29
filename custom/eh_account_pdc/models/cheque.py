# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Cheque record.

One model handles both directions:

* outgoing: we paid a vendor by cheque (drawn on our bank).
* incoming: we received a customer cheque (drawn on the customer bank).

State machine:

  draft -> registered -> presented -> cleared
                              \\-> bounced -> replaced (chained to a new
                                              cheque) or written off
                              \\-> cancelled

Outgoing cheques consume a serial from the cheque book. Incoming cheques
record the issuer bank, account, and cheque number as captured from the
physical instrument.

IFRS 9 recognition mapping (documented, not enforced by extra postings):

* registered: the entity becomes party to the contractual provisions of
  the instrument (IFRS 9.3.1.1). No journal entry: the underlying trade
  receivable or payable remains the recognised financial asset or
  liability until presentation.
* presented: no derecognition yet. The deposit entry moves the exposure
  from the receivable into the bank suspense account (an internal
  control choice: the cheque is with the bank but cash is not yet
  received, IFRS 9.3.2.4(b) collection is not yet virtually certain).
  The linked invoice receivable is reconciled at this point, so the
  open credit exposure lives on the suspense line until clearance.
* cleared: derecognition on settlement, cash is received and the
  suspense holding transfers to the bank account (IFRS 9.3.2.4(a)).
* bounced: reinstatement. The present entry is reversed at the bank
  dishonour date, restoring the receivable; bank charges levied for the
  dishonour are expensed at the same date.

FX note: suspense holdings of foreign currency cheques are monetary
items under IAS 21.8. The present and clear entries carry the signed
foreign amount in amount_currency so a period end FX revaluation run
(eh_account_fx_revaluation) can retranslate the suspense balance; flag
the bank journal's suspense account as revaluable there (the FX
Revaluation checkbox on the account) since suspense accounts are
asset_current and not auto-flagged.

ECL note: eh_ecl_exposure_lines() exposes the open suspense holdings of
presented incoming cheques with days outstanding, so a loss allowance
engine can include exposures that have left the receivables population.
"""

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhCheque(models.Model):
    _name = 'eh.cheque'
    _description = "Post Dated Cheque"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.cron.batch.mixin',
                'eh.workflow.guard']
    _order = 'value_date desc, id desc'

    # State may only change through the record's own transition actions, never
    # a direct RPC/ORM write. Blocks the "write({'state': 'cleared'}) to skip
    # action_clear and its journal entry" bypass. See eh.workflow.guard.
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        string="Reference",
        required=True, copy=False, default='/',
        tracking=True,
    )
    direction = fields.Selection([
        ('outgoing', "Issued (Payable)"),
        ('incoming', "Received (Receivable)"),
    ], required=True, default='incoming', tracking=True)

    state = fields.Selection([
        ('draft', "Draft"),
        ('registered', "Registered"),
        ('presented', "Presented"),
        ('cleared', "Cleared"),
        ('bounced', "Bounced"),
        ('replaced', "Replaced"),
        ('cancelled', "Cancelled"),
    ], default='draft', required=True, tracking=True)

    cheque_number = fields.Char(
        required=True, copy=False, tracking=True,
        help="Cheque serial number as printed on the cheque.",
    )
    book_id = fields.Many2one(
        'eh.cheque.book', string="Cheque Book",
        domain="[('state', '=', 'in_use'), "
               "('journal_id', '=', journal_id)]",
        tracking=True,
        help="Required for issued cheques. Drives serial allocation.",
    )
    journal_id = fields.Many2one(
        'account.journal', string="Bank Journal",
        required=True, tracking=True,
        domain="[('type', '=', 'bank')]",
        ondelete='restrict',
    )

    partner_id = fields.Many2one(
        'res.partner', string="Counterparty",
        required=True, tracking=True,
    )
    issuer_bank_name = fields.Char(
        string="Issuer Bank",
        help="For incoming cheques: name of the bank the customer cheque "
             "is drawn on.",
    )
    issuer_account = fields.Char(
        string="Issuer Account",
        help="Customer bank account masked or last 4 digits.",
    )

    amount = fields.Monetary(required=True, tracking=True)
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
    )
    amount_in_words = fields.Char(
        compute='_compute_amount_in_words',
        help="Cheque amount spelled out, for the printed cheque face.",
    )

    @api.depends('amount', 'currency_id')
    def _compute_amount_in_words(self):
        for cheque in self:
            currency = cheque.currency_id or cheque.company_id.currency_id
            cheque.amount_in_words = (
                currency.amount_to_text(cheque.amount or 0.0)
                if currency else ''
            )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )

    issue_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Date the cheque was written or received.",
    )
    value_date = fields.Date(
        required=True, tracking=True,
        help="Date on which the cheque becomes presentable.",
    )

    presented_at = fields.Datetime(readonly=True, tracking=True)
    cleared_at = fields.Datetime(readonly=True, tracking=True)
    bounced_at = fields.Datetime(readonly=True, tracking=True)
    cancelled_at = fields.Datetime(readonly=True, tracking=True)

    presented_by_id = fields.Many2one('res.users', readonly=True)
    cleared_by_id = fields.Many2one('res.users', readonly=True)
    bounced_by_id = fields.Many2one('res.users', readonly=True)
    cancelled_by_id = fields.Many2one('res.users', readonly=True)

    bounce_reason_id = fields.Many2one(
        'eh.cheque.bounce.reason', tracking=True,
    )
    bounce_charges = fields.Monetary(
        help="Bank charges levied for the bounce, captured on customer side.",
    )
    bounce_notes = fields.Text()
    dishonour_date = fields.Date(
        readonly=True, copy=False, tracking=True,
        help="Date the bank actually dishonoured the cheque. The bounce "
             "reversal and any bounce charge entry are dated here, not at "
             "the date the operator recorded the bounce.",
    )

    replaced_by_id = fields.Many2one(
        'eh.cheque', string="Replaced By", readonly=True, copy=False,
        help="If this cheque was replaced after a bounce, the new cheque.",
    )
    replaces_id = fields.Many2one(
        'eh.cheque', string="Replaces", readonly=True, copy=False,
        help="The original cheque this record replaces.",
    )

    invoice_id = fields.Many2one(
        'account.move', string="Linked Invoice",
        domain="[('move_type', 'in', ['out_invoice', 'in_invoice', "
               "'out_refund', 'in_refund'])]",
    )
    payment_id = fields.Many2one(
        'account.payment', string="Linked Payment", readonly=True,
    )

    notes = fields.Text()

    present_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        help="Journal entry posted on present (deposit / issue).",
    )
    clear_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        help="Journal entry posted on clearance (suspense to bank).",
    )
    bounce_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        help="Reversal of the present entry, posted on bounce.",
    )
    bounce_charge_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        help="Journal entry expensing the bank charges levied for the "
             "dishonour (debit bounce charges expense, credit bank).",
    )

    is_overdue = fields.Boolean(
        compute='_compute_is_overdue', store=False, search='_search_is_overdue',
    )
    days_outstanding = fields.Integer(
        compute='_compute_days_outstanding', store=False,
        help="Days since the value date while the cheque is still an open "
             "exposure (registered or presented). Feeds ageing and loss "
             "allowance population.",
    )

    _sql_constraints = [
        ('uniq_book_serial', 'unique(book_id, cheque_number)', 'A cheque serial cannot be reused within the same book.'),
    ]

    # ---- compute ----

    @api.depends('value_date', 'state')
    def _compute_is_overdue(self):
        today = fields.Date.context_today(self)
        for cheque in self:
            cheque.is_overdue = bool(
                cheque.value_date
                and cheque.value_date < today
                and cheque.state in ('draft', 'registered', 'presented')
            )

    @api.depends('value_date', 'state')
    def _compute_days_outstanding(self):
        today = fields.Date.context_today(self)
        for cheque in self:
            if cheque.value_date and cheque.state in (
                    'registered', 'presented'):
                cheque.days_outstanding = max(
                    (today - cheque.value_date).days, 0)
            else:
                cheque.days_outstanding = 0

    def _search_is_overdue(self, operator, value):
        if operator not in ('=', '!='):
            raise UserError(_("Unsupported search operator on is_overdue."))
        positive = bool(value) if operator == '=' else not bool(value)
        today = fields.Date.context_today(self)
        domain = [
            ('value_date', '<', today),
            ('state', 'in', ['draft', 'registered', 'presented']),
        ]
        return domain if positive else ['!'] + domain

    # ---- onchange ----

    @api.onchange('direction', 'journal_id')
    def _onchange_direction_journal(self):
        if self.direction == 'incoming':
            self.book_id = False

    @api.onchange('book_id')
    def _onchange_book(self):
        if self.book_id and self.direction == 'outgoing':
            if self.book_id.state == 'in_use' and self.book_id.next_number <= self.book_id.end_number:
                self.cheque_number = str(self.book_id.next_number)

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code('eh.cheque') or '/'
                vals['name'] = seq
        cheques = super().create(vals_list)
        for cheque in cheques:
            if cheque.direction == 'outgoing' and cheque.book_id:
                cheque._validate_serial_against_book()
        return cheques

    def _validate_serial_against_book(self):
        self.ensure_one()
        if not self.cheque_number or not self.cheque_number.isdigit():
            raise UserError(_(
                "Outgoing cheque serial must be numeric within a cheque book.",
            ))
        serial = int(self.cheque_number)
        if not (self.book_id.start_number <= serial <= self.book_id.end_number):
            raise UserError(_(
                "Cheque serial %(serial)s is outside book range "
                "%(start)s-%(end)s.",
                serial=serial,
                start=self.book_id.start_number,
                end=self.book_id.end_number,
            ))

    # ---- transitions ----

    def action_print_cheque(self):
        """Render the physical cheque PDF (payee, amount, words, date,
        serial) for printing onto pre-numbered cheque stock."""
        self.ensure_one()
        return self.env.ref(
            'eh_account_pdc.action_report_cheque_print'
        ).report_action(self, config=False)

    def action_register(self):
        self = self._eh_workflow_action()
        for cheque in self:
            if cheque.state != 'draft':
                raise UserError(_(
                    "Only draft cheques can be registered.",
                ))
            if cheque.direction == 'outgoing':
                if not cheque.book_id:
                    raise UserError(_(
                        "Issued cheques require a cheque book.",
                    ))
                # Lock the book row before reading next_number so two
                # concurrent registrations on the same book serialise
                # rather than racing. The book unique-constraint on
                # (book_id, cheque_number) is the safety net; the lock
                # turns a race into a queue.
                cheque.book_id._lock_for_update()
                cheque._validate_serial_against_book()
                book = cheque.book_id
                if book.state != 'in_use':
                    raise UserError(_(
                        "Cheque book %s is not active.", book.name,
                    ))
                serial = int(cheque.cheque_number)
                if serial != book.next_number:
                    raise UserError(_(
                        "Cheque serial %(serial)s does not match the "
                        "next available serial %(next)s on book "
                        "%(book)s. Refresh and try again, or close gaps "
                        "in the book first.",
                        serial=serial,
                        next=book.next_number,
                        book=book.name,
                    ))
                book.next_number = serial + 1
                if book.next_number > book.end_number:
                    book.state = 'exhausted'
            cheque.state = 'registered'

    def action_present(self):
        self = self._eh_workflow_action()
        today = fields.Date.context_today(self)
        for cheque in self:
            if cheque.state not in ('registered',):
                raise UserError(_(
                    "Only registered cheques can be presented to the bank.",
                ))
            # A post-dated cheque is not negotiable until the value date.
            # Real banks refuse to deposit it; presenting in Odoo before
            # the value date would post the suspense JE in advance and
            # mismatch when the bank actually clears later. Block early
            # so finance follows the calendar. Use the eh_pdc_force_early
            # context key if you genuinely need to backfill (data import,
            # opening balance migrations).
            if cheque.value_date and cheque.value_date > today and \
                    not self.env.context.get('eh_pdc_force_early'):
                raise UserError(_(
                    "Cheque %(name)s cannot be presented before its value "
                    "date %(value_date)s. Today is %(today)s.",
                    name=cheque.cheque_number or cheque.id,
                    value_date=fields.Date.to_string(cheque.value_date),
                    today=fields.Date.to_string(today),
                ))
            move = cheque._post_pdc_move('present')
            cheque.write({
                'state': 'presented',
                'presented_at': fields.Datetime.now(),
                'presented_by_id': self.env.user.id,
                'present_move_id': move.id,
            })

    def action_clear(self):
        self = self._eh_workflow_action()
        for cheque in self:
            if cheque.state != 'presented':
                raise UserError(_(
                    "Only presented cheques can be marked as cleared.",
                ))
            move = cheque._post_pdc_move('clear')
            cheque.write({
                'state': 'cleared',
                'cleared_at': fields.Datetime.now(),
                'cleared_by_id': self.env.user.id,
                'clear_move_id': move.id,
            })

    def action_cancel(self):
        self = self._eh_workflow_action()
        for cheque in self:
            if cheque.state in ('cleared', 'replaced'):
                raise UserError(_(
                    "Cleared or replaced cheques cannot be cancelled.",
                ))
            # A presented cheque carries a posted presentation entry and, when
            # linked, a reconciled invoice marked paid. A bare cancel would
            # strand that entry and leave the invoice reading paid though
            # nothing was collected. Force the bounce path, which reverses the
            # entry and re-opens the invoice, before the cheque can be voided.
            if cheque.state == 'presented' and cheque.present_move_id \
                    and cheque.present_move_id.state == 'posted' \
                    and not cheque.clear_move_id:
                raise UserError(_(
                    "Cheque %s has a posted presentation entry. Bounce it "
                    "first (this reverses the entry and re-opens the linked "
                    "invoice); a cancelled cheque must not leave a live "
                    "journal entry behind.", cheque.name))
            cheque.write({
                'state': 'cancelled',
                'cancelled_at': fields.Datetime.now(),
                'cancelled_by_id': self.env.user.id,
            })

    def action_open_bounce_wizard(self):
        self.ensure_one()
        if self.state != 'presented':
            raise UserError(_(
                "Only presented cheques can be bounced.",
            ))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.cheque.bounce.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_cheque_id': self.id},
        }

    def action_open_replace_wizard(self):
        self.ensure_one()
        if self.state != 'bounced':
            raise UserError(_(
                "Only bounced cheques can be replaced.",
            ))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.cheque.replace.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_cheque_id': self.id},
        }

    def _mark_bounced(self, reason, charges=0.0, notes=None,
                      dishonour_date=None, force_current_date=False):
        """Bounce the cheque, dating the accounting at the bank dishonour.

        With ``dishonour_date`` given (the wizard always supplies it), the
        reversal of the present entry and any bounce charge entry are dated
        at the dishonour date, validated against the presentation date and
        the accounting lock dates. Without it (legacy/programmatic callers)
        the reversal keeps the original behaviour of dating back into the
        present entry's period.
        """
        self = self._eh_workflow_action()
        for cheque in self:
            acct_date = None
            if dishonour_date:
                acct_date = cheque._pdc_dishonour_accounting_date(
                    dishonour_date, force_current_date=force_current_date,
                )
            reversal = False
            if cheque.present_move_id and cheque.present_move_id.state == 'posted':
                reversal = cheque._post_pdc_move(
                    'bounce', reverse_of=cheque.present_move_id,
                    date_override=acct_date,
                )
                # Re-open the linked invoice. Presentation reconciled the
                # present move's receivable leg against the invoice, marking
                # it paid; a bounce collected nothing, so that reconciliation
                # must be broken (invoice residual and payment_state revert)
                # and the cheque's own two receivable legs netted so only the
                # invoice is left open.
                if cheque.invoice_id:
                    cheque._pdc_reopen_invoice_on_bounce(reversal)
            charge_move = False
            if charges:
                charge_move = cheque._post_bounce_charge_move(
                    charges,
                    acct_date or fields.Date.context_today(self),
                )
            cheque.write({
                'state': 'bounced',
                'bounced_at': fields.Datetime.now(),
                'bounced_by_id': self.env.user.id,
                'bounce_reason_id': reason.id if reason else False,
                'bounce_charges': charges or 0.0,
                'bounce_notes': notes or False,
                'bounce_move_id': reversal.id if reversal else False,
                'bounce_charge_move_id': charge_move.id if charge_move else False,
                'dishonour_date': dishonour_date or False,
            })
            cheque.message_post(
                body=_("Cheque bounced: %s",
                       reason.name if reason else _("(no reason)")),
            )

    # ---- accounting helpers ----

    def _pdc_resolve_accounts(self):
        """Return (suspense_account, partner_account, bank_account) for posting.

        Hard fails if any account is unresolvable so we never post a
        broken or one-sided entry.
        """
        self.ensure_one()
        journal = self.journal_id
        suspense = journal.suspense_account_id
        if not suspense:
            raise UserError(_(
                "Bank journal %s has no Suspense Account configured. "
                "Set it on the journal before processing PDC accounting.",
                journal.display_name,
            ))
        bank = journal.default_account_id
        if not bank:
            raise UserError(_(
                "Bank journal %s has no Default Account configured.",
                journal.display_name,
            ))
        if self.direction == 'incoming':
            partner_account = self.partner_id.with_company(
                self.company_id,
            ).property_account_receivable_id
        else:
            partner_account = self.partner_id.with_company(
                self.company_id,
            ).property_account_payable_id
        if not partner_account:
            raise UserError(_(
                "Partner %s has no %s account configured for company %s.",
                self.partner_id.display_name,
                'receivable' if self.direction == 'incoming' else 'payable',
                self.company_id.display_name,
            ))
        return suspense, partner_account, bank

    def _pdc_lock_date(self):
        """Return the strictest (latest) accounting lock date that applies to
        PDC postings for this cheque's company, or False when none is set.

        Field-presence guarded so this stays cross-version safe: Odoo 16/17/18
        expose period_lock_date; fiscalyear_lock_date and hard_lock_date exist
        on 19 too. A missing field is simply skipped. Postings dated on or
        before this date must be refused; a locked period is closed.
        """
        self.ensure_one()
        company = self.company_id
        candidates = (
            'fiscalyear_lock_date',
            'period_lock_date',
            'hard_lock_date',
        )
        lock = False
        for fname in candidates:
            if fname not in company._fields:
                continue
            value = company[fname]
            if value and (not lock or value > lock):
                lock = value
        return lock

    def _pdc_check_lock_date(self, move_date):
        """Refuse a PDC posting whose date falls into a locked period.

        Opt-in-safe: only blocks when a lock date is actually set on the
        company; otherwise prior behaviour is preserved so existing tests
        and un-locked databases are unaffected.
        """
        self.ensure_one()
        lock = self._pdc_lock_date()
        if lock and move_date and move_date <= lock:
            raise UserError(_(
                "Cannot post PDC entry for cheque %(name)s dated "
                "%(date)s: the period is locked up to %(lock)s. "
                "Change the accounting date to an open period.",
                name=self.cheque_number or self.name,
                date=fields.Date.to_string(move_date),
                lock=fields.Date.to_string(lock),
            ))

    def _pdc_bounce_reversal_date(self, reverse_of):
        """Date for a bounce reversal.

        Reverse into the original present entry's period so the bounce lands
        in the same accounting period as the deposit, not today. If that
        period is locked, fall back to the earliest open date (the day after
        the lock date) rather than posting into a closed period.
        """
        self.ensure_one()
        origin_date = reverse_of.date or fields.Date.context_today(self)
        lock = self._pdc_lock_date()
        if lock and origin_date <= lock:
            return lock + timedelta(days=1)
        return origin_date

    def _pdc_dishonour_accounting_date(self, dishonour_date,
                                       force_current_date=False):
        """Validate and resolve the accounting date for a dishonour.

        * The dishonour cannot precede the presentation date: the bank
          cannot dishonour a cheque it has not yet received.
        * Backdating is warning-free only within an open period. When the
          dishonour date falls into a locked period the caller must set
          force_current_date, which books the reversal at today instead
          (the standard current-period alternative when the original
          period is closed).
        """
        self.ensure_one()
        present_date = self.present_move_id.date or (
            self.presented_at and self.presented_at.date())
        if present_date and dishonour_date < present_date:
            raise UserError(_(
                "The dishonour date %(dishonour)s cannot precede the "
                "presentation date %(present)s of cheque %(name)s.",
                dishonour=fields.Date.to_string(dishonour_date),
                present=fields.Date.to_string(present_date),
                name=self.cheque_number or self.name,
            ))
        lock = self._pdc_lock_date()
        if lock and dishonour_date <= lock:
            if not force_current_date:
                raise UserError(_(
                    "The dishonour date %(dishonour)s falls in a locked "
                    "period (locked up to %(lock)s). Enable 'Post at "
                    "Current Date' to book the bounce reversal in the "
                    "current period instead, and disclose the dishonour "
                    "date on the cheque record.",
                    dishonour=fields.Date.to_string(dishonour_date),
                    lock=fields.Date.to_string(lock),
                ))
            today = fields.Date.context_today(self)
            self._pdc_check_lock_date(today)
            return today
        return dishonour_date

    def _pdc_bounce_charge_account(self):
        """Expense account for bounce charges: journal first, company
        fallback. Empty means charges stay informational (no entry)."""
        self.ensure_one()
        return (
            self.journal_id.eh_pdc_bounce_charge_account_id
            or self.company_id.eh_pdc_bounce_charge_account_id
        )

    def _post_bounce_charge_move(self, charges, acct_date):
        """Post Dr bounce charges expense / Cr bank at the dishonour date.

        Returns the posted move, or False when no bounce charge expense
        account is configured; in that case the amount stays informational
        on the cheque (pre-existing behaviour) and a chatter note flags the
        missing configuration so the omission is visible, not silent.
        """
        self.ensure_one()
        expense_acc = self._pdc_bounce_charge_account()
        if not expense_acc:
            self.message_post(body=_(
                "Bounce charges %(amount)s %(currency)s recorded without a "
                "journal entry: no bounce charges expense account is "
                "configured on the bank journal or the company.",
                amount=charges,
                currency=self.currency_id.name,
            ))
            return False
        bank_acc = self.journal_id.default_account_id
        if not bank_acc:
            raise UserError(_(
                "Bank journal %s has no Default Account configured.",
                self.journal_id.display_name,
            ))
        self._pdc_check_lock_date(acct_date)
        currency = self.currency_id
        company_currency = self.company_id.currency_id
        if currency and currency != company_currency:
            company_amount = currency._convert(
                charges, company_currency, self.company_id, acct_date,
            )
        else:
            company_amount = charges
        ref = _("PDC bounce charges: %s") % self.name
        move = self.env['account.move'].sudo().create({
            'journal_id': self.journal_id.id,
            'date': acct_date,
            'ref': ref,
            'company_id': self.company_id.id,
            'line_ids': [
                (0, 0, {
                    'name': ref,
                    'account_id': expense_acc.id,
                    'partner_id': self.partner_id.id,
                    'currency_id': currency.id,
                    'amount_currency': charges,
                    'debit': company_amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': ref,
                    'account_id': bank_acc.id,
                    'partner_id': self.partner_id.id,
                    'currency_id': currency.id,
                    'amount_currency': -charges,
                    'debit': 0.0,
                    'credit': company_amount,
                }),
            ],
        })
        move.action_post()
        return move

    def _post_pdc_move(self, transition, reverse_of=False, date_override=None):
        """Post the journal entry for a PDC state transition.

        transitions: 'present', 'clear', 'bounce'
        For 'bounce' we post a reversal of `reverse_of` (the present move),
        dated at `date_override` (the validated dishonour date) when given,
        otherwise back into the present entry's period (legacy behaviour).
        """
        self.ensure_one()
        suspense, partner_acc, bank_acc = self._pdc_resolve_accounts()
        amount = self.amount
        currency = self.currency_id
        ref = _("PDC %s: %s") % (transition, self.name)
        # Pick journal: use bank journal for present/clear; for bounce use
        # the same journal as the original present move.
        if transition == 'bounce' and reverse_of:
            reversal_date = (
                date_override or self._pdc_bounce_reversal_date(reverse_of))
            # Guard: never reverse into a locked period. When the origin
            # period is closed we roll forward to the earliest open date;
            # this asserts the rolled date is genuinely open. A validated
            # date_override was already lock-checked, cheap to re-assert.
            self._pdc_check_lock_date(reversal_date)
            reversal = reverse_of._reverse_moves(
                [{'date': reversal_date, 'ref': ref}],
                cancel=False,
            )
            # _reverse_moves(cancel=False) returns a draft entry; the
            # reinstatement of the receivable/payable must be recognised
            # at the dishonour date, so post it. Sudo for parity with the
            # present/clear moves, which are created and posted sudo.
            if reversal.state == 'draft':
                reversal.sudo().action_post()
            return reversal
        if self.direction == 'incoming':
            if transition == 'present':
                # DR suspense, CR receivable
                debit_acc, credit_acc = suspense, partner_acc
            else:  # clear
                # DR bank, CR suspense
                debit_acc, credit_acc = bank_acc, suspense
        else:  # outgoing
            if transition == 'present':
                # DR payable, CR suspense
                debit_acc, credit_acc = partner_acc, suspense
            else:  # clear
                # DR suspense, CR bank
                debit_acc, credit_acc = suspense, bank_acc
        move_date = fields.Date.context_today(self)
        # Refuse present/clear postings into a locked period.
        self._pdc_check_lock_date(move_date)
        company_currency = self.company_id.currency_id
        # debit/credit are always in the company currency; amount_currency
        # carries the cheque-currency amount. For a foreign-currency cheque,
        # convert the amount to the company currency for the balance and keep
        # the foreign amount (signed) in amount_currency. Without this, FX
        # cheques posted with amount_currency 0 and an unconverted balance,
        # breaking reconciliation and the revaluation cron downstream.
        if currency and currency != company_currency:
            company_amount = currency._convert(
                amount, company_currency, self.company_id, move_date,
            )
        else:
            company_amount = amount
        move_vals = {
            'journal_id': self.journal_id.id,
            'date': move_date,
            'ref': ref,
            'company_id': self.company_id.id,
            'line_ids': [
                (0, 0, {
                    'name': ref,
                    'account_id': debit_acc.id,
                    'partner_id': self.partner_id.id,
                    'currency_id': currency.id,
                    'amount_currency': amount,
                    'debit': company_amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': ref,
                    'account_id': credit_acc.id,
                    'partner_id': self.partner_id.id,
                    'currency_id': currency.id,
                    'amount_currency': -amount,
                    'debit': 0.0,
                    'credit': company_amount,
                }),
            ],
        }
        move = self.env['account.move'].sudo().create(move_vals)
        move.action_post()
        if transition == 'present' and self.invoice_id:
            self._reconcile_present_with_invoice(move, partner_acc)
        return move

    def _reconcile_present_with_invoice(self, move, partner_acc):
        """Reconcile the present move's partner leg against the linked
        invoice's open receivable/payable line so the source invoice is
        marked paid when the cheque is deposited.

        Without this the cheque suspense/AR leg and the invoice both sit as
        separate open items on the same partner for the cheque's whole
        lifecycle, breaking open-item integrity (IFRS 9). We reconcile only
        the two lines on the shared partner account so the entry stays
        balanced and the invoice's amount_residual falls to zero.
        """
        self.ensure_one()
        invoice = self.invoice_id
        if invoice.state != 'posted':
            return
        # The invoice's open receivable/payable line and the present move's
        # partner leg must share the same reconcilable account; only then can
        # the framework match them. AR/AP accounts are reconcilable by type.
        if not partner_acc.reconcile:
            return
        invoice_lines = invoice.line_ids.filtered(
            lambda line: line.account_id == partner_acc
            and not line.reconciled
            and line.amount_residual != 0.0
        )
        move_lines = move.line_ids.filtered(
            lambda line: line.account_id == partner_acc
            and not line.reconciled
        )
        to_reconcile = invoice_lines | move_lines
        if len(invoice_lines) and len(move_lines):
            to_reconcile.reconcile()

    def _pdc_reopen_invoice_on_bounce(self, reversal):
        """Undo the presentation reconciliation so the linked invoice
        re-opens, then net the present and reversal receivable legs.

        On presentation the present move's receivable leg was reconciled with
        the invoice's open receivable line (invoice -> paid). A bounce reverses
        the presentation but does not, by itself, break that reconciliation, so
        without this the invoice keeps reading paid while an orphan receivable
        from the reversal floats free. Here we: (1) break the present<->invoice
        match, which returns the invoice residual and reverts payment_state,
        then (2) reconcile the present leg against the reversal leg (equal and
        opposite on the same partner account) so the cheque nets flat and only
        the invoice is left open.
        """
        self.ensure_one()
        if not reversal or not self.present_move_id or not self.invoice_id:
            return
        _suspense, partner_acc, _bank = self._pdc_resolve_accounts()
        if not partner_acc.reconcile:
            return
        present_leg = self.present_move_id.line_ids.filtered(
            lambda l: l.account_id == partner_acc)
        # Break the invoice reconciliation (re-opens invoice + present leg).
        present_leg.filtered('reconciled').remove_move_reconcile()
        # Net the cheque's own two receivable legs so the invoice's own line
        # is the only open item left on the partner account.
        rev_leg = reversal.line_ids.filtered(
            lambda l: l.account_id == partner_acc and not l.reconciled)
        net = (present_leg | rev_leg).filtered(
            lambda l: not l.reconciled and l.account_id.reconcile)
        if len(net) >= 2:
            net.reconcile()

    # ---- loss allowance (ECL) provider hook ----

    def eh_ecl_exposure_lines(self, reporting_date=None):
        """Open credit exposures held in bank suspense for these cheques.

        Provider hook for a loss-allowance engine (eh_account_ecl populate
        step). Rationale: at present time the linked invoice receivable is
        reconciled (see _reconcile_present_with_invoice), so a presented
        incoming cheque's exposure leaves the receivables population and
        sits on the journal suspense account until clearance. An ECL run
        that only ages open receivables would silently drop it; this hook
        returns those suspense holdings so they can be added back.

        Scope: incoming presented cheques only. Registered cheques are
        excluded on purpose: their receivable is still open and is already
        captured by the standard populate-from-receivables sweep, so
        including them here would double count. Outgoing cheques are
        liabilities, out of ECL scope.

        Returns a list of dicts, one per open suspense line:
        cheque_id, move_line_id, partner_id, account_id, currency_id,
        amount_residual (company currency), amount_residual_currency
        (cheque currency), due_date (value date), days_outstanding
        (reporting date minus value date, floored at zero).
        """
        reporting_date = reporting_date or fields.Date.context_today(self)
        exposures = []
        for cheque in self:
            if cheque.direction != 'incoming':
                continue
            if cheque.state != 'presented':
                continue
            move = cheque.present_move_id
            if not move or move.state != 'posted':
                continue
            suspense = cheque.journal_id.suspense_account_id
            lines = move.line_ids.filtered(
                lambda line: line.account_id == suspense and line.debit > 0.0
            )
            for line in lines:
                if line.account_id.reconcile:
                    if line.reconciled:
                        continue
                    residual = line.amount_residual
                    residual_currency = line.amount_residual_currency
                    if not residual:
                        continue
                else:
                    # Non reconcilable suspense: residual tracking is not
                    # available, the full posted holding is the exposure.
                    residual = line.balance
                    residual_currency = line.amount_currency

                due = cheque.value_date or move.date
                days = (reporting_date - due).days
                exposures.append({
                    'cheque_id': cheque.id,
                    'move_line_id': line.id,
                    'partner_id': cheque.partner_id.id,
                    'account_id': line.account_id.id,
                    'currency_id': (line.currency_id or cheque.currency_id).id,
                    'amount_residual': residual,
                    'amount_residual_currency': residual_currency,
                    'due_date': due,
                    'days_outstanding': max(days, 0),
                })
        return exposures

    def _mark_replaced_by(self, new_cheque):
        self.ensure_one()
        self = self._eh_workflow_action()
        self.write({
            'state': 'replaced',
            'replaced_by_id': new_cheque.id,
        })
        new_cheque.write({'replaces_id': self.id})

    # ---- cron ----

    @api.model
    def _cron_auto_present(self, batch_size=200):
        """Move registered cheques to presented when value_date arrives.

        Per record try/except so a single failure does not stop the batch.
        Audit trail: each transition is tracked through the standard
        message_post chain via state tracking.
        """
        today = fields.Date.context_today(self)
        domain = [
            ('state', '=', 'registered'),
            ('value_date', '<=', today),
        ]
        cheques = self.search(domain, limit=batch_size)
        # Per record savepoint via the shared batch mixin: without it, a
        # failure mid batch leaves the transaction aborted and every
        # subsequent cheque in the run also fails. The "single failure
        # does not stop the batch" promise only holds with the savepoint.
        self._eh_for_each_savepoint(
            cheques,
            lambda cheque: cheque.action_present(),
            log_label="Auto presentation",
        )

    # ---- helpers ----

    @api.depends(
        'cheque_number', 'partner_id', 'partner_id.display_name',
        'amount', 'currency_id', 'currency_id.name',
    )
    def _compute_display_name(self):
        for cheque in self:
            partner_label = (
                cheque.partner_id.display_name
                if cheque.partner_id else ''
            )
            label = "%s / %s" % (cheque.cheque_number or '', partner_label)
            if cheque.amount and cheque.currency_id:
                label = "%s (%s)" % (label, cheque.currency_id.name)
            cheque.display_name = label
