# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Cheque book registers.

A cheque book belongs to a bank journal and defines a serial range. The
book is the source of truth for the next available cheque number for
issued (outgoing) cheques. Received cheques do not consume book serials,
they just reference the customer cheque number as captured.

Multiple books can exist per journal (e.g. one finished, a new one in use).
The active book for a journal is the one with state 'in_use'.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL


class EhChequeBook(models.Model):
    _name = 'eh.cheque.book'
    _description = "Cheque Book"
    _order = 'state, journal_id, start_number'
    _inherit = ['mail.thread', 'eh.workflow.guard']

    # State may only change through the book's own actions (activate/close/
    # exhaust), never a direct RPC/ORM write. See eh.workflow.guard.
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        required=True, tracking=True,
        help="Internal label for this cheque book. Often the bank name "
             "and serial range.",
    )
    journal_id = fields.Many2one(
        'account.journal', string="Bank Journal",
        required=True, tracking=True,
        domain="[('type', '=', 'bank')]",
        ondelete='restrict',
    )
    start_number = fields.Integer(required=True, tracking=True)
    end_number = fields.Integer(required=True, tracking=True)
    next_number = fields.Integer(
        required=True, tracking=True,
        help="Next available cheque serial. Updated automatically when a "
             "cheque is registered against this book.",
    )
    state = fields.Selection([
        ('draft', "Draft"),
        ('in_use', "In Use"),
        ('exhausted', "Exhausted"),
        ('closed', "Closed"),
    ], default='draft', required=True, tracking=True)
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )
    notes = fields.Text()

    cheque_ids = fields.One2many('eh.cheque', 'book_id')
    cheque_count = fields.Integer(
        compute='_compute_cheque_count', store=False,
    )
    remaining_count = fields.Integer(
        compute='_compute_remaining', store=False,
        help="Cheques still available in the book based on next_number.",
    )

    _sql_constraints = [
        ('check_range', 'CHECK (end_number >= start_number)', 'End number must be greater than or equal to start number.'),
        ('check_next_in_range', 'CHECK (next_number >= start_number AND next_number <= end_number + 1)', 'Next number must be within the book range or just past the end.'),
    ]

    @api.depends('cheque_ids')
    def _compute_cheque_count(self):
        for book in self:
            book.cheque_count = len(book.cheque_ids)

    @api.depends('end_number', 'next_number')
    def _compute_remaining(self):
        for book in self:
            book.remaining_count = max(0, book.end_number - book.next_number + 1)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('next_number') in (None, 0, False):
                vals['next_number'] = vals.get('start_number', 1)
        return super().create(vals_list)

    def action_activate(self):
        self = self._eh_workflow_action()
        for book in self:
            if book.state != 'draft':
                raise UserError(_(
                    "Cheque book %s is not in draft state.", book.name,
                ))
            existing = self.search([
                ('journal_id', '=', book.journal_id.id),
                ('state', '=', 'in_use'),
                ('id', '!=', book.id),
                ('company_id', '=', book.company_id.id),
            ], limit=1)
            if existing:
                raise UserError(_(
                    "Journal %(journal)s already has an active cheque book "
                    "(%(book)s). Close it before activating a new one.",
                    journal=book.journal_id.display_name,
                    book=existing.name,
                ))
            book.state = 'in_use'

    def action_close(self):
        self = self._eh_workflow_action()
        for book in self:
            if book.state == 'closed':
                continue
            book.state = 'closed'

    def _lock_for_update(self):
        """Acquire a row-level lock to serialise concurrent serial work.

        Postgres SELECT ... FOR UPDATE blocks any other transaction that
        attempts to lock the same row, so two concurrent registrations
        on the same book queue rather than racing the next_number value.
        Flush pending writes first so the lock sees committed state, then
        invalidate the cache so the next read fetches the locked row.
        """
        self.ensure_one()
        self.flush_recordset(['next_number', 'state'])
        self.env.cr.execute(SQL(
            "SELECT id FROM eh_cheque_book WHERE id = %s FOR UPDATE",
            self.id,
        ))
        self.invalidate_recordset(['next_number', 'state'])

    def consume_next_serial(self):
        """Atomically reserve the next serial in this book.

        Acquires a row lock first so concurrent callers serialise on the
        book record. Raises if the book is not active or has run out of
        serials. The caller's transaction must commit for the increment
        to stick.
        """
        self.ensure_one()
        self = self._eh_workflow_action()
        self._lock_for_update()
        if self.state != 'in_use':
            raise UserError(_(
                "Cheque book %s is not active.", self.name,
            ))
        if self.next_number > self.end_number:
            self.state = 'exhausted'
            raise UserError(_(
                "Cheque book %s has no serials remaining.", self.name,
            ))
        serial = self.next_number
        self.next_number = serial + 1
        if self.next_number > self.end_number:
            self.state = 'exhausted'
        return serial
