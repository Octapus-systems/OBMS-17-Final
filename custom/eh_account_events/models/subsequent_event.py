# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 10 events after the reporting period."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhSubsequentEvent(models.Model):
    _name = 'eh.subsequent.event'
    _description = "Event after the reporting period (IAS 10)"
    _inherit = ['mail.thread', 'eh.gl.reversal', 'eh.workflow.guard']
    _order = 'event_date desc, id desc'
    _rec_name = 'name'

    # state is a state machine driven only by the booking/reset actions (which
    # run under sudo); a direct non-superuser RPC write to it is refused by the
    # inherited eh.workflow.guard, closing the "write({'state': 'posted'}) skips
    # action_book_adjusting_entry and its journal entry" bypass.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, tracking=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    reporting_date = fields.Date(
        required=True, help="End of the reporting period.")
    event_date = fields.Date(
        required=True, default=fields.Date.context_today,
        help="Date the event occurred, after the reporting period.")
    authorized_for_issue_date = fields.Date(
        string="Authorised for issue",
        default=lambda self: self._default_authorized_for_issue_date(),
        tracking=True,
        help="Date the financial statements were authorised for issue "
             "(IAS 10.5-6). IAS 10 covers only events between the reporting "
             "date and this date: an adjusting event dated after it belongs "
             "to the next reporting period and is blocked from booking "
             "(IAS 10.3), and any event after it is flagged as next-period "
             "for the following period's disclosure list. Defaults from the "
             "date the latest year-end close was posted when that module is "
             "installed; leave empty for an unbounded register.")
    next_period = fields.Boolean(
        string="Next period", compute='_compute_next_period', store=True,
        help="The event occurred after the authorised-for-issue date, so it "
             "belongs to the next reporting period (IAS 10.3).")
    is_adjusting = fields.Boolean(
        string="Adjusting event",
        help="True when the event provides evidence of conditions that "
             "existed at the reporting date (adjust the statements); false "
             "when it is indicative of conditions that arose after (disclose "
             "only) - IAS 10.8-10.")
    treatment = fields.Selection(
        [('adjust', "Adjust the financial statements"),
         ('disclose', "Disclose only")],
        compute='_compute_treatment', store=True)
    category = fields.Selection(
        [('litigation', "Litigation settlement"),
         ('asset_value', "Asset valuation evidence"),
         ('receivable', "Customer insolvency"),
         ('dividend', "Dividend declared"),
         ('acquisition', "Acquisition / disposal"),
         ('other', "Other")],
        default='other', required=True)
    estimated_effect = fields.Monetary(
        currency_field='currency_id',
        help="Estimated financial effect, or nil when it cannot be "
             "estimated (IAS 10.21).")
    description = fields.Text()

    # --- Optional GL booking of an adjusting event (IAS 10.8) -------------
    # Opt-in. An adjusting event provides evidence of conditions that existed
    # at the reporting date, so the financial statements are adjusted before
    # authorisation. Populating the two accounts below and booking posts that
    # adjusting entry to the reporting period; a register that leaves them
    # blank behaves exactly as before. A non-adjusting (disclose-only) event
    # cannot be booked (IAS 10.10). Booking is manager-gated and frozen.
    state = fields.Selection(
        [('draft', "Draft"), ('posted', "Posted")],
        default='draft', required=True, tracking=True,
        help="Posted once the adjusting entry has been written to the "
             "general ledger.")
    journal_id = fields.Many2one(
        'account.journal', string="Journal",
        domain="[('type', '=', 'general')]",
        help="Journal for the adjusting entry.")
    debit_account_id = fields.Many2one(
        'account.account', string="Debit account",
        help="Account debited by the adjusting entry.")
    credit_account_id = fields.Many2one(
        'account.account', string="Credit account",
        help="Account credited by the adjusting entry.")
    move_id = fields.Many2one(
        'account.move', string="Adjusting entry", readonly=True, copy=False,
        help="Journal entry booking the adjusting event to the reporting "
             "period.")
    move_count = fields.Integer(compute='_compute_move_count')

    # Fields feeding the adjusting entry, frozen once it is posted so a booked
    # adjustment cannot be retro-edited out from under its journal entry.
    _FROZEN_AFTER_POSTED = (
        'reporting_date', 'event_date', 'is_adjusting', 'estimated_effect',
        'journal_id', 'debit_account_id', 'credit_account_id', 'company_id',
        'authorized_for_issue_date',
    )

    @api.model
    def _default_authorized_for_issue_date(self):
        """Soft lookup: the date the latest year-end close was posted.

        When eh_account_year_end is installed, the day the company's most
        recent year-end closing run was posted is the best available proxy
        for the date the statements were authorised for issue; without the
        module (or with no posted run) the date stays manual. Registry
        lookup only - no hard dependency on the year-end module.
        """
        if 'eh.year.end.run' not in self.env:
            return False
        run = self.env['eh.year.end.run'].search(
            [('company_id', '=', self.env.company.id),
             ('state', '=', 'posted')],
            order='fiscal_year_end desc, id desc', limit=1)
        if run and run.posted_at:
            return fields.Date.to_date(run.posted_at)
        return False

    @api.depends('move_id')
    def _compute_move_count(self):
        for e in self:
            e.move_count = 1 if e.move_id else 0

    @api.depends('event_date', 'authorized_for_issue_date')
    def _compute_next_period(self):
        for e in self:
            e.next_period = bool(
                e.authorized_for_issue_date and e.event_date
                and e.event_date > e.authorized_for_issue_date)

    @api.onchange('reporting_date')
    def _onchange_reporting_date_authorized(self):
        """The default authorisation date comes from the latest posted
        year-end close, which belongs to an earlier period. When the user
        picks a reporting date after that default, clear it rather than
        leave a value the constraint below would refuse."""
        for e in self:
            if (e.authorized_for_issue_date and e.reporting_date
                    and e.authorized_for_issue_date < e.reporting_date):
                e.authorized_for_issue_date = False

    @api.model_create_multi
    def create(self, vals_list):
        """Mirror the onchange guard for programmatic creates.

        The default authorised-for-issue date is the posting date of the
        latest posted year-end close, which belongs to an earlier period.
        The form onchange clears that stale default when the user picks a
        later reporting date, but onchanges never run for imports, XML-RPC
        or create() in code: without this guard any programmatic create
        whose reporting date falls after the last close would trip
        _check_authorized_after_reporting. A caller that explicitly passes
        authorized_for_issue_date (in the vals or as a context default) is
        left untouched - a genuinely inconsistent explicit date is still a
        data error the constraint refuses.
        """
        if 'default_authorized_for_issue_date' not in self.env.context:
            for vals in vals_list:
                if 'authorized_for_issue_date' in vals:
                    continue
                reporting = fields.Date.to_date(
                    vals.get('reporting_date')
                    or self.env.context.get('default_reporting_date'))
                if not reporting:
                    continue
                company = (
                    self.env['res.company'].browse(vals['company_id'])
                    if vals.get('company_id') else self.env.company)
                default = self.with_company(
                    company)._default_authorized_for_issue_date()
                if default and default < reporting:
                    vals['authorized_for_issue_date'] = False
        return super().create(vals_list)

    @api.constrains('authorized_for_issue_date', 'reporting_date')
    def _check_authorized_after_reporting(self):
        """The statements of a period are authorised for issue after that
        period ends (IAS 10.4-6): an authorisation date before the reporting
        date is a data error, not a policy choice."""
        for e in self:
            if (e.authorized_for_issue_date and e.reporting_date
                    and e.authorized_for_issue_date < e.reporting_date):
                raise ValidationError(_(
                    "The authorised-for-issue date (%(auth)s) cannot be "
                    "before the end of the reporting period (%(rep)s): "
                    "financial statements are authorised for issue after "
                    "the period they report on ends (IAS 10.4-6).",
                    auth=e.authorized_for_issue_date, rep=e.reporting_date))

    @api.depends('is_adjusting')
    def _compute_treatment(self):
        for e in self:
            e.treatment = 'adjust' if e.is_adjusting else 'disclose'

    @api.constrains('category', 'is_adjusting')
    def _check_dividend_not_adjusting(self):
        """IAS 10.12-13: a dividend declared after the reporting period is a
        non-adjusting event. It is not recognised as a liability at the
        reporting date, only disclosed. Marking it adjusting (which would let
        action_book_adjusting_entry recognise it into the reporting period) is
        a black-letter violation, so refuse it."""
        for e in self:
            if e.category == 'dividend' and e.is_adjusting:
                raise ValidationError(_(
                    "A dividend declared after the reporting period is a "
                    "non-adjusting event (IAS 10.12-13): it is not recognised "
                    "as a liability at the reporting date, only disclosed. "
                    "Leave %s as disclose-only.", e.name))

    def write(self, vals):
        # Posted-figure INPUTS are frozen for everyone (restate via reversal
        # through action_reset_to_draft): a data-integrity guard, not su-gated.
        # STATE transitions are enforced by the inherited eh.workflow.guard,
        # which blocks a non-superuser direct write; the sanctioned booking/
        # reset actions run under sudo. Provenance is env.su, not a context key.
        frozen = [f for f in self._FROZEN_AFTER_POSTED if f in vals]
        if frozen:
            for e in self:
                if e.state == 'posted':
                    raise UserError(_(
                        "%(name)s carries a posted adjusting entry; "
                        "%(fields)s cannot be changed. Reset it to draft "
                        "(which reverses the entry) before editing.",
                        name=e.name, fields=', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        for e in self:
            if e.state == 'posted' or e.move_id:
                raise UserError(_(
                    "%s has a posted adjusting entry and cannot be deleted. "
                    "This preserves the IAS 10 audit trail.", e.name))
        return super().unlink()

    def action_book_adjusting_entry(self):
        """Book the adjusting event to the reporting period (manager only).

        An adjusting event is booked to ``reporting_date`` (the end of the
        reporting period), debiting and crediting the two chosen accounts by
        ``estimated_effect``, balanced by construction. A non-adjusting event
        is disclose-only and cannot be booked (IAS 10.10).
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can book an adjusting entry."))
        # Run as su so the guarded state write below passes the inherited
        # eh.workflow.guard; env.user is preserved for the audit trail.
        self = self._eh_workflow_action()
        for e in self:
            if e.state == 'posted':
                raise UserError(_("%s is already posted.", e.name))
            if e.treatment != 'adjust':
                raise UserError(_(
                    "Only an adjusting event books an entry. A non-adjusting "
                    "event is disclosed only and is not adjusted into the "
                    "financial statements (IAS 10.10)."))
            if (e.authorized_for_issue_date and e.event_date
                    and e.event_date > e.authorized_for_issue_date):
                raise UserError(_(
                    "%(name)s is dated %(event)s, after the financial "
                    "statements were authorised for issue on %(auth)s. "
                    "IAS 10 covers only events up to the authorisation date "
                    "(IAS 10.3): this event belongs to the next reporting "
                    "period and cannot be booked as an adjusting entry of "
                    "the period ended %(rep)s.",
                    name=e.name, event=e.event_date,
                    auth=e.authorized_for_issue_date, rep=e.reporting_date))
            if not (e.journal_id and e.debit_account_id
                    and e.credit_account_id):
                raise UserError(_(
                    "Set the journal, the debit account and the credit "
                    "account before booking the adjusting entry."))
            currency = e.currency_id or e.company_id.currency_id
            amount = currency.round(e.estimated_effect)
            if currency.is_zero(amount):
                raise UserError(_(
                    "The adjusting event has no estimated financial effect; "
                    "there is nothing to book. Record it as a disclosure "
                    "instead (IAS 10.21)."))
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': e.journal_id.id,
                'date': e.reporting_date,
                'ref': _("Adjusting event: %s", e.name),
                'company_id': e.company_id.id,
                'eh_sealed': True,
                'line_ids': [
                    (0, 0, {
                        'name': e.name,
                        'account_id': e.debit_account_id.id,
                        'debit': amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': e.name,
                        'account_id': e.credit_account_id.id,
                        'debit': 0.0,
                        'credit': amount,
                    }),
                ],
            })
            move.action_post()
            e.move_id = move.id
            e.state = 'posted'
        return True

    def action_reset_to_draft(self):
        """Reverse the posted adjusting entry and reopen (manager only)."""
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can reset a posted adjusting "
                "entry to draft."))
        for e in self:
            if e.state != 'posted':
                raise UserError(_(
                    "%s is not posted; there is nothing to reset.", e.name))
            move = e.move_id
            if move:
                reversal = move._reverse_moves([{
                    'date': e.reporting_date,
                    'journal_id': move.journal_id.id,
                    'ref': _("Reversal of adjusting event: %s", e.name),
                }], cancel=False)
                reversal.action_post()
                e._eh_seal_reversal(reversal)
            # state moves to draft through sudo so the inherited
            # eh.workflow.guard recognises this as a sanctioned action write
            # (env.su), not a forgeable direct RPC re-key.
            e.sudo().write({'state': 'draft', 'move_id': False})
        return True

    def action_view_move(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
        }
