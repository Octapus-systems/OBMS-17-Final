# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.year.end.run: per-(company, fiscal year) closing run.

Compute step:
* Aggregates posted account.move.line records in the fiscal year by
  account, restricted to income and expense account types.
* Produces one breakdown row per contributing account with its closing
  balance. Income balances are credit-side (negative); expense are
  debit-side (positive).

AOCI sub-reserve step (IAS 1.106), inside compute:
* When the company carries AOCI sub-reserve mapping rows
  (eh.aoci.reserve.map), each mapped OCI flow account's NET posted
  movement over the fiscal year becomes an OCI reclassification row.
  Net movement only: an amount recycled to P&L during the year (e.g. a
  CTA disposal reclassification, IAS 21.48) already left the flow
  account, so the close never double-moves it.
* Known OCI flow accounts (discovered from installed suite modules, or
  listed on an incomplete mapping row) that moved in the period but
  have no complete mapping are collected on a warning list; posting is
  blocked unless a manager overrides with a documented reason.
* With no mapping rows configured the behaviour is exactly the
  original pure P&L close.

Post step:
* Builds an account.move with one line per breakdown row plus one or
  two lines for retained earnings, plus one debit/credit pair per OCI
  reclassification row (flow account -> AOCI sub-reserve), ensuring
  the entry balances by construction. Retained earnings receives ONLY
  the P&L result; OCI components go to their sub-reserves, never to
  retained earnings. Posts the move on the fiscal year end date so
  the closing belongs to the fiscal year.
* Chronology guard: posting is refused while a later fiscal year's
  close already stands posted for the company; closes must post
  oldest-first so each year's equity roll builds on the prior one.

Reverse step:
* Generates the inverse entry dated one day after the fiscal year end
  so the income and expense accounts re-open for re-classification.
  The user can then recompute and re-post the corrected close.

Lock step:
* lock_after_post defaults to True and bumps
  res.company.fiscalyear_lock_date to the fiscal year end so
  prior-period entries cannot be edited. Idempotent; re-running does
  nothing if the lock is already past the date. Disabling the lock
  requires a documented reason, logged in the chatter when the close
  posts without advancing the lock date.
