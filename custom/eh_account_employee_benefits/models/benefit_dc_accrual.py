# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.benefit.dc.accrual: defined contribution accrual (IAS 19.51).

Deliberately trivial: when the employee has rendered service, the
contribution payable to a defined contribution plan is an expense and a
liability, no actuarial assumptions, no remeasurement. One record per
period, one sealed entry: Dr expense / Cr contributions payable. Present
so the module covers the whole of IAS 19's scope for a general commercial
entity, not just the defined benefit half.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhBenefitDcAccrual(models.Model):
    _name = 'eh.benefit.dc.accrual'
    _description = "Defined contribution accrual (IAS 19.51)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_date desc, id desc'
    # Block a direct RPC write of state that would skip action_post and its
    # journal entry; only the flagged actions may move state.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('posted', "Posted"), ('reversed', "Reversed")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    period_date = fields.Date(
        required=True, tracking=True, string="Period",
        help="Service period the contribution relates to (the entry is "
             "dated here).")
    amount = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True,
        help="Contribution payable to the defined contribution plan for "
             "service rendered in the period (IAS 19.51(a)).")

    expense_account_id = fields.Many2one(
        'account.account', string="Expense Account", tracking=True,
        domain="[('account_type', 'in', ['expense', 'expense_direct_cost'])]")
    payable_account_id = fields.Many2one(
        'account.account', string="Contributions Payable Account",
        tracking=True,
        domain="[('account_type', 'in', "
               "['liability_current', 'liability_payable'])]")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]")

    move_id = fields.Many2one(
        'account.move', string="Journal Entry", readonly=True, copy=False)
    move_ids = fields.One2many('account.move', 'eh_benefit_dc_accrual_id')
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    _sql_constraints = [
        ('check_amount', 'CHECK (amount >= 0)', 'A contribution accrual cannot be negative.'),
    ]

    _FROZEN_FIELDS = ('period_date', 'amount', 'expense_account_id',
                      'payable_account_id', 'journal_id', 'company_id')
    _FROZEN_STATES = ('posted', 'reversed')

    def _compute_move_count(self):
        for rec in self:
            rec.move_count = len(rec.move_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.benefit.dc.accrual') or '/'
        return super().create(vals_list)

    def write(self, vals):
        # Posted-figure INPUTS are frozen for everyone (restate via reversal):
        # data integrity, not su-gated. STATE is enforced by the inherited
        # eh.workflow.guard (blocks a non-superuser direct write).
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        if frozen:
            locked = self.filtered(
                lambda r: r.state in self._FROZEN_STATES)
            if locked:
                raise UserError(_(
                    "Accrual figures (%(fields)s) are frozen once "
                    "posted (%(names)s). Reverse the accrual to "
                    "restate it.",
                    fields=', '.join(frozen),
                    names=', '.join(locked.mapped('display_name'))))
        return super().write(vals)

    def unlink(self):
        posted = self.filtered(lambda r: r.state in self._FROZEN_STATES)
        if posted:
            raise UserError(_(
                "A posted accrual cannot be deleted (%s); reverse it "
                "instead.", ', '.join(posted.mapped('display_name'))))
        return super().unlink()

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post benefit accruals."))

    def action_post(self):
        self.ensure_one()
        self._check_manager()
        if self.state != 'draft':
            raise UserError(_("Only a draft accrual can be posted."))
        missing = []
        for fname, label in (
                ('journal_id', _("journal")),
                ('expense_account_id', _("expense account")),
                ('payable_account_id', _("contributions payable account"))):
            if not self[fname]:
                missing.append(label)
        if missing:
            raise UserError(_(
                "Configure the %(missing)s on %(name)s first.",
                missing=', '.join(missing), name=self.display_name))
        amount = self.currency_id.round(self.amount)
        if self.currency_id.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive contribution amount."))
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': self.period_date,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_benefit_dc_accrual_id': self.id,
            'eh_sealed': True,
            'line_ids': [
                (0, 0, {'name': _("DC contribution expense %s", self.name),
                        'account_id': self.expense_account_id.id,
                        'debit': amount, 'credit': 0.0}),
                (0, 0, {'name': _("DC contributions payable %s", self.name),
                        'account_id': self.payable_account_id.id,
                        'debit': 0.0, 'credit': amount}),
            ],
        })
        move.action_post()
        self.sudo().write({
            'state': 'posted', 'move_id': move.id})
        return True

    def action_reverse(self):
        self.ensure_one()
        self._check_manager()
        if self.state != 'posted':
            raise UserError(_("Only a posted accrual can be reversed."))
        reversal = self.env['account.move'].create({
            'move_type': 'entry',
            'date': fields.Date.context_today(self),
            'journal_id': self.journal_id.id,
            'ref': _("Reversal of %s", self.name),
            'eh_benefit_dc_accrual_id': self.id,
            'eh_sealed': True,
            'line_ids': [(0, 0, {
                'name': _("Reversal: %s", line.name or self.name),
                'account_id': line.account_id.id,
                'debit': line.credit, 'credit': line.debit,
            }) for line in self.move_id.line_ids],
        })
        reversal.action_post()
        self.sudo().write(
            {'state': 'reversed'})
        return True

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_benefit_dc_accrual_id', '=', self.id)],
        }
