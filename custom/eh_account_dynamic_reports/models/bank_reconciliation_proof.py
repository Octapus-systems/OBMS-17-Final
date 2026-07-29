# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank Reconciliation proof statement.

Per bank/cash journal in scope, a proof that bridges the bank's reported
cash to the company's book (GL) cash and surfaces any unexplained gap. The
bridge, authored from first principles:

    Last statement balance
  + Outstanding receipts (deposits in transit not yet on the statement)
  - Outstanding payments (cheques issued not yet cleared)
  = Adjusted bank balance
    Book balance per GL (cumulative ledger balance on the bank account)
    Unexplained difference = Book balance - Adjusted bank balance

When the difference is non-zero a balance_check line is emitted (kind=
'balance_check' so the PDF renderer paints it red), which is exactly the
signal an accountant wants: everything that is explained nets to zero, and
anything that does not is the stray entry to chase.

Per-journal mechanics:

* Last statement balance: the most recent account.bank.statement on or
  before date_to establishes the bank-side starting point; its
  balance_start plus the sum of its in-window line amounts is the bank
  balance as the statement last saw it.
* Outstanding receipts / payments: unreconciled residuals sitting in the
  journal's inbound / outbound outstanding-payment (suspense) accounts as
  of date_to. Each renders as an unfoldable section listing the individual
  move lines (date, label, amount) with drill-down to the journal item.
* Book balance per GL: cumulative SUM(aml.balance) on the journal's
  default_account_id up to date_to via MoveLineQuery (the same cumulative
  read the Cash Flow report uses for cash balances).

