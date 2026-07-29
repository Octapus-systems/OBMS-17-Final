# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.fx.cta.position: CTA reserve registry (IAS 21).

A CTA position is the parent-books ledger anchor for the cumulative
foreign currency translation reserve of ONE foreign operation. It does
not translate the foreign operation itself (that lives in the
consolidation engine); it owns the equity reserve account slice on the
parent's books and the IAS 21.48 disposal mechanics:

* Net investment hedges (IFRS 9 6.5.13) park their effective portion
  in the position's CTA equity account, tagging the journal entry to
  the position, so the position balance is ledger-fed at all times.
* Consolidation exports can point their translation differences at a
  position the same way (any posted account.move carrying
  eh_cta_position_id feeds the balance).
* On disposal of the foreign operation the FULL accumulated balance,
  including NIH effective portions parked there, is reclassified from
  equity to profit or loss (IAS 21.48). A partial disposal of an
  associate or joint venture reclassifies the proportionate share
  (IAS 21.48A-C, simplified to pct of balance).

Balance sign convention: credit minus debit on the CTA account lines
of posted moves tagged to the position. Positive = accumulated net
translation GAIN sitting in equity; negative = accumulated net loss.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# Fields that define what the accumulated reserve balance means. Once the
# position is disposed its balance has been reclassified to P&L against
# these exact settings; changing them afterwards would orphan the audit
# trail behind the posted reclassification entry.
_EH_FROZEN_POSITION_FIELDS = (
    'name',
    'company_id',
    'cta_account_id',
    'foreign_operation_partner_id',
    'foreign_operation_company_id',
    'operation_currency_id',
)


