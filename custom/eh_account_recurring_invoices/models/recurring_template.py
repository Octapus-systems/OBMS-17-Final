# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.recurring.invoice.template: recurring customer invoice template.

A template binds a customer, a journal, a recurrence cadence, and a list
of lines. The cron _cron_generate_due dispatches all active templates
whose next_run is past, generating one draft customer invoice per
template per pass. Generated invoices stay in draft unless auto_post is
enabled.

Lifecycle: draft -> active -> paused -> active -> finished. Active is
the only state the cron picks up. Finished is terminal: a template
finishes when end_date is past or when count_total has been reached.
"""

import calendar
import logging
import re

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


_CODE_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]*$')


class EhRecurringInvoiceTemplate(models.Model):
    _name = 'eh.recurring.invoice.template'
    _description = "Recurring invoice template"
    _order = 'state, next_run_date asc, name'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']

    # The lifecycle state may only change through the record's own actions
    # (which run under sudo), never a direct RPC/ORM write. Without this a
    # plain user could write({'state': 'active'}) to bypass action_activate's
    # line-present check, or write({'state': 'finished'}) to relabel the
    # record. _eh_guarded_fields is just ('state',) so the default create-side
    # stripping applies; no _eh_create_guarded_fields override is needed.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, tracking=True)
    code = fields.Char(required=True, copy=False)

    active = fields.Boolean(default=True)
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('active', "Active"),
            ('paused', "Paused"),
            ('finished', "Finished"),
        ],
        required=True,
        default='draft',
        tracking=True,
        index=True,
    )

    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id',
        store=True,
    )

    partner_id = fields.Many2one(
        'res.partner',
        required=True,
        index=True,
        tracking=True,
    )
    journal_id = fields.Many2one(
        'account.journal',
        required=True,
        domain="[('type', '=', 'sale')]",
        tracking=True,
    )
    payment_term_id = fields.Many2one('account.payment.term')
    fiscal_position_id = fields.Many2one('account.fiscal.position')

    line_ids = fields.One2many(
        'eh.recurring.invoice.line',
        'template_id',
        copy=True,
    )

    interval = fields.Integer(
        required=True,
        default=1,
        help="Number of interval_unit periods between generations.",
    )
    interval_unit = fields.Selection(
        [
            ('day', "Day(s)"),
            ('week', "Week(s)"),
            ('month', "Month(s)"),
            ('quarter', "Quarter(s)"),
            ('year', "Year(s)"),
        ],
        default='month',
        required=True,
    )

    start_date = fields.Date(required=True, default=fields.Date.context_today)
    next_run_date = fields.Date(required=True, default=fields.Date.context_today)
    end_date = fields.Date(
        help=(
            "Optional. When set, the template finishes after generating "
            "the last invoice on or before this date."
        ),
    )
    count_total = fields.Integer(
        default=0,
        help=(
            "Optional cap on total invoices. 0 means no cap. "
            "When set, the template finishes after generating "
            "count_total invoices."
        ),
    )
    count_generated = fields.Integer(default=0, readonly=True)
    count_remaining = fields.Integer(compute='_compute_count_remaining')

    auto_post = fields.Boolean(
        default=False,
        help=(
            "When enabled, generated invoices are posted automatically. "
            "Default off so the accountant reviews each draft."
        ),
    )

    ref = fields.Char(help="Default reference applied to generated invoices.")
    narration = fields.Html(help="Default note applied to generated invoices.")

    last_run_at = fields.Datetime(readonly=True)
    last_generated_move_id = fields.Many2one(
        'account.move', readonly=True, ondelete='set null',
    )
    last_error = fields.Text(readonly=True)

    generated_move_ids = fields.One2many(
        'account.move', 'eh_recurring_template_id', readonly=True,
    )
    generated_count = fields.Integer(compute='_compute_generated_count')

    recurring_subtotal = fields.Monetary(
        compute='_compute_mrr', currency_field='currency_id',
        help="Untaxed recurring amount per generation (sum of line "
             "subtotals).",
    )
    mrr = fields.Monetary(
        compute='_compute_mrr', currency_field='currency_id',
        help="Monthly recurring revenue: the recurring amount normalised "
             "to a calendar month. Zero unless the template is active.",
    )
    arr = fields.Monetary(
        compute='_compute_mrr', currency_field='currency_id',
        help="Annualised recurring revenue (12 x MRR).",
    )

    # Calendar months represented by one unit of each cadence. Day and
    # week use the mean month length (365.25 / 12) so a weekly plan
    # annualises correctly.
    _MONTHS_PER_UNIT = {
        'day': 1.0 / 30.4375,
        'week': 7.0 / 30.4375,
        'month': 1.0,
        'quarter': 3.0,
        'year': 12.0,
    }

    @api.depends('line_ids.subtotal', 'interval', 'interval_unit', 'state')
    def _compute_mrr(self):
        for rec in self:
            subtotal = sum(rec.line_ids.mapped('subtotal'))
            rec.recurring_subtotal = subtotal
            months = (rec.interval or 0) * rec._MONTHS_PER_UNIT.get(
                rec.interval_unit, 1.0)
            monthly = (
                subtotal / months
                if (rec.state == 'active' and months) else 0.0
            )
            rec.mrr = monthly
            rec.arr = monthly * 12.0

    @api.model
    def eh_total_mrr(self, company=None):
        """Total monthly recurring revenue across active templates."""
        company = company or self.env.company
        templates = self.search([
            ('state', '=', 'active'),
            ('company_id', '=', company.id),
        ])
        return sum(templates.mapped('mrr'))

    _sql_constraints = [
        ('unique_code_per_company', 'unique(code, company_id)', 'Template code must be unique per company.'),
        ('positive_interval', 'check(interval > 0)', 'Interval must be a positive integer.'),
        ('count_total_non_negative', 'check(count_total >= 0)', 'count_total must be zero or positive.'),
    ]

    @api.constrains('code')
    def _check_code_format(self):
        for rec in self:
            if not _CODE_RE.match(rec.code or ''):
                raise ValidationError(_(
                    "Template code must match [a-zA-Z][a-zA-Z0-9_]* (got %r).",
                ) % rec.code)

    # ---- onchange (live form feedback) ----

    @api.onchange('partner_id')
    def _onchange_partner_id_terms(self):
        """Pre-fill payment_term and fiscal_position from the
        partner's commercial entity when picked.

        Without this the user has to copy the partner's terms onto
        every template by hand. With it, picking the partner is
        usually enough.
        """
        for rec in self:
            if not rec.partner_id:
                continue
            commercial = rec.partner_id.commercial_partner_id or rec.partner_id
            if not rec.payment_term_id and commercial.property_payment_term_id:
                rec.payment_term_id = commercial.property_payment_term_id
            if not rec.fiscal_position_id and commercial.property_account_position_id:
                rec.fiscal_position_id = commercial.property_account_position_id

    @api.onchange('interval', 'interval_unit')
    def _onchange_interval_warn_long(self):
        """Soft-warn the user when they configure a recurrence
        longer than 12 months. A 5-year-monthly schedule is
        typically a typo (interval_unit set to year instead of
        month).
        """
        for rec in self:
            if not (rec.interval and rec.interval_unit):
                continue
            months_equiv = rec.interval * (
                12 if rec.interval_unit == 'year'
                else 3 if rec.interval_unit == 'quarter'
                else 1 if rec.interval_unit == 'month'
                else 0
            )
            if months_equiv > 12:
                return {
                    'warning': {
                        'title': "Long recurrence",
                        'message': (
                            "Recurrence is %s %s - this generates a "
                            "schedule longer than a year between "
                            "invoices. Verify this is intentional." % (
                                rec.interval, rec.interval_unit,
                            )
                        ),
                    }
                }

    @api.depends('count_total', 'count_generated')
    def _compute_count_remaining(self):
        for rec in self:
            if rec.count_total:
                rec.count_remaining = max(
                    rec.count_total - rec.count_generated, 0,
                )
            else:
                rec.count_remaining = 0

    @api.depends('generated_move_ids')
    def _compute_generated_count(self):
        for rec in self:
            rec.generated_count = len(rec.generated_move_ids)

    # ---- lifecycle ----

    def action_activate(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'finished':
                raise UserError(_(
                    "Cannot reactivate a finished template. Duplicate it.",
                ))
            if not rec.line_ids:
                raise UserError(_(
                    "Cannot activate template '%s' without lines.",
                ) % rec.name)
            rec.state = 'active'

    def action_pause(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'active':
                rec.state = 'paused'

    def action_resume(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'paused':
                rec.state = 'active'

    def action_finish(self):
        self = self._eh_workflow_action()
        for rec in self:
            rec.state = 'finished'

    # ---- generation ----

    def action_generate_now(self):
        """Manually generate one invoice and advance next_run.

        Takes a row lock first so two concurrent requests (a double click
        or a browser retry) cannot both read the same next_run_date and
        generate two invoices for the SAME period. Serialised by the lock,
        the second request observes the advanced next_run and generates the
        following period, exactly as two deliberate clicks would.
        """
        for rec in self:
            if rec.state not in ('active', 'draft'):
                raise UserError(_(
                    "Can only generate from active or draft templates.",
                ))
            rec._eh_lock_for_generate()
            rec._generate_one()
            rec._advance_next_run()
            rec._maybe_finish()
        return True

    def _eh_lock_for_generate(self):
        """Acquire a row lock on this template and drop cached field values
        so a serialised concurrent call reads the latest next_run_date
        rather than a stale pre-advance snapshot.
        """
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_recurring_invoice_template "
            "WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        self.invalidate_recordset()

    @api.model
    def _cron_generate_due(self):
        """Cron entry point. Dispatch every active template whose
        next_run_date is past. Per template failures are isolated and
        every template advances even when generation fails, so a single
        bad template never freezes the queue.

        The advance always runs in a savepoint that is independent of
        the generation attempt: a generation failure rolls back the
        partial draft move and we then commit a clean advance, marking
        the template with the captured error so the user can fix it
        without the cron retrying every minute.
        """
        today = fields.Date.context_today(self)
        due = self.search([
            ('state', '=', 'active'),
            ('active', '=', True),
            ('next_run_date', '<=', today),
        ])
        for tpl in due:
            try:
                with self.env.cr.savepoint():
                    # Serialise against the manual path (action_generate_now):
                    # take the SAME row lock before reading next_run_date and
                    # generating, so a manual generate racing this cron cannot
                    # both observe the same period and double-generate.
                    tpl._eh_lock_for_generate()
                    tpl._generate_one()
                generated = True
            except Exception as exc:
                _logger.warning(
                    "eh.recurring.invoice.template %s generation failed: %s",
                    tpl.id, exc,
                )
                tpl._record_failure(exc)
                generated = False
            try:
                with self.env.cr.savepoint():
                    tpl._advance_next_run()
                    if generated:
                        tpl._maybe_finish()
            except Exception as exc2:
                # If we cannot even advance the template, log loudly so
                # operators see the stuck record rather than letting it
                # silently cause every subsequent cron tick to retry.
                _logger.error(
                    "eh.recurring.invoice.template %s could not advance "
                    "next_run_date: %s. Template is parked; an admin must "
                    "review it.", tpl.id, exc2,
                )
                tpl._record_failure(exc2)
        return True

    def _generate_one(self):
        """Create one draft account.move from this template."""
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_(
                "Template '%s' has no lines; nothing to generate.",
            ) % self.name)
        Move = self.env['account.move']
        invoice_vals = self._build_invoice_vals()
        move = Move.create(invoice_vals)
        if self.auto_post:
            move.action_post()
        self.write({
            'last_run_at': fields.Datetime.now(),
            'last_generated_move_id': move.id,
            'last_error': False,
            'count_generated': self.count_generated + 1,
        })
        return move

    def _build_invoice_vals(self):
        self.ensure_one()
        invoice_date = self.next_run_date or fields.Date.context_today(self)
        line_vals = []
        for line in self.line_ids.sorted(lambda line_item: line_item.sequence):
            line_dict = {
                'name': line.name,
                'quantity': line.quantity or 1.0,
                'price_unit': line.price_unit or 0.0,
                'sequence': line.sequence,
            }
            if line.product_id:
                line_dict['product_id'] = line.product_id.id
            if line.account_id:
                line_dict['account_id'] = line.account_id.id
            if line.tax_ids:
                line_dict['tax_ids'] = [(6, 0, line.tax_ids.ids)]
            line_vals.append((0, 0, line_dict))

        vals = {
            'move_type': 'out_invoice',
            'partner_id': self.partner_id.id,
            'journal_id': self.journal_id.id,
            'company_id': self.company_id.id,
            'currency_id': self.currency_id.id,
            'invoice_date': invoice_date,
            'invoice_line_ids': line_vals,
            'eh_recurring_template_id': self.id,
        }
        if self.payment_term_id:
            vals['invoice_payment_term_id'] = self.payment_term_id.id
        if self.fiscal_position_id:
            vals['fiscal_position_id'] = self.fiscal_position_id.id
        if self.ref:
            vals['ref'] = self.ref
        if self.narration:
            vals['narration'] = self.narration
        return vals

    def _advance_next_run(self):
        for rec in self:
            base = (
                (rec.next_run_date or fields.Date.context_today(rec))
                + rec._compute_delta()
            )
            rec.next_run_date = rec._anchor_day_of_month(base)

    def _anchor_day_of_month(self, candidate):
        """Pin the day-of-month back to start_date's day for calendar
        cadences, clamped to the candidate month's length.

        relativedelta clamps an out-of-range day down (Jan 31 + 1 month
        becomes Feb 28). Advancing off that clamped value would carry the
        shortened day forward forever (Feb 28 -> Mar 28 -> ...), so the
        intended day-of-month silently decays. Re-anchoring to the
        original start_date day on every advance keeps a 31st-of-month
        schedule landing on the 31st whenever the month allows, while
        still honouring the relative advance (so a failed period still
        moves the pointer and never retries every cron tick).

        Day and week cadences have no day-of-month semantics; they pass
        through unchanged.
        """
        self.ensure_one()
        if self.interval_unit not in ('month', 'quarter', 'year'):
            return candidate
        if not self.start_date:
            return candidate
        last_day = calendar.monthrange(candidate.year, candidate.month)[1]
        target_day = min(self.start_date.day, last_day)
        return candidate.replace(day=target_day)

    def _compute_delta(self):
        self.ensure_one()
        if self.interval_unit == 'day':
            return relativedelta(days=self.interval)
        if self.interval_unit == 'week':
            return relativedelta(weeks=self.interval)
        if self.interval_unit == 'month':
            return relativedelta(months=self.interval)
        if self.interval_unit == 'quarter':
            return relativedelta(months=3 * self.interval)
        if self.interval_unit == 'year':
            return relativedelta(years=self.interval)
        return relativedelta(months=self.interval)

    def _maybe_finish(self):
        """Finish the template when end_date or count_total cap is reached.

        Called from action_generate_now and the cron; the state write is
        routed through the guard's sudo helper so it passes the workflow guard
        regardless of the acting user (the cron runs as its own user, not
        necessarily the superuser).
        """
        for rec in self:
            if (
                rec.end_date
                and rec.next_run_date
                and rec.next_run_date > rec.end_date
            ):
                rec._eh_workflow_write({'state': 'finished'})
                continue
            if rec.count_total and rec.count_generated >= rec.count_total:
                rec._eh_workflow_write({'state': 'finished'})

    def _record_failure(self, exc):
        for rec in self:
            rec.write({
                'last_run_at': fields.Datetime.now(),
                'last_error': str(exc)[:8000],
            })

    def action_view_generated(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Generated Invoices"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('eh_recurring_template_id', '=', self.id)],
            'context': {'default_eh_recurring_template_id': self.id},
        }


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_recurring_template_id = fields.Many2one(
        'eh.recurring.invoice.template',
        readonly=True,
        ondelete='set null',
        index=True,
        help=(
            "When set, this invoice was generated by the named recurring "
            "template. Used for traceability and counters."
        ),
    )