FALLBACKS: a journal with no statement yields an empty last-statement
section (book balance only) rather than an error; a journal with no
configured outstanding accounts yields an empty outstanding section; every
optional read is guarded so one bad journal does not break the render.
"""

from odoo import _, api, fields, models
from odoo.tools import SQL
from odoo.tools.translate import _lt

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery



class EhBankReconciliationHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.bank_reconciliation'
    _inherit = 'eh.account.dynamic.report.handler'
    _description = "Bank Reconciliation proof report handler"

    REPORT_CODE = 'bank_reconciliation'
    REPORT_NAME = _lt("Bank Reconciliation")

    @api.model
    def compute(self, options):
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))

        meta = {
            'report_code': self.REPORT_CODE,
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
        }

        journals = self._resolve_bank_journals(options, company_ids)
        lines = []
        grand_book = 0.0
        grand_diff = 0.0
        for journal in journals:
            section_lines, book_balance, difference = self._build_journal_proof(
                journal, date_to=date_to,
                company_ids=company_ids, posted_only=posted_only,
                options=options,
            )
            lines.extend(section_lines)
            grand_book += book_balance
            grand_diff += difference

        if not journals:
            meta['note'] = _(
                "No bank or cash journals are in scope for this company.")

        return {
            'columns': self._build_columns(),
            'lines': lines,
            'totals': {
                'amount': round(grand_book, 2),
                'book_balance': round(grand_book, 2),
                'difference': round(grand_diff, 2),
            },
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }

    # ---- column layout ----

    @api.model
    def _build_columns(self):
        return [
            {'expression_label': 'label', 'name': _("Description"),
             'figure_type': 'string'},
            {'expression_label': 'amount', 'name': _("Amount"),
             'figure_type': 'monetary'},
        ]

    # ---- journal resolution ----

    @api.model
    def _resolve_bank_journals(self, options, company_ids):
        """Bank/cash journals in scope.

        Honour options['journal_ids'] when given, filtered to bank/cash
        type; otherwise every bank/cash journal of the in-scope companies.
        """
        Journal = self.env['account.journal'].sudo()
        domain = [
            ('company_id', 'in', list(company_ids)),
            ('type', 'in', ('bank', 'cash')),
        ]
        journal_ids = options.get('journal_ids')
        if journal_ids:
            domain.append(('id', 'in', list(journal_ids)))
        return Journal.search(domain, order='id')

    # ---- per-journal proof ----

    @api.model
    def _build_journal_proof(
        self, journal, date_to, company_ids, posted_only, options,
    ):
        """Return (lines, book_balance, difference) for one journal."""
        section_id = "journal-%s" % journal.id
        lines = [{
            'id': "%s-header" % section_id,
            'name': _("%(journal)s (%(code)s)",
                      journal=journal.name, code=journal.code or ''),
            'level': 0,
            'columns': [{'expression_label': 'amount', 'value': ''}],
            'unfoldable': False,
            'meta': {'kind': 'section_header', 'section_id': section_id,
                     'journal_id': journal.id},
        }]

        # (a) Last statement balance (bank-side starting point).
        last_stmt_balance = self._last_statement_balance(
            journal, date_to)
        lines.append(self._proof_line(
            "%s-last-stmt" % section_id,
            _("Last statement balance"), last_stmt_balance,
            kind='statement_balance'))

        # (b) Outstanding receipts (inbound suspense, unreconciled).
        receipt_lines, receipts_total = self._outstanding_section(
            journal, date_to, section_id, inbound=True,
            label=_("Outstanding receipts"), company_ids=company_ids)
        lines.extend(receipt_lines)

        # (c) Outstanding payments (outbound suspense, unreconciled).
        payment_lines, payments_total = self._outstanding_section(
            journal, date_to, section_id, inbound=False,
            label=_("Outstanding payments"), company_ids=company_ids)
        lines.extend(payment_lines)

        adjusted_bank = round(
            last_stmt_balance + receipts_total - payments_total, 2)
        lines.append(self._proof_line(
            "%s-adjusted" % section_id,
            _("Adjusted bank balance"), adjusted_bank, kind='subtotal'))

        # (d) Book balance per GL (cumulative SUM on the bank account).
        book_balance = self._book_balance(
            journal, date_to, company_ids, posted_only, options)
        lines.append(self._proof_line(
            "%s-book" % section_id,
            _("Book balance per GL"), book_balance, kind='cash_balance'))

        # (e) Unexplained difference = book - adjusted bank.
        difference = round(book_balance - adjusted_bank, 2)
        lines.append(self._proof_line(
            "%s-difference" % section_id,
            _("Unexplained difference"), difference, kind='balance_check'))

        return lines, book_balance, difference

    # ---- (a) last statement ----

    @api.model
    def _last_statement_balance(self, journal, date_to):
        """balance_start + sum(in-window line amounts) of the latest
        statement on or before date_to. Zero when the journal has no
        statement (empty last-statement section, never an error)."""
        Statement = self.env['account.bank.statement'].sudo()
        try:
            # The statement.date field is computed from its posted lines and
            # can be False for a statement with no posted line yet. We
            # therefore fetch candidates by journal and pick the most recent
            # on or before date_to in Python, treating a date-less statement
            # as dated by its newest line (or, failing that, included so a
            # freshly-created opening statement still anchors the proof).
            candidates = Statement.search(
                [('journal_id', '=', journal.id)],
                order='id desc')
        except Exception:  # pragma: no cover - defensive
            return 0.0
        statement = self._pick_latest_statement(candidates, date_to)
        if not statement:
            return 0.0
        try:
            in_window = statement.line_ids.filtered(
                lambda l: l.date and l.date <= date_to)
            total = sum(in_window.mapped('amount'))
            return round(float(statement.balance_start or 0.0)
                         + float(total or 0.0), 2)
        except Exception:  # pragma: no cover - defensive
            return round(float(statement.balance_start or 0.0), 2)

    @staticmethod
    def _statement_effective_date(statement):
        """Best-effort date for a statement: its computed date, else the
        max line date, else None."""
        if statement.date:
            return statement.date
        line_dates = [l.date for l in statement.line_ids if l.date]
        return max(line_dates) if line_dates else None

    @api.model
    def _pick_latest_statement(self, candidates, date_to):
        """From a recordset of candidate statements, return the most recent
        whose effective date is on or before date_to. A statement with no
        resolvable date is eligible (an opening balance carries no line
        date) and ordered last so a dated statement always wins."""
        best = None
        best_key = None
        for stmt in candidates:
            eff = self._statement_effective_date(stmt)
            if eff is not None and eff > date_to:
                continue
            # Sort key: dated statements rank by date then id; a date-less
            # statement ranks below every dated one but is still eligible.
            key = (1, eff, stmt.id) if eff is not None else (0, None, stmt.id)
            if best is None:
                best, best_key = stmt, key
                continue
            # Compare with None-safe handling (date-less ranks lowest).
            if self._key_gt(key, best_key):
                best, best_key = stmt, key
        return best

    @staticmethod
    def _key_gt(a, b):
        """Return True if sort key a outranks b. Keys are
        (has_date, date_or_None, id); a date-less key (has_date=0) always
        ranks below a dated one."""
        if a[0] != b[0]:
            return a[0] > b[0]
        if a[0] == 1:  # both dated
            if a[1] != b[1]:
                return a[1] > b[1]
            return a[2] > b[2]
        return a[2] > b[2]  # both date-less, newest id wins

    # ---- (b/c) outstanding sections ----

    @api.model
    def _outstanding_accounts(self, journal, inbound):
        """Return the journal's inbound/outbound outstanding-payment
        account ids, guarded for journals without payment methods."""
        try:
            if inbound:
                accounts = (
                    journal._get_journal_inbound_outstanding_payment_accounts())
            else:
                accounts = (
                    journal._get_journal_outbound_outstanding_payment_accounts())
            return accounts.ids if accounts else []
        except Exception:  # pragma: no cover - defensive
            return []

    @api.model
    def _outstanding_section(
        self, journal, date_to, parent_section_id, inbound, label,
        company_ids,
    ):
        """Build an unfoldable section of unreconciled outstanding move
        lines. Returns (lines, total). Empty (header + zero total) when
        the journal has no suspense accounts or no open items."""
        sub_id = "%s-%s" % (
            parent_section_id, 'receipts' if inbound else 'payments')
        account_ids = self._outstanding_accounts(journal, inbound)
        header = {
            'id': "%s-header" % sub_id,
            'name': label,
            'level': 1,
            'columns': [{'expression_label': 'amount', 'value': ''}],
            'unfoldable': True,
            'unfolded': True,
            'meta': {'kind': 'section_line', 'section_id': sub_id},
        }
        if not account_ids:
            header['columns'] = [{'expression_label': 'amount', 'value': 0.0}]
            return [header], 0.0

        try:
            domain = [
                ('account_id', 'in', account_ids),
                ('company_id', 'in', list(company_ids)),
                ('date', '<=', self._iso_date(date_to)),
                ('parent_state', '=', 'posted'),
                ('reconciled', '=', False),
            ]
            move_lines = self.env['account.move.line'].sudo().search(
                domain, order='date, id')
        except Exception:  # pragma: no cover - defensive
            header['columns'] = [{'expression_label': 'amount', 'value': 0.0}]
            return [header], 0.0

        lines = [header]
        total = 0.0
        for ml in move_lines:
            # The outstanding amount is the residual still open on the
            # suspense account. When the suspense account is not configured
            # reconcilable, amount_residual is always 0; fall back to the
            # line balance so the proof still surfaces the open item. abs()
            # so receipts and payments both present as positive magnitudes
            # in their respective sections.
            residual = float(ml.amount_residual or 0.0)
            if not residual:
                residual = float(ml.balance or 0.0)
            amount = round(abs(residual), 2)
            if not amount:
                continue
            total += amount
            lines.append({
                'id': "outstanding-%s" % ml.id,
                'name': self._outstanding_line_label(ml),
                'level': 2,
                'parent_id': "%s-header" % sub_id,
                'columns': [{'expression_label': 'amount', 'value': amount}],
                'unfoldable': False,
                'meta': {
                    'kind': 'outstanding_item',
                    'move_line_id': ml.id,
                    'date': self._iso_date(ml.date) if ml.date else '',
                },
            })
        total = round(total, 2)
        header['columns'] = [{'expression_label': 'amount', 'value': total}]
        return lines, total

    @staticmethod
    def _outstanding_line_label(ml):
        bits = []
        if ml.date:
            bits.append(ml.date.isoformat() if hasattr(ml.date, 'isoformat')
                        else str(ml.date))
        label = ml.name or (ml.move_id.name if ml.move_id else '') or '/'
        bits.append(label)
        return " ".join(b for b in bits if b)

    # ---- (d) book balance ----

    @api.model
    def _book_balance(
        self, journal, date_to, company_ids, posted_only, options,
    ):
        """Cumulative SUM(aml.balance) on the journal's default bank account
        up to date_to. Mirrors the Cash Flow cash-balance read."""
        account = journal.default_account_id
        if not account:
            return 0.0
        try:
            query = MoveLineQuery(self.env, company_ids=company_ids)
            query.where_accounts([account.id])
            query.where_date_range(date_to=date_to)
            if posted_only:
                query.where_posted_only()
            self.apply_common_filters(query, options)
            query.select(SQL("COALESCE(SUM(aml.balance), 0)"), 'balance')
            rows = query.execute()
            if not rows:
                return 0.0
            return round(float(rows[0].get('balance') or 0.0), 2)
        except Exception:  # pragma: no cover - defensive
            return 0.0

    # ---- line factory ----

    @api.model
    def _proof_line(self, line_id, name, amount, kind):
        return {
            'id': line_id,
            'name': name,
            'level': 1,
            'columns': [
                {'expression_label': 'amount', 'value': round(amount, 2)},
            ],
            'unfoldable': False,
            'meta': {'kind': kind},
        }

    # ---- drilldown ----

    @api.model
    def get_drilldown_action(self, options, line_id):
        """Outstanding rows drill to the underlying journal item."""
        if not line_id or not isinstance(line_id, str):
            return None
        if not line_id.startswith('outstanding-'):
            return None
        try:
            ml_id = int(line_id.split('-', 1)[1])
        except (ValueError, IndexError):
            return None
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Item"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('id', '=', ml_id)],
        }
