# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.consol.run.line: one row in the consolidated trial balance.

The consolidation run produces one row per (account, kind, source).
Kinds:

  parent_balance: parent company's own balance.
  subsidiary_balance: a subsidiary's translated balance.
  elimination: an elimination journal line.
  equity_pickup: IAS 28 share-of-profit pick-up for an associate.
  cta: currency translation adjustment under IAS 21, split per member.
  cta_recycle: IAS 21.48 disposal reclassification of a member's CTA.
  nci: non-controlling interest carve-out (proportionate or fair value).
  goodwill: IFRS 3 goodwill / bargain-purchase residual.
  impairment: IAS 36 goodwill impairment charge.
  disclosure: zero-amount memo row (e.g. an IAS 28.1A election).

Each row carries the amount in the consolidation entity's
presentation currency. The aggregation view sums these by account
to produce the consolidated trial balance.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


# States in which a run's consolidated figures are settled and must not be
# extended by a hand-added line. The engine rebuilds every line from scratch
# while the run is still 'draft' (action_compute sets 'computed' only AFTER
# _build_lines), and later engine paths (IAS 36 impairment) create lines in
# 'computed' / 'reviewed'. The guard exempts the engine's own build path,
# which flags itself with this context key, and only ever fires on a manual
# append.
_FROZEN_APPEND_STATES = ('computed', 'reviewed', 'closed')

# Context key set by the run engine's controlled build path (see
# eh.consol.run._build_lines and action_impair_goodwill) so its own line
# creation is exempt from the frozen-append guard. env.su alone is not a
# reliable engine signal: the admin / superuser runs with su=True yet must
# still be blocked from hand-appending to a settled run.
CONSOL_ENGINE_CTX = 'eh_consol_engine_build'


_KIND_CHOICES = [
    ('parent_balance', "Parent balance"),
    ('subsidiary_balance', "Subsidiary balance"),
    ('elimination', "Elimination"),
    ('equity_pickup', "Equity pick-up (IAS 28)"),
    ('cta', "CTA (IAS 21)"),
    ('cta_recycle', "CTA recycling (IAS 21.48)"),
    ('nci', "Non-controlling interest"),
    ('goodwill', "Goodwill (IFRS 3)"),
    ('impairment', "Goodwill impairment (IAS 36)"),
    ('disclosure', "Disclosure (memo)"),
]


class EhConsolRunLine(models.Model):
    _name = 'eh.consol.run.line'
    _description = "Consolidation run line"
    _order = 'run_id, account_id, kind'

    run_id = fields.Many2one(
        'eh.consol.run', required=True,
        ondelete='cascade', index=True,
    )
    presentation_currency_id = fields.Many2one(
        related='run_id.presentation_currency_id',
        store=True, readonly=True,
    )

    account_id = fields.Many2one(
        'account.account',
        index=True,
        help=(
            "Account on the consolidated chart. May be empty for "
            "CTA / NCI lines when no matching account is configured."
        ),
    )
    account_code = fields.Char(
        string="Account Code", compute='_compute_account_snapshot',
        store=True, readonly=True,
        help="Snapshot of the account code at run time. account.account.code "
             "is a non-stored, company-dependent compute in Odoo 19, so it is "
             "captured here as a plain stored value for a reproducible run.",
    )
    account_name = fields.Char(
        string="Account Name", compute='_compute_account_snapshot',
        store=True, readonly=True,
        help="Snapshot of the account name at run time. Stored as a plain "
             "label so the consolidation run stays reproducible regardless "
             "of the active language.",
    )
    account_type = fields.Selection(
        related='account_id.account_type', store=True, readonly=True,
    )

    kind = fields.Selection(
        _KIND_CHOICES, required=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        help="Source company. Empty for CTA + multi-source rows.",
    )
    member_id = fields.Many2one(
        'eh.consol.member',
        help="Source member, when the row originates from a subsidiary.",
    )
    elimination_id = fields.Many2one(
        'eh.consol.elimination',
        help="Source elimination, when the row originates from one.",
    )
    cta_position_id = fields.Many2one(
        'eh.fx.cta.position', string="CTA Position",
        ondelete='set null',
        help=(
            "CTA reserve position (eh_account_fx_revaluation) the row's "
            "translation reserve slice belongs to. Carried on kind='cta' "
            "and kind='cta_recycle' rows of members linked to a position, "
            "so the run-side reserve movement is traceable to the "
            "parent-books registry (IAS 21.48)."
        ),
    )

    amount = fields.Monetary(
        currency_field='presentation_currency_id',
        help="Amount in the consolidation presentation currency.",
    )

    notes = fields.Char()

    @api.depends('account_id')
    def _compute_account_snapshot(self):
        for line in self:
            line.account_code = line.account_id.code or False
            line.account_name = line.account_id.name or False

    def _check_run_not_frozen(self):
        """Block any hand edit or delete of a line whose run is settled
        (computed / reviewed / closed).

        A settled run's consolidated figures are engine generated and, once
        closed, signed and cited in audit; its lines must be frozen
        (reproducible). Editing or deleting a line by hand would silently move
        the consolidated totals, so it is refused for the full settled set, not
        only 'closed' (matching the create-append guard). The engine's own
        controlled paths (recompute drops lines while the run is draft; the IAS
        36 impairment test drops its prior impairment lines on a computed /
        reviewed run) flag themselves with CONSOL_ENGINE_CTX and are exempt, so
        this guard only ever fires on a manual tamper. env.su alone is not an
        engine signal: even the superuser is blocked unless the engine context
        is set.
        """
        if self.env.context.get(CONSOL_ENGINE_CTX):
            return
        for line in self:
            if line.run_id.state in _FROZEN_APPEND_STATES:
                raise UserError(_(
                    "Run %(run)s is %(state)s. Its consolidation lines are "
                    "frozen and cannot be changed or deleted by hand; that "
                    "would silently move the consolidated totals. Reset the "
                    "run to draft and recompute instead.",
                    run=line.run_id.name, state=line.run_id.state,
                ))

    @api.model_create_multi
    def create(self, vals_list):
        """Block a manual append to a settled run.

        A closed / reviewed / computed run's consolidated totals are settled
        and (once closed) signed and cited in audit. Its lines are engine
        generated: the engine rebuilds them from scratch while the run is
        still draft, and the IAS 36 impairment path adds lines afterwards. A
        user (even a manager, who holds create rights on this model, and even
        the superuser) appending a line to such a run would silently move the
        consolidated figures and defeat the frozen-run control, so create is
        refused for any caller whose target run is in a settled state. The
        engine's own build path flags itself with the CONSOL_ENGINE_CTX
        context key and is exempt.
        """
        if not self.env.context.get(CONSOL_ENGINE_CTX):
            Run = self.env['eh.consol.run'].sudo()
            for vals in vals_list:
                run_id = vals.get('run_id')
                if not run_id:
                    continue
                run = Run.browse(run_id)
                if run.state in _FROZEN_APPEND_STATES:
                    raise UserError(_(
                        "Run %s is %s. Its consolidation lines are engine "
                        "generated and cannot be added by hand; adding a line "
                        "would silently change the consolidated totals. Reset "
                        "the run to draft and recompute instead.",
                        run.name, run.state,
                    ))
        return super().create(vals_list)

    def write(self, vals):
        self._check_run_not_frozen()
        return super().write(vals)

    def unlink(self):
        self._check_run_not_frozen()
        return super().unlink()
