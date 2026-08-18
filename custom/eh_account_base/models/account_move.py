# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move extension: bump the per company move version counter on state
changes. Drives reporting cache invalidation.

Hooks the write() method rather than _post() so every state transition
(post, draft, cancel) participates uniformly. Posting is implemented via
write({'state': 'posted'}) under the hood, so this single hook covers all
transitions without missing edge cases.

account.move.line extension: a posted move can still have its lines edited
in an unlocked period (the framework only blocks this when a period lock
covers the line date). Such an edit changes report figures without any
state transition, so the state-only hook above would let the reporting
cache serve a stale payload. The line write() hook below bumps the counter
whenever a financially-material field (amount, account, or date) changes on
a line whose parent move is posted, and only then, so unrelated writes
(analytic tags, narration, reconciliation bookkeeping) do not over-bump.

Adding a new line to, or removing an existing line from, an already-posted
move likewise changes report figures with no state transition. The line
create() and unlink() hooks below bump the counter whenever the affected
line belongs to a posted move, and only then, so building up a draft entry
before action_post does not over-bump.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Fields whose value feeds report figures. A change to any of these on a
# posted move's line must invalidate the reporting cache.
_EH_MATERIAL_LINE_FIELDS = frozenset({
    'debit',
    'credit',
    'balance',
    'amount_currency',
    'account_id',
    'date',
})

