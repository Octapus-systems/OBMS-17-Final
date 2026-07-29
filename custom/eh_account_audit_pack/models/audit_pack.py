# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.audit.pack: an audit-grade period close.

Enables the inalterable hash chain on the company's journals, scans the
period for integrity (no draft entries, every posted entry balanced and
hashed, no open suspense), and requires a segregated sign-off by a manager
other than the preparer, which advances the fiscal-year lock date.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Segregation-of-duties identity anchors. Once set by the workflow these
# record who prepared and who signed off the period and must never be
# reassigned to a different user or cleared.
_SOD_ID_ANCHORS = ('prepared_by_id', 'signed_by_id')
# Timestamp anchors may not be written without (validly) re-recording the
# matching identity, so a preparation / sign-off time cannot be backdated.
_SOD_AT_ANCHORS = {
    'prepared_at': 'prepared_by_id',
    'signed_at': 'signed_by_id',
}


class EhAuditPack(models.Model):
    _name = 'eh.audit.pack'
    _description = "Audit pack (period integrity & sign-off)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_to desc, id desc'
    _rec_name = 'name'

    # The period-close state machine (draft -> checks_run -> signed_off) may
    # only advance through action_run_checks / action_sign_off, which run under
    # sudo. A direct RPC write of 'state' by a plain user is refused by the
    # inherited guard, closing the "write past the sign-off gate and its
    # integrity scan / lock-date advance" bypass. The SoD identity/timestamp
    # anchors keep their own always-on guard (see _eh_guard_sod_anchors).
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('checks_run', "Checks Run"),
         ('signed_off', "Signed Off")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    period_from = fields.Date(required=True, tracking=True)
    period_to = fields.Date(required=True, tracking=True)
    advance_lock = fields.Boolean(
        default=True,
        help="Advance the fiscal-year lock date to the period end on "
             "sign-off so prior-period entries cannot be edited.")

    check_ids = fields.One2many('eh.audit.check', 'pack_id', copy=False)
    has_blocking_failure = fields.Boolean(compute='_compute_check_state')

    # ---- period-control chain (opt-in cross-module links) ----
    #
    # The three period-control modules install independently, so the audit
    # pack cannot declare a Many2one to eh.close.run / eh.year.end.run: the
    # ORM asserts the comodel is in the registry at setup, which would break a
    # stand-alone install. The links are therefore stored as plain record ids
    # and resolved through a registry-guarded browse. When a link is left at 0
    # the chain is inactive for that leg and behaviour is unchanged (this is
    # what keeps every existing test green and the module usable on its own).
    close_run_ref = fields.Integer(
        string="Linked period-close run",
        copy=False,
        help="Record id of the eh.close.run that must be approved before "
             "this period can be signed off. Left unset the close-approval "
             "gate is inactive. Present only as an id so the audit pack "
             "installs without the close-workflow module.")
    year_end_run_ref = fields.Integer(
        string="Linked year-end run",
        copy=False,
        help="Record id of the eh.year.end.run whose closing entry must be "
             "posted before this period can be signed off. Left unset the "
             "year-end gate is inactive.")
    chain_status = fields.Char(
        compute='_compute_chain_status',
        help="Human-readable state of the period-control chain: whether the "
             "linked close run is approved and the year-end close posted.")
    hash_chain_enabled = fields.Boolean(
        compute='_compute_hash_chain',
        help="Whether the inalterable hash chain is on for the company's "
             "posting journals.")

    prepared_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    prepared_at = fields.Datetime(readonly=True, copy=False)
    signed_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    signed_at = fields.Datetime(readonly=True, copy=False)

    notes = fields.Text()

    _sql_constraints = [
        ('unique_company_period', 'unique(company_id, period_to)', 'Only one audit pack per company per period end.'),
    ]

    @api.depends('check_ids.status', 'check_ids.is_blocking')
    def _compute_check_state(self):
        for pack in self:
            pack.has_blocking_failure = bool(pack.check_ids.filtered(
                lambda c: c.is_blocking and c.status == 'fail'))

    # ---- period-control chain resolution ----

    _CLOSE_MODEL = 'eh.close.run'
    _YEAR_END_MODEL = 'eh.year.end.run'

    def _resolve_close_run(self):
        """Return the linked close run, or an empty recordset.

        Registry-guarded: if the close-workflow module is not installed the
        model is absent from the registry and the link is treated as unset,
        so the chain leg is simply inactive rather than raising.
        """
        self.ensure_one()
        if not self.close_run_ref or self._CLOSE_MODEL not in self.env:
            return None
        run = self.env[self._CLOSE_MODEL].browse(self.close_run_ref)
        return run if run.exists() else None

    def _resolve_year_end_run(self):
        """Return the linked year-end run, or an empty recordset.

        Registry-guarded exactly like :meth:`_resolve_close_run`.
        """
        self.ensure_one()
        if not self.year_end_run_ref or self._YEAR_END_MODEL not in self.env:
            return None
        run = self.env[self._YEAR_END_MODEL].browse(self.year_end_run_ref)
        return run if run.exists() else None

    @api.depends('close_run_ref', 'year_end_run_ref')
    def _compute_chain_status(self):
        for pack in self:
            parts = []
            close = pack._resolve_close_run()
            if close is not None:
                parts.append(_("Close run: %s", close.state))
            year_end = pack._resolve_year_end_run()
            if year_end is not None:
                parts.append(_("Year-end: %s", year_end.state))
            pack.chain_status = "; ".join(parts) or _("No chain configured")

    def _compute_hash_chain(self):
        for pack in self:
            journals = self._posting_journals(pack.company_id)
            pack.hash_chain_enabled = bool(journals) and all(
                self._journal_hash_on(j) for j in journals)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.audit.pack') or '/'
        return super().create(vals_list)

    def write(self, vals):
        self._eh_guard_sod_anchors(vals)
        return super().write(vals)

    def _eh_guard_sod_anchors(self, vals):
        """Reject any write that would forge an SoD anchor.

        An identity anchor may only be set to the acting user's own id or
        left at its current value; it can never be reassigned to a different
        user or cleared. This lets the legitimate workflow transitions stand
        (action_run_checks and action_sign_off both stamp the acting user)
        while blocking a direct RPC write that reassigns the preparer or the
        signer of record. A timestamp anchor may not be written on its own,
        so a sign-off time cannot be backdated without also (validly)
        re-recording who signed.
        """
        uid = self.env.user.id
        for fname in _SOD_ID_ANCHORS:
            if fname not in vals:
                continue
            new = vals[fname]
            for rec in self:
                current = rec[fname].id
                if new and (new == current or new == uid):
                    continue
                raise UserError(_(
                    "The '%s' field anchors the segregation-of-duties control "
                    "and may only record the acting user; it cannot be "
                    "reassigned to another user or cleared.", fname))
        for at_field, id_field in _SOD_AT_ANCHORS.items():
            if at_field in vals and id_field not in vals:
                raise UserError(_(
                    "Audit-pack timestamps are stamped by the workflow and "
                    "cannot be edited on their own."))

    # ---- hash chain ----

    @api.model
    def _posting_journals(self, company):
        return self.env['account.journal'].sudo().search([
            ('company_id', '=', company.id),
            ('type', 'in', ('sale', 'purchase', 'general', 'bank', 'cash')),
        ])

    @api.model
    def _journal_hash_on(self, journal):
        if 'restrict_mode_hash_table' in journal._fields:
            return bool(journal.restrict_mode_hash_table)
        return False

    # ---- hash-chain recomputation ----
    #
    # The audit-pack gate must not merely observe that a move carries *a* hash;
    # it must re-derive the inalterable chain and confirm every link. A tampered
    # move (its stored hash edited, or a hashed field changed underneath a stale
    # hash) has a non-empty ``inalterable_hash`` yet no longer recomputes to that
    # value, so a presence-only check would wave it through. Here we walk the
    # secure-sequence chain per journal and recompute each link exactly as core
    # does, threading the previous move's hash forward and probing hash versions.

    _MAX_HASH_VERSION = 4

    @api.model
    def _hash_supported(self, Move):
        """Whether this Odoo version exposes a recomputable hash chain.

        18/19 recompute via ``account.move._calculate_hashes`` (returns a
        ``{move: hash}`` map); 16/17 via ``account.move._compute_hash``
        (returns a single string). Either is enough to re-derive the chain.
        """
        return (
            'inalterable_hash' in Move._fields
            and ('_calculate_hashes' in dir(Move) or '_compute_hash' in dir(Move))
        )

    @api.model
    def _recompute_move_hash(self, move, previous_hash, version):
        """Re-derive one move's hash at a given hash version, cross-version."""
        moved = move.with_context(hash_version=version)
        if hasattr(moved, '_calculate_hashes'):
            return moved._calculate_hashes(previous_hash)[move]
        # 16/17 signature.
        return moved._compute_hash(previous_hash=previous_hash)

    def _hash_chain_corrupt(self):
        """Recompute the inalterable hash chain and return True when broken.

        Delegates to core account's canonical ``_check_hash_integrity``, which
        partitions the chain PER sequence prefix (the entry sequence resets each
        fiscal year, so a hashed journal spanning years holds several
        independent chains, each seeded from an empty previous hash) and handles
        every hash version across Odoo 16-19. A hand-rolled single-thread walk
        seeds the next prefix from the prior prefix's last hash and orders on the
        removed ``secure_sequence_number``, so it mis-flags a legitimate
        multi-year ledger; delegating to core avoids both faults and stays
        default-safe (a clean ledger reports every prefix verified).
        """
        self.ensure_one()
        company = (self.company_id or self.env.company).sudo()
        check = getattr(company, '_check_hash_integrity', None)
        if not check:
            return False
        try:
            report = check()
        except Exception:
            # A recompute that errors must not silently vouch the ledger.
            return True
        results = report.get('results', []) if isinstance(report, dict) \
            else (report or [])
        for r in results:
            if not isinstance(r, dict):
                continue
            status = (r.get('status') or '').lower()
            msg = '%s %s' % (r.get('msg_cover') or '', r.get('msg') or '')
            if status == 'corrupted' or 'corrupt' in msg.lower():
                return True
        return False

    def _assert_chain_complete(self):
        """Block sign-off unless every configured chain leg is complete.

        This enforces the ordered close chain end-to-end at the sign-off
        gate: close run approved (closed) -> year-end close posted -> audit
        pack signed. Legs left unlinked are silent, so a stand-alone audit
        pack still signs off. Verified against the live linked records rather
        than the stored check rows, so a link added after the last check run
        is honoured.
        """
        self.ensure_one()
        close = self._resolve_close_run()
        if close is not None and close.state != 'closed':
            raise UserError(_(
                "The linked period-close run '%s' is not approved (state "
                "'%s'). Approve the close run before signing off the period.",
                close.display_name, close.state))
        year_end = self._resolve_year_end_run()
        if year_end is not None and year_end.state != 'posted':
            raise UserError(_(
                "The linked year-end closing entry '%s' is not posted (state "
                "'%s'). Post the year-end close before signing off the "
                "period.", year_end.display_name, year_end.state))

    def _assert_hash_chain_active(self):
        """Block the caller unless an inalterable hash chain is in force.

        Sign-off and the lock-date advance vouch that the period can no
        longer be altered. That guarantee rests on the core inalterable
        hash chain: if the capability is absent (the version does not carry
        ``inalterable_hash`` / ``restrict_mode_hash_table``) or the hash
        table is off on any posting journal in scope, there is no chain and
        the vouch would be hollow. Treat that as a blocking failure, not a
        warning, so a manager cannot complete sign-off or advance the lock
        date over an unprotected ledger.
        """
        self.ensure_one()
        Move = self.env['account.move'].sudo()
        if ('inalterable_hash' not in Move._fields
                or 'restrict_mode_hash_table'
                not in self.env['account.journal']._fields):
            raise UserError(_(
                "This Odoo version does not provide the inalterable hash "
                "chain, so an audit-grade sign-off that advances the lock "
                "date cannot be vouched. Sign-off is blocked."))
        journals = self._posting_journals(self.company_id)
        unprotected = journals.filtered(
            lambda j: not self._journal_hash_on(j))
        if not journals or unprotected:
            raise UserError(_(
                "The inalterable hash chain is not active on every posting "
                "journal. Enable the hash chain before signing off so the "
                "period cannot be altered after the lock date."))

    def action_enable_hash_chain(self):
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can enable the hash chain."))
        journals = self._posting_journals(self.company_id)
        if 'restrict_mode_hash_table' not in self.env['account.journal']._fields:
            raise UserError(_(
                "This Odoo version does not expose the journal hash-chain "
                "setting."))
        journals.filtered(
            lambda j: not j.restrict_mode_hash_table
        ).write({'restrict_mode_hash_table': True})
        # Force-hash the already-posted HISTORICAL entries in scope. Enabling
        # restrict mode only hashes moves at future post time, so without this
        # the sign-off check flags every pre-existing posted entry as unhashed
        # and blocks the period permanently. Odoo 19 exposes
        # _hash_moves(force_hash=True); guarded for older series where the API
        # differs, degrading to a message rather than an error.
        Move = self.env['account.move']
        secured = 0
        if hasattr(Move, '_hash_moves'):
            historical = Move.search(
                self._move_domain(state='posted')).filtered(
                lambda m: self._journal_hash_on(m.journal_id)
                and not m.inalterable_hash)
            if historical:
                try:
                    historical.sudo()._hash_moves(force_hash=True)
                    secured = len(historical)
                except Exception as exc:  # noqa: BLE001 - graceful fallback
                    self.message_post(body=_(
                        "Enabled the chain, but could not secure %(n)d "
                        "historical entr(ies) automatically (%(err)s). Use "
                        "the core Secure Entries wizard to hash them.",
                        n=len(historical), err=exc))
        self.message_post(body=_(
            "Inalterable hash chain enabled on %(j)d posting journal(s); "
            "%(s)d historical entr(ies) secured.",
            j=len(journals), s=secured))
        return True

    # ---- checks ----

    def _eh_refresh_check_rows(self):
        """Rescan the ledger and rewrite the check rows from the result.

        The rows are system-written: the internal flag below is the only path
        that may create or replace them, and it is set here after the scan,
        never from user input. sudo lets the rescan run for a preparer whose
        ACL is read-only on the rows. Used both by action_run_checks and, as a
        recompute-at-the-gate guard, by action_sign_off, so a stale or tampered
        stored row cannot vouch a period that no longer passes the checks.
        """
        self.ensure_one()
        self.check_ids.sudo().with_context(
            eh_audit_check_write=True).unlink()
        Check = self.env['eh.audit.check'].sudo().with_context(
            eh_audit_check_write=True)
        for vals in self._compute_checks():
            Check.create(dict(vals, pack_id=self.id))

    def action_run_checks(self):
        self.ensure_one()
        # Guarded state/anchor writes below run as su (env.su True, real
        # env.user preserved for the SoD stamps and access checks).
        self = self._eh_workflow_action()
        if self.state == 'signed_off':
            raise UserError(_("The period is already signed off."))
        self._eh_refresh_check_rows()
        vals = {'state': 'checks_run'}
        # Preparer-of-record is set once, on the first run, and is never
        # overwritten on re-run. Freezing it preserves segregation of duties:
        # a later re-run by a different user cannot silently reset the
        # preparer and thereby let the original preparer sign off.
        if not self.prepared_by_id:
            vals['prepared_by_id'] = self.env.user.id
            vals['prepared_at'] = fields.Datetime.now()
        self.write(vals)
        return True

    def _compute_checks(self):
        self.ensure_one()
        checks = [
            self._check_no_draft(),
            self._check_balanced(),
            self._check_hashed(),
            self._check_suspense(),
        ]
        # Period-control chain checks are appended only when the matching
        # link is configured. An unlinked pack yields exactly the four
        # checks above, so a stand-alone install and every pre-existing
        # test reproduces today's output unchanged.
        checks += self._chain_checks()
        return checks

    def _chain_checks(self):
        """Return the period-control chain checks that are in force.

        Each leg contributes a blocking check only when its link is set and
        the linked record still exists. A missing target module, an unset
        link, or a deleted target all leave the leg silent (opt-in), so the
        gate never fires on a stand-alone audit pack.
        """
        self.ensure_one()
        out = []
        close = self._resolve_close_run()
        if close is not None:
            out.append(self._check_close_approved(close))
        year_end = self._resolve_year_end_run()
        if year_end is not None:
            out.append(self._check_year_end_posted(year_end))
        return out

    def _check_close_approved(self, close_run):
        # The period-close run must reach its terminal approved state
        # (closed). A run still open / in progress / pending approval, or a
        # reopened run, means the checklist controls are not signed off, so
        # the audit pack must not be signed off over it.
        approved = close_run.state == 'closed'
        return {
            'code': 'chain_close_approved',
            'name': _("Period-close run approved"),
            'status': 'pass' if approved else 'fail',
            'count': 0 if approved else 1,
            'is_blocking': True,
            'detail': '' if approved else _(
                "The linked period-close run '%s' is in state '%s'; it must "
                "be approved (closed) before sign-off.",
                close_run.display_name, close_run.state),
        }

    def _check_year_end_posted(self, year_end_run):
        # The year-end closing entry must be posted. A run that is draft /
        # computed / cancelled has not zeroed income and expense to retained
        # earnings, so the accounting for the fiscal year is not complete.
        # A reversed run means the close was undone, which is also not a
        # completed close.
        posted = year_end_run.state == 'posted'
        return {
            'code': 'chain_year_end_posted',
            'name': _("Year-end closing entry posted"),
            'status': 'pass' if posted else 'fail',
            'count': 0 if posted else 1,
            'is_blocking': True,
            'detail': '' if posted else _(
                "The linked year-end run '%s' is in state '%s'; its closing "
                "entry must be posted before sign-off.",
                year_end_run.display_name, year_end_run.state),
        }

    def _move_domain(self, state=None):
        domain = [
            ('company_id', '=', self.company_id.id),
            ('date', '>=', self.period_from),
            ('date', '<=', self.period_to),
        ]
        if state:
            domain.append(('state', '=', state))
        return domain

    def _check_no_draft(self):
        count = self.env['account.move'].sudo().search_count(
            self._move_domain(state='draft'))
        return {
            'code': 'no_draft', 'name': _("No draft entries in period"),
            'status': 'fail' if count else 'pass', 'count': count,
            'is_blocking': True,
            'detail': _("%d draft entries must be posted or deleted.", count)
            if count else '',
        }

    def _check_balanced(self):
        posted = self.env['account.move'].sudo().search(
            self._move_domain(state='posted'))
        currency = self.company_id.currency_id
        count = sum(
            1 for m in posted
            if not currency.is_zero(sum(m.line_ids.mapped('balance'))))
        return {
            'code': 'balanced', 'name': _("Posted entries balanced"),
            'status': 'fail' if count else 'pass', 'count': count,
            'is_blocking': True,
            'detail': _("%d posted entries do not balance.", count)
            if count else '',
        }

    def _check_hashed(self):
        Move = self.env['account.move'].sudo()
        if 'inalterable_hash' not in Move._fields:
            return {
                'code': 'hashed', 'name': _("Posted entries hashed"),
                'status': 'fail', 'count': 0, 'is_blocking': True,
                'detail': _("The inalterable hash chain is not available in "
                            "this version, so the period cannot be vouched "
                            "as unalterable."),
            }
        posted = Move.search(self._move_domain(state='posted'))
        unhashed = [
            m for m in posted
            if self._journal_hash_on(m.journal_id) and not m.inalterable_hash]
        # Presence of a hash is necessary but not sufficient: recompute the
        # inalterable chain (via core's per-prefix integrity check) and confirm
        # every link. A tampered hash or hashed field breaks the recompute, so a
        # forged hash cannot pass this gate.
        chain_corrupt = self._hash_chain_corrupt()
        count = len(unhashed) + (1 if chain_corrupt else 0)
        if not count:
            detail = ''
        elif unhashed and chain_corrupt:
            detail = _(
                "%(unhashed)d posted entries are not hashed and the "
                "inalterable hash chain fails re-verification (tampered); the "
                "period cannot be vouched as unalterable.",
                unhashed=len(unhashed))
        elif chain_corrupt:
            detail = _(
                "The inalterable hash chain fails re-verification (a link does "
                "not recompute); the period has been tampered with and cannot "
                "be vouched as unalterable.")
        else:
            detail = _(
                "%d posted entries on a hash-restricted journal are not "
                "hashed; re-post after enabling the hash chain.", len(unhashed))
        return {
            'code': 'hashed', 'name': _("Posted entries hashed"),
            'status': 'fail' if count else 'pass', 'count': count,
            'is_blocking': True,
            'detail': detail,
        }

    def _check_suspense(self):
        journals = self.env['account.journal'].sudo().search([
            ('type', 'in', ('bank', 'cash')),
            ('company_id', '=', self.company_id.id)])
        suspense = journals.mapped('suspense_account_id')
        count = 0
        if suspense:
            count = self.env['account.move.line'].sudo().search_count([
                ('account_id', 'in', suspense.ids),
                ('reconciled', '=', False),
                ('parent_state', '=', 'posted'),
                ('company_id', '=', self.company_id.id),
                ('date', '<=', self.period_to),
            ])
        return {
            'code': 'suspense', 'name': _("No open bank/cash suspense"),
            'status': 'warn' if count else 'pass', 'count': count,
            'is_blocking': False,
            'detail': _("%d suspense lines remain open.", count)
            if count else '',
        }

    # ---- sign-off ----

    def action_sign_off(self):
        self.ensure_one()
        # Guarded state/anchor writes below run as su. sudo preserves env.user,
        # so the manager and segregation-of-duties checks still evaluate against
        # the real acting user.
        self = self._eh_workflow_action()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can sign off a period."))
        if self.state != 'checks_run':
            raise UserError(_(
                "Run the integrity checks before signing off."))
        # Recompute the checks against the live ledger at the gate, so a stale
        # or hand-tampered stored check row cannot vouch a period that has
        # since gained a draft / unbalanced / unhashed entry. The freshly
        # written rows drive has_blocking_failure below.
        self._eh_refresh_check_rows()
        if self.has_blocking_failure:
            raise UserError(_(
                "A blocking integrity check has failed; resolve it before "
                "signing off."))
        if self.prepared_by_id and self.prepared_by_id.id == self.env.user.id:
            raise UserError(_(
                "Segregation of duties: %s ran the checks and cannot also "
                "sign off. Another manager must sign off this period.",
                self.prepared_by_id.display_name))
        # The period-control chain must be complete: any linked close run
        # approved and any linked year-end close posted. This is re-verified
        # live (not read off possibly-stale check rows) so linking a run and
        # signing off without re-running the checks is still blocked.
        self._assert_chain_complete()
        # The inalterable hash chain must be in force before the period can be
        # vouched and (optionally) the lock date advanced.
        self._assert_hash_chain_active()
        self.write({
            'state': 'signed_off',
            'signed_by_id': self.env.user.id,
            'signed_at': fields.Datetime.now(),
        })
        if self.advance_lock:
            self._advance_lock_date()
        self.message_post(body=_("Period signed off."))
        return True

    def _advance_lock_date(self):
        self.ensure_one()
        company = self.company_id
        if 'fiscalyear_lock_date' not in company._fields:
            return
        current = company.fiscalyear_lock_date
        if current and current >= self.period_to:
            return
        company.sudo().fiscalyear_lock_date = self.period_to


