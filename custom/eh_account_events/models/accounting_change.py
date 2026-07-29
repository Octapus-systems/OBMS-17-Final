# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 8 accounting policy changes, estimate changes and error corrections."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_is_zero


class EhAccountingChange(models.Model):
    _name = 'eh.accounting.change'
    _description = "Accounting change / error (IAS 8)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.gl.reversal',
                'eh.workflow.guard']
    _order = 'change_date desc, id desc'
    _rec_name = 'name'

    # state is a state machine driven only by the posting/reset actions (which
    # run under sudo); a direct non-superuser RPC write to it is refused by the
    # inherited eh.workflow.guard, closing the "write({'state': 'posted'}) skips
    # action_post_restatement and its journal entry" bypass.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    change_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)

    change_type = fields.Selection(
        [('policy_change', "Change in accounting policy"),
         ('estimate_change', "Change in accounting estimate"),
         ('error_correction', "Correction of prior-period error")],
        default='policy_change', required=True, tracking=True)
    application = fields.Selection(
        [('retrospective', "Retrospective (restate comparatives)"),
         ('prospective', "Prospective")],
        compute='_compute_application', store=True,
        help="Policy changes and error corrections are retrospective; "
             "estimate changes are prospective (IAS 8.19, 36, 42).")
    description = fields.Text(
        help="Nature of the change or error and the reason for it "
             "(IAS 8.29, 49).")

    line_ids = fields.One2many('eh.accounting.change.line', 'change_id')
    retained_earnings_impact = fields.Monetary(
        compute='_compute_impact', store=True, currency_field='currency_id',
        help="Net adjustment to opening retained earnings from the "
             "restatement.")
    notes = fields.Text()

    # --- Multi-period comparative trail (IAS 8.22, 42, 49) ---------------
    # Opt-in. When off (default) the register keeps the original single
    # lumped opening-retained-earnings plug: one net adjustment against one
    # adjustment_account_id. When on, each restatement line carries its own
    # affected account and prior period, and posting books a per-account
    # opening-retained-earnings restatement across one or more prior periods
    # rather than a single net figure. Existing records leave this off and
    # behave exactly as before.
    comparative_mode = fields.Boolean(
        string="Per-account comparative trail",
        tracking=True,
        help="When set, each restatement line carries its affected account "
             "and prior period; posting books the opening retained-earnings "
             "restatement per affected account across the prior periods "
             "(IAS 8.22, 42), instead of one lumped adjustment. Leave off for "
             "the single net opening-retained-earnings plug.")

    # --- Optional GL posting of the restatement (IAS 8.26, 42) ------------
    # A retrospective change or error correction adjusts opening retained
    # earnings against the restated asset/liability. Populating the three
    # accounts below and posting produces that opening balance-sheet entry;
    # a register that leaves them blank behaves exactly as before.
    state = fields.Selection(
        [('draft', "Draft"), ('posted', "Posted")],
        default='draft', required=True, tracking=True,
        help="Posted once the opening retained-earnings restatement has been "
             "written to the general ledger.")
    retained_earnings_account_id = fields.Many2one(
        'account.account', string="Retained earnings account",
        domain="[('account_type', '=', 'equity')]",
        help="Equity account carrying opening retained earnings; receives the "
             "retrospective restatement adjustment (IAS 8.26).")
    adjustment_account_id = fields.Many2one(
        'account.account', string="Adjustment (other side) account",
        help="The restated asset or liability account posted against opening "
             "retained earnings.")
    journal_id = fields.Many2one(
        'account.journal', string="Journal",
        domain="[('type', '=', 'general')]",
        help="Journal for the opening retained-earnings restatement entry.")
    move_id = fields.Many2one(
        'account.move', string="Restatement entry", readonly=True, copy=False,
        help="Journal entry posting the opening retained-earnings "
             "restatement.")
    move_count = fields.Integer(compute='_compute_move_count')

    # Fields locked once the restatement is posted, so a posted opening
    # retained-earnings adjustment cannot be retro-edited out from under its
    # journal entry. Correcting a posted restatement runs through
    # action_reset_to_draft, which reverses the move first (IAS 8 audit
    # trail). Editing the restatement lines is blocked by the same guard.
    _FROZEN_AFTER_POSTED = (
        'change_date', 'change_type', 'retained_earnings_account_id',
        'adjustment_account_id', 'journal_id', 'line_ids', 'company_id',
        'name', 'comparative_mode',
    )

    @api.depends('move_id')
    def _compute_move_count(self):
        for c in self:
            c.move_count = 1 if c.move_id else 0

    @api.depends('change_type')
    def _compute_application(self):
        for c in self:
            c.application = (
                'prospective' if c.change_type == 'estimate_change'
                else 'retrospective')

    @api.depends('line_ids.adjustment')
    def _compute_impact(self):
        for c in self:
            c.retained_earnings_impact = sum(c.line_ids.mapped('adjustment'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.accounting.change') or '/'
        return super().create(vals_list)

    def write(self, vals):
        # Posted-figure INPUTS are frozen for everyone (restate via reversal
        # through action_reset_to_draft): a data-integrity guard, not su-gated.
        # STATE transitions are enforced by the inherited eh.workflow.guard,
        # which blocks a non-superuser direct write; the sanctioned posting/
        # reset actions run under sudo. Provenance is env.su, not a context key.
        frozen = [f for f in self._FROZEN_AFTER_POSTED if f in vals]
        if frozen:
            for c in self:
                if c.state == 'posted':
                    raise UserError(_(
                        "%(name)s carries a posted opening retained-earnings "
                        "restatement; %(fields)s cannot be changed. Reset the "
                        "restatement to draft (which reverses the entry) "
                        "before editing.",
                        name=c.name, fields=', '.join(frozen)))
        return super().write(vals)

    def action_post_restatement(self):
        """Post the opening retained-earnings restatement to the GL.

        For a retrospective change or error correction, this books the
        restatement against opening retained earnings, balanced by
        construction. A change in estimate is prospective and cannot be posted
        (IAS 8.36); posting is manager-gated. Records that never post keep the
        register behaviour unchanged.

        Two posting shapes:

        * Single-adjustment (default): the net ``retained_earnings_impact`` is
          booked between the retained-earnings equity account and one restated
          asset/liability (``adjustment_account_id``).
        * Per-account comparative trail (``comparative_mode``): each
          restatement line carrying an affected ``account_id`` posts its own
          adjustment leg against that account, and the sum is booked to the
          retained-earnings account across the prior periods (IAS 8.22, 42).
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post an opening "
                "retained-earnings restatement."))
        # Run as su so the guarded state write below passes the inherited
        # eh.workflow.guard; env.user is preserved for the audit trail.
        self = self._eh_workflow_action()
        for c in self:
            if c.state == 'posted':
                raise UserError(_(
                    "%s is already posted.", c.name))
            if c.application != 'retrospective':
                raise UserError(_(
                    "Only retrospective changes and error corrections post an "
                    "opening retained-earnings restatement. A change in "
                    "accounting estimate is applied prospectively (IAS 8.36) "
                    "and cannot be posted."))
            if not c.journal_id:
                raise UserError(_(
                    "Set the journal before posting the restatement."))
            if not c.retained_earnings_account_id:
                raise UserError(_(
                    "Set the retained-earnings account before posting the "
                    "restatement."))
            currency = c.currency_id or c.company_id.currency_id
            rounding = currency.rounding or 0.01
            if c.comparative_mode:
                move_line_vals = c._eh_comparative_move_lines(currency)
            else:
                move_line_vals = c._eh_single_move_lines(currency, rounding)
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': c.journal_id.id,
                'date': c.change_date,
                'ref': _("Restatement: %s", c.name),
                'company_id': c.company_id.id,
                'line_ids': move_line_vals,
                'eh_sealed': True,
            })
            move.action_post()
            c.move_id = move.id
            c.state = 'posted'
        return True

    def _eh_single_move_lines(self, currency, rounding):
        """Original lumped opening-RE plug: net impact vs one account."""
        self.ensure_one()
        if not self.adjustment_account_id:
            raise UserError(_(
                "Set the adjustment (other side) account before posting the "
                "restatement."))
        impact = currency.round(self.retained_earnings_impact)
        if float_is_zero(impact, precision_rounding=rounding):
            raise UserError(_(
                "The restatement has no net impact on opening retained "
                "earnings; there is nothing to post."))
        # A positive impact increases opening retained earnings (a credit to
        # the equity account, debit to the adjustment account); a negative
        # impact reverses it. Legs net to zero by construction.
        re_debit = -impact if impact < 0 else 0.0
        re_credit = impact if impact > 0 else 0.0
        adj_debit = impact if impact > 0 else 0.0
        adj_credit = -impact if impact < 0 else 0.0
        return [
            (0, 0, {
                'name': _("Opening retained earnings restatement"),
                'account_id': self.retained_earnings_account_id.id,
                'debit': re_debit,
                'credit': re_credit,
            }),
            (0, 0, {
                'name': _("Restated %s", self.adjustment_account_id.name),
                'account_id': self.adjustment_account_id.id,
                'debit': adj_debit,
                'credit': adj_credit,
            }),
        ]

    def _eh_comparative_move_lines(self, currency):
        """Per-account opening-RE restatement across the prior periods.

        Each line carrying an affected account posts its own adjustment leg to
        that account; the offsetting opening-retained-earnings leg is the sum
        of those adjustments, so the entry balances by construction. The
        retained-earnings account itself is not an affected line: booking its
        adjustment there directly would double-count against the balancing leg.
        """
        self.ensure_one()
        re_account = self.retained_earnings_account_id
        account_lines = self.line_ids.filtered(
            lambda ln: ln.account_id and ln.account_id != re_account)
        if not account_lines:
            raise UserError(_(
                "The per-account comparative trail needs at least one "
                "restatement line with an affected account (other than the "
                "retained-earnings account) before it can be posted."))
        move_lines = []
        re_total = 0.0
        for line in account_lines:
            adj = currency.round(line.adjustment)
            if currency.is_zero(adj):
                continue
            # A positive adjustment raises the affected asset/liability (a
            # debit) and the offsetting opening-RE leg credits equity by the
            # same signed sum; a negative adjustment reverses both.
            move_lines.append((0, 0, {
                'name': _("Restated %(item)s%(period)s",
                          item=line.name,
                          period=line._eh_period_suffix()),
                'account_id': line.account_id.id,
                'debit': adj if adj > 0 else 0.0,
                'credit': -adj if adj < 0 else 0.0,
            }))
            re_total += adj
        re_total = currency.round(re_total)
        if not move_lines or currency.is_zero(re_total):
            raise UserError(_(
                "The per-account comparative trail has no net impact on "
                "opening retained earnings; there is nothing to post."))
        # Balancing opening-retained-earnings leg: mirror of the summed
        # per-account adjustments, so total debits equal total credits.
        move_lines.append((0, 0, {
            'name': _("Opening retained earnings restatement"),
            'account_id': re_account.id,
            'debit': -re_total if re_total < 0 else 0.0,
            'credit': re_total if re_total > 0 else 0.0,
        }))
        return move_lines

    def action_reset_to_draft(self):
        """Reverse the posted restatement and reopen the record (manager only).

        Editing a posted restatement out of band would silently alter an
        opening balance-sheet adjustment. The only sanctioned correction path
        reverses the posted move (preserving both legs in the ledger for the
        IAS 8 audit trail), clears the link, and returns the record to draft
        so a corrected restatement can be re-posted.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can reset a posted "
                "restatement to draft."))
        for c in self:
            if c.state != 'posted':
                raise UserError(_(
                    "%s is not posted; there is nothing to reset.", c.name))
            move = c.move_id
            if move:
                reversal = move._reverse_moves([{
                    'date': c.change_date,
                    'journal_id': move.journal_id.id,
                    'ref': _("Reversal of restatement: %s", c.name),
                }], cancel=False)
                reversal.action_post()
                c._eh_seal_reversal(reversal)
            # state moves to draft through sudo so the inherited
            # eh.workflow.guard recognises this as a sanctioned action write
            # (env.su), not a forgeable direct RPC re-key.
            c.sudo().write({'state': 'draft', 'move_id': False})
        return True

    def unlink(self):
        for c in self:
            if c.state == 'posted' or c.move_id:
                raise UserError(_(
                    "%s has a posted opening retained-earnings restatement "
                    "and cannot be deleted. This preserves the IAS 8 audit "
                    "trail.", c.name))
        return super().unlink()

    def action_view_move(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
        }


