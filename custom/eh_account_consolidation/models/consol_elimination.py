# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.consol.elimination: an intercompany elimination journal entry.

A consolidation pulls every member's gross balance, but transactions
between members appear twice (once on each side) and must be
eliminated to produce a single-entity-equivalent set. This model
holds those elimination entries: a free-form list of debit / credit
lines against the consolidated chart, with an audit trail per
elimination so the reviewer can see exactly which IC transactions
were eliminated and by whom.

Lifecycle: draft -> posted (counts toward the run's totals) ->
cancelled.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhConsolElimination(models.Model):
    _name = 'eh.consol.elimination'
    _description = "Consolidation elimination entry"
    _order = 'run_id, sequence, id'
    _inherit = ['mail.thread', 'eh.workflow.guard']

    # State advances only through the record's own actions (post / cancel /
    # reset), which run under sudo. A direct non-superuser write to 'state' is
    # refused by eh.workflow.guard, so a plain user cannot RPC-write
    # {'state': 'posted'} past action_post and its balance check.
    _eh_guarded_fields = ('state',)

    run_id = fields.Many2one(
        'eh.consol.run', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(
        required=True, copy=False, default='/',
        help=(
            "Display name for the elimination, e.g. 'IC sales: "
            "AU sub vs UK sub' or 'IC loan principal'."
        ),
    )
    description = fields.Text(
        help=(
            "Document why this elimination exists, what underlying "
            "IC transactions it covers, and any cross-reference to "
            "the original journal entries."
        ),
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('posted', "Posted"),
            ('cancelled', "Cancelled"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )

    line_ids = fields.One2many(
        'eh.consol.elimination.line', 'elimination_id', copy=True,
        help=(
            "Per-account debit / credit lines. Total debit must "
            "equal total credit for the elimination to post."
        ),
    )

    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code(
                        'eh.consol.elimination',
                    ) or '/'
                )
        return super().create(vals_list)

    def action_post(self):
        self = self._eh_workflow_action()
        for elim in self:
            if elim.state != 'draft':
                raise UserError(_(
                    "Only draft eliminations can be posted.",
                ))
            if not elim.line_ids:
                raise UserError(_(
                    "Elimination %s has no lines; nothing to post.",
                ) % elim.display_name)
            debit = sum(elim.line_ids.mapped('debit'))
            credit = sum(elim.line_ids.mapped('credit'))
            if abs(debit - credit) > 0.01:
                raise UserError(_(
                    "Elimination %(name)s is unbalanced: total "
                    "debit %(d).2f vs credit %(c).2f. Adjust the "
                    "lines so they sum to zero.",
                    name=elim.display_name, d=debit, c=credit,
                ))
            elim.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
            })

    def action_cancel(self):
        # 'state' is guarded by eh.workflow.guard; this sanctioned transition
        # writes under sudo (env.su), the non-forgeable provenance the guard
        # checks, so a plain-user RPC write of state is still refused.
        self = self._eh_workflow_action()
        for elim in self:
            elim.write({'state': 'cancelled'})

    def action_reset_to_draft(self):
        self = self._eh_workflow_action()
        for elim in self:
            if elim.state == 'cancelled':
                elim.write({'state': 'draft'})


class EhConsolEliminationLine(models.Model):
    _name = 'eh.consol.elimination.line'
    _description = "Consolidation elimination line"
    _order = 'elimination_id, sequence, id'

    elimination_id = fields.Many2one(
        'eh.consol.elimination', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    presentation_currency_id = fields.Many2one(
        related='elimination_id.run_id.presentation_currency_id',
        store=True, readonly=True,
    )

    account_id = fields.Many2one(
        'account.account', required=True,
        help="Account to debit or credit.",
    )

    debit = fields.Monetary(
        currency_field='presentation_currency_id',
        help="Debit amount. Mutually exclusive with credit.",
    )
    credit = fields.Monetary(
        currency_field='presentation_currency_id',
        help="Credit amount. Mutually exclusive with debit.",
    )
    amount = fields.Monetary(
        compute='_compute_amount', store=True,
        currency_field='presentation_currency_id',
        help="Signed: debit positive, credit negative.",
    )

    note = fields.Char()

    @api.depends('debit', 'credit')
    def _compute_amount(self):
        for line in self:
            line.amount = (line.debit or 0.0) - (line.credit or 0.0)

    @api.constrains('debit', 'credit')
    def _check_one_side(self):
        for line in self:
            if (line.debit or 0.0) < 0 or (line.credit or 0.0) < 0:
                raise ValidationError(_(
                    "Debit and credit must be non-negative.",
                ))
            if (line.debit or 0.0) > 0 and (line.credit or 0.0) > 0:
                raise ValidationError(_(
                    "A line carries either a debit or a credit, "
                    "not both.",
                ))
