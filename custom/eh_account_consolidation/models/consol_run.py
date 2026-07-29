# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.consol.run: one consolidation pass for one period.

Lifecycle:

  draft -> computed -> reviewed -> closed
                    \\-> draft (manager reset)

draft: parameters being entered.
computed: per-member trial balances pulled, translated, summed; CTA
booked; NCI carved; eliminations applied. Lines on consol_run_line.
reviewed: a manager has signed off; eliminations are locked.
closed: the run is read-only and cited in audit.
"""

import logging

from collections import defaultdict
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_is_zero

from odoo.addons.eh_account_base.tools.orm_compat import grouped_sum

from .consol_run_line import CONSOL_ENGINE_CTX


def _acc_company_field(env):
    # account.account is multi-company (company_ids) from Odoo 18;
    # single company_id before that.
    Account = env['account.account']
    return 'company_ids' if 'company_ids' in Account._fields else 'company_id'


_logger = logging.getLogger(__name__)


_INCOME_TYPES = ('income', 'income_other')
_EXPENSE_TYPES = ('expense', 'expense_depreciation', 'expense_direct_cost')


# States in which the run's consolidated figures back a posted / audited GL
# figure and are frozen at the ORM write layer.
_POSTED_STATES = ('computed', 'reviewed', 'closed')

# Identity fields that fix which entity and period a run consolidates. Once the
# run is computed / reviewed / closed they are frozen at the write layer; change
# them by resetting to draft. 'state' is deliberately NOT listed: the action
# methods write only state + audit stamps, so a pure state-transition write
# carries no frozen field and always passes.
_FROZEN_AFTER_CONFIRM = (
    'entity_id', 'period_from', 'period_to',
    'override_policy_checks', 'override_policy_reason',
)

# Kinds whose amounts are genuine consolidated-figure legs; the CTA plug is
# their negated sum. 'impairment' and 'cta_recycle' are excluded: they are
# balanced pairs added AFTER compute and never disturb the plug.
_CTA_BASE_KINDS = (
    'subsidiary_balance', 'parent_balance', 'elimination',
    'equity_pickup', 'nci', 'goodwill',
)

# IFRS 10.B93: the gap between a member's own reporting date and the group's
# may never exceed three months.
_MAX_REPORTING_OFFSET_MONTHS = 3

# Goodwill impairment-test fields. These are legitimately set by the manager
# during the IAS 36 impairment test, which runs on a computed / reviewed run
# BEFORE the consolidation move is posted (action_impair_goodwill refuses once
# the move is posted). They therefore freeze only once the consolidation move
# is posted, at which point goodwill_impairment_amount is a booked GL figure.
_FROZEN_AFTER_POSTED_MOVE = (
    'goodwill_recoverable_amount', 'goodwill_impairment_amount',
    'goodwill_impairment_account_id',
)


class EhConsolRun(models.Model):
    _name = 'eh.consol.run'
    _description = "Consolidation run"
    _order = 'period_to desc, id desc'
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.gl.reversal',
        'eh.workflow.guard',
    ]

    # State advances only through the run's own actions (compute / review /
    # close / reset / reopen), which run under sudo. A direct non-superuser
    # write to 'state' is refused by eh.workflow.guard, closing the
    # RPC-write-past-the-action bypass.
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    entity_id = fields.Many2one(
        'eh.consol.entity', required=True,
        ondelete='restrict', index=True, tracking=True,
    )
    presentation_currency_id = fields.Many2one(
        related='entity_id.presentation_currency_id',
        store=True, readonly=True,
    )

    period_from = fields.Date(
        required=True, tracking=True,
        help="Inclusive start of the consolidation period.",
    )
    period_to = fields.Date(
        required=True, tracking=True,
        help=(
            "Inclusive end of the consolidation period. Closing "
            "rates for IAS 21 translation are sourced as at this "
            "date."
        ),
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('computed', "Computed"),
            ('reviewed', "Reviewed"),
            ('closed', "Closed"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )

    line_ids = fields.One2many(
        'eh.consol.run.line', 'run_id', copy=False,
    )
    line_count = fields.Integer(compute='_compute_counts', store=False)
    elimination_ids = fields.One2many(
        'eh.consol.elimination', 'run_id', copy=True,
    )
    elimination_count = fields.Integer(
        compute='_compute_counts', store=False,
    )
    unrealised_profit_ids = fields.One2many(
        'eh.consol.unrealised.profit', 'run_id', copy=True,
        help=(
            "Unrealised profit sitting in ending inventory from intra-group "
            "sales. Each record adds a balanced elimination (Dr COGS / "
            "retained earnings, Cr inventory) at compute time so the "
            "consolidated set removes the intra-group margin (IFRS 10 / "
            "IAS 27)."
        ),
    )

    cta_amount = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='presentation_currency_id',
        help=(
            "Currency translation adjustment for this run. The "
            "balancing OCI entry that arises when the closing-rate "
            "translation of the balance sheet does not match the "
            "average-rate translation of the P&L."
        ),
    )
    nci_amount = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='presentation_currency_id',
        help=(
            "Sum of non-controlling interest carved out across "
            "subsidiaries with ownership < 100%."
        ),
    )

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, tracking=True,
        ondelete='restrict',
        help=(
            "The posted consolidation journal entry booked into the entity's "
            "dedicated consolidation ledger company, when one is configured. "
            "Once posted the move is immutable via Odoo's posted state; "
            "reopening or resetting the run reverses and unlinks it. Empty "
            "for a memo-only run (no consolidation company set)."
        ),
    )
    move_state = fields.Selection(
        related='move_id.state', string="Move status", readonly=True,
    )
    consolidation_company_id = fields.Many2one(
        related='entity_id.consolidation_company_id', readonly=True,
    )

    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True)
    reviewed_at = fields.Datetime(readonly=True, tracking=True)
    reviewed_by_id = fields.Many2one('res.users', readonly=True)
    closed_at = fields.Datetime(readonly=True, tracking=True)
    closed_by_id = fields.Many2one('res.users', readonly=True)

    goodwill_recoverable_amount = fields.Monetary(
        currency_field='presentation_currency_id', copy=False,
        help=(
            "IAS 36 recoverable amount of the cash-generating unit carrying "
            "the recognised goodwill, in the presentation currency. Entered by "
            "a manager before testing goodwill for impairment. When the "
            "recognised goodwill exceeds this amount the difference is booked "
            "as an impairment charge that reduces goodwill."
        ),
    )
    goodwill_impairment_amount = fields.Monetary(
        readonly=True, copy=False,
        currency_field='presentation_currency_id',
        help=(
            "The IAS 36 goodwill impairment charge booked on this run, if any. "
            "Set by Test Goodwill Impairment when the recognised goodwill "
            "exceeds the recoverable amount."
        ),
    )
    goodwill_impairment_account_id = fields.Many2one(
        'account.account', copy=False,
        string="Goodwill Impairment Expense Account",
        help=(
            "Expense (or equity) account on the parent / consolidation chart "
            "the goodwill impairment charge is debited to. When left empty the "
            "impairment test falls back to a name heuristic (an expense "
            "account whose name contains 'impairment'); if that also resolves "
            "nothing the test is refused rather than posting an accountless "
            "charge."
        ),
    )

    override_policy_checks = fields.Boolean(
        string="Override Policy / Reporting-Date Checks",
        copy=False, tracking=True,
        help=(
            "IFRS 10.B87 / B92-B93 guard override. A compute is refused "
            "while any member reports more than three months off the group "
            "reporting date or is not confirmed policy-aligned. Setting "
            "this flag (with a mandatory reason) lets the compute proceed "
            "anyway; the override and its reason are recorded in the "
            "chatter audit trail."
        ),
    )
    override_policy_reason = fields.Text(
        string="Override Reason",
        copy=False,
        help=(
            "Mandatory justification when the policy / reporting-date "
            "checks are overridden, e.g. the conforming adjustments made "
            "for the offset period (IFRS 10.B93)."
        ),
    )

    notes = fields.Html()

    consolidation_warning = fields.Text(
        readonly=True, copy=False,
        help=(
            "Diagnostics raised at compute time: subsidiary investments not "
            "yet eliminated against equity (when the automatic IFRS 3 "
            "elimination is off or the member's acquisition fields are "
            "incomplete) and mismatched intragroup reciprocals. Surfaced so "
            "a run is never mistaken for a complete IFRS 10 consolidation "
            "while these manual steps are outstanding."
        ),
    )

    @api.depends('line_ids', 'elimination_ids')
    def _compute_counts(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)
            rec.elimination_count = len(rec.elimination_ids)

    @api.depends('line_ids.kind', 'line_ids.amount')
    def _compute_totals(self):
        for rec in self:
            cta = sum(
                rec.line_ids.filtered(lambda l: l.kind == 'cta')
                .mapped('amount'),
            )
            nci = sum(
                rec.line_ids.filtered(lambda l: l.kind == 'nci')
                .mapped('amount'),
            )
            rec.cta_amount = cta
            rec.nci_amount = nci

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code(
                    'eh.consol.run',
                ) or '/'
                vals['name'] = seq
        return super().create(vals_list)

    # ---- integrity: freeze input once the figures are settled ----

    def write(self, vals):
        """Freeze the measurement / input fields once the run is settled
        (computed / reviewed / closed); they are the basis of the consolidated
        figures and any posted consolidation move.

        A pure state-transition write (the action methods write only
        {'state': ...} plus the audit stamps) carries no frozen field and
        passes. A write touching a frozen figure while any record is settled is
        always blocked for everyone (a posted figure is restated by reversal,
        never by direct write). 'state' itself is owned by eh.workflow.guard:
        it may change only through the run's own actions (which run under sudo),
        so a direct RPC state write is refused there, not here.
        """
        # The sanctioned engine paths (the IAS 36 goodwill impairment test,
        # which is allowed on a computed / reviewed run and writes the readonly
        # goodwill_impairment_amount) flag themselves with the engine context
        # key and are exempt from the figure freeze. env.su alone is not a
        # reliable engine signal, so only the flagged path is exempted.
        engine = self.env.context.get(CONSOL_ENGINE_CTX)
        frozen = [f for f in _FROZEN_AFTER_CONFIRM if f in vals]
        confirmed = self.filtered(lambda r: r.state in _POSTED_STATES)
        if frozen and confirmed and not engine:
            raise UserError(_(
                "Figures on a settled consolidation run are frozen "
                "(%(fields)s). Reset it to draft (consolidation manager only) "
                "to change it.",
                fields=', '.join(frozen)))
        # The goodwill impairment-test fields freeze once the consolidation
        # move is posted; before that they are the manager's live inputs to
        # the IAS 36 test. The impairment engine writes goodwill_impairment_
        # amount only pre-posting under its own context key, so it is exempt.
        post_frozen = [f for f in _FROZEN_AFTER_POSTED_MOVE if f in vals]
        posted_move = self.filtered(
            lambda r: r.move_id and r.move_id.state == 'posted')
        if post_frozen and posted_move and not engine:
            raise UserError(_(
                "Goodwill impairment figures on a consolidation run whose "
                "move is posted are frozen (%(fields)s); they back the posted "
                "GL entry. Reverse the consolidation move to change them.",
                fields=', '.join(post_frozen)))
        # 'state' is guarded by eh.workflow.guard: a non-superuser cannot write
        # it directly at all, and the action methods (compute / review / close /
        # reset / reopen) run under sudo. The manager gate for un-freezing a
        # settled run (reset / reopen) is enforced at the top of those actions,
        # so there is no state-crossing arm to police here.
        return super().write(vals)

    def unlink(self):
        """A settled run cannot be deleted: it carries consolidated figures
        and (once a consolidation move is posted) a GL entry that would be
        orphaned. Reset it to draft first (manager-gated) to delete it.
        """
        settled = self.filtered(lambda r: r.state in _POSTED_STATES)
        if settled:
            raise UserError(_(
                "A computed, reviewed or closed consolidation run cannot be "
                "deleted; it carries settled consolidation figures and may "
                "back a posted GL move. Reset it to draft first."))
        return super().unlink()

    # ---- transitions ----

    def action_compute(self):
        for run in self:
            if run.state not in ('draft',):
                raise UserError(_(
                    "Compute is only available in draft state.",
                ))
            run._eh_check_policy_guards()
            run.line_ids.sudo().unlink()
            auto_warnings = run._build_lines()
            warnings = run._eh_compute_warnings() + (auto_warnings or [])
            run.sudo().write({
                'state': 'computed',
                'computed_at': fields.Datetime.now(),
                'computed_by_id': self.env.user.id,
                'consolidation_warning': '\n'.join(warnings) or False,
            })
            if warnings:
                run.message_post(body=_(
                    "Consolidation computed with outstanding manual steps:"
                    "<br/>- %s", '<br/>- '.join(warnings)))
        return True

    def _eh_check_policy_guards(self):
        """IFRS 10.B87 / B92-B93 gate, checked before every compute.

        Consolidated financial statements must be prepared using uniform
        accounting policies (B87) and member reporting dates may differ from
        the group's by at most three months (B92-B93). A member breaching
        either rule blocks the compute with a UserError naming the member
        and the rule. The run-level override (override_policy_checks plus a
        mandatory reason) lets the compute proceed; the override, the
        reason, and the affected members are logged to the chatter so the
        exception is audit-visible, never silent.
        """
        self.ensure_one()
        breaches = []
        for member in self.entity_id.member_ids:
            name = member.company_id.display_name
            if member.reporting_date_offset_months \
                    > _MAX_REPORTING_OFFSET_MONTHS:
                breaches.append(_(
                    "%(member)s reports %(offset)d months off the group "
                    "reporting date (IFRS 10.B93 allows at most %(max)d)",
                    member=name,
                    offset=member.reporting_date_offset_months,
                    max=_MAX_REPORTING_OFFSET_MONTHS,
                ))
            if not member.policy_aligned:
                breaches.append(_(
                    "%(member)s is not confirmed policy-aligned "
                    "(IFRS 10.B87 requires uniform group accounting "
                    "policies)",
                    member=name,
                ))
            # Interim-period CTA guard. Balance-sheet accounts are summed
            # cumulatively to period_to, but P&L only over [period_from,
            # period_to]. If the run starts mid-fiscal-year and the member has
            # posted current-year P&L before period_from, that pre-period
            # result's balance-sheet side sits inside the cumulative BS while
            # its P&L side is excluded, so the untranslated trial balance no
            # longer nets to zero and the residual is silently booked to the
            # translation reserve (CTA). Block it (overridable like the IFRS 10
            # guards) and point the preparer at the fiscal-year start.
            if self.period_from and self.period_to:
                fy_start = member.company_id.compute_fiscalyear_dates(
                    self.period_to)['date_from']
                if self.period_from > fy_start:
                    pre_pl = self.env['account.move.line'].sudo().search_count([
                        ('company_id', '=', member.company_id.id),
                        ('parent_state', '=', 'posted'),
                        ('account_id.account_type', 'in',
                         list(_INCOME_TYPES + _EXPENSE_TYPES)),
                        ('date', '>=', fy_start),
                        ('date', '<', self.period_from),
                    ])
                    if pre_pl:
                        breaches.append(_(
                            "%(member)s has posted current-year P&L dated "
                            "before the run's start (%(start)s); an interim "
                            "window leaks that pre-period result into the "
                            "translation reserve. Set period_from to the "
                            "fiscal-year start (%(fy)s) so the consolidated "
                            "trial balance nets to zero before CTA.",
                            member=name, start=self.period_from, fy=fy_start,
                        ))
        if not breaches:
            return
        if not self.override_policy_checks:
            raise UserError(_(
                "Run %(run)s cannot be computed; IFRS 10 consolidation "
                "guards failed:\n- %(breaches)s\n\nAlign the members, or "
                "set the run's policy-check override with a documented "
                "reason to proceed anyway.",
                run=self.name,
                breaches='\n- '.join(breaches),
            ))
        if not (self.override_policy_reason or '').strip():
            raise UserError(_(
                "Run %s overrides the IFRS 10 policy / reporting-date "
                "checks, so a written override reason is required.",
                self.name,
            ))
        self.message_post(body=_(
            "IFRS 10 policy / reporting-date checks overridden by "
            "%(user)s.<br/>Reason: %(reason)s<br/>Outstanding breaches:"
            "<br/>- %(breaches)s",
            user=self.env.user.display_name,
            reason=self.override_policy_reason,
            breaches='<br/>- '.join(breaches),
        ))

    def action_review(self):
        for run in self:
            if run.state != 'computed':
                raise UserError(_(
                    "Review is only available in computed state.",
                ))
            run.sudo().write({
                'state': 'reviewed',
                'reviewed_at': fields.Datetime.now(),
                'reviewed_by_id': self.env.user.id,
            })

    def action_close(self):
        for run in self:
            if run.state not in ('reviewed',):
                raise UserError(_(
                    "Close is only available in reviewed state.",
                ))
            # When the entity carries a dedicated consolidation ledger company,
            # closing a run requires the immutable consolidation move to be
            # posted first: the signed, audited consolidation is the posted
            # entry, not just the memo run lines. Runs on entities with no
            # consolidation company behave exactly as before.
            if (
                run.entity_id.consolidation_company_id
                and not (run.move_id and run.move_id.state == 'posted')
            ):
                raise UserError(_(
                    "Run %s targets a consolidation ledger company, so the "
                    "consolidation journal entry must be posted before the "
                    "run can be closed. Use Post Consolidation Move first.",
                    run.name,
                ))
            run.sudo().write({
                'state': 'closed',
                'closed_at': fields.Datetime.now(),
                'closed_by_id': self.env.user.id,
            })

    def action_reset_to_draft(self):
        if not self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        ):
            raise UserError(_(
                "Only a consolidation manager can reset a run.",
            ))
        for run in self:
            # A closed run is signed and cited in audit. It must not be
            # reset (and so recomputed) in place. Reopening it is a distinct,
            # explicit action that leaves an audit trail; require it first.
            if run.state == 'closed':
                raise UserError(_(
                    "Run %s is closed and cannot be reset. A manager must "
                    "reopen it first (Reopen), which is recorded, before it "
                    "can be reset and recomputed.", run.name,
                ))
            # A reset drops all computed lines, so a consolidation move posted
            # from those lines must be undone first, otherwise the posted move
            # would outlive the figures it represents. Reverse and unlink it
            # (manager-gated here, logged) before dropping the lines.
            run._eh_reverse_consolidation_move()
            run.line_ids.sudo().with_context(
                **{CONSOL_ENGINE_CTX: True}).unlink()
            run.sudo().write({'state': 'draft'})

    def action_reopen(self):
        """Manager-gated reopen of a closed run.

        A closed consolidation is locked and reproducible: its lines are
        frozen and it cannot be recomputed or reset. Reopening is the single
        explicit, audited path back to an editable state. It moves the run to
        reviewed (not draft) so the existing lines are preserved until the
        manager deliberately resets and recomputes.
        """
        if not self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        ):
            raise UserError(_(
                "Only a consolidation manager can reopen a closed run.",
            ))
        for run in self:
            if run.state != 'closed':
                raise UserError(_(
                    "Reopen is only available for a closed run.",
                ))
            # Reopening a closed run that carried a posted consolidation move
            # must undo that move: the signed consolidation is being unlocked
            # for correction, so its posted entry can no longer stand. Reverse
            # and unlink it (logged) as part of the reopen.
            run._eh_reverse_consolidation_move()
            run.sudo().write({
                'state': 'reviewed',
                'closed_at': False,
                'closed_by_id': False,
            })
            run.message_post(body=_(
                "Run reopened for correction by %s.",
                self.env.user.display_name,
            ))

    # ---- IAS 36 goodwill impairment ----

    def _eh_goodwill_accounts(self):
        """Return the account.account records that carry recognised goodwill
        for this run: the goodwill account configured on each full member whose
        IFRS 3 acquisition elimination booked goodwill. Empty when no member
        carries a goodwill account.
        """
        self.ensure_one()
        accounts = self.env['account.account'].browse()
        for member in self.entity_id.member_ids:
            if member.goodwill_account_id:
                accounts |= member.goodwill_account_id
        return accounts

    def _eh_recognised_goodwill(self):
        """Return the net recognised goodwill on this run: the signed sum of
        every run line booked to a goodwill account, in the presentation
        currency. Goodwill is an asset, so a positive figure is a debit
        balance (recognised goodwill); a bargain-purchase credit nets it down.
        """
        self.ensure_one()
        goodwill_accounts = self._eh_goodwill_accounts()
        if not goodwill_accounts:
            return 0.0
        total = sum(
            self.line_ids.filtered(
                lambda l: l.account_id in goodwill_accounts
            ).mapped('amount')
        )
        return self.presentation_currency_id.round(total)

    def _eh_impairment_account(self):
        """Return the account the goodwill impairment charge is debited to.

        Prefers the run's goodwill_impairment_account_id; only when unset falls
        back to a name heuristic (an expense account on the parent chart whose
        name contains 'impairment'). Returns empty when neither resolves, so
        the caller refuses the test rather than posting an accountless charge.
        """
        self.ensure_one()
        if self.goodwill_impairment_account_id:
            return self.goodwill_impairment_account_id
        Account = self.env['account.account'].sudo()
        return Account.search([
            ('account_type', 'in', list(_EXPENSE_TYPES)),
            ('name', 'ilike', 'impairment'),
            (_acc_company_field(self.env), 'in',
             self.entity_id.parent_company_id.ids),
        ], limit=1)

    def action_impair_goodwill(self):
        """Manager-gated IAS 36 goodwill impairment test on a computed /
        reviewed run.

        Tests the recognised goodwill (the signed sum of run lines on the
        members' goodwill accounts) against goodwill_recoverable_amount. When
        the recognised goodwill exceeds the recoverable amount the excess is
        booked as a balanced two-leg impairment run line:

          Dr impairment expense account   amount = +impairment
          Cr goodwill account             amount = -impairment

        The pair nets to zero by construction, so the run stays balanced and
        the CTA plug is untouched. When a consolidation ledger company is
        configured the new lines feed the posted move exactly like every other
        run line (post the move after running the test). Re-running the test
        first reverses any prior impairment on the run so the charge is never
        double-booked. When the goodwill is not impaired (recoverable amount at
        or above recognised goodwill) any prior impairment is reversed and no
        new charge is booked.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only a consolidation manager can test goodwill for "
                "impairment.",
            ))
        for run in self:
            if run.state not in ('computed', 'reviewed'):
                raise UserError(_(
                    "Goodwill impairment can only be tested on a computed or "
                    "reviewed run.",
                ))
            if run.move_id and run.move_id.state == 'posted':
                raise UserError(_(
                    "Run %s already has a posted consolidation move; reopen it "
                    "before re-testing goodwill impairment so the charge is "
                    "reflected in the move.", run.name,
                ))
            goodwill_accounts = run._eh_goodwill_accounts()
            if not goodwill_accounts:
                raise UserError(_(
                    "Run %s carries no goodwill account (no full member has a "
                    "Goodwill Account configured), so there is no recognised "
                    "goodwill to test for impairment.", run.name,
                ))
            # Drop any prior impairment lines so the test is idempotent and the
            # recognised-goodwill base is measured before the charge.
            prior = run.line_ids.filtered(lambda l: l.kind == 'impairment')
            if prior:
                prior.sudo().with_context(
                    **{CONSOL_ENGINE_CTX: True}).unlink()
            currency = run.presentation_currency_id
            rounding = currency.rounding or 0.01
            recognised = run._eh_recognised_goodwill()
            recoverable = currency.round(run.goodwill_recoverable_amount or 0.0)
            impairment = currency.round(recognised - recoverable)
            if (
                float_is_zero(impairment, precision_rounding=rounding)
                or impairment < 0.0
            ):
                # Not impaired: recoverable amount is at or above recognised
                # goodwill. Leave the run with no impairment charge. This is a
                # sanctioned engine write of a frozen (readonly) figure, so it
                # carries the engine context flag past the write freeze.
                run.with_context(
                    **{CONSOL_ENGINE_CTX: True}
                ).goodwill_impairment_amount = 0.0
                run.message_post(body=_(
                    "Goodwill impairment test: recognised goodwill %(gw)s is "
                    "within the recoverable amount %(rec)s. No impairment "
                    "booked.",
                    gw=recognised, rec=recoverable,
                ))
                continue
            impairment_account = run._eh_impairment_account()
            if not impairment_account:
                raise UserError(_(
                    "Run %(run)s has a goodwill impairment of %(amount)s to "
                    "book, but no impairment expense account could be "
                    "resolved. Set a Goodwill Impairment Expense Account on "
                    "the run (or create an expense account whose name contains "
                    "'impairment' on the parent chart).",
                    run=run.name,
                    amount=currency.round(impairment),
                ))
            # Credit the goodwill account carrying the largest recognised
            # goodwill balance (asset, debit-positive), so the impairment
            # reduces the goodwill that is actually recognised.
            goodwill_target = run._eh_impairment_goodwill_target(
                goodwill_accounts)
            Line = self.env['eh.consol.run.line'].sudo().with_context(
                **{CONSOL_ENGINE_CTX: True})
            base = {
                'run_id': run.id,
                'kind': 'impairment',
            }
            Line.create([
                dict(base,
                     account_id=impairment_account.id,
                     amount=impairment,
                     notes=_("IAS 36 goodwill impairment charge")),
                dict(base,
                     account_id=goodwill_target.id,
                     amount=-impairment,
                     notes=_("IAS 36 goodwill impairment write-down")),
            ])
            run.with_context(
                **{CONSOL_ENGINE_CTX: True}
            ).goodwill_impairment_amount = impairment
            run.message_post(body=_(
                "Goodwill impairment of %(amount)s booked: recognised goodwill "
                "%(gw)s exceeds the recoverable amount %(rec)s (IAS 36).",
                amount=currency.round(impairment),
                gw=recognised, rec=recoverable,
            ))
        return True

    def _eh_impairment_goodwill_target(self, goodwill_accounts):
        """Return the single goodwill account to credit for the impairment
        write-down: the one carrying the largest recognised (debit) goodwill
        balance on this run, so the write-down lands on real goodwill. Falls
        back to the first configured goodwill account when none carries a
        balance (should not happen when recognised goodwill is positive).
        """
        self.ensure_one()
        best = None
        best_bal = None
        for account in goodwill_accounts:
            bal = sum(
                self.line_ids.filtered(
                    lambda l: l.account_id == account
                ).mapped('amount')
            )
            if best_bal is None or bal > best_bal:
                best, best_bal = account, bal
        return best or goodwill_accounts[:1]

    # ---- IAS 21.48 member-disposal CTA recycling ----

    def _eh_member_cta_balance(self, member):
        """Return the member's REMAINING accumulated CTA on this run: the
        signed sum of its kind='cta' lines plus any prior kind='cta_recycle'
        legs booked on the same CTA account(s) (the recycle debit reduces
        the credit reserve, so repeated partial disposals draw the balance
        down and can never recycle more than was accumulated). Credit
        negative: a negative balance is an accumulated translation gain.
        """
        self.ensure_one()
        cta_lines = self.line_ids.filtered(
            lambda l: l.kind == 'cta' and l.member_id == member)
        cta_accounts = cta_lines.mapped('account_id')
        recycle_reserve_legs = self.line_ids.filtered(
            lambda l: l.kind == 'cta_recycle' and l.member_id == member
            and l.account_id in cta_accounts)
        return self.presentation_currency_id.round(
            sum(cta_lines.mapped('amount'))
            + sum(recycle_reserve_legs.mapped('amount')))

    def _eh_recycle_member_cta(self, member, pct):
        """Book the IAS 21.48 disposal reclassification of a member's
        accumulated CTA on this run (called from the member's manager-gated
        action_dispose_member).

        reclass = remaining member CTA balance x pct / 100, rounded in the
        presentation currency. Two balanced kind='cta_recycle' legs:

          accumulated GAIN (balance credit-negative):
            Dr CTA reserve account   amount = -reclass  (positive)
            Cr FX gain account       amount = +reclass  (negative)

          accumulated LOSS (balance debit-positive): the mirrored pair
          through the FX loss account.

        The pair nets to zero by construction, so the run stays balanced.
        The P&L account comes from the member's CTA position link
        (gain/loss reclass accounts) when set, else from the entity's CTA
        recycling accounts. Refused when the run's consolidation move is
        already posted (reopen first, like the impairment test) so the
        posted GL never diverges from the run lines.
        """
        self.ensure_one()
        if self.state not in ('computed', 'reviewed'):
            raise UserError(_(
                "CTA can only be recycled on a computed or reviewed run.",
            ))
        if self.move_id and self.move_id.state == 'posted':
            raise UserError(_(
                "Run %s already has a posted consolidation move; reopen it "
                "before recycling CTA so the reclassification is reflected "
                "in the move.", self.name,
            ))
        currency = self.presentation_currency_id
        rounding = currency.rounding or 0.01
        balance = self._eh_member_cta_balance(member)
        if float_is_zero(balance, precision_rounding=rounding):
            raise UserError(_(
                "Member %(member)s carries no remaining accumulated CTA on "
                "run %(run)s; there is nothing to recycle.",
                member=member.company_id.display_name,
                run=self.name,
            ))
        reclass = currency.round(balance * pct / 100.0)
        if float_is_zero(reclass, precision_rounding=rounding):
            raise UserError(_(
                "The disposal share of member %s's accumulated CTA rounds "
                "to zero; nothing to recycle.",
                member.company_id.display_name,
            ))
        # The reserve leg reverses the plug: a credit-negative (gain)
        # balance is debited out of the reserve; the P&L leg books the
        # mirrored gain (credit) or loss (debit).
        position = member.cta_position_id
        if balance < 0.0:
            pl_account = (
                position.gain_account_id
                or self.entity_id.cta_gain_account_id
            )
            missing_label = _("CTA Recycling Gain Account")
        else:
            pl_account = (
                position.loss_account_id
                or self.entity_id.cta_loss_account_id
            )
            missing_label = _("CTA Recycling Loss Account")
        if not pl_account:
            raise UserError(_(
                "No %(label)s is configured (set it on the entity, or on "
                "the member's linked CTA position), so the CTA of member "
                "%(member)s cannot be recycled to profit or loss.",
                label=missing_label,
                member=member.company_id.display_name,
            ))
        cta_lines = self.line_ids.filtered(
            lambda l: l.kind == 'cta' and l.member_id == member)
        reserve_account = cta_lines.mapped('account_id')[:1]
        if not reserve_account:
            raise UserError(_(
                "Member %s carries no CTA reserve line on this run.",
                member.company_id.display_name,
            ))
        Line = self.env['eh.consol.run.line'].sudo().with_context(
            **{CONSOL_ENGINE_CTX: True})
        note = _(
            "IAS 21.48 CTA recycling on disposal of %(member)s "
            "(%(pct).2f%%)",
            member=member.company_id.display_name, pct=pct,
        )
        base = {
            'run_id': self.id,
            'kind': 'cta_recycle',
            'company_id': member.company_id.id,
            'member_id': member.id,
            'cta_position_id': position.id or False,
            'notes': note,
        }
        Line.create([
            dict(base, account_id=reserve_account.id, amount=-reclass),
            dict(base, account_id=pl_account.id, amount=reclass),
        ])
        self.message_post(body=_(
            "CTA of %(amount)s recycled from the translation reserve to "
            "profit or loss on disposal of %(member)s (%(pct).2f%%, "
            "IAS 21.48) by %(user)s. Remaining member CTA: %(left)s.",
            amount=currency.round(abs(reclass)),
            member=member.company_id.display_name,
            pct=pct,
            user=self.env.user.display_name,
            left=self._eh_member_cta_balance(member),
        ))
        return True

    # ---- consolidation move (IFRS 10 auditability) ----

    def action_post_move(self):
        """Post a single balanced, immutable consolidation move (manager-gated).

        Available only when the entity carries a dedicated consolidation ledger
        company and the run is computed or reviewed. Aggregates every run line
        by account into one signed net per account, maps each account into the
        consolidation company's chart by code, and books one journal line per
        account (Dr for a positive net, Cr for a negative net). The run already
        nets to zero (the CTA plug guarantees it), so the move balances by
        construction. The move is stored on the run and, once posted, is
        immutable via Odoo's posted state.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only a consolidation manager can post the consolidation move.",
            ))
        for run in self:
            company = run.entity_id.consolidation_company_id
            if not company:
                raise UserError(_(
                    "Run %s has no consolidation ledger company configured on "
                    "its entity; there is nowhere to post a consolidation "
                    "move.", run.name,
                ))
            if run.state not in ('computed', 'reviewed'):
                raise UserError(_(
                    "The consolidation move can only be posted from a computed "
                    "or reviewed run.",
                ))
            if run.move_id and run.move_id.state == 'posted':
                raise UserError(_(
                    "Run %s already has a posted consolidation move (%s).",
                    run.name, run.move_id.name,
                ))
            move = run._eh_build_consolidation_move(company)
            move.action_post()
            run.move_id = move.id
            run.message_post(body=_(
                "Consolidation move %s posted to %s by %s.",
                move.name, company.display_name, self.env.user.display_name,
            ))
        return True

    def _eh_consolidation_journal(self, company):
        """Return the general journal to book the consolidation move to.

        Prefers the entity's configured consolidation journal; otherwise the
        first general journal in the consolidation company. Raises when none
        exists.
        """
        self.ensure_one()
        journal = self.entity_id.consolidation_journal_id
        if journal and journal.company_id == company:
            return journal
        journal = self.env['account.journal'].sudo().search([
            ('company_id', '=', company.id),
            ('type', '=', 'general'),
        ], limit=1)
        if not journal:
            raise UserError(_(
                "No general journal exists in the consolidation company %s. "
                "Create one (or set a Consolidation Journal on the entity) "
                "before posting the consolidation move.",
                company.display_name,
            ))
        return journal

    def _eh_resolve_consol_account(self, account, company, cache):
        """Map a run-line account into the consolidation company's chart by
        code, returning the resolved account.account in that company.

        When the consolidation company already owns the account (shared chart)
        it is returned as-is. Otherwise an account with the same code in the
        consolidation company is looked up. Raises a clear UserError naming the
        account when no counterpart exists, so a missing mapping fails loudly
        rather than silently dropping a leg.
        """
        self.ensure_one()
        if not account:
            raise UserError(_(
                "Run %s carries a consolidation line with no account (for "
                "example a CTA or NCI line with no configured account). "
                "Configure the account before posting the consolidation "
                "move.", self.name,
            ))
        if account.id in cache:
            return cache[account.id]
        company_field = _acc_company_field(self.env)
        if company_field == 'company_ids':
            owns = company in account.company_ids
        else:
            owns = account.company_id == company
        if owns:
            cache[account.id] = account
            return account
        # Resolve by the source account's code. account.account.code is a
        # company-dependent computed field on Odoo 19, so read it in the
        # SOURCE account's own company context (env.company may be a third
        # company under which a parent-only account's code reads empty), then
        # look it up in the TARGET company's context to match the code as
        # stored there.
        if company_field == 'company_ids':
            source_company = account.company_ids[:1] or company
        else:
            source_company = account.company_id or company
        code = account.with_company(source_company).code
        Account = self.env['account.account'].sudo().with_company(company)
        resolved = Account.search([
            ('code', '=', code),
            (company_field, 'in', company.ids),
        ], limit=1)
        if not resolved:
            raise UserError(_(
                "Account %(code)s %(name)s is not present in the "
                "consolidation company %(company)s chart of accounts. Add it "
                "(by code), or point the consolidation company at the parent "
                "chart, before posting the consolidation move.",
                code=code or '?',
                name=account.name or '',
                company=company.display_name,
            ))
        cache[account.id] = resolved
        return resolved

    def _eh_build_consolidation_move(self, company):
        """Build (not post) the balanced consolidation account.move for one run
        in the consolidation ledger company.

        One journal line per account, from the signed net of the run lines on
        that account. The signed net follows Odoo's convention (debit positive,
        credit negative), so a positive net books a debit and a negative net a
        credit. Rounding is in the company currency. The run already nets to
        zero, so the sum of the legs is zero by construction; a residual (only
        possible from per-account rounding) is absorbed into the largest leg so
        the move always balances.
        """
        self.ensure_one()
        currency = company.currency_id
        journal = self._eh_consolidation_journal(company)
        cache = {}
        # Aggregate signed amounts by the resolved consolidation-company
        # account. Different source accounts that map to the same consolidation
        # account (never, with a 1:1 code map, but safe) merge into one line.
        by_account = defaultdict(float)
        for line in self.line_ids:
            if line.kind == 'disclosure':
                # Memo-only rows (zero amount, no account): they document an
                # election (e.g. IAS 28.1A) and never post to the ledger.
                continue
            if not line.account_id:
                # A CTA / NCI line with no account cannot be posted; surface it.
                self._eh_resolve_consol_account(
                    line.account_id, company, cache)
            resolved = self._eh_resolve_consol_account(
                line.account_id, company, cache)
            by_account[resolved.id] += line.amount
        line_vals = []
        rounding = currency.rounding or 0.01
        for acc_id, amount in by_account.items():
            rounded = currency.round(amount)
            if float_is_zero(rounded, precision_rounding=rounding):
                continue
            line_vals.append({
                'account_id': acc_id,
                'debit': rounded if rounded > 0.0 else 0.0,
                'credit': -rounded if rounded < 0.0 else 0.0,
            })
        if not line_vals:
            raise UserError(_(
                "Run %s produced no non-zero consolidation lines to post.",
                self.name,
            ))
        # Absorb any residual (from per-account rounding) into the largest leg
        # so the move balances exactly. The run nets to zero pre-rounding, so
        # this residual is at most a rounding unit per account.
        net = currency.round(
            sum(v['debit'] - v['credit'] for v in line_vals))
        if not float_is_zero(net, precision_rounding=rounding):
            biggest = max(
                line_vals,
                key=lambda v: abs(v['debit'] - v['credit']))
            adjusted = currency.round(
                (biggest['debit'] - biggest['credit']) - net)
            biggest['debit'] = adjusted if adjusted > 0.0 else 0.0
            biggest['credit'] = -adjusted if adjusted < 0.0 else 0.0
        move = self.env['account.move'].sudo().create({
            'company_id': company.id,
            'move_type': 'entry',
            'date': self.period_to,
            'journal_id': journal.id,
            'ref': _("Consolidation %s", self.name),
            'eh_sealed': True,
            'line_ids': [(0, 0, v) for v in line_vals],
        })
        return move

    def _eh_reverse_consolidation_move(self):
        """Reverse and unlink a posted consolidation move, if any, logging it.

        Called from the manager-gated reopen / reset paths. A posted move is
        immutable, so it is reversed (a balanced counter-entry) and then both
        the original and the reversal are unlinked, leaving the consolidation
        company's ledger flat again. Draft (never-posted) moves are unlinked
        directly. No-op when the run carries no move.
        """
        self.ensure_one()
        move = self.move_id
        if not move:
            return
        if move.state == 'posted':
            reversal = move._reverse_moves(
                default_values_list=[{
                    'date': self.period_to,
                    'ref': _("Reversal of consolidation %s", self.name),
                }],
                cancel=True,
            )
            self._eh_seal_reversal(reversal)
            self.message_post(body=_(
                "Consolidation move %s reversed (%s) on run reopen/reset by %s.",
                move.name,
                reversal.name if reversal else '',
                self.env.user.display_name,
            ))
            # A posted move cannot be unlinked directly; set both the original
            # and its reversal to draft (they are fully reconciled by cancel=
            # True, so this leaves the consolidation ledger flat) and unlink so
            # a subsequent recompute + repost starts from a clean ledger.
            to_remove = (move | reversal).sudo()
            # The consolidation move is eh_sealed; this reopen is the
            # sanctioned unwind, so it carries the allow-unpost flag.
            to_remove.sudo().with_context(eh_allow_unpost=True).button_draft()
            # Clear the run's reference before deleting the move: move_id is
            # ondelete='restrict', so the FK must be released first.
            self.move_id = False
            to_remove.unlink()
        else:
            self.move_id = False
            move.sudo().unlink()

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No consolidation move has been posted."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Consolidation move"),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
        }

    # ---- compute ----

    def _build_lines(self):
        """Pull each member's trial balance, translate, sum, eliminate.

        Output is one eh.consol.run.line per (consolidated_account,
        kind, source_member). Kind values:

          subsidiary_balance: the translated balance for a subsidiary.
          parent_balance: the parent company's own balance.
          elimination: an elimination journal line.
          cta: the IAS 21 currency translation adjustment.
          nci: non-controlling interest carve-out.

        The kinds aggregate at view time so users see a consolidated
        account total broken down by source.
        """
        self.ensure_one()
        # Auto-intragroup diagnostics collected during this build and returned
        # to action_compute (a recordset cannot hold a transient attribute
        # across calls). Empty unless the entity opted into auto elimination.
        auto_warnings = []
        # Run lines are engine generated and read only to users (see the
        # ir.model.access rules). Create them as sudo so a non manager who
        # triggers a compute is not blocked by the read only access.
        Line = self.env['eh.consol.run.line'].sudo().with_context(
            **{CONSOL_ENGINE_CTX: True})
        vals_list = []
        # Parent balances first.
        parent_company = self.entity_id.parent_company_id
        parent_lines = self._fetch_balances(
            company=parent_company,
            currency_to=self.presentation_currency_id,
            translate=False,
        )
        for acc_id, amount in parent_lines.items():
            vals_list.append({
                'run_id': self.id,
                'account_id': acc_id,
                'kind': 'parent_balance',
                'company_id': parent_company.id,
                'amount': amount,
            })
        # Member balances next.
        for member in self.entity_id.member_ids:
            if member.method == 'equity':
                # Equity-method members are not rolled up line by line; the
                # parent carries a single investment line. Under the IAS
                # 28.1A fair value option no pick-up is booked at all (the
                # fair-value engine remeasures the investment); the run
                # carries a zero-amount memo disclosure line recording the
                # election. Otherwise the pick-up configuration is
                # MANDATORY: a missing investment or share-of-profit
                # account refuses the compute rather than silently
                # dropping the associate from the consolidated result.
                if member.fv_option:
                    vals_list.append({
                        'run_id': self.id,
                        'kind': 'disclosure',
                        'company_id': member.company_id.id,
                        'member_id': member.id,
                        'amount': 0.0,
                        'notes': _(
                            "IAS 28.1A fair value option elected for "
                            "%s: no equity pick-up; the investment is "
                            "measured at fair value through profit or "
                            "loss by the fair-value engine.",
                            member.company_id.display_name,
                        ),
                    })
                    continue
                self._eh_require_equity_config(member)
                pickup_vals = self._build_equity_pickup_vals(member)
                if pickup_vals:
                    vals_list.extend(pickup_vals)
                continue
            translated = self._fetch_balances(
                company=member.company_id,
                currency_to=self.presentation_currency_id,
                translate=True,
                source_currency=member.functional_currency_id,
            )
            # Proportional method (IFRS 11-style proportionate
            # consolidation): the ownership share is applied to EVERY
            # balance, and it is applied AFTER translation. Translation
            # first, scaling second keeps the IAS 21 closing / average rate
            # arithmetic identical to the full method (scaling a translated
            # amount equals translating a scaled amount for a linear rate,
            # but scaling before translation would break the CTA
            # attribution audit trail, so the order is fixed here and
            # relied on by the tests).
            scale = (
                member.ownership_pct / 100.0
                if member.method == 'proportional' else 1.0
            )
            for acc_id, amount in translated.items():
                vals_list.append({
                    'run_id': self.id,
                    'account_id': acc_id,
                    'kind': 'subsidiary_balance',
                    'company_id': member.company_id.id,
                    'member_id': member.id,
                    'amount': amount * scale,
                })
            # IFRS 3 acquisition elimination: when the investment is fully
            # configured, auto-generate the elimination legs that remove the
            # parent's investment against acquisition-date equity (and book
            # acquisition NCI + goodwill). Those legs already book NCI, so the
            # separate _build_nci_vals carve-out is suppressed for this member
            # to avoid double-counting the minority interest.
            elim_vals = self._build_acquisition_elimination_vals(member)
            if elim_vals:
                vals_list.extend(elim_vals)
                # The elimination books NCI on the ACQUISITION-DATE equity only.
                # Post-acquisition profit/loss must also be attributed to the
                # minority, so book a further NCI line for the movement in the
                # subsidiary's reporting equity+P&L base since acquisition. This
                # is zero when there has been no post-acquisition movement, so a
                # sub that has not moved since acquisition behaves as before.
                post_acq_vals = self._build_post_acq_nci_vals(member, translated)
                if post_acq_vals:
                    vals_list.extend(post_acq_vals)
            elif member.method == 'full' and member.ownership_pct < 100.0:
                # Compute and persist NCI for this member when ownership
                # is below 100% on the full method and no auto-elimination
                # booked the acquisition NCI.
                nci_vals = self._build_nci_vals(member, translated)
                if nci_vals:
                    vals_list.extend(nci_vals)
        # Apply eliminations as run lines tagged kind='elimination'.
        for elim in self.elimination_ids.filtered(
            lambda e: e.state == 'posted',
        ):
            for line in elim.line_ids:
                vals_list.append({
                    'run_id': self.id,
                    'account_id': line.account_id.id,
                    'kind': 'elimination',
                    'amount': line.amount,
                    'elimination_id': elim.id,
                })
        # IFRS 10.B86 automatic intragroup elimination: when opted in on the
        # entity, generate balanced elimination pairs for reciprocal
        # receivables/payables and sales/purchases between member companies.
        # Off by default, so a run on an entity without the flag behaves
        # exactly as before.
        if self.entity_id.auto_eliminate_intragroup:
            auto_vals, auto_warnings = self._build_auto_intragroup_vals()
            if auto_vals:
                vals_list.extend(auto_vals)
        # IFRS 10 / IAS 27 intra-group unrealised-profit elimination: for each
        # configured record, remove the margin sitting in the buyer's ending
        # inventory. Only fires when such records exist, so a run without any
        # behaves exactly as before.
        for up in self.unrealised_profit_ids:
            up_vals = self._build_unrealised_profit_vals(up)
            if up_vals:
                vals_list.extend(up_vals)
        if vals_list:
            Line.create(vals_list)
        # CTA must come last: it reads the lines just written to
        # balance the run. The sum is computed from self.line_ids,
        # so the prior batch must have flushed.
        #
        # The plug is SPLIT BY MEMBER: each member's translated lines (plus
        # its balanced NCI / elimination pairs, which net to zero) leave a
        # per-member residual, booked as that member's own kind='cta' line
        # tagged with the member (and its CTA position link, when set), so
        # the accumulated translation reserve is attributable per foreign
        # operation and recyclable on that member's disposal (IAS 21.48).
        # Any residue not attributable to a member (manual eliminations,
        # unrealised-profit records, auto intragroup pairs; all balanced,
        # so normally zero) lands on an untagged residual CTA line. The sum
        # of all CTA lines equals the old single-line plug exactly, so the
        # run still nets to zero and cta_amount is unchanged.
        cta_total = self._compute_cta()
        currency = self.presentation_currency_id
        rounding = currency.rounding or 0.01
        cta_vals = []
        booked = 0.0
        for member in self.entity_id.member_ids:
            member_total = sum(
                self.line_ids.filtered(
                    lambda l: l.member_id == member
                    and l.kind in _CTA_BASE_KINDS
                ).mapped('amount'),
            )
            member_plug = -currency.round(member_total)
            if float_is_zero(member_plug, precision_rounding=rounding):
                continue
            cta_vals.append({
                'run_id': self.id,
                'kind': 'cta',
                'company_id': member.company_id.id,
                'member_id': member.id,
                'cta_position_id': member.cta_position_id.id or False,
                'amount': member_plug,
            })
            booked += member_plug
        residual = currency.round(cta_total - booked)
        if not float_is_zero(residual, precision_rounding=rounding):
            cta_vals.append({
                'run_id': self.id,
                'kind': 'cta',
                'amount': residual,
            })
        if cta_vals:
            cta_account = self._cta_account()
            if not cta_account:
                # A non-zero CTA with no account would post a run line with
                # account_id False (silently dropping the translation reserve
                # from the consolidated set). Refuse and tell the user exactly
                # what to configure rather than emitting an accountless line.
                raise UserError(_(
                    "Run %(run)s needs to book a currency translation "
                    "adjustment (CTA) of %(amount)s, but no CTA account could "
                    "be resolved. Set a CTA / Translation Reserve Account on "
                    "the consolidation entity %(entity)s (or create an equity "
                    "account whose name contains 'translation' or 'CTA' on the "
                    "parent chart).",
                    run=self.name,
                    amount=currency.round(cta_total),
                    entity=self.entity_id.display_name,
                ))
            for vals in cta_vals:
                vals['account_id'] = cta_account.id
            Line.create(cta_vals)
        return auto_warnings

    def _fetch_balances(
        self, company, currency_to, translate=False,
        source_currency=None,
    ):
        """Return {account_id: amount} of balances for the company up
        to period_to.

        Income / expense accounts use period sum (date in
        [period_from, period_to]); balance-sheet accounts use
        cumulative sum to period_to.

        When translate=True, amounts are converted from
        source_currency to currency_to. Income / expense use the
        period average rate (time weighted across the period per
        IAS 21); balance-sheet uses the closing rate (rate at
        period_to). The gap between the two rates is exactly what the
        CTA captures, so the average rate must not equal the closing
        rate.
        """
        self.ensure_one()
        AML = self.env['account.move.line'].sudo()
        balances_pl = grouped_sum(
            AML,
            [
                ('company_id', '=', company.id),
                ('parent_state', '=', 'posted'),
                ('account_id.account_type', 'in',
                 list(_INCOME_TYPES + _EXPENSE_TYPES)),
                ('date', '>=', self.period_from),
                ('date', '<=', self.period_to),
            ],
            'account_id', 'balance',
        )
        balances_bs = grouped_sum(
            AML,
            [
                ('company_id', '=', company.id),
                ('parent_state', '=', 'posted'),
                ('account_id.account_type', 'not in',
                 list(_INCOME_TYPES + _EXPENSE_TYPES)),
                ('date', '<=', self.period_to),
            ],
            'account_id', 'balance',
        )
        out = {}
        # rates
        if translate and source_currency and currency_to and source_currency != currency_to:
            # Balance-sheet items translate at the closing rate (period_to).
            # P&L items translate at the period average rate. Sourcing both
            # at period_to (the previous behaviour) made the two rates
            # identical, which collapsed the CTA to zero for every run with
            # a foreign-currency subsidiary, materially misstating equity.
            closing_rate_per = source_currency._convert(
                1.0, currency_to, company, self.period_to,
            )
            avg_rate_per = self._period_average_rate(
                source_currency, currency_to, company,
            )
        else:
            avg_rate_per = 1.0
            closing_rate_per = 1.0
        for acc_id, amount in balances_pl:
            out[acc_id] = out.get(acc_id, 0.0) + (amount or 0.0) * avg_rate_per
        for acc_id, amount in balances_bs:
            out[acc_id] = (
                out.get(acc_id, 0.0) + (amount or 0.0) * closing_rate_per
            )
        return out

    def _period_average_rate(self, source_currency, currency_to, company):
        """Return the time weighted average conversion rate for the run
        period: the average number of units of currency_to per one unit of
        source_currency across [period_from, period_to], per IAS 21.

        Each daily spot rate is weighted by the number of days it is in
        effect inside the period, so a rate that changes mid period
        contributes in proportion to its duration. Falls back to the
        period midpoint rate when the rate table holds no change inside
        the period, and to the closing rate for a degenerate (single day
        or inverted) period.
        """
        self.ensure_one()
        date_from = self.period_from
        date_to = self.period_to
        if not date_from or not date_to or date_to <= date_from:
            return source_currency._convert(
                1.0, currency_to, company, date_to or self.period_to,
            )
        Rate = self.env['res.currency.rate'].sudo()
        change_dates = Rate.search([
            ('currency_id', 'in', (source_currency | currency_to).ids),
            ('name', '>', date_from),
            ('name', '<=', date_to),
        ]).mapped('name')
        boundaries = sorted(
            {date_from} | {d for d in change_dates if date_from < d <= date_to}
        )
        weighted_sum = 0.0
        total_days = 0
        for idx, seg_start in enumerate(boundaries):
            seg_end = (
                boundaries[idx + 1] if idx + 1 < len(boundaries)
                else date_to + timedelta(days=1)
            )
            days = (seg_end - seg_start).days
            if days <= 0:
                continue
            rate = source_currency._convert(
                1.0, currency_to, company, seg_start,
            )
            weighted_sum += rate * days
            total_days += days
        if not total_days:
            midpoint = date_from + timedelta(
                days=(date_to - date_from).days // 2,
            )
            return source_currency._convert(
                1.0, currency_to, company, midpoint,
            )
        return weighted_sum / total_days

    def _build_nci_vals(self, member, translated_balances):
        """Return the NCI carve-out run-line vals for a member (a balanced
        two-leg list), or None.

        NCI = subsidiary_net_assets x (1 - ownership_pct / 100), where the
        net-asset base is the subsidiary's equity PLUS its current-period
        result. Within a period the profit or loss has not yet closed to
        the equity accounts, so it lives on the P&L accounts; leaving it
        out understated NCI for every profitable or loss-making subsidiary
        and left the whole period result attributed to the parent. Both
        equity and P&L balances are already translated in
        translated_balances (equity at closing rate, P&L at average rate).

        The carve-out is a reclass WITHIN equity, so it books TWO legs:

          Cr NCI equity account            amount = nci_amount (credit-negative)
          Dr parent equity / retained-earn amount = -nci_amount (debit-positive)

        The pair nets to zero by construction, so the minority share moves out
        of parent equity into the NCI account without leaving an unmatched leg
        for the CTA plug to absorb (booking only the NCI credit contaminated the
        reported IAS 21 translation adjustment). The debit leg is tagged
        'elimination' (a consolidation reclass), not 'nci', so the reported NCI
        total stays the genuine minority interest. Returns vals rather than
        creating, so the caller can batch the insert.
        """
        self.ensure_one()
        Account = self.env['account.account'].sudo()
        acc_ids = list(translated_balances.keys())
        if not acc_ids:
            return None
        accounts = Account.browse(acc_ids)
        equity_accs = accounts.filtered(
            lambda a: a.account_type in ('equity', 'equity_unaffected')
        )
        pl_accs = accounts.filtered(
            lambda a: a.account_type in (_INCOME_TYPES + _EXPENSE_TYPES)
        )
        equity_total = sum(
            translated_balances.get(a.id, 0.0) for a in equity_accs
        )
        # P&L balances carry the unclosed period result in Odoo's sign
        # convention (income credit-negative, expense debit-positive), so
        # adding them extends the equity base by the period result in the
        # same convention.
        pl_total = sum(
            translated_balances.get(a.id, 0.0) for a in pl_accs
        )
        nci_base = equity_total + pl_total
        nci_share = 1.0 - member.ownership_pct / 100.0
        currency = self.presentation_currency_id
        if member.nci_basis == 'fair_value':
            # IFRS 3.19(a) fair-value basis, carve path (no acquisition
            # elimination booked for this member): NCI carrying amount =
            # acquisition-date fair value of the minority PLUS the minority
            # share of the post-acquisition equity movement.
            #
            #   base   = translated equity + P&L, Odoo sign (credit-negative)
            #   -base  = equity-positive reporting net assets
            #   A      = acquisition-date equity (presentation currency)
            #   move   = (-base) - A          (post-acquisition movement)
            #   NCI    = FV + (1-o) * move    (equity-positive)
            #
            # stored credit-negative like every equity carve leg.
            A = self._eh_acquisition_equity_pres(member)
            post_acq_movement = (-nci_base) - A
            nci_amount = currency.round(-(
                (member.nci_fair_value or 0.0)
                + nci_share * post_acq_movement
            ))
        else:
            nci_amount = currency.round(nci_base * nci_share)
        # Round-aware zero test: after an FX multiply nci_amount can carry
        # a tiny residual (e.g. 1e-12) that exact == 0.0 never catches,
        # which would emit a spurious near-zero NCI line.
        rounding = currency.rounding or 0.01
        if float_is_zero(nci_amount, precision_rounding=rounding):
            return None
        nci_account = member.nci_account_id or self._nci_account()
        self._eh_require_nci_accounts(member, nci_account)
        reclass_account = self._nci_reclass_account(member)
        self._eh_require_reclass_account(member, reclass_account)
        base = {
            'run_id': self.id,
            'company_id': member.company_id.id,
            'member_id': member.id,
        }
        return [
            dict(base,
                 account_id=nci_account.id,
                 kind='nci',
                 amount=nci_amount),
            dict(base,
                 account_id=reclass_account.id,
                 kind='elimination',
                 amount=-nci_amount),
        ]

    def _eh_require_nci_accounts(self, member, nci_account):
        """Refuse the compute when a member needs an NCI carve-out but no NCI
        equity account can be resolved. Booking the carve-out with account_id
        False would silently drop the minority interest from the consolidated
        set, so surface exactly what to configure instead.
        """
        self.ensure_one()
        if nci_account:
            return
        raise UserError(_(
            "Run %(run)s needs to carve out non-controlling interest for "
            "subsidiary %(sub)s, but no NCI account could be resolved. Set an "
            "NCI Account on the member, or a Default NCI Account on the "
            "consolidation entity %(entity)s (or create an equity account "
            "whose name contains 'non-controlling' or 'minority' on the "
            "parent chart).",
            run=self.name,
            sub=member.company_id.display_name,
            entity=self.entity_id.display_name,
        ))

    def _eh_require_reclass_account(self, member, reclass_account):
        """Refuse the compute when the NCI carve-out cannot resolve the parent
        equity / retained-earnings account it debits. Without it the reclass
        would post a leg with account_id False and unbalance the equity pair,
        leaking into the CTA plug.
        """
        self.ensure_one()
        if reclass_account:
            return
        raise UserError(_(
            "Run %(run)s needs to reclassify the minority share of subsidiary "
            "%(sub)s out of consolidated retained earnings, but no parent "
            "equity account could be resolved for the offsetting leg. "
            "Configure an Equity Elimination Account on the member, or a "
            "retained-earnings / equity account on the parent chart of entity "
            "%(entity)s.",
            run=self.name,
            sub=member.company_id.display_name,
            entity=self.entity_id.display_name,
        ))

    def _reporting_equity_base(self, translated_balances):
        """Return the subsidiary's translated net-asset base: equity accounts
        (closing rate) plus P&L accounts (average rate), in Odoo's sign
        convention. This is the same base _build_nci_vals carves NCI from,
        factored out so the acquisition and post-acquisition NCI paths agree.
        """
        self.ensure_one()
        Account = self.env['account.account'].sudo()
        acc_ids = list(translated_balances.keys())
        if not acc_ids:
            return 0.0
        accounts = Account.browse(acc_ids)
        equity_accs = accounts.filtered(
            lambda a: a.account_type in ('equity', 'equity_unaffected')
        )
        pl_accs = accounts.filtered(
            lambda a: a.account_type in (_INCOME_TYPES + _EXPENSE_TYPES)
        )
        equity_total = sum(
            translated_balances.get(a.id, 0.0) for a in equity_accs
        )
        pl_total = sum(
            translated_balances.get(a.id, 0.0) for a in pl_accs
        )
        return equity_total + pl_total

    def _build_post_acq_nci_vals(self, member, translated_balances):
        """Return the POST-ACQUISITION NCI run-line vals for an
        investment-configured full member, or None when there is no movement.

        The IFRS 3 acquisition elimination books NCI on the ACQUISITION-DATE
        equity A only (nci_leg = -(1-o)*A). It does not attribute any of the
        subsidiary's post-acquisition profit or loss to the minority, so
        without this line the whole post-acquisition result would sit with the
        parent. Here we book the minority's share of the movement in the
        subsidiary's reporting net-asset base since acquisition:

          base = translated reporting equity + P&L (Odoo sign convention)
          post_acq_movement_equity = -base - A     (equity-positive)
          nci = (1 - o) * post_acq_movement_equity

        In Odoo's sign convention a credit-balance equity/net-profit carries a
        negative amount, so -base is the equity-positive net-asset figure and
        subtracting A (the equity-positive acquisition equity) leaves the
        post-acquisition movement. The NCI line is stored in the same
        credit-negative sign the acquisition nci_leg uses, so total NCI for the
        member = acquisition NCI + this line. It is zero (skipped) when the
        reporting base equals the acquisition equity, i.e. no post-acquisition
        movement.

        Like the base carve-out this is a reclass WITHIN equity and books TWO
        legs (Cr NCI equity, Dr parent equity / retained earnings) that net to
        zero by construction, so the minority's share of the post-acquisition
        result moves out of parent equity into NCI without leaving an unmatched
        leg for the CTA plug to absorb. The debit leg is tagged 'elimination',
        not 'nci', so the reported NCI total stays the genuine minority interest.
        """
        self.ensure_one()
        currency = self.presentation_currency_id
        A = self._eh_acquisition_equity_pres(member)
        base = self._reporting_equity_base(translated_balances)
        # -base is the equity-positive reporting net assets; the post-acq
        # movement is that less the acquisition-date equity A.
        post_acq_movement = (-base) - A
        nci_share = 1.0 - member.ownership_pct / 100.0
        # Store in the credit-negative convention the acquisition nci_leg uses:
        # a post-acq profit (positive movement) credits NCI (negative amount).
        nci_amount = currency.round(-post_acq_movement * nci_share)
        rounding = currency.rounding or 0.01
        if float_is_zero(nci_amount, precision_rounding=rounding):
            return None
        nci_account = member.nci_account_id or self._nci_account()
        self._eh_require_nci_accounts(member, nci_account)
        reclass_account = self._nci_reclass_account(member)
        self._eh_require_reclass_account(member, reclass_account)
        base_vals = {
            'run_id': self.id,
            'company_id': member.company_id.id,
            'member_id': member.id,
        }
        return [
            dict(base_vals,
                 account_id=nci_account.id,
                 kind='nci',
                 amount=nci_amount),
            dict(base_vals,
                 account_id=reclass_account.id,
                 kind='elimination',
                 amount=-nci_amount),
        ]

    def _investment_configured(self, member):
        """True when a member carries the config the IFRS 3 auto-elimination
        needs: full method, an investment account, and a positive investment
        amount. Members without this behave exactly as before (no elimination
        auto-fires), preserving existing runs.
        """
        return bool(
            member.method == 'full'
            and member.investment_account_id
            and member.investment_amount
            and member.investment_amount > 0.0
        )

    def _eh_acquisition_equity_pres(self, member):
        """Return the member's acquisition-date equity in the PRESENTATION
        currency.

        acquisition_equity is stated in the presentation currency by
        default. When the member carries a historical_rate (> 0) it is
        stated in the subsidiary's functional currency instead and is
        translated at that acquisition-date rate (IAS 21.23(b): the
        pre-acquisition equity removed by the elimination is a non-monetary
        historical figure, translated at the historical rate, never
        retranslated at closing)."""
        self.ensure_one()
        A = member.acquisition_equity or 0.0
        rate = member.historical_rate or 0.0
        if rate > 0.0:
            A = A * rate
        return A

    def _build_acquisition_elimination_vals(self, member):
        """Return the IFRS 3 acquisition-elimination run-line vals for a
        member, or None when its investment is not configured (or the
        entity has switched the auto-elimination off).

        Removes the parent's investment against the subsidiary's
        acquisition-date equity and books acquisition-date NCI and goodwill:

          Let A = acquisition-date equity in the presentation currency
                  (_eh_acquisition_equity_pres: acquisition_equity, times
                  the historical rate when one is set),
              I = member.investment_amount,
              o = member.ownership_pct / 100,
              N = acquisition-date NCI:
                    proportionate basis: N = (1-o) * A     (IFRS 3.19(b))
                    fair-value basis:    N = nci_fair_value (IFRS 3.19(a))

          Dr equity_elimination_account by A         amount = +A
          Cr investment_account       by I           amount = -I
          Cr nci_account              by N           amount = -N
          Dr/Cr goodwill_account      by (I + N - A) amount = +(I + N - A)

        The four signed amounts sum to zero by construction:
          A - I - N + (I + N - A) = 0.

        On the proportionate basis the goodwill residual reduces to the
        familiar partial-goodwill figure I - o*A; on the fair-value basis it
        is the full-goodwill figure I + FV(NCI) - A. A negative residual is
        a bargain purchase, carried as a credit (gain) on the goodwill
        account. The goodwill leg is tagged kind='goodwill' so recognised
        goodwill is queryable by kind, and the acquisition-date minority leg
        is tagged 'nci' so the nci_amount KPI includes it.

        All legs are rounded to the presentation currency. Returns a list of
        vals so the caller can batch the insert. Skips gracefully (returns
        None) when the required accounts are not all set, falling back to the
        compute warning.
        """
        self.ensure_one()
        if not self.entity_id.auto_eliminate_investment:
            # Opted out: keep the diagnostic-warning behaviour and leave the
            # elimination to a manual entry.
            return None
        if not self._investment_configured(member):
            return None
        if not (
            member.equity_elimination_account_id
            and member.goodwill_account_id
            and member.nci_account_id
        ):
            # Config incomplete: cannot build a balanced elimination. Leave it
            # for the compute warning and the manual/NCI path.
            return None
        currency = self.presentation_currency_id
        o = member.ownership_pct / 100.0
        A = self._eh_acquisition_equity_pres(member)
        I = member.investment_amount or 0.0
        equity_leg = currency.round(A)
        investment_leg = currency.round(-I)
        if member.nci_basis == 'fair_value':
            nci_leg = currency.round(-(member.nci_fair_value or 0.0))
        else:
            nci_leg = currency.round(-(1.0 - o) * A)
        # Goodwill absorbs the rounding residual so the four legs sum to
        # exactly zero regardless of currency rounding.
        goodwill_leg = -(equity_leg + investment_leg + nci_leg)
        base = {
            'run_id': self.id,
            'kind': 'elimination',
            'company_id': member.company_id.id,
            'member_id': member.id,
        }
        return [
            dict(base, account_id=member.equity_elimination_account_id.id,
                 amount=equity_leg),
            dict(base, account_id=member.investment_account_id.id,
                 amount=investment_leg),
            # Tag the acquisition-date minority leg 'nci' (not 'elimination')
            # so the nci_amount KPI includes the acquisition-date minority
            # share, per its help text. The four legs still sum to zero by
            # construction, so the balance identity is untouched.
            dict(base, account_id=member.nci_account_id.id,
                 kind='nci',
                 amount=nci_leg),
            dict(base, account_id=member.goodwill_account_id.id,
                 kind='goodwill',
                 amount=goodwill_leg),
        ]

    def _equity_pickup_configured(self, member):
        """True when an equity-method member can be picked up automatically:
        it must carry both an investment account and a share-of-profit
        account.
        """
        return bool(
            member.method == 'equity'
            and member.investment_account_id
            and member.share_of_profit_account_id
        )

    def _eh_require_equity_config(self, member):
        """Refuse the compute when an equity-method member (without the IAS
        28.1A fair value option) is missing its pick-up configuration.

        Skipping the member silently would drop the associate from the
        consolidated result entirely, which is exactly the kind of
        honour-system gap IAS 28 does not allow: equity accounting is
        mandatory for an associate unless the fair value option applies.
        """
        self.ensure_one()
        if self._equity_pickup_configured(member):
            return
        missing = []
        if not member.investment_account_id:
            missing.append(_("an Investment Account"))
        if not member.share_of_profit_account_id:
            missing.append(_("a Share of Profit Account"))
        raise UserError(_(
            "Run %(run)s cannot be computed: equity-method member "
            "%(member)s is missing %(missing)s. IAS 28 equity accounting "
            "is mandatory for an associate, so configure the member (or "
            "elect the IAS 28.1A fair value option on it) before "
            "computing.",
            run=self.name,
            member=member.company_id.display_name,
            missing=_(" and ").join(missing),
        ))

    def _build_equity_pickup_vals(self, member):
        """Return the IAS 28 equity pick-up run-line vals for an equity-method
        member, or None when it is not configured for auto pick-up.

        Computes the member's translated period profit from its P&L accounts,
        takes the parent's ownership share, and books two balanced legs that
        increase the investment carrying value and consolidated income:

          share = (ownership_pct / 100) * profit

          Dr investment_account       by share   amount = +share
          Cr share_of_profit_account  by share   amount = -share

        Profit here is the positive (income - expense) result. In Odoo's sign
        convention a translated P&L balance carries income credit-negative and
        expense debit-positive, so the net period result is the negation of
        the summed P&L balances. The investment is debited (increased) by the
        parent's share of a profit, credited by its share of a loss; the two
        legs sum to zero by construction. Returns None (skips) when profit
        rounds to zero.
        """
        self.ensure_one()
        if not self._equity_pickup_configured(member):
            return None
        translated = self._fetch_balances(
            company=member.company_id,
            currency_to=self.presentation_currency_id,
            translate=True,
            source_currency=member.functional_currency_id,
        )
        Account = self.env['account.account'].sudo()
        acc_ids = list(translated.keys())
        if not acc_ids:
            return None
        accounts = Account.browse(acc_ids)
        pl_accs = accounts.filtered(
            lambda a: a.account_type in (_INCOME_TYPES + _EXPENSE_TYPES)
        )
        pl_total = sum(translated.get(a.id, 0.0) for a in pl_accs)
        # pl_total is credit-negative for a net profit; negate to get the
        # profit as a positive number.
        profit = -pl_total
        currency = self.presentation_currency_id
        share = currency.round((member.ownership_pct / 100.0) * profit)
        rounding = currency.rounding or 0.01
        if float_is_zero(share, precision_rounding=rounding):
            return None
        base = {
            'run_id': self.id,
            'kind': 'equity_pickup',
            'company_id': member.company_id.id,
            'member_id': member.id,
        }
        return [
            dict(base, account_id=member.investment_account_id.id,
                 amount=share),
            dict(base, account_id=member.share_of_profit_account_id.id,
                 amount=-share),
        ]

    def _build_unrealised_profit_vals(self, unrealised):
        """Return the intra-group unrealised-profit elimination run-line vals
        for one record, or None when the margin rounds to zero.

        Removes profit left in the buyer's ending inventory from an intra-group
        sale (IFRS 10 / IAS 27): the seller recognised a margin that, from the
        group's point of view, is not yet realised because the stock has not
        left the group. Consolidated inventory is written back to group cost
        and consolidated profit is reduced by the same amount:

          Let m = unrealised.unrealised_amount (positive margin).

          Dr cogs / retained-earnings account   amount = +m
          Cr inventory account                  amount = -m

        The two signed amounts sum to zero by construction, so the run stays
        balanced. Returns a list of vals so the caller can batch the insert.
        Skips gracefully (returns None) when both accounts are unset or when
        the margin rounds to zero.
        """
        self.ensure_one()
        currency = self.presentation_currency_id
        amount = currency.round(unrealised.unrealised_amount or 0.0)
        rounding = currency.rounding or 0.01
        if float_is_zero(amount, precision_rounding=rounding):
            return None
        if not (
            unrealised.cogs_or_re_account_id
            and unrealised.inventory_account_id
        ):
            return None
        base = {
            'run_id': self.id,
            'kind': 'elimination',
        }
        return [
            dict(base, account_id=unrealised.cogs_or_re_account_id.id,
                 amount=amount),
            dict(base, account_id=unrealised.inventory_account_id.id,
                 amount=-amount),
        ]

    # ---- automatic intragroup elimination (IFRS 10.B86) ----

    def _eh_group_companies(self):
        """Return the res.company records that participate in intragroup
        elimination: the parent plus every full / proportional member.

        Equity-method members are not rolled up line by line, so their
        intragroup balances are not double-counted in the consolidated set and
        must not be auto-eliminated here.
        """
        self.ensure_one()
        companies = self.entity_id.parent_company_id
        for member in self.entity_id.member_ids:
            if member.method in ('full', 'proportional') and member.company_id:
                companies |= member.company_id
        return companies

    def _eh_intragroup_translate_rate(self, company, is_pl):
        """Return the per-unit conversion rate from a company's currency to the
        presentation currency, consistent with _fetch_balances: closing rate
        (period_to) for balance-sheet items, period average rate for P&L.
        Returns 1.0 when the company currency already is the presentation
        currency.
        """
        self.ensure_one()
        source = company.currency_id
        target = self.presentation_currency_id
        if not source or not target or source == target:
            return 1.0
        if is_pl:
            return self._period_average_rate(source, target, company)
        return source._convert(1.0, target, company, self.period_to)

    def _eh_intragroup_balance(self, company, counterparty, account_types,
                               is_pl):
        """Return the signed, presentation-currency balance on `company`'s
        posted AML for the given account types, restricted to lines whose
        partner's commercial partner is `counterparty`'s company partner.

        The sign follows Odoo's convention (debit positive). P&L uses the
        period window and the average rate; balance-sheet uses the cumulative
        balance to period_to and the closing rate, matching _fetch_balances so
        the eliminated amounts are on the same basis as the rolled-up balances.
        Returns {account_id: translated_amount}.
        """
        self.ensure_one()
        counter_partner = counterparty.partner_id.commercial_partner_id
        if not counter_partner:
            return {}
        AML = self.env['account.move.line'].sudo()
        domain = [
            ('company_id', '=', company.id),
            ('parent_state', '=', 'posted'),
            ('account_id.account_type', 'in', list(account_types)),
            ('partner_id.commercial_partner_id', '=', counter_partner.id),
        ]
        if is_pl:
            domain += [
                ('date', '>=', self.period_from),
                ('date', '<=', self.period_to),
            ]
        else:
            domain += [('date', '<=', self.period_to)]
        rows = grouped_sum(AML, domain, 'account_id', 'balance')
        rate = self._eh_intragroup_translate_rate(company, is_pl)
        return {
            acc_id: (amount or 0.0) * rate
            for acc_id, amount in rows if amount
        }

    def _eh_manual_elim_accounts(self):
        """Return the set of account ids already covered by a posted manual
        elimination on this run. Used to skip auto elimination for a pair whose
        accounts a reviewer has already eliminated by hand, so the two paths do
        not double-eliminate.
        """
        self.ensure_one()
        return set(
            self.elimination_ids.filtered(lambda e: e.state == 'posted')
            .line_ids.mapped('account_id').ids
        )

    def _build_auto_intragroup_vals(self):
        """Build automatic IFRS 10.B86 intragroup elimination run-line vals.

        Returns (vals_list, warnings). For each ordered pair of group companies
        (A, B):

          receivables/payables: A's receivable to B is eliminated against
          A's own reciprocal (removing the intragroup AR), paired with the
          removal of B's payable to A (removing the intragroup AP). Each is a
          balanced two-leg pair summing to zero, so the run stays balanced and
          the CTA is untouched.

          sales/purchases: A's sales income earned on the counterparty B is
          eliminated against B's purchases / cost of sales on A, again as a
          balanced pair.

        When the reciprocal balances do not agree (A's AR to B does not equal
        B's AP to A, or A's sales to B do not equal B's purchases from A) a
        diagnostic is collected rather than the difference being silently
        plugged; the matched (receivable / sales-side) amount is still
        eliminated. Accounts already covered by a posted manual elimination are
        skipped to avoid double elimination.
        """
        self.ensure_one()
        currency = self.presentation_currency_id
        rounding = currency.rounding or 0.01
        companies = self._eh_group_companies()
        manual_accounts = self._eh_manual_elim_accounts()
        vals_list = []
        warnings = []
        base = {'run_id': self.id, 'kind': 'elimination'}

        def _sum(d):
            return sum(d.values())

        def _emit_pair(acc_a, amt_a, acc_b, amt_b, note):
            """Append a balanced two-leg elimination that removes amt_a from
            acc_a and amt_b from acc_b. amt_a and amt_b are the balances to
            remove; the legs book their negation so the group balance nets out.
            The pair sums to zero only when amt_a == -amt_b, which holds for a
            reciprocal (AR debit vs AP credit / income credit vs expense debit)
            that agrees. When it does not agree we still emit the receivable /
            sales-side removal against an equal-and-opposite counter leg so the
            pair is balanced by construction, and warn about the gap.
            """
            leg_a = currency.round(-amt_a)
            if float_is_zero(leg_a, precision_rounding=rounding):
                return
            vals_list.append(dict(
                base, account_id=acc_a, amount=leg_a, notes=note))
            vals_list.append(dict(
                base, account_id=acc_b, amount=-leg_a, notes=note))

        for company_a in companies:
            for company_b in companies:
                if company_a == company_b:
                    continue
                # --- reciprocal receivables vs payables ---
                ar = self._eh_intragroup_balance(
                    company_a, company_b,
                    ('asset_receivable',), is_pl=False)
                ap = self._eh_intragroup_balance(
                    company_b, company_a,
                    ('liability_payable',), is_pl=False)
                ar_total = _sum(ar)
                ap_total = _sum(ap)
                # Only act when A actually carries a receivable to B (ordered
                # pair), so the reciprocal is eliminated once, from A's side.
                if not float_is_zero(ar_total, precision_rounding=rounding):
                    ar_acc = max(ar, key=lambda k: abs(ar[k])) if ar else None
                    ap_acc = max(ap, key=lambda k: abs(ap[k])) if ap else None
                    if (
                        ar_acc is not None and ap_acc is not None
                        and ar_acc not in manual_accounts
                        and ap_acc not in manual_accounts
                    ):
                        # Reciprocal agrees when ar_total == -ap_total (AR
                        # debit-positive, AP credit-negative). Remove the AR
                        # from the receivable account and the matched amount
                        # from the payable account (balanced pair).
                        _emit_pair(
                            ar_acc, ar_total, ap_acc, -ar_total,
                            _("Auto IC AR/AP: %(a)s vs %(b)s",
                              a=company_a.name, b=company_b.name))
                        if not float_is_zero(
                            ar_total + ap_total,
                            precision_rounding=rounding,
                        ):
                            warnings.append(_(
                                "Intragroup receivable of %(a)s to %(b)s "
                                "(%(ar).2f) does not agree with the reciprocal "
                                "payable (%(ap).2f); the receivable amount was "
                                "eliminated but the %(diff).2f difference needs "
                                "review.",
                                a=company_a.name, b=company_b.name,
                                ar=ar_total, ap=ap_total,
                                diff=ar_total + ap_total))
                # --- sales income vs purchases / cost of sales ---
                sales = self._eh_intragroup_balance(
                    company_a, company_b, _INCOME_TYPES, is_pl=True)
                purch = self._eh_intragroup_balance(
                    company_b, company_a, _EXPENSE_TYPES, is_pl=True)
                sales_total = _sum(sales)
                purch_total = _sum(purch)
                if not float_is_zero(sales_total, precision_rounding=rounding):
                    sales_acc = (
                        max(sales, key=lambda k: abs(sales[k]))
                        if sales else None)
                    purch_acc = (
                        max(purch, key=lambda k: abs(purch[k]))
                        if purch else None)
                    if (
                        sales_acc is not None and purch_acc is not None
                        and sales_acc not in manual_accounts
                        and purch_acc not in manual_accounts
                    ):
                        # Income is credit-negative, expense debit-positive; an
                        # agreeing pair has sales_total == -purch_total. Remove
                        # the sales income and the matched expense (balanced).
                        _emit_pair(
                            sales_acc, sales_total, purch_acc, -sales_total,
                            _("Auto IC sales/COGS: %(a)s vs %(b)s",
                              a=company_a.name, b=company_b.name))
                        if not float_is_zero(
                            sales_total + purch_total,
                            precision_rounding=rounding,
                        ):
                            warnings.append(_(
                                "Intragroup sales of %(a)s to %(b)s "
                                "(%(s).2f) do not agree with the counterparty "
                                "purchases / cost of sales (%(p).2f); the "
                                "sales amount was eliminated but the "
                                "%(diff).2f difference needs review.",
                                a=company_a.name, b=company_b.name,
                                s=sales_total, p=purch_total,
                                diff=sales_total + purch_total))
        return vals_list, warnings

    def _eh_compute_warnings(self):
        """Return diagnostics for consolidation steps not yet automated.

        Two structural gaps are deferred to the IFRS 3 / IAS 28 build and
        must never be shipped silently:

        * A full-method subsidiary's investment in the parent's books has
          to be eliminated against the subsidiary's equity, or consolidated
          equity is overstated (the parent's investment and the sub's share
          capital both appear). We flag any full member whose configured
          investment account is not covered by a posted elimination, and
          note full members with no investment account configured at all.
        * Equity-method members are not rolled up and their share of profit
          is not picked up automatically, so they are invisible unless the
          reviewer adds a manual investment / share-of-profit line.
        """
        self.ensure_one()
        warns = []
        members = self.entity_id.member_ids
        # Equity-method members without a pick-up configuration now BLOCK the
        # compute (see _eh_require_equity_config) unless they elect the IAS
        # 28.1A fair value option, so no warning branch remains for them; a
        # fair-value-option member is documented by its disclosure line.
        posted_elim_accounts = self.elimination_ids.filtered(
            lambda e: e.state == 'posted'
        ).line_ids.mapped('account_id')
        unresolved = []
        unconfigured = []
        for m in members.filtered(lambda m: m.method == 'full'):
            # A member whose investment is fully configured for the IFRS 3
            # auto-elimination is resolved: the run books the elimination
            # itself, so it is neither unconfigured nor un-eliminated. When
            # the entity has switched the auto-elimination off, the same
            # member falls back to the diagnostic warnings below.
            if (
                self.entity_id.auto_eliminate_investment
                and self._investment_configured(m)
                and m.equity_elimination_account_id
                and m.goodwill_account_id
                and m.nci_account_id
            ):
                continue
            if not m.investment_account_id:
                unconfigured.append(m.company_id.display_name)
            elif m.investment_account_id not in posted_elim_accounts:
                unresolved.append(m.company_id.display_name)
        if unresolved:
            warns.append(_(
                "The parent's investment is not eliminated against the "
                "equity of these subsidiaries, so consolidated equity is "
                "overstated until you post an investment-elimination entry "
                "(configure the member's acquisition fields and enable "
                "Auto-Eliminate Investment on the entity to have the run "
                "book it automatically): %s.",
                ', '.join(unresolved)))
        if unconfigured:
            warns.append(_(
                "These full-consolidation subsidiaries have no investment "
                "account configured, so the investment elimination cannot "
                "be verified; confirm the investment-in-subsidiary is "
                "eliminated: %s.",
                ', '.join(unconfigured)))
        return warns

    def _compute_cta(self):
        """Compute the CTA balancing entry.

        After translating each member's TB at the configured rates,
        the consolidated balance sheet may not balance because
        income / expense use average rate while assets / liabilities
        use closing rate. The difference is the CTA that goes to
        OCI under IAS 21. We compute it as the negation of the sum
        of every translated balance line on the run so the books
        balance after the entry posts.
        """
        self.ensure_one()
        total = sum(
            self.line_ids.filtered(
                lambda l: l.kind in _CTA_BASE_KINDS,
            ).mapped('amount'),
        )
        # CTA balances the books -> negate. Round in the presentation
        # currency so a 0-dp (JPY) or 3-dp (KWD/BHD) plug carries the right
        # precision and sum(run lines) still nets to zero.
        return -self.presentation_currency_id.round(total)

    def _cta_account(self):
        """Return the CTA / translation-reserve account for this run.

        Prefers the entity's explicit cta_account_id; only when unset falls
        back to the name heuristic (an equity account on the parent chart
        whose name contains 'translation' or 'CTA'). Returns empty when
        neither resolves; the caller then refuses the compute rather than
        posting a CTA line with no account.
        """
        self.ensure_one()
        if self.entity_id.cta_account_id:
            return self.entity_id.cta_account_id
        Account = self.env['account.account'].sudo()
        return Account.search([
            ('account_type', '=', 'equity'),
            '|',
            ('name', 'ilike', 'translation'),
            ('name', 'ilike', 'CTA'),
            (_acc_company_field(self.env), 'in',
             self.entity_id.parent_company_id.ids),
        ], limit=1)

    def _nci_account(self):
        """Return the default NCI equity account for this run.

        Prefers the entity's explicit nci_account_id; only when unset falls
        back to the name heuristic (an equity account on the parent chart
        whose name contains 'non-controlling' or 'minority'). Returns empty
        when neither resolves; callers that would book an NCI line then refuse
        rather than posting a line with no account.
        """
        self.ensure_one()
        if self.entity_id.nci_account_id:
            return self.entity_id.nci_account_id
        Account = self.env['account.account'].sudo()
        return Account.search([
            ('account_type', '=', 'equity'),
            '|',
            ('name', 'ilike', 'non-controlling'),
            ('name', 'ilike', 'minority'),
            (_acc_company_field(self.env), 'in',
             self.entity_id.parent_company_id.ids),
        ], limit=1)

    def _nci_reclass_account(self, member):
        """Return the consolidated parent-equity / retained-earnings account
        the NCI carve-out debits (reclassifying the minority share OUT of parent
        equity INTO the NCI account), or empty when none can be resolved.

        The NCI carve-out is a two-legged reclass within equity: it credits the
        NCI equity account and must debit an equal amount out of consolidated
        retained earnings / parent equity, otherwise the run does not balance by
        that pair alone and the unmatched leg pollutes the CTA plug. Preference
        order:

          1. member.equity_elimination_account_id, the subsidiary's
             share-capital / retained-earnings equity account already configured
             for the IFRS 3 acquisition path, when set.
          2. a retained-earnings account on the parent chart
             (account_type == 'equity_unaffected').
          3. any equity account on the parent chart other than the NCI account
             itself, so the reclass has a genuine counterparty.
        """
        self.ensure_one()
        if member.equity_elimination_account_id:
            return member.equity_elimination_account_id
        Account = self.env['account.account'].sudo()
        company_field = _acc_company_field(self.env)
        parent_ids = self.entity_id.parent_company_id.ids
        re_account = Account.search([
            ('account_type', '=', 'equity_unaffected'),
            (company_field, 'in', parent_ids),
        ], limit=1)
        if re_account:
            return re_account
        nci_account = self._nci_account()
        return Account.search([
            ('account_type', '=', 'equity'),
            ('id', 'not in', nci_account.ids),
            (company_field, 'in', parent_ids),
        ], limit=1)

    # ---- helpers ----

    def action_view_lines(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Run lines"),
            'res_model': 'eh.consol.run.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('run_id', '=', self.id)],
            'context': {'default_run_id': self.id},
        }

    def action_view_eliminations(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Eliminations"),
            'res_model': 'eh.consol.elimination',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('run_id', '=', self.id)],
            'context': {'default_run_id': self.id},
        }