class EhAccountingChangeLine(models.Model):
    _name = 'eh.accounting.change.line'
    _description = "Accounting change restatement line"
    _order = 'change_id, sequence, id'

    change_id = fields.Many2one(
        'eh.accounting.change', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        related='change_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='change_id.currency_id', store=True, readonly=True)

    name = fields.Char(
        required=True,
        help="Affected financial-statement line item.")
    # Optional per-line prior period the figures belong to (IAS 8.22, 42:
    # each prior period presented is restated). Left blank the line behaves
    # exactly as before; populated it lets one register carry the comparative
    # trail across more than one prior year.
    fiscal_year = fields.Integer(
        string="Prior year",
        help="Prior financial year these comparative figures relate to. Used "
             "by the per-account comparative trail to restate more than one "
             "prior period.")
    period_label = fields.Char(
        string="Period",
        help="Free-form label for the prior period (for example a half-year "
             "or quarter) when a bare year is not enough.")
    # Optional affected general-ledger account. Populated only for the
    # per-account comparative trail, where each affected line posts its own
    # adjustment leg to this account against opening retained earnings.
    account_id = fields.Many2one(
        'account.account', string="Affected account",
        help="General-ledger account this restatement line affects. Set only "
             "for the per-account comparative trail; the adjustment is posted "
             "to this account against opening retained earnings.")
    as_previously_reported = fields.Monetary(currency_field='currency_id')
    adjustment = fields.Monetary(
        currency_field='currency_id',
        help="Restatement adjustment (signed).")
    as_restated = fields.Monetary(
        compute='_compute_restated', store=True, currency_field='currency_id')

    _FROZEN_LINE_FIELDS = (
        'name', 'as_previously_reported', 'adjustment', 'sequence',
        'account_id', 'fiscal_year', 'period_label')

    def _eh_period_suffix(self):
        """Readable ' (period)' suffix for a move line, or '' when unset."""
        self.ensure_one()
        label = self.period_label or (
            str(self.fiscal_year) if self.fiscal_year else '')
        return _(" (%s)", label) if label else ''

    @api.depends('as_previously_reported', 'adjustment')
    def _compute_restated(self):
        for line in self:
            line.as_restated = line.as_previously_reported + line.adjustment

    @api.model_create_multi
    def create(self, vals_list):
        # Appending a line to a posted restatement would recompute the stored
        # retained_earnings_impact off the posted move, silently drifting the
        # parent figures away from the entry in the ledger. Freezing a posted
        # restatement therefore requires a create guard alongside write/unlink;
        # a create-append hole is not optional. The sanctioned correction path
        # (action_reset_to_draft) flips the parent back to draft before any
        # line is added, so it passes this guard unchanged.
        changes = self.env['eh.accounting.change'].browse(
            [v['change_id'] for v in vals_list if v.get('change_id')])
        for change in changes:
            if change.state == 'posted':
                raise UserError(_(
                    "%s is posted; a restatement line cannot be added. Reset "
                    "the restatement to draft (which reverses the entry) "
                    "before editing.", change.name))
        return super().create(vals_list)

    def _eh_check_parent_not_posted(self, action):
        # A posted restatement is booked to the GL; its lines feed the stored
        # retained_earnings_impact and the disclosure, so a direct line edit
        # or unlink would drift the record off the posted move. Block it unless
        # the restatement has first been reset to draft (which reverses the
        # entry). The parent reset flips the parent to draft before any line
        # edits, so the sanctioned path passes.
        for line in self:
            if line.change_id.state == 'posted':
                raise UserError(_(
                    "This restatement is posted; its lines cannot be %s. "
                    "Reset the restatement to draft (which reverses the "
                    "entry) before editing.", action))

    def write(self, vals):
        if any(f in vals for f in self._FROZEN_LINE_FIELDS):
            self._eh_check_parent_not_posted(_("changed"))
        return super().write(vals)

    def unlink(self):
        self._eh_check_parent_not_posted(_("removed"))
        return super().unlink()
