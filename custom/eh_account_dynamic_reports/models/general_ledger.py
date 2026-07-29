# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
General Ledger handler.

The first line level report in the bundle. Unlike Trial Balance, P&L, and
Balance Sheet (which aggregate to one line per account), General Ledger
renders one line per journal item, grouped under each account, with an
opening balance, every entry in the period, and a running closing balance.

Two SQL passes:

1. Opening balances per account: SUM(balance) where date < date_from.
   Returns a dict {account_id: opening_amount}.
2. Per line entries within [date_from, date_to]: ordered by account code,
   then date, then aml.id. Includes journal code, move name, partner name,
   label, debit, credit, and per line balance.

Running balance is computed in Python: starts at the opening, increments
by debit minus credit for each entry. This keeps the SQL simple and avoids
window functions that some PostgreSQL configurations are slower at.

Drill down:

* account-N (account header line): opens the journal items list filtered
  to that account and date range. Inherits the base default.
* aml-X (entry line): opens the specific journal entry form. Overridden
  here.

Other line ids (account-N-opening, account-N-total) are not drillable.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL
from odoo.tools.translate import _lt

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery



# Absolute ceiling on rows materialised in a single render. The JSON
# payload bloats past this scale and the OWL viewer struggles with
# virtual scrolling. The default soft cap lives on the company record
# (eh_gl_row_limit) and the operator changes it under
# Settings > Accounting > ERP Heritage Reporting > GL Row Limit.
# The right path past the ceiling is to narrow the date range or add
# account / partner filters.
_ABSOLUTE_ROW_LIMIT = 100_000


class EhGeneralLedgerHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.general_ledger'
    _inherit = 'eh.account.dynamic.report.handler'
    _description = "General Ledger report handler"

    REPORT_CODE = 'general_ledger'
    REPORT_NAME = _lt("General Ledger")

    @api.model
    def compute(self, options):
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        show_zero = bool(options.get('show_zero', False))

        # Lazy screen mode: emit only header + opening + total per account
        # and mark each header lazy/unfoldable/collapsed; journal items
        # arrive via expand_account_line on demand. This keeps the initial
        # render O(accounts) and removes the row ceiling for screen.
        #
        # Backward compatibility: compute() stays EAGER by default. The
        # lazy path is opt-in via options['lazy_expand'] (set by the OWL
        # viewer). Export (render_xlsx / PDF) and the existing direct
        # compute() callers never set the flag, so they keep inlining
        # every journal item exactly as before. options['eager_expand']
        # forces eager even if lazy_expand is set (export over a screen
        # options snapshot).
        lazy = bool(options.get('lazy_expand')) and not options.get(
            'eager_expand')
        if lazy:
            return self._compute_lazy(
                options, company_ids, date_from, date_to,
                posted_only, show_zero,
            )

        opening_by_account = self._fetch_opening_balances(
            company_ids=company_ids, date_from=date_from,
            posted_only=posted_only, options=options,
        )
        entries = self._fetch_line_entries(
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )

        # Foreign-currency context: map currency_id -> code for any line in a
        # currency other than its company's, so the report can show the
        # original transaction amount alongside the company-currency figure.
        company_currency_ids = set(
            self.env['res.company'].browse(
                [int(c) for c in company_ids]).mapped('currency_id').ids)
        currency_ids = {
            e['currency_id'] for e in entries
            if e.get('currency_id')
            and e['currency_id'] not in company_currency_ids
        }
        currency_codes = {
            c.id: c.name for c in
            self.env['res.currency'].browse(list(currency_ids))
        }

        lines = self._build_lines(
            opening_by_account, entries, show_zero, currency_codes)

        return {
            'columns': self._build_columns(),
            'lines': lines,
            'totals': {},
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': {
                'report_code': self.REPORT_CODE,
                'date_from': self._iso_date(date_from),
                'date_to': self._iso_date(date_to),
                'company_ids': sorted(int(c) for c in company_ids),
                'posted_only': posted_only,
                'show_zero': show_zero,
            },
        }

    # ---- lazy screen path (Wave 0 engine contract) ----

    @api.model
    def _compute_lazy(
        self, options, company_ids, date_from, date_to,
        posted_only, show_zero,
    ):
        """Build the O(accounts) skeleton: header + opening + total only.

        No journal-item rows are materialised; each account header is a
        lazy/unfoldable/collapsed leaf whose children come from
        expand_account_line. The per-account opening and period
        debit/credit aggregates are cheap GROUP BY queries, so the whole
        render stays O(accounts) regardless of journal-item volume.
        """
        opening_by_account = self._fetch_opening_balances(
            company_ids=company_ids, date_from=date_from,
            posted_only=posted_only, options=options,
        )
        period_by_account = self._fetch_period_aggregates(
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )

        # Union of accounts touched by opening or period activity.
        account_ids = set(opening_by_account) | set(period_by_account)
        account_meta = {}
        if account_ids:
            for acc in self.env['account.account'].browse(
                    sorted(account_ids)).sorted('code'):
                account_meta[acc.id] = {
                    'code': acc.code or '', 'name': acc.name or '',
                }

        ordered_account_ids = sorted(
            account_ids, key=lambda i: account_meta.get(i, {}).get('code', ''))

        lines = []
        for account_id in ordered_account_ids:
            opening = round(opening_by_account.get(account_id, 0.0), 2)
            agg = period_by_account.get(account_id)
            period_debit = round(agg['debit'], 2) if agg else 0.0
            period_credit = round(agg['credit'], 2) if agg else 0.0
            has_activity = bool(agg and agg['count'])
            if not show_zero and opening == 0.0 and not has_activity:
                continue
            closing = round(opening + period_debit - period_credit, 2)
            meta = account_meta.get(
                account_id, {'code': '', 'name': ''})

            header = self._account_header_line(account_id, meta)
            # Flip the header into a lazy-expandable leaf via the shared
            # hook (no-op for GL multi-column safety; GL is single-period).
            self._eh_apply_leaf_lazy_flags(header, options)
            lines.append(header)
            lines.append(self._opening_line(account_id, opening))
            lines.append(
                self._account_total_line(account_id, meta, closing))
        return {
            'columns': self._build_columns(),
            'lines': lines,
            'totals': {},
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': {
                'report_code': self.REPORT_CODE,
                'date_from': self._iso_date(date_from),
                'date_to': self._iso_date(date_to),
                'company_ids': sorted(int(c) for c in company_ids),
                'posted_only': posted_only,
                'show_zero': show_zero,
                'lazy': True,
            },
        }

    @api.model
    def _fetch_period_aggregates(
        self, company_ids, date_from, date_to, posted_only, options,
    ):
        """Per-account {debit, credit, count} for the period window.

        Cheap GROUP BY aggregate that tells the lazy builder which
        accounts have activity (and their closing) without materialising
        any journal item. Same filters as the entry fetch so the figures
        reconcile to what an expand will page.
        """
        query = self._base_query(
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        query.select_field('account_id')
        query.select(SQL("SUM(aml.debit)"), 'period_debit')
        query.select(SQL("SUM(aml.credit)"), 'period_credit')
        query.select(SQL("COUNT(aml.id)"), 'row_count')
        query.group_by(SQL("aml.account_id"))
        rows = query.execute()
        return {
            r['account_id']: {
                'debit': float(r['period_debit'] or 0.0),
                'credit': float(r['period_credit'] or 0.0),
                'count': int(r['row_count'] or 0),
            }
            for r in rows
        }

    @api.model
    def expand_account_line(self, options, line_id, offset=0, limit=None):
        """GL override: page one account's journal items with running balance.

        Reuses _fetch_line_entries' column shape via a single-account
        MoveLineQuery, ordered (date, id), offset/limit paged. The page's
        starting running balance is opening + SUM(debit - credit) over every
        row before `offset` (a single cheap prefix-sum query), so page N's
        last running value continues seamlessly into page N+1 and the final
        page's closing equals the eager closing. All defensive: any failure
        degrades to an empty collapsed page (handled by the orchestrator).
        """
        empty = {
            'child_lines': [], 'has_more': False,
            'next_offset': int(offset or 0), 'total_count': 0,
        }
        account_id = self._expand_account_id_from_line_id(line_id)
        if account_id is None:
            return empty
        try:
            date_from = self._extract_date(options, 'date_from')
            date_to = self._extract_date(options, 'date_to')
        except UserError:
            return empty

        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))

        if limit is None:
            limit = self._resolve_expand_page_size(options)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = self._resolve_expand_page_size(options)
        if limit <= 0:
            limit = self._resolve_expand_page_size(options)
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0

        opening_by_account = self._fetch_opening_balances(
            company_ids=company_ids, date_from=date_from,
            posted_only=posted_only, options=options,
        )
        opening = round(opening_by_account.get(account_id, 0.0), 2)

        # Prefix sum of (debit - credit) over rows BEFORE this offset, so
        # the page's first running balance continues from prior pages.
        prefix = self._expand_prefix_balance(
            options, account_id, date_from, date_to,
            posted_only, company_ids, offset,
        )
        running = round(opening + prefix, 2)

        # Total count (count-only pre-flight) and the page itself.
        count_query = self._expand_build_page_query(
            options, account_id, date_from, date_to)
        count_query.select_count(alias='row_count')
        count_rows = count_query.execute()
        total_count = int(count_rows[0]['row_count']) if count_rows else 0

        page_query = self._expand_build_page_query(
            options, account_id, date_from, date_to)
        self._expand_select_columns(page_query)
        page_query.select_field('amount_currency')
        page_query.select_field('currency_id')
        page_query.order_by('date', 'ASC')
        page_query.order_by('id', 'ASC')
        page_query.offset(offset)
        page_query.limit(limit + 1)
        rows = page_query.execute()

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        # Foreign-currency context, mirroring the eager path.
        company_currency_ids = set(
            self.env['res.company'].browse(
                [int(c) for c in company_ids]).mapped('currency_id').ids)
        currency_ids = {
            r['currency_id'] for r in rows
            if r.get('currency_id')
            and r['currency_id'] not in company_currency_ids
        }
        currency_codes = {
            c.id: c.name for c in
            self.env['res.currency'].browse(list(currency_ids))
        }

        child_lines = []
        for entry in rows:
            debit = round(float(entry.get('debit') or 0.0), 2)
            credit = round(float(entry.get('credit') or 0.0), 2)
            running = round(running + debit - credit, 2)
            child_lines.append(
                self._entry_line(entry, debit, credit, running, currency_codes))

        return {
            'child_lines': child_lines,
            'has_more': has_more,
            'next_offset': offset + len(rows),
            'total_count': total_count,
        }

    @api.model
    def _expand_prefix_balance(
        self, options, account_id, date_from, date_to,
        posted_only, company_ids, offset,
    ):
        """SUM(debit - credit) over the first `offset` ordered rows.

        Used to seed a page's running balance so it continues from earlier
        pages. Computed with the same (date, id) order as the page so the
        boundary is exact. Returns 0.0 when offset is 0 (the first page
        starts at the opening balance) or on any failure (degrades to the
        opening-only start rather than raising).
        """
        if not offset:
            return 0.0
        try:
            ids_query = self._expand_build_page_query(
                options, account_id, date_from, date_to)
            ids_query.select_field('id', alias='aml_id')
            ids_query.select_field('debit')
            ids_query.select_field('credit')
            ids_query.order_by('date', 'ASC')
            ids_query.order_by('id', 'ASC')
            ids_query.limit(offset)
            prefix_rows = ids_query.execute()
            total = 0.0
            for r in prefix_rows:
                total += float(r.get('debit') or 0.0) - float(
                    r.get('credit') or 0.0)
            return round(total, 2)
        except Exception:  # pragma: no cover - defensive
            return 0.0

    @api.model
    def get_drilldown_action(self, options, line_id):
        """Override: aml-X opens the specific journal entry form. Other ids
        fall through to the base account drill down (account-N).
        """
        if line_id and line_id.startswith('aml-'):
            try:
                aml_id = int(line_id.split('-', 1)[1])
            except ValueError:
                return None
            aml = self.env['account.move.line'].browse(aml_id).exists()
            if not aml:
                return None
            return {
                'type': 'ir.actions.act_window',
                'name': _("Journal Entry"),
                'res_model': 'account.move',
                'res_id': aml.move_id.id,
                'view_mode': 'form',
                'views': [(False, 'form')],
            }
        return super().get_drilldown_action(options, line_id)

    # ---- internal helpers ----

    def _build_columns(self):
        return [
            {'expression_label': 'description', 'name': _("Description"),
             'figure_type': 'string'},
            {'expression_label': 'date', 'name': _("Date"),
             'figure_type': 'string'},
            {'expression_label': 'journal', 'name': _("Journal"),
             'figure_type': 'string'},
            {'expression_label': 'move', 'name': _("Move"),
             'figure_type': 'string'},
            {'expression_label': 'partner', 'name': _("Partner"),
             'figure_type': 'string'},
            {'expression_label': 'label', 'name': _("Label"),
             'figure_type': 'string'},
            {'expression_label': 'debit', 'name': _("Debit"),
             'figure_type': 'monetary'},
            {'expression_label': 'credit', 'name': _("Credit"),
             'figure_type': 'monetary'},
            {'expression_label': 'balance', 'name': _("Balance"),
             'figure_type': 'monetary'},
            {'expression_label': 'foreign', 'name': _("Amount in Currency"),
             'figure_type': 'string'},
        ]

    def _fetch_opening_balances(
        self, company_ids, date_from, posted_only, options,
    ):
        """Return a dict {account_id: opening_balance, ...}, fiscal-year aware.

        Opening treatment (WS4):

        * Balance-sheet accounts (internal_group NOT income/expense AND not
          equity_unaffected) keep the legacy rule: the sum of every line
          strictly before date_from. These accounts carry their balance
          forward across fiscal years.
        * Income / expense accounts reset at each fiscal-year boundary, so
          their opening is ONLY current-fiscal-year-to-date movement, i.e.
          lines dated >= fiscal_year_start AND < date_from. Prior-year P&L
          (date < fiscal_year_start) is excluded from the per-account
          opening and instead rolled, per company, into that company's
          unaffected-earnings (equity_unaffected) account opening, surfaced
          as a single Current Year Earnings opening row. This is the
          conventional ledger treatment and is what makes a GL run from the
          first day of a fiscal year show P&L accounts opening at zero.

        Multi-company consolidation converts each company's balance into the
        presentation currency via a CurrencyTable before summing; the
        single-currency hot path is unaffected (monocurrency emits no join).

        FALLBACK: if the fiscal-year split cannot be built (e.g. no company
        rows), the method degrades to the legacy "all lines before date_from"
        opening so a GL still renders rather than raising.
        """
        currency_table = self._resolve_currency_table(options, company_ids)

        # Per-company fiscal-year start, bound as a CASE on aml.company_id so
        # the whole opening stays one SQL pass even when companies have
        # different fiscal calendars.
        fy_starts = self._fiscalyear_starts(company_ids, date_from)
        fy_case = self._fiscalyear_start_case(fy_starts, date_from)
        unaffected_by_company = self._unaffected_account_ids(company_ids)

        # Predicate identifying a P&L (income/expense) account that resets at
        # the fiscal-year boundary. Mirrors core include_initial_balance:
        # internal_group in (income, expense). account_type values for those
        # groups are exactly the ones prefixed income*/expense*.
        pl_predicate = SQL(
            "(acc.account_type LIKE %s OR acc.account_type LIKE %s)",
            'income%', 'expense%',
        )

        try:
            return self._fetch_opening_balances_fy_aware(
                company_ids, date_from, posted_only, options,
                currency_table, fy_case, pl_predicate, unaffected_by_company,
            )
        except Exception:  # pragma: no cover - degrade to legacy opening
            return self._fetch_opening_balances_legacy(
                company_ids, date_from, posted_only, options, currency_table,
            )

    def _fetch_opening_balances_fy_aware(
        self, company_ids, date_from, posted_only, options,
        currency_table, fy_case, pl_predicate, unaffected_by_company,
    ):
        """Fiscal-year-aware opening: per-account + rolled unaffected earnings.

        Two contributions in one scan via conditional aggregation:

        * per-account opening: balance-sheet accounts contribute everything
          before date_from; P&L accounts contribute only current-FY-to-date.
        * prior-year P&L roll-up: P&L lines dated before the company's
          fiscal-year start, summed per company, to be reclassified onto the
          unaffected-earnings account.
        """
        balance = currency_table is not None and not currency_table.is_monocurrency
        # Converted (or raw) balance expression reused in both CASE sums.
        bal_sql = (
            SQL("(aml.balance) * %s", currency_table.rate_expr())
            if balance else SQL("aml.balance")
        )

        query = MoveLineQuery(
            self.env, company_ids=company_ids, currency_table=currency_table)
        query.where_raw(SQL("aml.date < %s", date_from))
        if posted_only:
            query.where_posted_only()
        self.apply_common_filters(query, options)
        query.join_account()

        query.select_field('company_id')
        query.select_field('account_id')
        # Per-account opening: P&L accounts only current-FY-to-date; others
        # all-time-before-date_from.
        query.select(
            SQL(
                "SUM(CASE WHEN %s THEN "
                "(CASE WHEN aml.date >= (%s) THEN (%s) ELSE 0 END) "
                "ELSE (%s) END)",
                pl_predicate, fy_case, bal_sql, bal_sql,
            ),
            'account_opening',
        )
        # Prior-year P&L (before fiscal-year start) rolled to unaffected.
        query.select(
            SQL(
                "SUM(CASE WHEN %s AND aml.date < (%s) THEN (%s) ELSE 0 END)",
                pl_predicate, fy_case, bal_sql,
            ),
            'prior_pl',
        )
        query.group_by(SQL("aml.company_id"))
        query.group_by(SQL("aml.account_id"))

        rows = query.execute()

        openings = {}
        unaffected_roll = {}
        for r in rows:
            account_id = r['account_id']
            company_id = r['company_id']
            account_opening = float(r['account_opening'] or 0.0)
            if account_opening:
                openings[account_id] = (
                    openings.get(account_id, 0.0) + account_opening)
            prior = float(r['prior_pl'] or 0.0)
            if prior:
                unaffected_roll[company_id] = (
                    unaffected_roll.get(company_id, 0.0) + prior)

        # Reclassify each company's prior-year P&L onto its unaffected
        # earnings account so the GL shows one Current Year Earnings opening.
        for company_id, amount in unaffected_roll.items():
            if not amount:
                continue
            unaffected_id = unaffected_by_company.get(company_id)
            if not unaffected_id:
                continue
            openings[unaffected_id] = openings.get(unaffected_id, 0.0) + amount

        return {k: v for k, v in openings.items()}

    def _fetch_opening_balances_legacy(
        self, company_ids, date_from, posted_only, options,
        currency_table=None,
    ):
        """Legacy opening: SUM(balance) for every line before date_from.

        The pre-WS4 behaviour, retained as the defensive fallback and used
        verbatim when the fiscal-year split cannot be constructed. Threads
        the currency table so consolidation still converts; with no/mono
        table it is byte-identical to the original query.
        """
        query = MoveLineQuery(
            self.env, company_ids=company_ids, currency_table=currency_table)
        query.where_raw(SQL("aml.date < %s", date_from))
        if posted_only:
            query.where_posted_only()
        self.apply_common_filters(query, options)

        query.select_field('account_id')
        query.select_balance_sum_converted('opening_balance')
        query.group_by(SQL("aml.account_id"))

        rows = query.execute()
        return {
            r['account_id']: float(r['opening_balance'] or 0.0)
            for r in rows
        }

    def _unaffected_account_ids(self, company_ids):
        """Map {company_id: unaffected_earnings_account_id}.

        Resolves (auto-provisioning if needed) each company's
        equity_unaffected account so prior-year P&L can be reclassified onto
        a real, drillable account id. Tolerant of failure per company.
        """
        result = {}
        companies = self.env['res.company'].sudo().browse([
            int(c) for c in (company_ids or [])
        ])
        for company in companies:
            try:
                account = company.get_unaffected_earnings_account()
                if account:
                    result[company.id] = account.id
            except Exception:  # pragma: no cover - defensive
                continue
        return result

    def _fetch_line_entries(
        self, company_ids, date_from, date_to, posted_only, options,
    ):
        """Return a list of per line dicts ordered by account code, date,
        aml.id, with all the columns the report needs.

        Row count is capped to keep the payload bounded. The cap is
        enforced in two layers: a count-only query first (cheap, uses
        the index) tells us whether we are about to materialise too
        much data, and the SQL LIMIT on the data query is the
        belt-and-braces protection. When the count exceeds the
        requested limit we raise a UserError pointing the user at
        narrowing filters; we do not silently truncate, because a
        truncated GL is a subtly wrong financial document.
        """
        row_limit = self._resolve_row_limit(options)
        # Count-only pre-flight: same filters, single SUM, cheap.
        count_query = self._base_query(
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        count_query.select(SQL("COUNT(aml.id)"), 'row_count')
        count_rows = count_query.execute()
        total_rows = int(count_rows[0]['row_count']) if count_rows else 0
        if total_rows > row_limit:
            raise UserError(_(
                "General Ledger would return %(total)s journal items, "
                "exceeding the cap of %(limit)s. Narrow the date range "
                "or add account / partner filters, or pass options."
                "row_limit up to %(ceiling)s for an explicit higher cap.",
                total=total_rows,
                limit=row_limit,
                ceiling=_ABSOLUTE_ROW_LIMIT,
            ))

        query = self._base_query(
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        query.select_field('id', alias='aml_id')
        query.select_field('account_id')
        query.select_field('partner_id')
        query.select_field('date')
        query.select_field('debit')
        query.select_field('credit')
        query.select_field('balance')
        query.select_field('amount_currency')
        query.select_field('currency_id')
        query.select_field('name', alias='line_label')
        query.select_field('ref')
        query.select_account_field('code', alias='account_code')
        query.select_account_field('name', alias='account_name')
        query.join_journal()
        query.select(SQL("aj.code"), 'journal_code')
        query.join_partner()
        query.select(SQL("p.name"), 'partner_name')
        query.select(SQL("am.name"), 'move_name')

        query.order_by_account_field('code', 'ASC')
        query.order_by('date', 'ASC')
        query.order_by('id', 'ASC')
        query.limit(row_limit)

        return query.execute()

    def _base_query(
        self, company_ids, date_from, date_to, posted_only, options,
    ):
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_date_range(date_from=date_from, date_to=date_to)
        if posted_only:
            query.where_posted_only()
        self.apply_common_filters(query, options)
        return query

    @api.model
    def _resolve_row_limit(self, options):
        # Soft cap is the per-company setting; the absolute ceiling is a
        # hard constant the operator cannot override.
        soft_cap = self.env.company.eh_gl_row_limit or 10000
        requested = options.get('row_limit')
        if requested is None:
            return soft_cap
        try:
            requested = int(requested)
        except (TypeError, ValueError):
            return soft_cap
        if requested <= 0:
            return soft_cap
        return min(requested, _ABSOLUTE_ROW_LIMIT)

    def _build_lines(self, opening_by_account, entries, show_zero,
                     currency_codes=None):
        """Compose the hierarchical line list.

        Account ordering: defined by the entry stream (which is ordered by
        account code). Accounts that have only an opening balance and no
        period entries are still rendered (sorted by account_id; their
        position is best effort since we do not have their codes from the
        opening query). show_zero forces accounts with neither activity nor
        opening to appear, but in practice the GL only knows about accounts
        that have at least one move line, so the flag is informational.
        """
        # Group entries by account, preserving order.
        entries_by_account = {}
        account_meta = {}
        for entry in entries:
            entries_by_account.setdefault(entry['account_id'], []).append(entry)
            account_meta.setdefault(entry['account_id'], {
                'code': entry['account_code'],
                'name': entry['account_name'],
            })

        # Accounts that have no entries but do have an opening balance still
        # deserve a header + initial balance + total. We do not have their
        # codes from the opening query alone; fetch lazily.
        accounts_with_opening_only = (
            set(opening_by_account.keys()) - set(entries_by_account.keys())
        )
        if accounts_with_opening_only:
            for acc in self.env['account.account'].browse(
                list(accounts_with_opening_only),
            ).sorted('code'):
                account_meta[acc.id] = {
                    'code': acc.code or '',
                    'name': acc.name or '',
                }

        # Build the account ordering: entries first (in their order), then
        # opening only accounts (sorted by code).
        ordered_account_ids = list(entries_by_account.keys())
        ordered_account_ids += sorted(
            accounts_with_opening_only,
            key=lambda i: account_meta[i]['code'],
        )

        lines = []
        for account_id in ordered_account_ids:
            opening = round(opening_by_account.get(account_id, 0.0), 2)
            account_entries = entries_by_account.get(account_id, [])
            meta = account_meta[account_id]

            if not show_zero and opening == 0.0 and not account_entries:
                continue

            lines.append(self._account_header_line(account_id, meta))
            lines.append(self._opening_line(account_id, opening))

            running = opening
            for entry in account_entries:
                debit = round(float(entry.get('debit') or 0.0), 2)
                credit = round(float(entry.get('credit') or 0.0), 2)
                running = round(running + debit - credit, 2)
                lines.append(self._entry_line(
                    entry, debit, credit, running, currency_codes or {}))

            lines.append(self._account_total_line(account_id, meta, running))
        return lines

    def _account_header_line(self, account_id, meta):
        return {
            'id': "account-%s" % account_id,
            'name': "%s %s" % (meta['code'], meta['name']),
            'level': 0,
            'columns': [
                {'expression_label': 'date', 'value': ''},
                {'expression_label': 'journal', 'value': ''},
                {'expression_label': 'move', 'value': ''},
                {'expression_label': 'partner', 'value': ''},
                {'expression_label': 'label', 'value': ''},
                {'expression_label': 'debit', 'value': ''},
                {'expression_label': 'credit', 'value': ''},
                {'expression_label': 'balance', 'value': ''},
                {'expression_label': 'foreign', 'value': ''},
            ],
            'unfoldable': False,
            'meta': {
                'kind': 'account_header',
                'account_id': account_id,
                'account_code': meta['code'],
            },
        }

    def _opening_line(self, account_id, opening):
        return {
            'id': "account-%s-opening" % account_id,
            'name': _("Initial Balance"),
            'level': 1,
            'columns': [
                {'expression_label': 'date', 'value': ''},
                {'expression_label': 'journal', 'value': ''},
                {'expression_label': 'move', 'value': ''},
                {'expression_label': 'partner', 'value': ''},
                {'expression_label': 'label', 'value': ''},
                {'expression_label': 'debit', 'value': ''},
                {'expression_label': 'credit', 'value': ''},
                {'expression_label': 'balance', 'value': opening},
                {'expression_label': 'foreign', 'value': ''},
            ],
            'unfoldable': False,
            'meta': {'kind': 'opening_balance', 'account_id': account_id},
        }

    def _entry_line(self, entry, debit, credit, running_balance,
                    currency_codes=None):
        currency_codes = currency_codes or {}
        foreign = ''
        cur_id = entry.get('currency_id')
        amount_currency = entry.get('amount_currency')
        if cur_id and cur_id in currency_codes and amount_currency:
            foreign = "%.2f %s" % (
                round(float(amount_currency), 2), currency_codes[cur_id])
        return {
            'id': "aml-%s" % entry['aml_id'],
            'name': entry.get('ref') or entry.get('line_label') or '',
            'level': 1,
            'columns': [
                {'expression_label': 'date',
                 'value': self._iso_date(entry['date']) if entry.get('date') else None},
                {'expression_label': 'journal',
                 'value': entry.get('journal_code') or ''},
                {'expression_label': 'move',
                 'value': entry.get('move_name') or ''},
                {'expression_label': 'partner',
                 'value': entry.get('partner_name') or ''},
                {'expression_label': 'label',
                 'value': entry.get('line_label') or ''},
                {'expression_label': 'debit', 'value': debit},
                {'expression_label': 'credit', 'value': credit},
                {'expression_label': 'balance', 'value': running_balance},
                {'expression_label': 'foreign', 'value': foreign},
            ],
            'unfoldable': False,
            'meta': {
                'kind': 'aml',
                'aml_id': entry['aml_id'],
                'account_id': entry['account_id'],
            },
        }

    def _account_total_line(self, account_id, meta, closing):
        return {
            'id': "account-%s-total" % account_id,
            'name': _("Total %(code)s %(name)s") % {
                'code': meta['code'], 'name': meta['name'],
            },
            'level': 0,
            'columns': [
                {'expression_label': 'date', 'value': ''},
                {'expression_label': 'journal', 'value': ''},
                {'expression_label': 'move', 'value': ''},
                {'expression_label': 'partner', 'value': ''},
                {'expression_label': 'label', 'value': ''},
                {'expression_label': 'debit', 'value': ''},
                {'expression_label': 'credit', 'value': ''},
                {'expression_label': 'balance', 'value': closing},
                {'expression_label': 'foreign', 'value': ''},
            ],
            'unfoldable': False,
            'meta': {'kind': 'account_total', 'account_id': account_id},
        }
