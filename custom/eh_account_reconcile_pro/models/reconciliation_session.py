# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.reconciliation.session: tracks an in progress reconciliation context
per user and journal.

A session is opened when a user enters the reconciliation workspace for
a journal. As they match, write off, or skip statement lines, decisions
flow through the session's apply_match and apply_write_off methods. Each
decision creates an eh.reconciliation.audit row and bumps a counter on
the session.

Sessions close manually (button) or implicitly when the user opens the
workspace for a different journal. Closed sessions persist as historical
work logs: how long it took to clear a journal's backlog, how many of
the matches came from the suggestion engine versus manual review.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL


_ALLOWED_COUNTERS = frozenset({
    'matches_made', 'matches_via_suggestion', 'matches_manual',
    'write_offs', 'skips',
})


class EhReconciliationSession(models.Model):
    _name = 'eh.reconciliation.session'
    _description = "Bank reconciliation session"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'opened_at desc'
    _rec_name = 'name'

    # The state machine may only advance through the session's own actions
    # (action_close), which run under sudo. A direct non-superuser
    # write({'state': 'closed'}) is refused so the close cannot be forged
    # past its closed_at stamp and lifecycle checks.
    _eh_guarded_fields = ('state',)

    name = fields.Char(compute='_compute_name', store=True)

    user_id = fields.Many2one(
        'res.users',
        required=True,
        default=lambda self: self.env.user,
        index=True,
    )
    journal_id = fields.Many2one(
        'account.journal',
        required=True,
        ondelete='cascade',
        index=True,
        domain="[('type', 'in', ('bank', 'cash'))]",
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )

    opened_at = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    closed_at = fields.Datetime()
    state = fields.Selection(
        [
            ('open', "Open"),
            ('closed', "Closed"),
        ],
        required=True,
        default='open',
        index=True,
    )

    statements_processed = fields.Integer(default=0)
    matches_made = fields.Integer(default=0)
    matches_via_suggestion = fields.Integer(default=0)
    matches_manual = fields.Integer(default=0)
    write_offs = fields.Integer(default=0)
    skips = fields.Integer(default=0)

    duration_seconds = fields.Integer(
        compute='_compute_duration', store=True,
    )

    audit_ids = fields.One2many(
        'eh.reconciliation.audit', 'session_id',
    )
    audit_count = fields.Integer(compute='_compute_audit_count')

    @api.depends('user_id', 'journal_id', 'opened_at')
    def _compute_name(self):
        for rec in self:
            if rec.opened_at and rec.journal_id:
                ts = fields.Datetime.to_string(rec.opened_at)
                rec.name = "%s %s" % (rec.journal_id.code or '', ts)
            else:
                rec.name = "New Session"

    @api.depends('opened_at', 'closed_at')
    def _compute_duration(self):
        for rec in self:
            if rec.closed_at and rec.opened_at:
                rec.duration_seconds = int(
                    (rec.closed_at - rec.opened_at).total_seconds()
                )
            else:
                rec.duration_seconds = 0

    @api.depends('audit_ids')
    def _compute_audit_count(self):
        for rec in self:
            rec.audit_count = len(rec.audit_ids)

    # ---- onchange (live form feedback) ----

    @api.onchange('journal_id')
    def _onchange_journal_id_company(self):
        """Pin company to the journal's company.

        A journal is single-company in Odoo; if the user picks a journal
        owned by a different company than the session header, the form
        should reconcile that immediately rather than wait for the
        post-save validation to bounce.
        """
        for rec in self:
            if rec.journal_id and rec.journal_id.company_id:
                rec.company_id = rec.journal_id.company_id

    # ---- lifecycle ----

    @api.model
    def open_or_create(self, journal_id):
        """Return the active open session for the current user and journal,
        or create one. Multiple users can have concurrent sessions on the
        same journal; the index is per user.
        """
        existing = self.search(
            [
                ('user_id', '=', self.env.user.id),
                ('journal_id', '=', journal_id),
                ('state', '=', 'open'),
            ],
            limit=1,
        )
        if existing:
            return existing
        return self.create({'journal_id': journal_id})

    def action_close(self):
        """Mark the session closed and stamp closed_at."""
        # State writes are guarded (eh.workflow.guard): run the transition
        # under sudo so the state/closed_at write passes, while the real
        # env.user is preserved for audit stamps.
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'closed':
                continue
            rec.write({
                'state': 'closed',
                'closed_at': fields.Datetime.now(),
            })
        return True

    def action_view_audits(self):
        """Open the audit list filtered to this session."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Reconciliation Decisions"),
            'res_model': 'eh.reconciliation.audit',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('session_id', '=', self.id)],
            'context': {'default_session_id': self.id},
        }

    # ---- decisions ----

    def apply_match(self, statement_line_id, aml_ids, source='manual'):
        """Reconcile a statement line with the given AML lines.

        :param statement_line_id: id of an account.bank.statement.line.
        :param aml_ids: list of account.move.line ids to reconcile against.
        :param source: 'manual' for an explicit single click,
            'drag_drop' for a drag-and-drop match (counts as manual),
            'suggestion' when the user accepted an engine recommendation,
            'rule' for an account.reconcile.model match,
            'bulk' for a batched apply of several AMLs in one call.
        :return: True on success.
        """
        self.ensure_one()
        if self.state != 'open':
            raise UserError(_("Cannot apply matches on a closed session."))
        if not aml_ids:
            raise UserError(_("apply_match requires at least one aml id."))

        statement_line = self.env['account.bank.statement.line'].browse(
            statement_line_id,
        )
        if not statement_line.exists():
            raise UserError(_("Unknown statement line: %s") % statement_line_id)

        # A statement line that is already fully reconciled must not be
        # re-matched. Without this guard a duplicate apply_match would
        # write an audit row, hit Odoo's reconciliation no-op path, and
        # leave the audit log claiming a match that did not happen on
        # this transaction. Refuse early so the caller surfaces the real
        # state to the user.
        if getattr(statement_line, 'is_reconciled', False):
            raise UserError(_(
                "Statement line %s is already reconciled. Unreconcile it "
                "from the bank statement before matching it again."
            ) % (statement_line.payment_ref or statement_line.id))

        aml_records = self.env['account.move.line'].browse(aml_ids).exists()
        if not aml_records:
            raise UserError(_("No valid AMLs in: %s") % aml_ids)

        # Each candidate AML must still be open (amount_residual non zero
        # and not flagged reconciled). A fully-reconciled AML cannot
        # absorb more reconciliation; Odoo's reconcile() would silently
        # skip it, but the audit row would still record a match. Reject
        # the call instead so the caller gets a clear error.
        already_reconciled = aml_records.filtered(lambda l: l.reconciled)
        if already_reconciled:
            raise UserError(_(
                "These journal items are already reconciled and cannot be "
                "matched again: %s"
            ) % ', '.join(
                a.move_id.name or str(a.id) for a in already_reconciled
            ))

        # Direction guard. A statement line may only clear a candidate SET
        # whose reclassified suspense can actually offset it. An amount
        # received (amount > 0) reclassifies to a debit-side leg, so it can
        # only net a debit residual (an open receivable, or a vendor refund
        # on payable); an amount paid (amount < 0) can only net a credit
        # residual (an open payable, or a customer refund on receivable).
        #
        # For a SINGLE candidate this is a per-line side check. For a
        # multi-line group the correct invariant is that the group's
        # AGGREGATE signed residual shares the statement amount's sign, not
        # that every individual member does: a legitimate contra set (an
        # invoice netted against a credit note the customer paid net)
        # carries an opposite-sign member the rest of the group more than
        # offsets, and the reclassified suspense clears against the group's
        # net, not each member individually. Rejecting per-member would
        # wrongly block that everyday net match.
        #
        # Matching a group that as a whole sits on the wrong side (e.g. a
        # customer deposit pointed at a vendor bill, both credits) still
        # makes core reconcile() a no-op: the suspense would clear against
        # the reclass counter-leg, silently marking the statement line
        # reconciled while the cash lands on the wrong-side account and the
        # candidates stay open. Refuse the wrong-side grouping before
        # anything posts.
        if not self._direction_compatible_group(statement_line, aml_records):
            raise UserError(_(
                "These journal items net to the wrong side of the ledger "
                "for a bank line that was %(direction)s and cannot be "
                "matched to it: %(items)s. An amount received must clear a "
                "net debit (open receivable or vendor refund); an amount "
                "paid must clear a net credit (open payable or customer "
                "refund).",
                direction=(
                    _("received") if statement_line.amount > 0
                    else _("paid")
                ),
                items=', '.join(
                    a.move_id.name or str(a.id) for a in aml_records
                ),
            ))

        engine = self.env['eh.reconciliation.suggestion.engine']
        primary = aml_records[0]
        score = engine.score_match(statement_line, primary)
        confidence = score['total']
        rules = ','.join(score['rules_fired'])

        self._perform_reconciliation(statement_line, aml_records)

        Audit = self.env['eh.reconciliation.audit']
        for aml in aml_records:
            Audit.create({
                'session_id': self.id,
                'statement_line_id': statement_line.id,
                'aml_id': aml.id,
                'user_id': self.env.user.id,
                'confidence': confidence,
                'rules_fired': rules,
                'decision': 'match',
                'source': source,
            })

        self._increment_counters('matches_made')
        if source == 'suggestion':
            self._increment_counters('matches_via_suggestion')
        elif source in ('manual', 'drag_drop'):
            # A drag-and-drop match is an explicit manual user action. It
            # carries its own audit source for analytics but still counts
            # as a manual match on the session summary counters.
            self._increment_counters('matches_manual')
        return True

    # Default gates for unattended auto-reconciliation.
    AUTO_THRESHOLD = 0.85
    AUTO_MARGIN = 0.10

    def auto_reconcile(self, threshold=None, margin=None, max_lines=None,
                       require_exact_amount=True):
        """Batch, deterministic auto-reconciliation of this session's
        journal.

        For each unreconciled statement line the suggestion engine ranks
        candidate journal items; a line is matched automatically only when
        it is safe to do so:

        * an `auto_confirm` match rule fired on the top candidate, or
        * the top candidate scores at or above `threshold` AND is
          unambiguous (its lead over the runner-up is at least `margin`)
          AND, when `require_exact_amount`, the open residual equals the
          statement amount to the currency rounding.

        Everything else is left for the operator. Nothing is guessed: an
        ambiguous or low-confidence line is reported, not matched. Returns
        a summary dict of counts and the matched line ids.
        """
        self.ensure_one()
        if self.state != 'open':
            raise UserError(_("Cannot auto-reconcile a closed session."))
        threshold = self.AUTO_THRESHOLD if threshold is None else threshold
        margin = self.AUTO_MARGIN if margin is None else margin

        engine = self.env['eh.reconciliation.suggestion.engine']
        lines = self.env['account.bank.statement.line'].search(
            [('journal_id', '=', self.journal_id.id),
             ('is_reconciled', '=', False)],
            order='date, id', limit=max_lines or None,
        )
        summary = {
            'considered': 0,
            'reconciled': 0,
            'by_rule': 0,
            'by_score': 0,
            'skipped_no_candidate': 0,
            'skipped_low_score': 0,
            'skipped_ambiguous': 0,
            'matched_line_ids': [],
        }
        for line in lines:
            if getattr(line, 'is_reconciled', False):
                continue
            summary['considered'] += 1
            suggestions = engine.find_suggestions(line, limit=2)
            if not suggestions:
                summary['skipped_no_candidate'] += 1
                continue
            top = suggestions[0]
            rules = self._matching_auto_confirm_rules(top)
            source = None
            if rules:
                source = 'rule'
            elif top['score'] >= threshold:
                runner_up = suggestions[1]['score'] if len(
                    suggestions) > 1 else 0.0
                if runner_up > 0.0 and (top['score'] - runner_up) < margin:
                    summary['skipped_ambiguous'] += 1
                    continue
                if require_exact_amount and not self._auto_amount_exact(
                        line, top['aml_id']):
                    summary['skipped_ambiguous'] += 1
                    continue
                source = 'suggestion'
            else:
                summary['skipped_low_score'] += 1
                continue
            try:
                self.apply_match(line.id, [top['aml_id']], source=source)
            except UserError:
                # A candidate that went stale between scoring and applying
                # (e.g. reconciled by a parallel session) is skipped, not
                # fatal to the batch.
                summary['skipped_ambiguous'] += 1
                continue
            if source == 'rule':
                rules.record_fire()
                summary['by_rule'] += 1
            else:
                summary['by_score'] += 1
            summary['reconciled'] += 1
            summary['matched_line_ids'].append(line.id)
        return summary

    def _matching_auto_confirm_rules(self, suggestion):
        """Return the auto_confirm match rules that fired on a suggestion."""
        codes = [c for c in (suggestion.get('rules_fired') or []) if c]
        if not codes:
            return self.env['eh.reconciliation.rule']
        return self.env['eh.reconciliation.rule'].search([
            ('code', 'in', codes),
            ('company_id', '=', self.company_id.id),
            ('rule_type', '=', 'match'),
            ('auto_confirm', '=', True),
        ])

    @staticmethod
    def _sign(value):
        """Return the sign (-1, 0, 1) of a numeric value."""
        if value > 0:
            return 1
        if value < 0:
            return -1
        return 0

    def _direction_compatible(self, statement_line, aml):
        """True when ``aml`` sits on a ledger side this statement line can
        actually clear.

        A bank statement line's open suspense residual is the opposite sign
        of its signed amount (an inbound amount > 0 posts a credit suspense;
        an outbound amount < 0 posts a debit suspense). Reclassifying that
        residual onto the candidate's account produces a target leg whose
        sign matches the statement amount, so the candidate's own open
        residual must share the statement amount's sign for core reconcile()
        to net the two to zero. Opposite signs make reconcile() a no-op.

        A zero sign on either side (a zero-amount line, or a candidate with
        no open residual) is left to the existing empty/already-reconciled
        guards rather than blocked here, so this method only rejects the
        definite wrong-side pairing.
        """
        amount_sign = self._sign(statement_line.amount)
        residual_sign = self._sign(aml.amount_residual)
        if amount_sign == 0 or residual_sign == 0:
            return True
        return amount_sign == residual_sign

    def _direction_compatible_group(self, statement_line, aml_records):
        """True when the candidate SET sits on a ledger side this statement
        line can clear.

        For a single candidate this delegates to :meth:`_direction_compatible`
        (the per-line side check). For a multi-line group the correct
        invariant is the group's AGGREGATE signed residual, not each member:
        a legitimate contra set (e.g. an open invoice netted against a credit
        note the customer paid net) carries an opposite-sign member the other
        members more than offset, and the reclassified suspense clears against
        the group's net residual, not each line individually. A per-member
        check would wrongly reject that everyday net match; the net check
        still refuses a group that as a whole sits on the wrong side, which
        core reconcile() would no-op on.

        A zero net (a fully self-cancelling group) or a zero-amount line is
        left to the empty/already-reconciled guards and the post-reconcile
        no-op backstop in _perform_reconciliation rather than blocked here, so
        this method only rejects a group whose net is definitely wrong-side.
        """
        if len(aml_records) == 1:
            return self._direction_compatible(statement_line, aml_records)
        amount_sign = self._sign(statement_line.amount)
        net_residual = sum(aml_records.mapped('amount_residual'))
        residual_sign = self._sign(net_residual)
        if amount_sign == 0 or residual_sign == 0:
            return True
        return amount_sign == residual_sign

    def _auto_amount_exact(self, statement_line, aml_id):
        """True when the open residual equals the statement amount to the
        currency rounding (sign-agnostic)."""
        aml = self.env['account.move.line'].browse(aml_id)
        if not aml.exists():
            return False
        currency = statement_line.currency_id or self.company_id.currency_id
        delta = abs(statement_line.amount) - abs(aml.amount_residual)
        if currency:
            return currency.is_zero(delta)
        return round(delta, 2) == 0.0

    def apply_write_off(self, statement_line_id, account_id, label=None):
        """Reconcile a statement line as a write off to the given account.

        Creates an audit row with decision=write_off and aml_id null.
        The actual journal entry is posted via the standard Odoo
        write off mechanism in _perform_write_off.
        """
        self.ensure_one()
        if self.state != 'open':
            raise UserError(_("Cannot write off on a closed session."))

        statement_line = self.env['account.bank.statement.line'].browse(
            statement_line_id,
        )
        account = self.env['account.account'].browse(account_id)
        if not statement_line.exists() or not account.exists():
            raise UserError(_("Unknown statement line or account."))

        # Already-reconciled lines cannot be written off again; the
        # write-off would silently no-op while the audit row claimed a
        # decision had been recorded.
        if getattr(statement_line, 'is_reconciled', False):
            raise UserError(_(
                "Statement line %s is already reconciled and cannot be "
                "written off again."
            ) % (statement_line.payment_ref or statement_line.id))

        self._perform_write_off(statement_line, account, label)

        self.env['eh.reconciliation.audit'].create({
            'session_id': self.id,
            'statement_line_id': statement_line.id,
            'aml_id': False,
            'user_id': self.env.user.id,
            'confidence': 0.0,
            'rules_fired': '',
            'decision': 'write_off',
            'source': 'manual',
        })
        self._increment_counters('write_offs')
        return True

    def apply_fx_writeoff(self, statement_line_id, label=None,
                          max_amount=None):
        """Write off the residual of a statement line to the company's
        configured currency-exchange gain or loss account.

        Picks the gain account when residual > 0 (more cash than book),
        the loss account when residual < 0. Refuses when no FX accounts
        are configured on the company so the bank charge does not get
        misrouted to a generic suspense account.

        max_amount: optional safety cap. When set, the absolute residual
        must not exceed this value or the call raises UserError. Lets
        site administrators expose the action behind a "small variance
        only" guardrail (typical: 5.00 of company currency).
        """
        self.ensure_one()
        if self.state != 'open':
            raise UserError(_("Cannot write off on a closed session."))
        statement_line = self.env['account.bank.statement.line'].browse(
            statement_line_id,
        )
        if not statement_line.exists():
            raise UserError(_("Unknown statement line: %s") % statement_line_id)
        if getattr(statement_line, 'is_reconciled', False):
            raise UserError(_(
                "Statement line %s is already reconciled.",
                statement_line.payment_ref or statement_line.id,
            ))
        company = statement_line.company_id or self.env.company
        gain = company.income_currency_exchange_account_id
        loss = company.expense_currency_exchange_account_id
        if not gain or not loss:
            raise UserError(_(
                "Configure the currency-exchange gain and loss accounts "
                "on company %(company)s before using FX auto write-off.",
                company=company.display_name,
            ))
        # Compute the residual on the statement line's auto-move so we
        # can pick the right account before delegating to the standard
        # write-off helper.
        move = statement_line.move_id
        suspense_lines = move.line_ids.filtered(
            lambda l: l.account_id.reconcile and not l.reconciled,
        )
        if not suspense_lines:
            raise UserError(_(
                "Cannot write off statement line %s: no reconcilable "
                "suspense line on its move.",
                statement_line.display_name,
            ))
        residual = sum(suspense_lines.mapped('amount_residual'))
        if max_amount is not None and abs(residual) > max_amount:
            raise UserError(_(
                "Residual %(amt).2f exceeds the FX auto-write-off cap "
                "of %(cap).2f. Use a manual write-off and capture a "
                "reason instead.",
                amt=residual, cap=max_amount,
            ))
        # residual > 0 means we still have a debit residual on a
        # receivable-style suspense; the bank received less than booked
        # so we recognise an exchange loss. residual < 0 means we
        # received more than booked, hence a gain.
        target_account = loss if residual > 0 else gain
        write_off_label = label or _("FX rounding write-off")
        self._perform_write_off(statement_line, target_account, write_off_label)
        self.env['eh.reconciliation.audit'].create({
            'session_id': self.id,
            'statement_line_id': statement_line.id,
            'aml_id': False,
            'user_id': self.env.user.id,
            'confidence': 1.0,
            'rules_fired': 'fx_writeoff',
            'decision': 'write_off',
            'source': 'manual',
        })
        self._increment_counters('write_offs')
        return True

    def apply_skip(self, statement_line_id):
        """Mark a statement line as deliberately skipped.

        Skip decisions do not perform any reconciliation; they just record
        that the user reviewed the line and chose to leave it for later.
        Useful for surfacing review backlog in audit reports.
        """
        self.ensure_one()
        if self.state != 'open':
            raise UserError(_("Cannot skip on a closed session."))

        self.env['eh.reconciliation.audit'].create({
            'session_id': self.id,
            'statement_line_id': statement_line_id,
            'aml_id': False,
            'user_id': self.env.user.id,
            'confidence': 0.0,
            'rules_fired': '',
            'decision': 'skip',
            'source': 'manual',
        })
        self._increment_counters('skips')
        return True

    def _increment_counters(self, *fields_to_bump):
        """Atomically increment one or more session counters.

        The previous code did self.field += 1, which is read-modify-
        write at the ORM level: two concurrent calls could read the
        same value and both write the same incremented value, losing
        one increment. A direct SQL UPDATE column = column + 1 is
        atomic at the database level so concurrent decisions accumulate
        correctly. Field names are checked against an allowlist before
        being interpolated into SQL.
        """
        if not self:
            return
        validated = []
        for f in fields_to_bump:
            if f not in _ALLOWED_COUNTERS:
                raise ValueError(f"Counter {f!r} not in allowlist")
            validated.append(f)
        if not validated:
            return
        self.flush_recordset(validated)
        set_clause = SQL(', ').join(
            SQL("%s = %s + 1", SQL.identifier(f), SQL.identifier(f))
            for f in validated
        )
        self.env.cr.execute(SQL(
            "UPDATE eh_reconciliation_session SET %s WHERE id IN %s",
            set_clause, tuple(self.ids),
        ))
        self.invalidate_recordset(validated)

    # ---- workspace RPC ----

    @api.model
    def load_workspace(self, journal_id):
        """Load reconciliation workspace state for a journal.

        Opens (or reuses) a session for the current user on this journal,
        gathers unreconciled statement lines, and returns a flat dict the
        OWL workspace component can render directly.
        """
        session = self.open_or_create(journal_id)
        SLine = self.env['account.bank.statement.line']
        # In Odoo 19 the line's `date` is a non-stored related field
        # on move_id and cannot be used in the SQL ORDER BY. Order by
        # id descending: newest line first, matches the user's mental
        # model since ids on a journal grow with the import sequence.
        try:
            sl_records = SLine.search(
                [
                    ('journal_id', '=', journal_id),
                    ('is_reconciled', '=', False),
                ],
                limit=200, order='id desc',
            )
        except ValueError:
            # Older versions may not have is_reconciled as searchable;
            # fall back to a Python filter.
            sl_records = SLine.search(
                [('journal_id', '=', journal_id)],
                limit=400, order='id desc',
            ).filtered(lambda s: not getattr(s, 'is_reconciled', False))[:200]

        statement_lines = []
        for sl in sl_records:
            currency = sl.currency_id or sl.company_id.currency_id
            statement_lines.append({
                'id': sl.id,
                'date': sl.date.isoformat() if sl.date else None,
                'amount': sl.amount,
                'partner_id': sl.partner_id.id or False,
                'partner_name': sl.partner_id.name or '',
                'payment_ref': sl.payment_ref or '',
                'ref': sl.ref or '',
                'currency_code': currency.name if currency else '',
            })

        return {
            'session': self._serialize_session(session),
            'statement_lines': statement_lines,
        }

    @api.model
    def get_suggestions_for_line(self, statement_line_id, limit=10,
                                 threshold=0.3):
        """Return scored suggestions enriched with the AML fields the OWL
        widget needs to render each candidate.
        """
        statement_line = self.env['account.bank.statement.line'].browse(
            statement_line_id,
        ).exists()
        if not statement_line:
            return []
        engine = self.env['eh.reconciliation.suggestion.engine']
        raw = engine.find_suggestions(
            statement_line, limit=limit, threshold=threshold,
        )
        if not raw:
            return []
        aml_ids = [r['aml_id'] for r in raw]
        amls = self.env['account.move.line'].browse(aml_ids)
        aml_by_id = {a.id: a for a in amls}
        out = []
        for r in raw:
            aml = aml_by_id.get(r['aml_id'])
            if not aml:
                continue
            currency = aml.currency_id or aml.company_id.currency_id
            out.append({
                'aml_id': aml.id,
                'score': r['score'],
                'breakdown': r['breakdown'],
                'rules_fired': r['rules_fired'],
                'date': aml.date.isoformat() if aml.date else None,
                'partner_name': aml.partner_id.name or '',
                'amount_residual': aml.amount_residual,
                'currency_code': currency.name if currency else '',
                'move_name': aml.move_id.name or '',
                'ref': aml.ref or '',
                'label': aml.name or '',
            })
        return out

    @staticmethod
    def _serialize_session(session):
        return {
            'id': session.id,
            'name': session.name or '',
            'state': session.state,
            'opened_at': (
                session.opened_at.isoformat() if session.opened_at else None
            ),
            'matches_made': session.matches_made,
            'matches_via_suggestion': session.matches_via_suggestion,
            'matches_manual': session.matches_manual,
            'write_offs': session.write_offs,
            'skips': session.skips,
        }

    # ---- integration points ----

    def _find_open_suspense(self, statement_line):
        """Return the open (unreconciled) reconcilable suspense line(s) of a
        statement line's journal move, or an empty recordset."""
        move = statement_line.move_id
        liquidity, suspense, other = statement_line._seek_for_lines()
        open_suspense = suspense.filtered(lambda l: not l.reconciled)
        if not open_suspense:
            open_suspense = move.line_ids.filtered(
                lambda l: l.account_id.reconcile and not l.reconciled
                and l.account_id != statement_line.journal_id.default_account_id
            )
        return open_suspense

    def _post_reclassification_entry(self, open_suspense, target_account,
                                     label):
        """Post a balanced adjusting entry that carries the open suspense
        balance onto ``target_account`` and reconcile the original suspense
        line against it.

        The original posted statement move is left untouched: audit
        inalterability requires that a posted move is never redrafted,
        edited, and reposted. Instead we book a fresh, balanced move on the
        same journal whose first leg sits on the suspense account (opposite
        sign to the open residual, so the two clear against each other) and
        whose second leg reclassifies the balance to ``target_account``.

        Returns the posted adjusting move.
        """
        move = open_suspense.move_id[:1]
        journal = move.journal_id
        company = move.company_id or self.env.company
        currency = company.currency_id
        suspense_account = open_suspense.account_id[:1]
        # The pair (original suspense line + our counter-leg) can only be
        # reconciled if the suspense account allows reconciliation. A bank
        # journal's suspense account is reconcilable by default. If it is
        # not, refuse up front and name the account: silently rewriting the
        # account's reconcile flag as a side effect of posting would mutate
        # chart-of-accounts configuration behind the user's back. The user
        # must correct the account (or journal) configuration deliberately.
        if not suspense_account.reconcile:
            raise UserError(_(
                "Cannot reclassify the suspense balance: account %s is not "
                "marked as reconcilable, so the adjusting entry could not "
                "clear against the original suspense line. Enable "
                "'Allow Reconciliation' on this account, or point the bank "
                "journal at a reconcilable suspense account, then retry.",
                suspense_account.display_name,
            ))
        # Net residual on the open suspense line(s), rounded in company
        # currency so the adjusting entry balances by construction.
        residual = currency.round(sum(open_suspense.mapped('amount_residual')))
        partner = open_suspense.partner_id[:1]
        # The counter-leg on the suspense account must carry the opposite
        # sign of the residual so the pair nets to zero and reconciles.
        # residual > 0 => open suspense holds a debit residual, so the
        # counter-leg is a credit on suspense and a debit on the target.
        suspense_debit = -residual if residual < 0 else 0.0
        suspense_credit = residual if residual > 0 else 0.0
        adjusting = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'date': move.date or fields.Date.context_today(self),
            'company_id': company.id,
            'ref': label,
            'line_ids': [
                (0, 0, {
                    'account_id': suspense_account.id,
                    'name': label,
                    'partner_id': partner.id or False,
                    'debit': suspense_debit,
                    'credit': suspense_credit,
                }),
                (0, 0, {
                    'account_id': target_account.id,
                    'name': label,
                    'partner_id': partner.id or False,
                    'debit': suspense_credit,
                    'credit': suspense_debit,
                }),
            ],
        })
        adjusting.action_post()
        counter_suspense = adjusting.line_ids.filtered(
            lambda l: l.account_id == suspense_account and not l.reconciled
        )
        (open_suspense + counter_suspense).reconcile()
        return adjusting

    def _adjusting_target_line(self, adjusting, target_account):
        """Return the open reclassification leg of an adjusting entry that
        sits on ``target_account`` (the leg that clears against candidate
        AMLs during a match)."""
        return adjusting.line_ids.filtered(
            lambda l: l.account_id == target_account and not l.reconciled
        )

    def _perform_reconciliation(self, statement_line, aml_records):
        """Perform the actual reconciliation between a statement line and
        candidate AMLs. Wraps Odoo's standard reconciliation API.

        Override this method if you need to plug in a different
        reconciliation backend (custom journal entry creation, intercompany
        offsets, etc.).
        """
        # A bank statement line posts to a liquidity line plus an open
        # suspense line. Odoo's reconcile() requires every line to sit on
        # one account, so the suspense balance must first be carried onto
        # the counterpart account (the candidate items' account) before it
        # can clear against them. Rather than redraft and edit the posted
        # statement move (which would break audit inalterability), we post a
        # balanced adjusting entry that reclassifies the suspense balance
        # onto the target account and reconciles against the original
        # suspense line. The original posted move stays immutable.
        if not aml_records:
            return
        open_suspense = self._find_open_suspense(statement_line)
        if not open_suspense:
            return
        target_account = aml_records[0].account_id
        move = statement_line.move_id
        with self.env.cr.savepoint():
            if any(l.account_id != target_account for l in open_suspense):
                label = _("Reconciliation reclass %s", move.name or '')
                adjusting = self._post_reclassification_entry(
                    open_suspense, target_account, label)
                # The adjusting entry's target-account leg carries the
                # balance now sitting against the candidate items.
                to_reconcile = self._adjusting_target_line(
                    adjusting, target_account)
            else:
                # The suspense line already sat on the target account, so it
                # clears directly against the candidate AMLs. Reconciling it
                # to zero drives the statement line to reconciled without
                # touching the posted move's account distribution.
                to_reconcile = open_suspense.filtered(
                    lambda l: not l.reconciled)
            if to_reconcile:
                # Defense in depth against a silent no-op. _post_reclassifi-
                # cation_entry already cleared the suspense against its
                # counter-leg (so the statement line reads reconciled) - if
                # the target/candidate reconcile below then clears nothing,
                # the reclass would be left stranded on the wrong account.
                # Measure whether the candidates actually absorbed residual;
                # a genuine match strictly reduces it. If nothing moved,
                # raise so the savepoint rolls the reclassification back and
                # no false 'match' is recorded.
                pre_residual = sum(
                    abs(a.amount_residual) for a in aml_records)
                (to_reconcile + aml_records).reconcile()
                aml_records.invalidate_recordset(
                    ['amount_residual', 'reconciled'])
                post_residual = sum(
                    abs(a.amount_residual) for a in aml_records)
                if post_residual >= pre_residual:
                    raise UserError(_(
                        "Reconciliation did not clear any of the selected "
                        "journal items; they are incompatible with this "
                        "statement line. No adjusting entry was posted."
                    ))

    def _perform_write_off(self, statement_line, account, label):
        """Write off a statement line's residual to the supplied account.

        A bank statement line posts to a liquidity line and an open
        suspense line. Writing the residual off means carrying that
        suspense balance onto the write-off account. Rather than redraft
        and edit the posted statement move (which would break audit
        inalterability), we post a balanced adjusting entry that moves the
        suspense balance to the write-off account and reconcile it against
        the original open suspense line. The statement line then shows as
        reconciled while the original posted move stays immutable.

        Hard fails if the statement line has no open reconcilable line.
        """
        move = statement_line.move_id
        open_suspense = self._find_open_suspense(statement_line)
        if not open_suspense:
            raise UserError(_(
                "Cannot write off statement line %s: it has no open "
                "reconcilable suspense line on its journal move. "
                "Verify the bank journal's suspense account is set "
                "and the statement line was processed normally.",
                statement_line.display_name,
            ))
        residual = sum(open_suspense.mapped('amount_residual'))
        if move.company_id.currency_id.is_zero(residual):
            raise UserError(_(
                "Cannot write off statement line %s: residual is "
                "already zero.",
                statement_line.display_name,
            ))
        write_off_label = label or _("Write-off")
        with self.env.cr.savepoint():
            self._post_reclassification_entry(
                open_suspense, account, write_off_label)
        return True
