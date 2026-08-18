# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
FX revaluation run record.

A run translates every open foreign currency journal item on flagged
accounts at the closing rate of revaluation_date and books the
unrealised gain or loss to a single audited journal entry.

State machine:

  draft -> computed -> posted -> reversed
                              \\-> cancelled
                    -> cancelled

Lines (eh.fx.revaluation.line) are produced in the compute step and
made immutable on post. The generated move and the optional reversal
move are linked back to the run.
"""

import logging
from collections import defaultdict
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Measurement / input fields that become the audit record behind a posted
# journal entry. Once the run has posted (or reversed after auto-reverse),
# these are frozen at the ORM layer. 'state' is deliberately NOT listed: the
# action methods write only state + audit stamps, so a pure state-transition
# write is never blocked, while a write touching any of these while posted is
# always blocked. action_compute runs only in draft/computed, so freezing on
# 'posted'/'reversed' never interferes with recompute.
_FROZEN_AFTER_POST = (
    'line_ids', 'revaluation_date', 'journal_id', 'gain_account_id',
    'loss_account_id', 'auto_reverse', 'aggregate_by_partner',
    'company_id', 'description', 'notes',
)


class EhFxRevaluationRun(models.Model):
    _name = 'eh.fx.revaluation.run'
    _description = "FX Revaluation Run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard', 'eh.gl.reversal']
    _order = 'revaluation_date desc, id desc'

    # A run's state is a posting state machine: each transition posts / reverses
    # a journal entry. Block any direct write to state that does not originate
    # from one of the action_* methods (which flag the write).
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    description = fields.Char(
        help="Optional label, e.g. 'March 2026 month end'.",
    )
    state = fields.Selection([
        ('draft', "Draft"),
        ('computed', "Computed"),
        ('posted', "Posted"),
        ('reversed', "Reversed"),
        ('cancelled', "Cancelled"),
    ], default='draft', required=True, tracking=True)

    revaluation_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Closing date for which rates are fetched and balances "
             "translated.",
    )
    journal_id = fields.Many2one(
        'account.journal', string="Journal", required=True,
        domain="[('type', '=', 'general')]",
    )
    gain_account_id = fields.Many2one(
        'account.account', string="Unrealised FX Gain Account",
        required=True,
        domain="[('account_type', '=', 'income_other')]",
    )
    loss_account_id = fields.Many2one(
        'account.account', string="Unrealised FX Loss Account",
        required=True,
        domain="[('account_type', '=', 'expense')]",
    )
    auto_reverse = fields.Boolean(
        default=True,
        help="If set, posting the run also creates and posts a reversal "
             "entry dated the day after the revaluation date.",
    )
    aggregate_by_partner = fields.Boolean(
        default=True,
        help="If set, lines are aggregated per (account, partner, "
             "currency). Otherwise lines are aggregated per (account, "
             "currency) only.",
    )

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', readonly=True, store=True,
    )

    line_ids = fields.One2many(
        'eh.fx.revaluation.line', 'run_id', copy=False,
    )
    move_id = fields.Many2one(
        'account.move', string="Revaluation Entry", readonly=True,
        copy=False, ondelete='restrict',
    )
    reversal_move_id = fields.Many2one(
        'account.move', string="Reversal Entry", readonly=True,
        copy=False, ondelete='restrict',
    )

    # ---- audit ----
    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)
    reversed_at = fields.Datetime(readonly=True, tracking=True)
    reversed_by_id = fields.Many2one('res.users', readonly=True)
    cancelled_at = fields.Datetime(readonly=True, tracking=True)
    cancelled_by_id = fields.Many2one('res.users', readonly=True)

    # ---- totals ----
    total_gain = fields.Monetary(compute='_compute_totals', store=True)
    total_loss = fields.Monetary(compute='_compute_totals', store=True)
    net_adjustment = fields.Monetary(compute='_compute_totals', store=True)
    line_count = fields.Integer(compute='_compute_totals', store=True)
    realized_gain_loss = fields.Monetary(
        compute='_compute_realization_split',
        string="Realized Gain/Loss",
        help=(
            "Net adjustment of lines whose source journal items have "
            "all since been fully reconciled: the exposure settled, so "
            "the FX movement measured by this run is now realized. "
            "Not stored: it tracks live reconciliation state."
        ),
    )
    unrealized_gain_loss = fields.Monetary(
        compute='_compute_realization_split',
        string="Unrealized Gain/Loss",
        help=(
            "Net adjustment of lines whose source journal items are "
            "still open (unreconciled). These follow the auto-reverse "
            "pattern: the entry reverses the day after and the "
            "movement re-measures at the next close until settled."
        ),
    )

    notes = fields.Text()

    _sql_constraints = [
        ('uniq_date_company', 'unique(revaluation_date, company_id)', 'Only one revaluation run per date per company.'),
    ]

    # ---- compute ----

    @api.depends('line_ids.adjustment')
    def _compute_totals(self):
        for run in self:
            gains = sum(line_item.adjustment for line_item in run.line_ids if line_item.adjustment > 0)
            losses = sum(-line_item.adjustment for line_item in run.line_ids if line_item.adjustment < 0)
            run.total_gain = gains
            run.total_loss = losses
            run.net_adjustment = gains - losses
            run.line_count = len(run.line_ids)

    @api.depends('line_ids.adjustment', 'line_ids.is_realized')
    def _compute_realization_split(self):
        """Split the run's net adjustment into realized vs unrealized.

        Derivation: every revaluation line stores the exact source
        journal items it aggregated (source_move_line_ids). A line is
        realized once ALL of its source items are fully reconciled,
        i.e. the exposure has been settled and the FX movement this run
        measured has crystallised through settlement. Everything else
        (still-open items) is unrealized and lives under the
        auto-reverse pattern. Both totals are signed nets, so
        realized_gain_loss + unrealized_gain_loss == net_adjustment.
        """
        for run in self:
            realized = 0.0
            unrealized = 0.0
            for line in run.line_ids:
                if line.is_realized:
                    realized += line.adjustment
                else:
                    unrealized += line.adjustment
            run.realized_gain_loss = realized
            run.unrealized_gain_loss = unrealized

    # ---- onchange (live form feedback) ----

    @api.onchange('company_id')
    def _onchange_company_id_default_accounts(self):
        """Pre-fill gain/loss accounts and journal from the company's
        FX defaults when the user picks a company on a fresh draft.

        onchange always fires on a single NewId record, so we treat
        self as one and avoid the search-in-loop shape. We only write
        fields that are still empty so a deliberate choice is never
        overwritten.
        """
        self.ensure_one()
        if not self.company_id or self.journal_id:
            return
        journal = self.env['account.journal'].sudo().search([
            ('type', '=', 'general'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        if journal:
            self.journal_id = journal

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code('eh.fx.revaluation.run') or '/'
                vals['name'] = seq
        return super().create(vals_list)

    # ---- write / unlink guards ----

    def write(self, vals):
        """Freeze measurement / input fields once the run has posted.

        A pure state-transition write (the action methods write only
        {'state': ...} plus posted_at/posted_by and the move links) carries
        no frozen field and passes. A write touching a frozen field while
        any record is posted or reversed is always blocked. action_compute
        runs only in draft/computed, so its line_ids rebuild is never
        blocked here.
        """
        frozen = [f for f in _FROZEN_AFTER_POST if f in vals]
        confirmed = self.filtered(lambda r: r.state in ('posted', 'reversed'))
        if frozen and confirmed:
            raise UserError(_(
                "Fields on a posted FX revaluation run are frozen "
                "(%(fields)s). Reverse it first (EH Accounting Manager "
                "only) to change it.",
                fields=', '.join(frozen)))
        # A posted / reversed run's state is a control point: resetting it to
        # draft would lift the freeze above. A raw ORM state write without the
        # sanctioned-transition context flag is manager-gated so a plain user
        # cannot un-freeze a GL-backed run.
        if 'state' in vals \
                and not self.env.context.get('eh_run_state_change'):
            crossing = confirmed.filtered(lambda r: r.state != vals['state'])
            if crossing:
                crossing._check_manager()
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager may change the state of a "
                "posted or reversed FX revaluation run."))

    def unlink(self):
        posted = self.filtered(lambda r: r.state in ('posted', 'reversed'))
        if posted:
            raise UserError(_(
                "A posted FX revaluation run cannot be deleted; reverse "
                "it first."))
        return super().unlink()

    # ---- transitions ----

    def action_compute(self):
        """Scan open foreign currency balances and produce revaluation lines."""
        self = self._eh_workflow_action()
        for run in self:
            if run.state not in ('draft', 'computed'):
                raise UserError(_(
                    "Compute is only available in draft or computed state.",
                ))
            run.line_ids.unlink()
            run._build_lines()
            run.write({
                'state': 'computed',
                'computed_at': fields.Datetime.now(),
                'computed_by_id': self.env.user.id,
            })

    def action_post(self):
        self = self._eh_workflow_action()
        for run in self:
            if not self.env.user.has_group('eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only accounting managers can post FX revaluation runs.",
                ))
            # Serialise concurrent posts (a double click or a browser retry)
            # BEFORE reading state, so two transactions cannot both observe
            # 'computed', both build+post a move and both stamp 'posted' -
            # which would leave two posted revaluation entries and orphan the
            # first move. The loser re-reads the committed 'posted'/'reversed'
            # state and stops at the guard below.
            run._eh_lock_for_post()
            if run.state != 'computed':
                raise UserError(_(
                    "Run must be in computed state before posting.",
                ))
            if not run.line_ids:
                raise UserError(_(
                    "No revaluation lines to post.",
                ))
            move = run._build_move()
            run.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'move_id': move.id,
            })
            if run.auto_reverse:
                rev_move = run._build_reversal_move()
                run.with_context(eh_run_state_change=True).write({
                    'state': 'reversed',
                    'reversed_at': fields.Datetime.now(),
                    'reversed_by_id': self.env.user.id,
                    'reversal_move_id': rev_move.id,
                })

    def action_reverse(self):
        self = self._eh_workflow_action()
        for run in self:
            if not self.env.user.has_group('eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only accounting managers can reverse FX revaluation runs.",
                ))
            # Same double-submit guard as action_post: lock and re-read before
            # checking state / reversal_move so two concurrent reversals cannot
            # both build a reversal move for the one posted run.
            run._eh_lock_for_post()
            if run.state != 'posted':
                raise UserError(_(
                    "Only posted runs can be reversed.",
                ))
            if run.reversal_move_id:
                raise UserError(_(
                    "Run already has a reversal move.",
                ))
            rev_move = run._build_reversal_move()
            run.with_context(eh_run_state_change=True).write({
                'state': 'reversed',
                'reversed_at': fields.Datetime.now(),
                'reversed_by_id': self.env.user.id,
                'reversal_move_id': rev_move.id,
            })

    def action_cancel(self):
        self = self._eh_workflow_action()
        for run in self:
            if not self.env.user.has_group('eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only accounting managers can cancel FX revaluation runs.",
                ))
            if run.state in ('posted', 'reversed'):
                raise UserError(_(
                    "Cannot cancel a posted or reversed run.",
                ))
            run.write({
                'state': 'cancelled',
                'cancelled_at': fields.Datetime.now(),
                'cancelled_by_id': self.env.user.id,
            })

    def action_set_to_draft(self):
        self = self._eh_workflow_action()
        for run in self:
            if run.state != 'cancelled':
                raise UserError(_(
                    "Only cancelled runs can return to draft.",
                ))
            run.line_ids.unlink()
            run.write({
                'state': 'draft',
                'cancelled_at': False,
                'cancelled_by_id': False,
            })

    def action_fetch_missing_rates(self):
        """Pull rates from the configured provider for revaluation_date.

        The button is most useful right before action_compute so the
        ledger has rates dated on the revaluation date itself, removing
        the need for finance to enter them manually. The configured
        provider serves the company's currency as base; ECB cross-
        derives via EUR for non-EUR companies. Only foreign currencies
        with at least one open monetary line in scope are requested,
        which keeps the cron-friendly path light on cold caches.
        """
        for run in self:
            config = self.env['eh.fx.rate.config'].search([
                ('company_id', '=', run.company_id.id),
            ], limit=1)
            if not config or not config.enabled:
                raise UserError(_(
                    "Company %s has no enabled FX rate config. Open "
                    "Configuration > Heritage > FX Rate Config to "
                    "select a provider.",
                ) % run.company_id.display_name)
            # Only ask for currencies that actually appear in open
            # lines on revaluable accounts; this avoids hammering the
            # provider for currencies the ledger never used.
            accounts = run._eligible_accounts()
            raw = run._open_lines_query(accounts)
            wanted_ids = {row['currency_id'] for row in raw if row.get('currency_id')}
            currencies = self.env['res.currency'].browse(list(wanted_ids))
            codes = [c.name for c in currencies if c and c != run.company_id.currency_id]
            if not codes:
                raise UserError(_(
                    "Run %s has no foreign-currency exposure to fetch "
                    "rates for.",
                ) % (run.name or run.id))
            config.fetch_rates(currency_codes=codes, on_date=run.revaluation_date)
            run.message_post(body=_(
                "Fetched %(count)s rate(s) from %(provider)s for "
                "%(date)s.",
                count=len(codes),
                provider=config.provider,
                date=run.revaluation_date,
            ))

    # ---- helpers ----

    def _eh_lock_for_post(self):
        """Take a row lock on this run and drop cached state so a serialised
        concurrent post/reverse re-reads the committed state rather than a
        stale pre-transition snapshot.

        Closes the double-submit race in which two transactions both read
        state=='computed', both build+post a revaluation move and both stamp
        'posted', producing two posted entries for one run and orphaning the
        first move. Mirrors eh_account_recurring_invoices'
        _eh_lock_for_generate.
        """
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_fx_revaluation_run WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        self.invalidate_recordset()

    def _eligible_accounts(self):
        self.ensure_one()
        Account = self.env['account.account']
        # account.account is multi-company (company_ids) from Odoo 18; it
        # carries a single company_id before that.
        company_field = ('company_ids' if 'company_ids' in Account._fields
                         else 'company_id')
        return Account.search([
            ('eh_fx_revalue', '=', True),
            (company_field, 'in', self.company_id.ids),
        ])

    def _open_lines_query(self, accounts):
        """Fetch open foreign currency journal items as raw dicts.

        Selects posted account.move.line records on the given accounts
        whose currency_id != company.currency_id and that have a non
        zero residual in foreign currency.
        """
        self.ensure_one()
        if not accounts:
            return []
        AccountMoveLine = self.env['account.move.line']
        domain = [
            ('account_id', 'in', accounts.ids),
            ('parent_state', '=', 'posted'),
            ('date', '<=', self.revaluation_date),
            ('currency_id', '!=', self.company_id.currency_id.id),
            ('currency_id', '!=', False),
            ('company_id', '=', self.company_id.id),
        ]
        # We want OPEN balances, so:
        # - reconciled lines: skip
        # - unreconciled: include even if reconciled with later date
        #   (residual will be non zero)
        lines = AccountMoveLine.search(domain)
        out = []
        for line in lines:
            # Point-in-time residual: reconstruct the balance that was
            # still open AS OF the revaluation date, ignoring any partial
            # reconciliation dated after it. Reading the live
            # amount_residual would understate exposure for a line that
            # was open on the revaluation date but settled afterwards.
            balance_company, balance_foreign = self._residual_at_date(
                line, self.revaluation_date,
            )
            if line.currency_id.is_zero(balance_foreign):
                continue
            out.append({
                'line_id': line.id,
                'account_id': line.account_id.id,
                'partner_id': line.partner_id.id,
                'currency_id': line.currency_id.id,
                'balance_company': balance_company,
                'balance_foreign': balance_foreign,
            })
        return out

    def _residual_at_date(self, line, date):
        """Residual of `line` (company currency, foreign currency) as it
        stood on `date`, reversing only partials reconciled on or before
        that date.

        Mirrors Odoo's residual computation but filters partials by
        max_date so a settlement dated after the revaluation date does
        not retroactively close a line that was open at period end.
        """
        residual = line.balance
        residual_currency = line.amount_currency
        # line is the debit move of these partials.
        for partial in line.matched_credit_ids:
            if partial.max_date and partial.max_date <= date:
                residual -= partial.amount
                residual_currency -= partial.debit_amount_currency
        # line is the credit move of these partials.
        for partial in line.matched_debit_ids:
            if partial.max_date and partial.max_date <= date:
                residual += partial.amount
                residual_currency += partial.credit_amount_currency
        return residual, residual_currency

    def _closing_rate(self, currency):
        """Return units of company currency per 1 unit of foreign currency
        at the revaluation date. Uses res.currency rate of the foreign
        currency at the revaluation date relative to company currency.
        """
        self.ensure_one()
        company_ccy = self.company_id.currency_id
        if currency == company_ccy:
            return 1.0
        # rate is units of currency per 1 unit of company_currency by
        # Odoo convention; invert.
        rate = currency._get_conversion_rate(
            currency, company_ccy, self.company_id, self.revaluation_date,
        )
        if not rate:
            raise UserError(_(
                "No closing rate available for currency %(name)s on "
                "%(date)s.",
                name=currency.name,
                date=self.revaluation_date,
            ))
        return rate

    def _build_lines(self):
        self.ensure_one()
        accounts = self._eligible_accounts()
        raw = self._open_lines_query(accounts)
        groups = defaultdict(lambda: {
            'balance_company': 0.0,
            'balance_foreign': 0.0,
            'line_ids': [],
        })
        for row in raw:
            partner_key = row['partner_id'] if self.aggregate_by_partner else False
            key = (row['account_id'], partner_key, row['currency_id'])
            g = groups[key]
            g['balance_company'] += row['balance_company']
            g['balance_foreign'] += row['balance_foreign']
            g['line_ids'].append(row['line_id'])

        Line = self.env['eh.fx.revaluation.line']
        Currency = self.env['res.currency']
        Account = self.env['account.account']
        company_ccy = self.company_id.currency_id

        rate_cache = {}

        for (account_id, partner_id, currency_id), data in groups.items():
            if currency_id not in rate_cache:
                rate_cache[currency_id] = self._closing_rate(
                    Currency.browse(currency_id),
                )
            closing_rate = rate_cache[currency_id]
            new_balance_company = company_ccy.round(
                data['balance_foreign'] * closing_rate,
            )
            adjustment = company_ccy.round(
                new_balance_company - data['balance_company'],
            )
            account = Account.browse(account_id)
            nature = self._classify_nature(account, adjustment)
            Line.create({
                'run_id': self.id,
                'account_id': account_id,
                'partner_id': partner_id or False,
                'foreign_currency_id': currency_id,
                'balance_company': company_ccy.round(data['balance_company']),
                'balance_foreign': data['balance_foreign'],
                'closing_rate': closing_rate,
                'new_balance_company': new_balance_company,
                'adjustment': adjustment,
                'nature': nature,
                'source_line_count': len(data['line_ids']),
                'source_move_line_ids': [(6, 0, data['line_ids'])],
            })

    @staticmethod
    def _classify_nature(account, adjustment):
        """Classify the adjustment as gain or loss using the SIGNED
        balance convention.

        balance_company is Odoo's signed balance: positive for asset
        / debit-side, negative for liability / credit-side. Adjustment
        is (new - old) of that signed balance.

        Asset (positive base balance): positive adjustment grows the
        asset = gain, negative shrinks it = loss.
        Liability (negative base balance): negative adjustment makes
        the balance more negative (debt grows) = loss; positive
        adjustment moves toward zero (debt shrinks) = gain.
        """
        if adjustment == 0:
            return 'flat'
        is_asset = account.account_type in (
            'asset_cash', 'asset_receivable', 'asset_current',
            'asset_non_current', 'asset_fixed', 'asset_prepayments',
        )
        if is_asset:
            return 'gain' if adjustment > 0 else 'loss'
        # Liability: more negative (adjustment < 0) = liability grows = loss.
        return 'loss' if adjustment < 0 else 'gain'

    def _build_move(self):
        self.ensure_one()
        company_ccy = self.company_id.currency_id
        move_lines = []
        gain_total = 0.0
        loss_total = 0.0
        for line in self.line_ids:
            if line.adjustment == 0 or line.nature == 'flat':
                continue
            # The leg sign follows the economic nature of the adjustment,
            # not the raw sign of (new minus old). _classify_nature has
            # already encoded the asset / liability inversion:
            #   gain on asset (asset up):        DR account, CR FX gain
            #   loss on asset (asset down):      CR account, DR FX loss
            #   gain on liability (liab down):   DR account, CR FX gain
            #   loss on liability (liab up):     CR account, DR FX loss
            # In every case: gain implies debit on the account leg, loss
            # implies credit. The foreign currency residual is kept on the
            # original journal items; this leg is a pure functional
            # currency translation adjustment, so currency_id is omitted
            # and amount_currency stays at 0.
            amount = company_ccy.round(abs(line.adjustment))
            if line.nature == 'gain':
                debit, credit = amount, 0.0
                gain_total += amount
            else:  # 'loss'
                debit, credit = 0.0, amount
                loss_total += amount
            move_lines.append((0, 0, {
                'name': _("FX revaluation %s", line.account_id.code or ''),
                'account_id': line.account_id.id,
                'partner_id': line.partner_id.id if line.partner_id else False,
                'debit': debit,
                'credit': credit,
            }))

        if gain_total:
            move_lines.append((0, 0, {
                'name': _("Unrealised FX gain"),
                'account_id': self.gain_account_id.id,
                'debit': 0.0,
                'credit': company_ccy.round(gain_total),
            }))
        if loss_total:
            move_lines.append((0, 0, {
                'name': _("Unrealised FX loss"),
                'account_id': self.loss_account_id.id,
                'debit': company_ccy.round(loss_total),
                'credit': 0.0,
            }))

        if not move_lines:
            raise UserError(_(
                "Revaluation produced no adjustments to post.",
            ))

        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': self.revaluation_date,
            'journal_id': self.journal_id.id,
            'ref': _("FX Revaluation %s", self.name),
            'line_ids': move_lines,
            'eh_sealed': True,
        })
        move.action_post()
        return move

    def _build_reversal_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_(
                "Cannot reverse: original revaluation move is missing.",
            ))
        reversal_date = self.revaluation_date + timedelta(days=1)
        defaults = {
            'date': reversal_date,
            'journal_id': self.journal_id.id,
            'ref': _("FX Revaluation Reversal %s", self.name),
        }
        rev_move = self.move_id._reverse_moves(
            [defaults], cancel=False,
        )
        rev_move.action_post()
        self._eh_seal_reversal(rev_move)
        return rev_move