class EhFxCtaPosition(models.Model):
    _name = 'eh.fx.cta.position'
    _description = "CTA Reserve Position (IAS 21)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'company_id, name, id'

    # 'disposed' is reached only through action_dispose, which posts the IAS
    # 21.48 reclassification entry. Block any direct write to state that does
    # not originate from that action (which flags the write).
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        required=True, tracking=True,
        help=(
            "Free label for the foreign operation this reserve slice "
            "belongs to, e.g. 'Net investment in DE subsidiary'."
        ),
    )
    state = fields.Selection(
        [
            ('open', "Open"),
            ('disposed', "Disposed"),
        ],
        default='open', required=True, tracking=True, index=True,
        help=(
            "open: the reserve accumulates translation differences and "
            "NIH effective portions. disposed: the foreign operation "
            "was disposed of and the full balance was reclassified to "
            "P&L (IAS 21.48); the position is frozen."
        ),
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True,
    )

    foreign_operation_partner_id = fields.Many2one(
        'res.partner', string="Foreign Operation (Partner)",
        ondelete='restrict',
        help=(
            "Partner record of the foreign operation, when the "
            "subsidiary / associate / JV is tracked as a partner."
        ),
    )
    foreign_operation_company_id = fields.Many2one(
        'res.company', string="Foreign Operation (Company)",
        ondelete='restrict',
        help=(
            "Company record of the foreign operation, when the "
            "subsidiary lives in this database (consolidation exports "
            "point here)."
        ),
    )
    operation_currency_id = fields.Many2one(
        'res.currency', string="Operation Currency",
        help=(
            "Functional currency of the foreign operation. "
            "Informational: the reserve itself is carried in the "
            "parent's presentation currency."
        ),
    )

    cta_account_id = fields.Many2one(
        'account.account', string="CTA Equity Account", required=True,
        domain="[('account_type', 'in', ('equity', 'equity_unaffected'))]",
        help=(
            "Equity account carrying this position's slice of the "
            "foreign currency translation reserve. NIH effective "
            "portions and consolidation translation differences tagged "
            "to the position post here."
        ),
    )
    journal_id = fields.Many2one(
        'account.journal', string="Journal",
        domain="[('type', '=', 'general')]",
        help="Journal used to post the disposal reclassification.",
    )
    gain_account_id = fields.Many2one(
        'account.account', string="Reclass Gain Account",
        domain="[('account_type', '=', 'income_other')]",
        help=(
            "P&L account credited when a positive (gain) reserve "
            "balance is reclassified on disposal."
        ),
    )
    loss_account_id = fields.Many2one(
        'account.account', string="Reclass Loss Account",
        domain="[('account_type', '=', 'expense')]",
        help=(
            "P&L account debited when a negative (loss) reserve "
            "balance is reclassified on disposal."
        ),
    )

    balance = fields.Monetary(
        compute='_compute_balance', currency_field='currency_id',
        help=(
            "Ledger-fed accumulated reserve balance: credit minus "
            "debit on the CTA account lines of posted journal entries "
            "tagged to this position. Positive = accumulated net "
            "translation gain."
        ),
    )
    move_count = fields.Integer(compute='_compute_move_count')

    disposal_pct = fields.Float(
        string="Disposal %", default=100.0, digits=(5, 2),
        help=(
            "Share of the accumulated balance to reclassify on the "
            "next Dispose. 100 disposes the whole operation and closes "
            "the position (IAS 21.48). Below 100 reclassifies the "
            "proportionate share for a partial disposal of an "
            "associate or JV (IAS 21.48A-C) and keeps the position "
            "open."
        ),
    )
    disposal_move_id = fields.Many2one(
        'account.move', string="Disposal Entry", readonly=True,
        copy=False, ondelete='restrict',
        help="Reclassification entry of the final (full) disposal.",
    )
    disposed_at = fields.Datetime(readonly=True, tracking=True)
    disposed_by_id = fields.Many2one('res.users', readonly=True)

    notes = fields.Text()

    # ---- computes ----

    def _compute_balance(self):
        MoveLine = self.env['account.move.line']
        for pos in self:
            if not pos.ids or not pos.cta_account_id:
                pos.balance = 0.0
                continue
            lines = MoveLine.search([
                ('move_id.eh_cta_position_id', '=', pos.id),
                ('account_id', '=', pos.cta_account_id.id),
                ('parent_state', '=', 'posted'),
            ])
            pos.balance = (
                sum(lines.mapped('credit')) - sum(lines.mapped('debit'))
            )

    def _compute_move_count(self):
        Move = self.env['account.move']
        for pos in self:
            pos.move_count = Move.search_count([
                ('eh_cta_position_id', '=', pos.id),
            ]) if pos.ids else 0

    # ---- constraints ----

    @api.constrains('cta_account_id')
    def _check_cta_account_equity(self):
        for pos in self:
            if pos.cta_account_id and pos.cta_account_id.account_type \
                    not in ('equity', 'equity_unaffected'):
                raise ValidationError(_(
                    "CTA position %(name)s: the CTA account must be an "
                    "equity account. The translation reserve is a "
                    "component of equity until disposal (IAS 21.39(c)).",
                    name=pos.display_name,
                ))

    @api.constrains('disposal_pct')
    def _check_disposal_pct(self):
        for pos in self:
            if pos.disposal_pct <= 0.0 or pos.disposal_pct > 100.0:
                raise ValidationError(_(
                    "Disposal %% must be greater than 0 and at most "
                    "100. Got %.2f.",
                ) % pos.disposal_pct)

    # ---- ORM guards ----

    @api.model_create_multi
    def create(self, vals_list):
        # A position reaches 'disposed' only through action_dispose, which
        # posts the reclassification entry. Creating one directly disposed
        # would fabricate a closed reserve with no entry behind it.
        for vals in vals_list:
            if vals.get('state') and vals['state'] != 'open':
                raise UserError(_(
                    "A CTA position is created open and reaches the "
                    "disposed state only through the Dispose action, "
                    "which posts the IAS 21.48 reclassification entry.",
                ))
        return super().create(vals_list)

    def write(self, vals):
        touched = [f for f in _EH_FROZEN_POSITION_FIELDS if f in vals]
        if touched:
            disposed = self.filtered(lambda p: p.state == 'disposed')
            if disposed:
                raise UserError(_(
                    "CTA position %(name)s is disposed: its identity "
                    "fields are frozen (%(fields)s) because the full "
                    "reserve balance has been reclassified to P&L "
                    "against them (IAS 21.48).",
                    name=disposed[0].display_name,
                    fields=', '.join(touched),
                ))
        # The state of a disposed position is a control point: resetting it
        # to open would silently lift the freeze above and reopen a reserve
        # whose balance was already recycled to P&L. A raw ORM state write
        # without the sanctioned-transition flag is manager-gated.
        if 'state' in vals \
                and not self.env.context.get('eh_cta_state_change'):
            crossing = self.filtered(
                lambda p: p.state == 'disposed' and p.state != vals['state'])
            if crossing:
                crossing._check_manager()
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager may change the state of "
                "a disposed CTA position."))

    def unlink(self):
        disposed = self.filtered(lambda p: p.state == 'disposed')
        if disposed:
            raise UserError(_(
                "A disposed CTA position cannot be deleted; it is the "
                "audit anchor of the posted IAS 21.48 reclassification "
                "entry."))
        Move = self.env['account.move']
        for pos in self:
            if Move.search_count([('eh_cta_position_id', '=', pos.id)]):
                raise UserError(_(
                    "CTA position %(name)s has journal entries tagged "
                    "to it and cannot be deleted; the position is the "
                    "ledger anchor of those entries.",
                    name=pos.display_name,
                ))
        return super().unlink()

    # ---- actions ----

    def action_dispose(self):
        """Reclassify the accumulated CTA balance to P&L (IAS 21.48).

        At disposal_pct == 100 the FULL accumulated balance, including
        NIH effective portions parked in the reserve, moves from the
        CTA equity account to profit or loss and the position closes.
        Below 100 the proportionate share is reclassified (IAS
        21.48A-C simplified: pct of the ledger balance) and the
        position stays open for the retained interest.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only accounting managers can dispose a CTA position. "
                "Disposal recycles the translation reserve into profit "
                "or loss and is a segregation-of-duties control.",
            ))
        self = self._eh_workflow_action()
        for pos in self:
            if pos.state != 'open':
                raise UserError(_(
                    "CTA position %(name)s is already disposed; the "
                    "reserve was reclassified in full and cannot be "
                    "disposed again.",
                    name=pos.display_name,
                ))
            pct = pos.disposal_pct or 0.0
            if pct <= 0.0 or pct > 100.0:
                raise UserError(_(
                    "Disposal %% must be greater than 0 and at most "
                    "100. Got %.2f.",
                ) % pct)
            if not pos.journal_id or not pos.gain_account_id \
                    or not pos.loss_account_id:
                raise UserError(_(
                    "CTA position %(name)s needs a journal, a reclass "
                    "gain account and a reclass loss account before "
                    "disposal.",
                    name=pos.display_name,
                ))
            company_ccy = pos.company_id.currency_id
            pos.invalidate_recordset(['balance'])
            balance = pos.balance
            if company_ccy.is_zero(balance):
                raise UserError(_(
                    "CTA position %(name)s has a zero accumulated "
                    "balance; there is nothing to reclassify.",
                    name=pos.display_name,
                ))
            full = company_ccy.compare_amounts(pct, 100.0) == 0
            amount = company_ccy.round(abs(balance) * pct / 100.0)
            today = fields.Date.context_today(self)
            label = _("CTA disposal reclassification %(name)s (%(pct).2f%%)",
                      name=pos.name, pct=pct)
            # Balance sign is credit-positive on the equity reserve:
            #   positive balance = accumulated gain -> Dr CTA / Cr gain P&L
            #   negative balance = accumulated loss -> Dr loss P&L / Cr CTA
            if balance > 0:
                lines = [
                    (0, 0, {
                        'name': label,
                        'account_id': pos.cta_account_id.id,
                        'debit': amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': label,
                        'account_id': pos.gain_account_id.id,
                        'debit': 0.0,
                        'credit': amount,
                    }),
                ]
            else:
                lines = [
                    (0, 0, {
                        'name': label,
                        'account_id': pos.loss_account_id.id,
                        'debit': amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': label,
                        'account_id': pos.cta_account_id.id,
                        'debit': 0.0,
                        'credit': amount,
                    }),
                ]
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': pos.journal_id.id,
                'date': today,
                'ref': _("CTA disposal %s", pos.name),
                'line_ids': lines,
                'eh_sealed': True,
                'eh_cta_position_id': pos.id,
            })
            move.action_post()
            pos.invalidate_recordset(['balance'])
            if full:
                pos.with_context(eh_cta_state_change=True).write({
                    'state': 'disposed',
                    'disposed_at': fields.Datetime.now(),
                    'disposed_by_id': self.env.user.id,
                    'disposal_move_id': move.id,
                })
                pos.message_post(body=_(
                    "Foreign operation disposed: the full accumulated "
                    "CTA balance of %(amount).2f was reclassified from "
                    "equity to profit or loss (IAS 21.48). Entry "
                    "%(move)s.",
                    amount=abs(balance), move=move.name,
                ))
            else:
                pos.message_post(body=_(
                    "Partial disposal: %(pct).2f%% of the accumulated "
                    "CTA balance (%(amount).2f) was reclassified to "
                    "profit or loss (IAS 21.48A-C). The position stays "
                    "open for the retained interest. Entry %(move)s.",
                    pct=pct, amount=amount, move=move.name,
                ))
        return True

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("CTA Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('eh_cta_position_id', '=', self.id)],
            'context': {'create': False},
        }


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_cta_position_id = fields.Many2one(
        'eh.fx.cta.position', string="CTA Position",
        index='btree_not_null', copy=False, ondelete='restrict',
        help=(
            "CTA reserve position this entry feeds. NIH effective "
            "portions, consolidation translation exports and disposal "
            "reclassifications carry this tag so the position balance "
            "is computed straight from the ledger."
        ),
    )
