# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.nrv.run: a period-end inventory net-realisable-value assessment.

Compute totals the required write-down; post recognises only the movement
from the opening position. An increase debits the write-down expense and
credits the write-down allowance; a recovery reverses it, capped at the
amount previously recognised because each line's write-down is floored at
zero (IAS 2.33).

Assessment basis (IAS 2.29). Write-downs are usually computed item by item,
but similar or related items may be grouped and assessed together. The run
carries an explicit assessment basis:

* item: each line is measured on its own; the write-down is the excess of
  the line's cost over its NRV, floored at zero per line.
* category: each product category is one unit of assessment. Surpluses (NRV
  above cost) and deficits inside the category are netted BEFORE the floor
  at zero is applied, so the category requirement is
  max(total cost - total NRV, 0). The category is therefore never carried
  above its aggregate cost, and because the requirement is allocated only
  over the lines with an item-level deficit (pro-rata by deficit), no
  individual line ever carries a negative write-down (never above its own
  cost) or more than its own deficit (never below its own NRV).

The basis is locked once the run is posted, tracked in the chatter, and
disclosed on the posted movement entry.
"""

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhNrvRun(models.Model):
    _name = 'eh.nrv.run'
    _description = "Inventory NRV run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard', 'eh.gl.reversal']
    _order = 'reporting_date desc, id desc'
    _rec_name = 'name'

    # eh.workflow.guard: state only ever changes through this model's own
    # action_* methods (which flag the write via _eh_workflow_action). A plain
    # user cannot RPC write({'state': 'posted'}) straight past action_post and
    # its manager gate, account validation and journal entry.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('computed', "Computed"),
         ('posted', "Posted"), ('reversed', "Reversed"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    reporting_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    assessment_basis = fields.Selection(
        [('item', "Item by item"), ('category', "Category (grouped)")],
        required=True, default='item', tracking=True,
        help="Unit of assessment for the lower-of-cost-and-NRV test "
             "(IAS 2.29). Item by item: each line is floored at zero on its "
             "own. Category (grouped): similar or related items sharing a "
             "product category are assessed as one unit, so surpluses and "
             "deficits inside the category are netted before the write-down "
             "is floored at zero; the category is never carried above its "
             "aggregate cost and no line is ever written up above its own "
             "cost. The choice is locked once the run is posted and is "
             "disclosed on the movement entry.")

    line_ids = fields.One2many('eh.nrv.line', 'run_id', copy=True)

    total_cost = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    closing_writedown = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    movement = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Net movement to post; positive = charge to P&L.")

    writedown_expense_account_id = fields.Many2one(
        'account.account', string="Write-down Expense Account", tracking=True,
        domain="[('account_type', 'in', "
               "['expense', 'expense_direct_cost'])]")
    allowance_account_id = fields.Many2one(
        'account.account', string="Write-down Allowance Account",
        tracking=True,
        domain="[('account_type', 'in', ['asset_current'])]",
        help="Contra-asset that carries the accumulated inventory "
             "write-down.")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]")

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')

    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)

    notes = fields.Text()

    _sql_constraints = [
        ('unique_company_date', 'unique(company_id, reporting_date)', 'Only one inventory NRV run per company per reporting date.'),
    ]

    # Measurement / input fields frozen once the run is posted or reversed. A
    # posted run has recognised a balanced ledger movement (IAS 2.34); its
    # inputs must not silently drift from what was posted. The state-transition
    # writes performed by action_compute / action_post / action_reverse /
    # action_cancel touch only state + audit stamps + move links, none of which
    # appear here, so those flows are never blocked. State itself is never
    # frozen. Reverse the run (manager-gated) to reopen it for editing.
    _FROZEN_AFTER_POST = (
        'reporting_date', 'company_id', 'writedown_expense_account_id',
        'allowance_account_id', 'journal_id', 'line_ids',
        'assessment_basis',
    )

    @api.depends('line_ids.required_writedown', 'line_ids.movement',
                 'line_ids.cost')
    def _compute_totals(self):
        for run in self:
            run.total_cost = sum(run.line_ids.mapped('cost'))
            run.closing_writedown = sum(
                run.line_ids.mapped('required_writedown'))
            run.movement = sum(run.line_ids.mapped('movement'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.nrv.run') or '/'
        return super().create(vals_list)

    def write(self, vals):
        frozen = [f for f in self._FROZEN_AFTER_POST if f in vals]
        posted = self.filtered(lambda r: r.state in ('posted', 'reversed'))
        # A pure state-transition write (action_compute / action_post /
        # action_reverse / action_cancel write only state + audit stamps +
        # move links) carries no frozen field and passes. A write touching a
        # measurement / input field while any record is posted or reversed is
        # always blocked, so a raw ORM write cannot drift a posted run's
        # figures from the recognised ledger movement.
        if frozen and posted:
            raise UserError(_(
                "Inputs on a posted NRV run are frozen (%(fields)s). "
                "Reverse it first (EH Accounting Manager only) to change it "
                "(IAS 2.34).",
                fields=', '.join(frozen)))
        # A posted / reversed run's state is a control point: resetting it to
        # draft would lift the freeze above. A raw ORM state write without the
        # sanctioned-transition context flag is manager-gated so a plain user
        # cannot un-freeze a GL-backed run.
        if 'state' in vals \
                and not self.env.context.get('eh_nrv_state_change'):
            crossing = posted.filtered(lambda r: r.state != vals['state'])
            if crossing:
                crossing._check_manager()
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager may change the state of a "
                "posted or reversed NRV run."))

    def unlink(self):
        posted = self.filtered(lambda r: r.state in ('posted', 'reversed'))
        if posted:
            raise UserError(_(
                "A posted NRV run cannot be deleted; reverse it first "
                "(EH Accounting Manager only)."))
        return super().unlink()

    # ---- transitions ----

    def action_compute(self):
        self = self._eh_workflow_action()
        for run in self:
            if run.state not in ('draft', 'computed'):
                raise UserError(_(
                    "Compute is only available in draft or computed state."))
            if not run.line_ids:
                raise UserError(_("Add at least one inventory line."))
            run._validate_basis()
            run.write({
                'state': 'computed',
                'computed_at': fields.Datetime.now(),
                'computed_by_id': self.env.user.id,
            })
        return True

    def action_post(self):
        self = self._eh_workflow_action()
        for run in self:
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only an EH Accounting Manager can post an NRV run."))
            if run.state != 'computed':
                raise UserError(_("Run must be computed before posting."))
            # Re-validate at post: lines stay editable between compute and
            # post, so a category could have been cleared in the meantime.
            run._validate_basis()
            run._validate_accounts()
            move = run._build_move()
            if not move:
                raise UserError(_(
                    "The write-down movement is nil; nothing to post for %s.",
                    run.display_name))
            run.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'move_id': move.id,
            })
        return True

    def action_reverse(self):
        self = self._eh_workflow_action()
        for run in self:
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only an EH Accounting Manager can reverse an NRV run."))
            if run.state != 'posted' or not run.move_id:
                raise UserError(_("Only a posted run with a move can reverse."))
            reversal = run.move_id._reverse_moves([{
                'date': run.reporting_date + timedelta(days=1),
                'journal_id': run.journal_id.id,
                'ref': _("NRV reversal %s", run.name),
            }], cancel=False)
            reversal.action_post()
            run._eh_seal_reversal(reversal)
            run.with_context(eh_nrv_state_change=True).write({
                'state': 'reversed',
                'reversal_move_id': reversal.id,
            })
        return True

    def action_cancel(self):
        self = self._eh_workflow_action()
        for run in self:
            if run.state in ('posted', 'reversed'):
                raise UserError(_("Cannot cancel a posted or reversed run."))
            run.state = 'cancelled'

    def action_set_to_draft(self):
        self = self._eh_workflow_action()
        for run in self:
            if run.state != 'cancelled':
                raise UserError(_("Only cancelled runs can return to draft."))
            run.state = 'draft'

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No movement entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move', 'res_id': self.move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }

    # ---- helpers ----

    def _validate_basis(self):
        """Category assessment needs a category on every line: IAS 2.29
        groups similar or related items, and an uncategorised line has no
        group to be assessed in."""
        self.ensure_one()
        if self.assessment_basis != 'category':
            return
        missing = self.line_ids.filtered(lambda ln: not ln.product_category_id)
        if missing:
            raise UserError(_(
                "The category assessment basis needs a product category on "
                "every line, because IAS 2.29 groups similar or related "
                "items and assesses each group as one unit. Missing on: "
                "%s.", ', '.join(missing.mapped('name'))))

    def _category_allocation(self):
        """Per-line closing write-down under the category basis (IAS 2.29).

        Each product category is one unit of assessment. The category
        requirement is max(total cost - total NRV, 0): surpluses and
        deficits inside the category are netted BEFORE the floor at zero,
        so the category as a whole is never carried above its aggregate
        cost. The requirement is then allocated over the lines carrying an
        item-level deficit, pro-rata by deficit, which guarantees:

        * a surplus line gets no allocation (it is never written up, so no
          item is ever carried above its own cost); and
        * a deficit line never gets more than its own deficit (it is never
          carried below its own NRV), because the netted requirement is at
          most the sum of the deficits.

        Rounding: each share is rounded to company currency; the last
        deficit line takes the residual so the shares tie exactly to the
        category requirement.

        Returns {line id: allocated closing write-down} covering every line
        of the run (zero for surplus lines).
        """
        self.ensure_one()
        currency = self.currency_id

        def rnd(value):
            return currency.round(value) if currency else round(value, 2)

        groups = {}
        for line in self.line_ids:
            groups.setdefault(line.product_category_id, []).append(line)
        alloc = {}
        for lines in groups.values():
            required = max(rnd(
                sum(ln.cost for ln in lines)
                - sum(ln.net_realisable_value for ln in lines)), 0.0)
            deficits = [
                (ln, max(ln.cost - ln.net_realisable_value, 0.0))
                for ln in lines]
            for ln, _deficit in deficits:
                alloc[ln.id] = 0.0
            deficit_total = sum(d for _ln, d in deficits)
            if not required or not deficit_total:
                continue
            carriers = [(ln, d) for ln, d in deficits if d > 0.0]
            allocated = 0.0
            for ln, deficit in carriers[:-1]:
                share = rnd(required * deficit / deficit_total)
                alloc[ln.id] = share
                allocated += share
            alloc[carriers[-1][0].id] = rnd(required - allocated)
        return alloc

    def _basis_label(self):
        self.ensure_one()
        labels = dict(self._fields['assessment_basis']
                      ._description_selection(self.env))
        return labels.get(self.assessment_basis, self.assessment_basis)

    def _validate_accounts(self):
        self.ensure_one()
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        if not self.writedown_expense_account_id:
            missing.append(_("write-down expense account"))
        if not self.allowance_account_id:
            missing.append(_("write-down allowance account"))
        if missing:
            raise UserError(_(
                "Configure the %s on run %s before posting.",
                ', '.join(missing), self.display_name))

    def _build_move(self):
        self.ensure_one()
        currency = self.currency_id
        movement = currency.round(self.movement)
        if currency.is_zero(movement):
            return self.env['account.move']
        if movement > 0:
            lines = [
                (0, 0, {
                    'name': _("Inventory write-down %s", self.name),
                    'account_id': self.writedown_expense_account_id.id,
                    'debit': movement, 'credit': 0.0}),
                (0, 0, {
                    'name': _("Write-down allowance %s", self.name),
                    'account_id': self.allowance_account_id.id,
                    'debit': 0.0, 'credit': movement}),
            ]
        else:
            amount = -movement
            lines = [
                (0, 0, {
                    'name': _("Write-down allowance release %s", self.name),
                    'account_id': self.allowance_account_id.id,
                    'debit': amount, 'credit': 0.0}),
                (0, 0, {
                    'name': _("Inventory write-down recovery %s", self.name),
                    'account_id': self.writedown_expense_account_id.id,
                    'debit': 0.0, 'credit': amount}),
            ]
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': self.reporting_date,
            'journal_id': self.journal_id.id,
            # IAS 2.36(a): the assessment basis is part of the measurement
            # policy applied, so it is disclosed on the ledger entry itself.
            'ref': _("Inventory NRV %(name)s (%(basis)s assessment)",
                     name=self.name, basis=self._basis_label()),
            'line_ids': lines,
            'eh_sealed': True,
        })
        move.action_post()
        return move