"""

from datetime import timedelta
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import SQL

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery

from .aoci_reserve_map import AOCI_KINDS

_logger = logging.getLogger(__name__)


_INCOME_TYPES = ('income', 'income_other')
_EXPENSE_TYPES = (
    'expense', 'expense_depreciation', 'expense_direct_cost', 'expense_other',
)

# Input / measurement fields on the run that become the basis of a posted
# closing entry. Once the run is posted or reversed they are frozen at the ORM
# write layer. 'state' is deliberately NOT listed: the action methods write
# only state + the audit stamps (posted_at/posted_by_id, ...) + the move
# links, so a pure state-transition write carries no frozen field and always
# passes. The breakdown lines are guarded on their own model; line_ids appears
# here so a raw reassignment of the whole set on a posted run is also refused.
_FROZEN_AFTER_POST = (
    'line_ids', 'fiscal_year_start', 'fiscal_year_end', 'company_id',
    'journal_id', 'retained_earnings_account_id', 'lock_after_post',
    'no_lock_reason', 'override_unmapped_oci', 'override_unmapped_reason',
)


class EhYearEndRun(models.Model):
    _name = 'eh.year.end.run'
    _description = "Year-end closing run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard', 'eh.gl.reversal']
    _order = 'fiscal_year_end desc, id desc'
    _rec_name = 'name'

    # The state machine is enforced in the UI (readonly statusbar + header
    # buttons) and by the write() freeze below, but a draft run's state is not
    # frozen: without this guard a plain user could RPC write({'state':
    # 'posted'}) straight past action_post and its journal entry. eh.workflow.
    # guard blocks any direct (non-action, non-superuser) write to 'state'; the
    # action_* methods flag their own state writes via self._eh_workflow_action.
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('computed', "Computed"),
            ('posted', "Posted"),
            ('reversed', "Reversed"),
            ('cancelled', "Cancelled"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', readonly=True, store=True,
    )

    fiscal_year_start = fields.Date(
        required=True, tracking=True,
        help=(
            "Start of the fiscal year being closed. The closing entry "
            "aggregates posted moves whose date falls in [start, end]."
        ),
    )
    fiscal_year_end = fields.Date(
        required=True, tracking=True,
        help="End of the fiscal year. Posting date for the closing entry.",
    )

    journal_id = fields.Many2one(
        'account.journal', required=True, tracking=True,
        domain="[('type', '=', 'general')]",
    )
    retained_earnings_account_id = fields.Many2one(
        'account.account', required=True, tracking=True,
        domain=(
            "[('account_type', '=', 'equity')]"
        ),
        help=(
            "Equity account that absorbs the net profit / loss for the "
            "fiscal year. Most charts call this Retained Earnings or "
            "Accumulated Profit and Loss."
        ),
    )
    lock_after_post = fields.Boolean(
        default=True, tracking=True,
        help=(
            "When set, posting bumps res.company.fiscalyear_lock_date "
            "to the fiscal year end so prior period entries cannot be "
            "edited. Disabling this control requires a documented "
            "reason; the reason is logged in the chatter when the close "
            "posts without advancing the lock date."
        ),
    )
    no_lock_reason = fields.Text(
        string="Reason For Not Locking",
        help=(
            "Documented reason for posting the year-end close without "
            "advancing the fiscal-year lock date. Required when 'Lock "
            "After Post' is disabled; logged in the chatter on post."
        ),
    )

    line_ids = fields.One2many(
        'eh.year.end.line', 'run_id', copy=False,
    )
    line_count = fields.Integer(compute='_compute_totals', store=True)

    total_income = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='currency_id',
    )
    total_expense = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='currency_id',
    )
    net_profit = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='currency_id',
        help="total_income minus total_expense; positive = profit. "
             "P&L only: OCI reclassification rows never contribute, so "
             "retained earnings receives exactly this amount.",
    )
    total_oci_reclass = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='currency_id',
        help="Net OCI movement of the fiscal year swept into AOCI "
             "sub-reserves by the closing entry, credit-positive "
             "(positive = net OCI gain for the year). Zero when no AOCI "
             "mapping rows are configured.",
    )

    # ---- AOCI governance (IAS 1.106) ----
    has_unmapped_oci = fields.Boolean(
        readonly=True, copy=False,
        help="True when known OCI flow accounts moved during the fiscal "
             "year but carry no complete AOCI sub-reserve mapping. "
             "Posting is blocked unless a manager overrides with a "
             "documented reason.",
    )
    unmapped_oci_note = fields.Text(
        string="Unmapped OCI Accounts", readonly=True, copy=False,
        help="Known OCI flow accounts (discovered from installed suite "
             "modules or listed on incomplete mapping rows) whose period "
             "movement would stay commingled in equity because no AOCI "
             "sub-reserve account is mapped.",
    )
    override_unmapped_oci = fields.Boolean(
        string="Override Unmapped OCI", default=False, copy=False,
        tracking=True,
        help="Post the close even though OCI flow accounts lack an AOCI "
             "sub-reserve mapping. Requires a documented reason, logged "
             "in the chatter on post.",
    )
    override_unmapped_reason = fields.Text(
        string="Override Reason",
        help="Documented basis for closing with unmapped OCI accounts. "
             "Required when the override is ticked; logged in the "
             "chatter on post.",
    )

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
    )
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
    )

    # ---- audit ----
    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True, tracking=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True, tracking=True)
    reversed_at = fields.Datetime(readonly=True, tracking=True)
    reversed_by_id = fields.Many2one('res.users', readonly=True, tracking=True)

    notes = fields.Text()

    _sql_constraints = [
        ('unique_company_year', 'unique(company_id, fiscal_year_end)', 'Only one year-end run per company per fiscal year end date.'),
        ('check_year_dates', 'CHECK (fiscal_year_start <= fiscal_year_end)', 'fiscal_year_start must be on or before fiscal_year_end.'),
    ]

    @api.depends(
        'line_ids', 'line_ids.income_balance', 'line_ids.expense_balance',
        'line_ids.line_kind', 'line_ids.oci_balance',
    )
    def _compute_totals(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)
            # OCI rows carry zero income/expense balances by construction,
            # so the P&L totals (and with them net_profit and the retained
            # earnings amount) are structurally untouched by AOCI rows.
            rec.total_income = sum(rec.line_ids.mapped('income_balance'))
            rec.total_expense = sum(rec.line_ids.mapped('expense_balance'))
            rec.net_profit = rec.total_income - rec.total_expense
            # oci_balance is ledger-signed (debit positive); an OCI gain
            # accumulates as a credit, so negate to report gain-positive.
            oci_lines = rec.line_ids.filtered(
                lambda l: l.line_kind == 'oci')
            rec.total_oci_reclass = -sum(oci_lines.mapped('oci_balance'))

    # ---- onchange (live form feedback) ----

    @api.onchange('fiscal_year_end')
    def _onchange_fiscal_year_end_derive_start(self):
        """When the user picks a fiscal year-end, prefill the matching
        start as one year prior plus a day, unless the user already
        set a different start. Catches the common case of a calendar
        fiscal year (1 Jan to 31 Dec) without the operator typing twice.
        """
        from datetime import timedelta
        for rec in self:
            if not rec.fiscal_year_end:
                continue
            # Only auto-fill when start is empty or clearly a stale
            # placeholder (start > end means user just bumped end);
            # never overwrite a deliberate start the user typed.
            if rec.fiscal_year_start and rec.fiscal_year_start <= rec.fiscal_year_end:
                continue
            try:
                inferred = rec.fiscal_year_end.replace(
                    year=rec.fiscal_year_end.year - 1,
                ) + timedelta(days=1)
            except ValueError:
                # Leap-day end: replace year drops to Feb 28; close enough.
                inferred = rec.fiscal_year_end.replace(
                    year=rec.fiscal_year_end.year - 1,
                    day=rec.fiscal_year_end.day - 1,
                ) + timedelta(days=1)
            rec.fiscal_year_start = inferred

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code(
                    'eh.year.end.run',
                ) or '/'
                vals['name'] = seq
        return super().create(vals_list)

    # ---- integrity: freeze input once the close is posted ----

    def write(self, vals):
        """Freeze the measurement / input fields once the run is posted or
        reversed; they are the basis of a posted closing entry.

        A pure state-transition write (the action methods write only
        {'state': ...} plus the audit stamps and move links) carries no frozen
        field and passes. A write touching a frozen figure while any record is
        posted or reversed is always blocked. 'state' is never frozen, so
        recompute in draft / computed and the legitimate transitions keep
        working.
        """
        frozen = [f for f in _FROZEN_AFTER_POST if f in vals]
        confirmed = self.filtered(lambda r: r.state in ('posted', 'reversed'))
        if frozen and confirmed:
            raise UserError(_(
                "Figures on a posted year-end run are frozen (%(fields)s). "
                "Reverse it first (EH Accounting Manager only) to change it.",
                fields=', '.join(frozen)))
        # The state of a posted / reversed run is itself a control point:
        # resetting it to draft would silently lift the figure freeze above.
        # A raw ORM state write on such a run without the sanctioned-transition
        # context flag must be manager-gated, so a plain user cannot un-freeze
        # a GL-backed run. action_reverse sets the flag after its own manager
        # check and move handling.
        if 'state' in vals \
                and not self.env.context.get('eh_year_end_state_change'):
            crossing = confirmed.filtered(lambda r: r.state != vals['state'])
            if crossing:
                crossing._check_manager()
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager may change the state of a "
                "posted or reversed year-end run."))

    def unlink(self):
        posted = self.filtered(lambda r: r.state in ('posted', 'reversed'))
        if posted:
            raise UserError(_(
                "A posted or reversed year-end run cannot be deleted; it "
                "carries a posted GL closing entry. Reverse it first."))
        return super().unlink()

    @api.constrains('retained_earnings_account_id', 'company_id')
    def _check_retained_earnings_company(self):
        for rec in self:
            if not rec.retained_earnings_account_id:
                continue
            account = rec.retained_earnings_account_id
            # account.account is multi-company (company_ids) from Odoo 18;
            # single company_id before that.
            acc_companies = (account.company_ids
                             if 'company_ids' in account._fields
                             else account.company_id)
            if rec.company_id not in acc_companies:
                raise ValidationError(_(
                    "Retained earnings account must belong to the "
                    "company being closed.",
                ))

    # ---- transitions ----

    def action_compute(self):
        self = self._eh_workflow_action()
        for run in self:
            if run.state not in ('draft', 'computed'):
                raise UserError(_(
                    "Compute is only available in draft or computed state.",
                ))
            run.line_ids.unlink()
            run._build_lines()
            run._build_oci_lines()
            run._refresh_unmapped_oci()
            run.write({
                'state': 'computed',
                'computed_at': fields.Datetime.now(),
                'computed_by_id': self.env.user.id,
            })

    def action_post(self):
        self = self._eh_workflow_action()
        for run in self:
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager',
            ):
                raise UserError(_(
                    "Only an EH Accounting Manager can post a year-end "
                    "closing run.",
                ))
            if run.state != 'computed':
                raise UserError(_(
                    "Run must be computed before posting.",
                ))
            if not run.line_ids:
                raise UserError(_(
                    "No income or expense balances to close. "
                    "Recompute or cancel the run.",
                ))
            run._check_post_lock()
            run._check_oci_governance()
            move = run._build_closing_move()
            run.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'move_id': move.id,
            })
            if run.has_unmapped_oci and run.override_unmapped_oci:
                run.message_post(body=_(
                    "Year-end close posted with unmapped OCI accounts "
                    "(manager override). Reason: %(reason)s\n%(detail)s",
                    reason=run.override_unmapped_reason,
                    detail=run.unmapped_oci_note or '',
                ))
            if run.lock_after_post:
                run._maybe_advance_lock_date()
            else:
                run.message_post(body=_(
                    "Fiscal-year lock date NOT advanced on post "
                    "(lock disabled on this run). Reason: %s",
                    run.no_lock_reason,
                ))

    def action_reverse(self):
        self = self._eh_workflow_action()
        for run in self:
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager',
            ):
                raise UserError(_(
                    "Only an EH Accounting Manager can reverse a "
                    "year-end closing run.",
                ))
            if run.state != 'posted':
                raise UserError(_(
                    "Only posted runs can be reversed.",
                ))
            if not run.move_id:
                raise UserError(_(
                    "Run has no posted move to reverse.",
                ))
            reversal = run._build_reversal_move()
            run.with_context(eh_year_end_state_change=True).write({
                'state': 'reversed',
                'reversed_at': fields.Datetime.now(),
                'reversed_by_id': self.env.user.id,
                'reversal_move_id': reversal.id,
            })

    def action_cancel(self):
        self = self._eh_workflow_action()
        for run in self:
            if run.state in ('posted', 'reversed'):
                raise UserError(_(
                    "Cannot cancel a posted or reversed run.",
                ))
            run.state = 'cancelled'

    def action_set_to_draft(self):
        self = self._eh_workflow_action()
        for run in self:
            if run.state != 'cancelled':
                raise UserError(_(
                    "Only cancelled runs can return to draft.",
                ))
            run.line_ids.unlink()
            # The unmapped-OCI snapshot belongs to the discarded compute.
            run.write({
                'state': 'draft',
                'has_unmapped_oci': False,
                'unmapped_oci_note': False,
            })

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No closing entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    def action_seed_aoci_map(self):
        """Discover known OCI flow accounts from the installed suite modules
        and seed the company's AOCI sub-reserve mapping rows, then open the
        mapping so the manager can assign the sub-reserve accounts."""
        self.ensure_one()
        self.env['eh.aoci.reserve.map'].action_seed_from_modules(
            self.company_id)
        return {
            'type': 'ir.actions.act_window',
            'name': _("AOCI Sub-Reserve Mapping"),
            'res_model': 'eh.aoci.reserve.map',
            'view_mode': 'list,form',
            'domain': [('company_id', '=', self.company_id.id)],
            'context': {'default_company_id': self.company_id.id},
        }

    # ---- helpers ----

    def _build_lines(self):
        """Aggregate posted journal lines per account in [start, end].

        Income and expense accounts both contribute: income lines
        produce credit-side (negative) balances and expense lines
        produce debit-side (positive) balances. The breakdown row
        stores both fields so the totals view can sum them
        independently.
        """
        self.ensure_one()
        Line = self.env['eh.year.end.line']
        rows = self._fetch_account_balances()
        if not rows:
            return
        # Read every contributing account's type in one prefetch rather
        # than browsing one account per row, then create all breakdown
        # lines in a single batched insert. A full chart can carry
        # hundreds of income and expense accounts; the per-row browse
        # plus per-row create was two round-trips per account.
        accounts = self.env['account.account'].browse(
            [row['account_id'] for row in rows]
        )
        account_type_by_id = {acc.id: acc.account_type for acc in accounts}
        line_vals = []
        for row in rows:
            account_id = row['account_id']
            account_type = account_type_by_id.get(account_id)
            if account_type in _INCOME_TYPES:
                # Income balance is naturally credit-side; flip sign.
                line_vals.append({
                    'run_id': self.id,
                    'account_id': account_id,
                    'income_balance': -float(row['balance']),
                    'expense_balance': 0.0,
                })
            elif account_type in _EXPENSE_TYPES:
                line_vals.append({
                    'run_id': self.id,
                    'account_id': account_id,
                    'income_balance': 0.0,
                    'expense_balance': float(row['balance']),
                })
        if line_vals:
            Line.create(line_vals)

    def _fetch_account_balances(self):
        """Return [{'account_id': X, 'balance': Y}] per income/expense
        account with non-zero balance in the fiscal year.
        """
        self.ensure_one()
        query = MoveLineQuery(
            self.env, company_ids=[self.company_id.id],
        )
        query.where_account_types(_INCOME_TYPES + _EXPENSE_TYPES)
        query.where_date_range(
            date_from=self.fiscal_year_start,
            date_to=self.fiscal_year_end,
        )
        query.where_posted_only()
        query.select_field('account_id')
        query.select(SQL("SUM(aml.balance)"), 'balance')
        query.group_by(SQL("aml.account_id"))
        return [
            row for row in query.execute()
            if abs(float(row.get('balance') or 0.0)) > 0.005
        ]

    # ---- AOCI sub-reserve reclassification (IAS 1.106) ----

    def _fetch_period_balances(self, accounts):
        """Net posted movement per account over [start, end], ledger-signed
        (debit positive), as {account_id: balance}.

        NET movement is the whole point: when a recyclable component
        recycles during the year (e.g. a CTA disposal reclassification
        posted by the FX module under IAS 21.48, debiting the flow account
        against P&L), the recycled amount has already left the flow
        account, so the net figure excludes it and the close never moves
        it twice.
        """
        self.ensure_one()
        if not accounts:
            return {}
        lines = self.env['account.move.line'].search([
            ('company_id', '=', self.company_id.id),
            ('account_id', 'in', accounts.ids),
            ('parent_state', '=', 'posted'),
            ('date', '>=', self.fiscal_year_start),
            ('date', '<=', self.fiscal_year_end),
        ])
        balances = {}
        for line in lines:
            balances[line.account_id.id] = (
                balances.get(line.account_id.id, 0.0) + line.balance)
        return balances

    def _build_oci_lines(self):
        """One OCI reclassification row per mapped flow account with a
        non-zero net period movement.

        Only complete mapping rows (reserve account AND source accounts)
        produce reclassification rows; incomplete rows feed the
        unmapped-OCI warning list instead. With no mapping rows configured
        this is a no-op and the close stays a pure P&L close.
        """
        self.ensure_one()
        rows = self.env['eh.aoci.reserve.map'].search([
            ('company_id', '=', self.company_id.id),
        ]).filtered(lambda r: r.reserve_account_id and r.source_account_ids)
        if not rows:
            return
        line_vals = []
        for row in rows:
            balances = self._fetch_period_balances(row.source_account_ids)
            for account in row.source_account_ids:
                net = balances.get(account.id, 0.0)
                if abs(net) <= 0.005:
                    continue
                line_vals.append({
                    'run_id': self.id,
                    'account_id': account.id,
                    'line_kind': 'oci',
                    'oci_kind': row.kind,
                    'oci_balance': net,
                    'reserve_account_id': row.reserve_account_id.id,
                    'income_balance': 0.0,
                    'expense_balance': 0.0,
                })
        if line_vals:
            self.env['eh.year.end.line'].create(line_vals)

    def _refresh_unmapped_oci(self):
        """Rebuild the unmapped-OCI warning list.

        Candidates are the union of (a) known OCI flow accounts discovered
        from the installed suite modules and (b) source accounts listed on
        incomplete mapping rows (no reserve account yet), minus accounts on
        complete rows. A candidate only warns when it actually moved in
        the period; a silent account cannot commingle anything.
        """
        self.ensure_one()
        MapModel = self.env['eh.aoci.reserve.map']
        rows = MapModel.search([('company_id', '=', self.company_id.id)])
        complete = rows.filtered(
            lambda r: r.reserve_account_id and r.source_account_ids)
        candidates = (rows - complete).mapped('source_account_ids')
        for accounts in MapModel._discover_oci_sources(
                self.company_id).values():
            candidates |= accounts
        candidates -= complete.mapped('source_account_ids')
        # Retained earnings is the P&L destination, never an OCI flow.
        candidates -= self.retained_earnings_account_id
        balances = self._fetch_period_balances(candidates)
        problems = []
        for account in candidates.sorted(lambda a: a.code or a.name):
            net = balances.get(account.id, 0.0)
            if abs(net) <= 0.005:
                continue
            problems.append(_(
                "%(code)s %(name)s: period movement %(amount).2f has no "
                "AOCI sub-reserve mapping",
                code=account.code or '', name=account.name,
                # Report gain-positive (credit-positive), the reading a
                # preparer expects for an OCI reserve.
                amount=-net))
        self.write({
            'has_unmapped_oci': bool(problems),
            'unmapped_oci_note': '\n'.join(problems) if problems else False,
        })

    def _check_oci_governance(self):
        """Post-time gates for the AOCI architecture and lock governance.

        * Unmapped OCI accounts block posting unless the manager override
          is ticked with a documented reason (suite-standard
          blocking-with-override).
        * Disabling lock_after_post requires a documented reason; the
          reason is logged in the chatter after the post succeeds.
        """
        self.ensure_one()
        if self.has_unmapped_oci:
            if not self.override_unmapped_oci:
                raise UserError(_(
                    "OCI accounts moved this fiscal year without a "
                    "complete AOCI sub-reserve mapping; closing now would "
                    "leave accumulated OCI commingled in equity "
                    "(IAS 1.106):\n%(detail)s\n\nMap them via 'Discover "
                    "OCI Reserves' / the AOCI Sub-Reserve Mapping, or "
                    "tick the override with a documented reason.",
                    detail=self.unmapped_oci_note or ''))
            if not (self.override_unmapped_reason or '').strip():
                raise UserError(_(
                    "Posting with unmapped OCI accounts requires a "
                    "documented override reason; it is logged in the "
                    "chatter."))
        if not self.lock_after_post \
                and not (self.no_lock_reason or '').strip():
            raise UserError(_(
                "Posting without advancing the fiscal-year lock date "
                "requires a documented reason ('Reason For Not Locking'); "
                "it is logged in the chatter."))

    def _build_closing_move(self):
        """Build the closing journal entry.

        Income accounts: balance is credit-side; debit each by
        income_balance to bring them to zero. Expense accounts:
        balance is debit-side; credit each by expense_balance to
        bring them to zero. The retained earnings account absorbs
        the net: credit when net_profit > 0, debit when < 0.

        OCI reclassification rows add one debit/credit pair each,
        moving the flow account's net period movement into its AOCI
        sub-reserve. Retained earnings receives ONLY the P&L result:
        no OCI amount ever posts against it (IAS 1.106), which the
        guard below enforces structurally.
        """
        self.ensure_one()
        line_vals = []
        pl_lines = self.line_ids.filtered(lambda l: l.line_kind != 'oci')
        oci_lines = self.line_ids.filtered(lambda l: l.line_kind == 'oci')
        for line in pl_lines:
            account = line.account_id
            if line.income_balance:
                # Income closes with a debit.
                line_vals.append((0, 0, {
                    'name': _("Year-end close: %s", account.code or account.name),
                    'account_id': account.id,
                    'debit': line.income_balance,
                    'credit': 0.0,
                }))
            if line.expense_balance:
                # Expense closes with a credit.
                line_vals.append((0, 0, {
                    'name': _("Year-end close: %s", account.code or account.name),
                    'account_id': account.id,
                    'debit': 0.0,
                    'credit': line.expense_balance,
                }))
        # Retained earnings line.
        net = self.net_profit
        if net > 0:
            line_vals.append((0, 0, {
                'name': _("Net profit transferred to retained earnings"),
                'account_id': self.retained_earnings_account_id.id,
                'debit': 0.0,
                'credit': net,
            }))
        elif net < 0:
            line_vals.append((0, 0, {
                'name': _("Net loss transferred from retained earnings"),
                'account_id': self.retained_earnings_account_id.id,
                'debit': -net,
                'credit': 0.0,
            }))
        # net == 0 produces no retained earnings line; the close still
        # balances because income == expense exactly.
        # ---- AOCI sub-reserve reclassifications (IAS 1.106) ----
        kind_labels = dict(AOCI_KINDS)
        for line in oci_lines:
            source = line.account_id
            reserve = line.reserve_account_id
            if not reserve:
                raise UserError(_(
                    "OCI reclassification row for %(code)s carries no AOCI "
                    "sub-reserve account. Recompute the run after "
                    "completing the AOCI mapping.",
                    code=source.code or source.name))
            # Retained-earnings purity: RE receives ONLY the P&L result;
            # an OCI component routed through RE would silently commingle
            # accumulated OCI with retained earnings (the audit finding
            # this architecture exists to close).
            if self.retained_earnings_account_id in (source, reserve):
                raise UserError(_(
                    "Retained earnings cannot take part in an OCI "
                    "reclassification (source or sub-reserve). OCI "
                    "components are carried in their own AOCI "
                    "sub-reserves, never in retained earnings "
                    "(IAS 1.106)."))
            amount = line.oci_balance
            label = kind_labels.get(line.oci_kind, line.oci_kind or '')
            if amount < 0.0:
                # Accumulated credit (net OCI gain): debit the flow
                # account to zero its period movement, credit the reserve.
                line_vals.append((0, 0, {
                    'name': _("AOCI reclass (%(kind)s): %(code)s",
                              kind=label, code=source.code or source.name),
                    'account_id': source.id,
                    'debit': -amount,
                    'credit': 0.0,
                }))
                line_vals.append((0, 0, {
                    'name': _("AOCI sub-reserve (%(kind)s)", kind=label),
                    'account_id': reserve.id,
                    'debit': 0.0,
                    'credit': -amount,
                }))
            elif amount > 0.0:
                # Accumulated debit (net OCI loss): credit the flow
                # account, debit the reserve.
                line_vals.append((0, 0, {
                    'name': _("AOCI reclass (%(kind)s): %(code)s",
                              kind=label, code=source.code or source.name),
                    'account_id': source.id,
                    'debit': 0.0,
                    'credit': amount,
                }))
                line_vals.append((0, 0, {
                    'name': _("AOCI sub-reserve (%(kind)s)", kind=label),
                    'account_id': reserve.id,
                    'debit': amount,
                    'credit': 0.0,
                }))
        if not line_vals:
            raise UserError(_(
                "The closing entry has no lines; nothing to post.",
            ))
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': self.fiscal_year_end,
            'journal_id': self.journal_id.id,
            'ref': _("Year-end close %s", self.name),
            'line_ids': line_vals,
            'eh_sealed': True,
        })
        move.action_post()
        return move

    def _build_reversal_move(self):
        self.ensure_one()
        if not self.move_id:
            return self.env['account.move']
        reversal_date = self.fiscal_year_end + timedelta(days=1)
        defaults = {
            'date': reversal_date,
            'journal_id': self.journal_id.id,
            'ref': _("Year-end close reversal %s", self.name),
        }
        rev = self.move_id._reverse_moves(
            [defaults], cancel=False,
        )
        rev.action_post()
        self._eh_seal_reversal(rev)
        return rev

    def _check_post_lock(self):
        """Pre-post overlap / lock guard.

        Reject posting a year-end close when governance would be
        breached:

        * Another already-posted (or reversed) run exists for the same
          company whose fiscal year overlaps this run's [start, end]
          window. Two closings that overlap the same period would
          double-close income and expense, so a second post is blocked
          while a sibling posted run stands.
        * The company's fiscal year lock date is already at or past this
          run's fiscal year end, i.e. the year is closed and frozen.
          Posting a fresh closing entry into a locked year would either
          be rejected by the ledger or silently mis-dated, so it is
          blocked here with a clear message.
        * Chronology: a LATER fiscal year's close already stands posted
          for the company. Closes must post oldest-first: the later
          year's retained earnings and AOCI roll were built on this
          year's then-unclosed balances, so posting an earlier close
          afterwards would silently restate the later year's opening
          equity. Reverse the later close first. A reversed later run
          no longer stands, so it does not block.

        Opt-in-safe: the sibling check only fires when a genuinely
        posted run overlaps, the chronology check only when a later
        posted run stands, and the lock check only fires when the lock
        date is actually set at or past the year end. A first, clean
        close is never affected.
        """
        self.ensure_one()
        # 1. Overlapping posted (or reversed) sibling run.
        sibling = self.search([
            ('id', '!=', self.id),
            ('company_id', '=', self.company_id.id),
            ('state', 'in', ('posted', 'reversed')),
            ('fiscal_year_start', '<=', self.fiscal_year_end),
            ('fiscal_year_end', '>=', self.fiscal_year_start),
        ], limit=1)
        if sibling:
            raise UserError(_(
                "A year-end close (%s) has already been posted for this "
                "company over an overlapping fiscal year. Reverse it "
                "before posting another close for the same period.",
                sibling.name,
            ))
        # 2. Chronology: a later fiscal year is already closed.
        later = self.search([
            ('id', '!=', self.id),
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'posted'),
            ('fiscal_year_start', '>', self.fiscal_year_end),
        ], limit=1, order='fiscal_year_start asc')
        if later:
            raise UserError(_(
                "A later fiscal year has already been closed for this "
                "company (%(name)s, year ending %(end)s). Year-end closes "
                "must post oldest-first so each year's equity roll builds "
                "on the prior close; reverse the later close before "
                "posting this one.",
                name=later.name, end=later.fiscal_year_end,
            ))
        # 3. Fiscal year already lock-dated (year closed and frozen).
        company = self.company_id
        lock_date = (company.fiscalyear_lock_date
                     if 'fiscalyear_lock_date' in company._fields
                     else False)
        if lock_date and lock_date >= self.fiscal_year_end:
            raise UserError(_(
                "The fiscal year ending %(end)s is already locked "
                "(lock date %(lock)s). Posting a year-end close into a "
                "locked year is not allowed.",
                end=self.fiscal_year_end,
                lock=lock_date,
            ))

    def _maybe_advance_lock_date(self):
        """Bump res.company.fiscalyear_lock_date to the fiscal year end.

        Idempotent: if the company's lock date is already at or past
        the fiscal year end, do nothing. Done via sudo() because the
        lock date update requires admin-level access; the post-time
        manager guard already gated the call.
        """
        self.ensure_one()
        company = self.company_id
        current = company.fiscalyear_lock_date
        if current and current >= self.fiscal_year_end:
            return
        company.sudo().fiscalyear_lock_date = self.fiscal_year_end
        self.message_post(body=_(
            "Fiscal year lock date advanced to %s.",
            self.fiscal_year_end,
        ))