class EhAuditCheck(models.Model):
    _name = 'eh.audit.check'
    _description = "Audit pack integrity check result"
    _order = 'pack_id, id'

    pack_id = fields.Many2one(
        'eh.audit.pack', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='pack_id.company_id', store=True, readonly=True)
    code = fields.Char(required=True)
    name = fields.Char(required=True)
    status = fields.Selection(
        [('pass', "Pass"), ('warn', "Warning"), ('fail', "Fail")],
        default='pass', required=True)
    count = fields.Integer()
    is_blocking = fields.Boolean()
    detail = fields.Char()

    # ---- system-written / append-only ----
    # These rows ARE the sign-off blocking gate
    # (eh.audit.pack.has_blocking_failure). They carry the result of an
    # automated integrity scan and are written only by the pack's check
    # refresh. A direct create / write / unlink - by anyone, including a
    # manager - could flip a failed blocking check to 'pass' and clear the
    # gate without fixing the underlying issue, so every mutation must carry
    # the internal flag set by that refresh. The ACL grants no group direct
    # create / write / unlink on top of this guard.
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get('eh_audit_check_write'):
            raise UserError(_(
                "Audit check rows are produced by Run Checks, not created "
                "directly."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.context.get('eh_audit_check_write'):
            raise UserError(_(
                "Audit check rows record an automated integrity scan and "
                "cannot be edited directly; re-run the checks to refresh "
                "them."))
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('eh_audit_check_write'):
            raise UserError(_(
                "Audit check rows cannot be deleted directly; re-run the "
                "checks to refresh them."))
        return super().unlink()