# Context flag the sanctioned reversal / reset paths set so their own unpost or
# figure edit passes the seal below. Everything else is refused.
_EH_ALLOW_UNPOST = 'eh_allow_unpost'


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_sealed = fields.Boolean(
        default=False, copy=False, index=True,
        help="Set by an ERP Heritage sub-ledger when this journal entry is the "
             "posted GL counterpart of a frozen figure (a provision, revenue "
             "contract, asset, tax run, and so on). A sealed posted entry "
             "cannot be reset to draft or have its figures edited in place; it "
             "is unwound only by reversing the source record, which posts a "
             "reversing entry and preserves the audit trail.")

    def _eh_sealed_posted(self):
        return self.filtered(lambda m: m.eh_sealed and m.state == 'posted')

    def _eh_guard_sealed(self, action):
        # The unseal is permitted ONLY when it is server-initiated (env.su)
        # AND explicitly marked (the context key). A context key alone is
        # forgeable over RPC, so it can never be the sole gate; su proves the
        # call came from a sanctioned sudo() reversal path, not a crafted
        # client request that set the key itself.
        if self.env.context.get(_EH_ALLOW_UNPOST) and self.env.su:
            return
        sealed = self._eh_sealed_posted()
        if sealed:
            raise UserError(_(
                "Journal entry %(names)s is the posted counterpart of an ERP "
                "Heritage sub-ledger figure and cannot be %(action)s directly. "
                "Reverse the source record instead: it posts a reversing entry "
                "and re-opens the figure, preserving the audit trail.",
                names=', '.join(sealed.mapped('name') or ['/']),
                action=action))

    def button_draft(self):
        self._eh_guard_sealed(_("reset to draft"))
        return super().button_draft()

    def button_cancel(self):
        self._eh_guard_sealed(_("cancelled"))
        return super().button_cancel()

    @api.model_create_multi
    def create(self, vals_list):
        """Bump version when a move is created already in posted state.

        Common case is unaffected (moves are created in draft and posted via
        action_post, which goes through write()). This override exists for the
        rare paths that create directly with state='posted', so the cache stays
        consistent.
        """
        moves = super().create(vals_list)
        posted = moves.filtered(lambda m: m.state == 'posted')
        if posted:
            company_ids = set(posted.mapped('company_id.id'))
            if company_ids:
                self.env['res.company'].sudo()._eh_bump_move_version(company_ids)
        return moves

    def write(self, vals):
        # A sealed posted entry must not be reset to draft or cancelled by a
        # raw ORM write (the button guards above only cover the UI path); the
        # sanctioned reversal / reset sets the context flag.
        if vals.get('state') in ('draft', 'cancel') \
                and not (self.env.context.get(_EH_ALLOW_UNPOST)
                         and self.env.su):
            self._eh_guard_sealed(_("reset to draft"))
        # Snapshot the prior state per id so we only bump the move
        # version counter when state actually changes. The previous
        # implementation bumped on every write that *included* state in
        # vals, even when the new value matched the old, which produced
        # spurious cache invalidation during routine recomputes.
        if 'state' in vals and self:
            new_state = vals['state']
            changed_ids = [m.id for m in self if m.state != new_state]
        else:
            changed_ids = []
        result = super().write(vals)
        if changed_ids:
            changed = self.browse(changed_ids)
            company_ids = set(changed.mapped('company_id.id'))
            if company_ids:
                self.env['res.company'].sudo()._eh_bump_move_version(company_ids)
        return result


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    @api.model_create_multi
    def create(self, vals_list):
        # Adding a line to an already-posted move (in an unlocked period)
        # changes report figures with no state transition, so the write()
        # hook alone would let the reporting cache serve a stale payload.
        # Bump the version for every newly created line whose parent move is
        # already posted. Lines on draft moves (the common case: entry built
        # up before action_post) do not bump, so we do not over-bump.
        lines = super().create(vals_list)
        posted = lines.filtered(lambda line: line.move_id.state == 'posted')
        # A new line added to a SEALED posted move would move the figure.
        lines._eh_guard_sealed_lines()
        bump_company_ids = set(posted.mapped('company_id.id'))
        if bump_company_ids:
            self.env['res.company'].sudo()._eh_bump_move_version(
                bump_company_ids)
        return lines

    def unlink(self):
        # Removing a line from a SEALED posted move would move the figure.
        self._eh_guard_sealed_lines()
        # Removing a line from an already-posted move (in an unlocked period)
        # changes report figures with no state transition. Snapshot the
        # affected companies before the delete, since the records are gone
        # after super().unlink(). Only posted moves bump, so removing lines
        # from a draft move does not over-bump.
        posted = self.filtered(lambda line: line.move_id.state == 'posted')
        bump_company_ids = set(posted.mapped('company_id.id'))
        result = super().unlink()
        if bump_company_ids:
            self.env['res.company'].sudo()._eh_bump_move_version(
                bump_company_ids)
        return result

    def _eh_guard_sealed_lines(self):
        if self.env.context.get(_EH_ALLOW_UNPOST) and self.env.su:
            return
        sealed = self.filtered(
            lambda line: line.move_id.eh_sealed
            and line.move_id.state == 'posted')
        if sealed:
            raise UserError(_(
                "The figures on journal entry %s are frozen: it is the posted "
                "counterpart of an ERP Heritage sub-ledger figure. Reverse the "
                "source record to change them.",
                ', '.join(sealed.mapped('move_id.name') or ['/'])))

    def write(self, vals):
        # Editing a financially-material field on a line of a SEALED posted
        # move would desync the sub-ledger figure from its GL entry; refuse it.
        if _EH_MATERIAL_LINE_FIELDS.intersection(vals):
            self._eh_guard_sealed_lines()
        # Only a change to a financially-material field on a line belonging
        # to a posted move affects report figures. Snapshot the affected
        # companies before the write so a change to the line's account (and
        # therefore, on some series, to the derived company) is captured
        # against the company the figures were reported under.
        bump_company_ids = set()
        if self and _EH_MATERIAL_LINE_FIELDS.intersection(vals):
            posted = self.filtered(lambda line: line.move_id.state == 'posted')
            bump_company_ids = set(posted.mapped('company_id.id'))
        result = super().write(vals)
        if bump_company_ids:
            self.env['res.company'].sudo()._eh_bump_move_version(
                bump_company_ids)
        return result


class AccountPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    # Reconciliation changes no material line field and performs no move state
    # transition, yet it changes cash-basis recognition (income/expense
    # recognised in proportion to the matched amount as of a date) and aging.
    # Those figures key on the per-company eh_move_version counter, so
    # reconciling / un-reconciling must bump it or cash-basis and aged reports
    # serve stale cached numbers until an unrelated move posts.

    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        companies = recs.company_id
        if companies:
            self.env['res.company'].sudo()._eh_bump_move_version(companies.ids)
        return recs

    def unlink(self):
        company_ids = set(self.mapped('company_id.id'))
        result = super().unlink()
        if company_ids:
            self.env['res.company'].sudo()._eh_bump_move_version(company_ids)
        return result
