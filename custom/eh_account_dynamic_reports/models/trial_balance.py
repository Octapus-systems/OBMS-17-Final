# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Trial Balance handler.

Single SQL pass. Conditional aggregation splits the same set of journal
lines into three buckets per account in one scan:

* opening_balance: lines whose date is strictly before date_from.
* period_debit and period_credit: lines whose date falls within
  [date_from, date_to].

Closing balance is then derived in Python as
opening_balance + period_debit - period_credit. The split into debit and
credit display columns happens at presentation time based on the sign of
the underlying balance, which matches accounting convention.

The handler honours all standard filters (companies, journals, partners,
accounts, posted_only, show_zero) by composing them through MoveLineQuery,
so SQL safety, multi company scoping, and cancelled exclusion are inherited
automatically.
"""

from odoo import _, api, fields, models
from odoo.tools import SQL
from odoo.tools.translate import _lt

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery



class EhTrialBalanceHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.trial_balance'
    _inherit = 'eh.account.dynamic.report.handler'
    _description = "Trial Balance report handler"

    REPORT_CODE = 'trial_balance'
    REPORT_NAME = _lt("Trial Balance")

    @api.model
    def compute(self, options):
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        show_zero = bool(options.get('show_zero', False))
        hierarchical = bool(options.get('hierarchical_groups', True))
        unfolded_ids = set(options.get('unfolded_lines') or [])

        # Multi-currency consolidation table (WS4). Monocurrency /
        # single-company yields a no-op table, so the figures are unchanged.
        currency_table = self._resolve_currency_table(options, company_ids)

        rows = self._fetch_account_buckets(
            company_ids=company_ids,
            date_from=date_from,
            date_to=date_to,
            posted_only=posted_only,
            options=options,
            currency_table=currency_table,
        )

        # Prior-year P&L rolled into a synthetic unaffected-earnings line so
        # the TB foots at a fiscal-year boundary once P&L openings reset.
        unaffected = self._fetch_unaffected_earnings(
            company_ids=company_ids, date_from=date_from,
            posted_only=posted_only, options=options,
            currency_table=currency_table,
        )

        if hierarchical:
            lines, totals = self._build_hierarchical_lines_and_totals(
                rows, show_zero, unfolded_ids, options=options,
                unaffected=unaffected,
            )
        else:
            lines, totals = self._build_lines_and_totals(
                rows, show_zero, options=options, unaffected=unaffected)

        return {
            'columns': self._build_columns(),
            'lines': lines,
            'totals': totals,
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': {
                'report_code': self.REPORT_CODE,
                'date_from': self._iso_date(date_from),
                'date_to': self._iso_date(date_to),
                'company_ids': sorted(int(c) for c in company_ids),
                'posted_only': posted_only,
                'show_zero': show_zero,
                'hierarchical_groups': hierarchical,
                **self._currency_meta(currency_table),
            },
        }

    @api.model
    def _currency_meta(self, currency_table):
        """Disclose currency-conversion posture in the payload meta.

        Reports whether the run consolidated across currencies and, if any
        company's rate had to be defaulted, surfaces those flags so the
        figure is never silently approximate. Defensive: any failure yields
        an empty dict rather than breaking the payload.
        """
        if currency_table is None:
            return {}
        try:
            meta = {'multi_currency': not currency_table.is_monocurrency}
            if not currency_table.is_monocurrency:
                meta['presentation_currency_id'] = (
                    currency_table.presentation_currency_id)
                if currency_table.fallback_flags:
                    meta['currency_rate_fallbacks'] = (
                        currency_table.fallback_flags)
            return meta
        except Exception:  # pragma: no cover - defensive
            return {}

    # Drill down behaviour comes from the base handler; the default impl
    # already opens filtered journal items for any line whose id is
    # 'account-N'. Trial Balance follows that scheme exactly.

    # ---- internal helpers ----

    def _build_columns(self):
        return [
            {'expression_label': 'account', 'name': _("Account"),
             'figure_type': 'string'},
            {'expression_label': 'opening_debit', 'name': _("Opening DB"),
             'figure_type': 'monetary'},
            {'expression_label': 'opening_credit', 'name': _("Opening CR"),
             'figure_type': 'monetary'},
            {'expression_label': 'period_debit', 'name': _("Movement DB"),
             'figure_type': 'monetary'},
            {'expression_label': 'period_credit', 'name': _("Movement CR"),
             'figure_type': 'monetary'},
            {'expression_label': 'closing_debit', 'name': _("Closing DB"),
             'figure_type': 'monetary'},
            {'expression_label': 'closing_credit', 'name': _("Closing CR"),
             'figure_type': 'monetary'},
        ]

    def _fetch_account_buckets(
        self, company_ids, date_from, date_to, posted_only, options,
        currency_table=None,
    ):
        query = MoveLineQuery(
            self.env, company_ids=company_ids, currency_table=currency_table)
        # Lines up to date_to participate. We need everything before
        # date_from for the opening balance plus everything in the period
        # for the movement columns.
        query.where_date_range(date_to=date_to)
        if posted_only:
            query.where_posted_only()
        self.apply_common_filters(query, options)
        query.join_account()

        # Fiscal-year-aware opening (WS4). P&L (income/expense) accounts reset
        # at the fiscal-year boundary, so their opening is only
        # current-FY-to-date (>= fiscal_year_start AND < date_from); their
        # prior-year movement is rolled into the unaffected-earnings line
        # (_fetch_unaffected_earnings). Balance-sheet accounts keep the
        # legacy all-time-before-date_from rule. Per-company fiscal-year start
        # is bound as a CASE so it stays one SQL pass across mixed calendars.
        fy_starts = self._fiscalyear_starts(company_ids, date_from)
        fy_case = self._fiscalyear_start_case(fy_starts, date_from)
        pl_predicate = SQL(
            "(acc.account_type LIKE %s OR acc.account_type LIKE %s)",
            'income%', 'expense%',
        )
        convert = (
            currency_table is not None and not currency_table.is_monocurrency)
        bal_sql = (
            SQL("(aml.balance) * %s", currency_table.rate_expr())
            if convert else SQL("aml.balance")
        )
        debit_sql = (
            SQL("(aml.debit) * %s", currency_table.rate_expr())
            if convert else SQL("aml.debit")
        )
        credit_sql = (
            SQL("(aml.credit) * %s", currency_table.rate_expr())
            if convert else SQL("aml.credit")
        )

        query.select_field('account_id')
        query.select_account_field('code', alias='account_code')
        query.select_account_field('name', alias='account_name')
        query.select(
            SQL(
                "SUM(CASE "
                "WHEN %s THEN "
                "(CASE WHEN aml.date >= (%s) AND aml.date < %s "
                "THEN (%s) ELSE 0 END) "
                "ELSE (CASE WHEN aml.date < %s THEN (%s) ELSE 0 END) END)",
                pl_predicate, fy_case, date_from, bal_sql,
                date_from, bal_sql,
            ),
            'opening_balance',
        )
        query.select(
            SQL(
                "SUM(CASE WHEN aml.date >= %s AND aml.date <= %s "
                "THEN (%s) ELSE 0 END)",
                date_from, date_to, debit_sql,
            ),
            'period_debit',
        )
        query.select(
            SQL(
                "SUM(CASE WHEN aml.date >= %s AND aml.date <= %s "
                "THEN (%s) ELSE 0 END)",
                date_from, date_to, credit_sql,
            ),
            'period_credit',
        )
        # Group by all non aggregated SELECT expressions for portability.
        # The account_code and account_name expressions are resolved via the
        # MoveLineQuery helpers so GROUP BY matches SELECT verbatim, including
        # the per-language COALESCE branch on translated jsonb columns. A
        # mismatch here (e.g. hardcoding ``en_US`` in GROUP BY while SELECT
        # uses COALESCE for a non-en_US env.lang) causes PostgreSQL to
        # reject the query with "must appear in the GROUP BY clause", which
        # surfaces to the user as a generic "Odoo Server Error" with no
        # actionable detail. Keep the three call sites symmetric.
        query.group_by(SQL("aml.account_id"))
        query.group_by_account_field('code')
        query.group_by_account_field('name')
        query.order_by_account_field('code', 'ASC')
        return query.execute()

    def _fetch_unaffected_earnings(
        self, company_ids, date_from, posted_only, options,
        currency_table=None,
    ):
        """Net prior-year P&L to roll into the unaffected-earnings line.

        Single SUM (no GROUP BY, the cheapest possible query) over every
        income/expense line dated strictly before each company's fiscal-year
        start. Returns the signed net balance (positive = net credit = profit
        in balance terms, since income posts as a credit -> negative balance,
        so a profit yields a negative sum here, surfaced on the credit side
        by the line builder).

        Per-company fiscal-year start is honoured via the same CASE the
        opening uses, so a mid-year date_from still rolls exactly the
        prior-year portion. Multi-currency converts before summing.

        FALLBACK: returns 0.0 on any failure, which simply omits the
        unaffected line (the TB then behaves as pre-WS4 for that run).
        """
        try:
            fy_starts = self._fiscalyear_starts(company_ids, date_from)
            fy_case = self._fiscalyear_start_case(fy_starts, date_from)
            convert = (
                currency_table is not None
                and not currency_table.is_monocurrency)
            bal_sql = (
                SQL("(aml.balance) * %s", currency_table.rate_expr())
                if convert else SQL("aml.balance")
            )
            query = MoveLineQuery(
                self.env, company_ids=company_ids,
                currency_table=currency_table)
            query.where_date_range(date_to=date_from)
            if posted_only:
                query.where_posted_only()
            self.apply_common_filters(query, options)
            query.join_account()
            query.select(
                SQL(
                    "SUM(CASE WHEN "
                    "(acc.account_type LIKE %s "
                    "OR acc.account_type LIKE %s) "
                    "AND aml.date < (%s) THEN (%s) ELSE 0 END)",
                    'income%', 'expense%', fy_case, bal_sql,
                ),
                'unaffected_balance',
            )
            rows = query.execute()
            if not rows:
                return 0.0
            return float(rows[0].get('unaffected_balance') or 0.0)
        except Exception:  # pragma: no cover - defensive
            return 0.0

    def _unaffected_earnings_line(self, value, level=1, parent_id=None):
        """Synthetic Current Year Earnings (unallocated) row.

        Carries rolled-up prior-year P&L into opening_debit / opening_credit
        (and the same into closing, since it has no period movement of its
        own), placed under equity so the trial balance foots once P&L
        openings reset. ``value`` is the net prior-year balance: a credit
        (negative balance => net profit) lands on the credit side, a debit
        (net loss) on the debit side, matching accounting convention.
        """
        opening = round(float(value or 0.0), 2)
        opening_debit = opening if opening > 0 else 0.0
        opening_credit = -opening if opening < 0 else 0.0
        line = {
            'id': 'account-unaffected-earnings',
            'name': _("Current Year Earnings (unallocated)"),
            'level': level,
            'columns': [
                {'expression_label': 'opening_debit',
                 'value': round(opening_debit, 2)},
                {'expression_label': 'opening_credit',
                 'value': round(opening_credit, 2)},
                {'expression_label': 'period_debit', 'value': 0.0},
                {'expression_label': 'period_credit', 'value': 0.0},
                {'expression_label': 'closing_debit',
                 'value': round(opening_debit, 2)},
                {'expression_label': 'closing_credit',
                 'value': round(opening_credit, 2)},
            ],
            'unfoldable': False,
            'meta': {
                'kind': 'unaffected_earnings',
                'account_id': None,
                # account_code present (empty) so the common line-indexing
                # helpers that key on meta['account_code'] never KeyError on
                # this synthetic row.
                'account_code': '',
            },
        }
        if parent_id:
            line['parent_id'] = parent_id
        return line

    @staticmethod
    def _unaffected_contribution(value):
        """Six-column contribution of the unaffected line, for totals.

        Returns a dict matching the per-account column tuple so both the
        flat and hierarchical total roll-ups can add the unaffected line
        without duplicating sign logic.
        """
        opening = round(float(value or 0.0), 2)
        opening_debit = opening if opening > 0 else 0.0
        opening_credit = -opening if opening < 0 else 0.0
        return {
            'opening_debit': opening_debit,
            'opening_credit': opening_credit,
            'period_debit': 0.0,
            'period_credit': 0.0,
            'closing_debit': opening_debit,
            'closing_credit': opening_credit,
        }

    @api.model
    def _expand_child_columns(self, options, aml_row):
        """Trial-Balance override: one aml fills the movement columns.

        A single journal item is a period movement, so its debit / credit
        land in period_debit / period_credit; it has no opening or closing
        balance of its own, so those columns are blanked. The page of
        children therefore sums to the account's period_debit /
        period_credit cell (the reconciliation invariant).
        """
        debit = round(float(aml_row.get('debit') or 0.0), 2)
        credit = round(float(aml_row.get('credit') or 0.0), 2)
        date_val = aml_row.get('date')
        return [{
            'id': "aml-%s" % aml_row.get('aml_id'),
            'name': aml_row.get('ref') or aml_row.get('line_label') or '',
            'level': 2,
            'columns': [
                {'expression_label': 'opening_debit', 'value': ''},
                {'expression_label': 'opening_credit', 'value': ''},
                {'expression_label': 'period_debit', 'value': debit},
                {'expression_label': 'period_credit', 'value': credit},
                {'expression_label': 'closing_debit', 'value': ''},
                {'expression_label': 'closing_credit', 'value': ''},
            ],
            'unfoldable': False,
            'unfolded': False,
            'lazy': False,
            'meta': {
                'kind': 'aml',
                'aml_id': aml_row.get('aml_id'),
                'account_id': aml_row.get('account_id'),
                'date': self._iso_date(date_val) if date_val else None,
                'move': aml_row.get('move_name') or '',
                'partner': aml_row.get('partner_name') or '',
            },
        }]

    def _build_lines_and_totals(self, rows, show_zero, options=None,
                                unaffected=0.0):
        lines = []
        totals = {
            'opening_debit': 0.0, 'opening_credit': 0.0,
            'period_debit': 0.0, 'period_credit': 0.0,
            'closing_debit': 0.0, 'closing_credit': 0.0,
        }
        for row in rows:
            opening = float(row.get('opening_balance') or 0.0)
            period_debit = float(row.get('period_debit') or 0.0)
            period_credit = float(row.get('period_credit') or 0.0)
            closing = opening + period_debit - period_credit

            opening_debit = opening if opening > 0 else 0.0
            opening_credit = -opening if opening < 0 else 0.0
            closing_debit = closing if closing > 0 else 0.0
            closing_credit = -closing if closing < 0 else 0.0

            sum_all = (opening_debit + opening_credit + period_debit
                       + period_credit + closing_debit + closing_credit)
            if not show_zero and sum_all == 0.0:
                continue

            leaf = {
                'id': "account-%s" % row['account_id'],
                'name': "%s %s" % (row['account_code'], row['account_name']),
                'level': 1,
                'columns': [
                    {'expression_label': 'opening_debit',
                     'value': round(opening_debit, 2)},
                    {'expression_label': 'opening_credit',
                     'value': round(opening_credit, 2)},
                    {'expression_label': 'period_debit',
                     'value': round(period_debit, 2)},
                    {'expression_label': 'period_credit',
                     'value': round(period_credit, 2)},
                    {'expression_label': 'closing_debit',
                     'value': round(closing_debit, 2)},
                    {'expression_label': 'closing_credit',
                     'value': round(closing_credit, 2)},
                ],
                'unfoldable': False,
                'meta': {
                    'account_id': row['account_id'],
                    'account_code': row['account_code'],
                },
            }
            if options is not None:
                self._eh_apply_leaf_lazy_flags(leaf, options)
            lines.append(leaf)

            totals['opening_debit'] += opening_debit
            totals['opening_credit'] += opening_credit
            totals['period_debit'] += period_debit
            totals['period_credit'] += period_credit
            totals['closing_debit'] += closing_debit
            totals['closing_credit'] += closing_credit

        # Append the unaffected-earnings (Current Year Earnings) line so the
        # TB foots once P&L openings have been reset to the fiscal year.
        if round(float(unaffected or 0.0), 2) != 0.0:
            lines.append(self._unaffected_earnings_line(unaffected))
            contrib = self._unaffected_contribution(unaffected)
            for k in totals:
                totals[k] += contrib[k]

        totals = {k: round(v, 2) for k, v in totals.items()}
        return lines, totals

    def _build_hierarchical_lines_and_totals(
        self, rows, show_zero, unfolded_ids, options=None, unaffected=0.0,
    ):
        """Multi-column hierarchical builder for Trial Balance.

        Computes the per-account 6-column tuple in Python first
        (opening_db / cr, period_db / cr, closing_db / cr), then walks
        account.account.group_id and account.group.parent_id to build
        the nested line list with parent_id linkage. Group totals are
        the sum of every descendant account's column tuple.

        Mirrors the shape of _render_account_lines_grouped on the
        sectioned base but is multi-column-aware and lives here
        because Trial Balance is the only multi-column report whose
        grouping aggregates across columns.
        """
        if not rows:
            return self._hierarchical_only_unaffected(unaffected)
        # Per-account computed columns (six values each).
        per_account = {}
        for row in rows:
            opening = float(row.get('opening_balance') or 0.0)
            period_debit = float(row.get('period_debit') or 0.0)
            period_credit = float(row.get('period_credit') or 0.0)
            closing = opening + period_debit - period_credit
            opening_debit = opening if opening > 0 else 0.0
            opening_credit = -opening if opening < 0 else 0.0
            closing_debit = closing if closing > 0 else 0.0
            closing_credit = -closing if closing < 0 else 0.0
            sum_all = (
                opening_debit + opening_credit
                + period_debit + period_credit
                + closing_debit + closing_credit
            )
            if not show_zero and sum_all == 0.0:
                continue
            per_account[row['account_id']] = {
                'code': row['account_code'],
                'name': row['account_name'],
                'opening_debit': opening_debit,
                'opening_credit': opening_credit,
                'period_debit': period_debit,
                'period_credit': period_credit,
                'closing_debit': closing_debit,
                'closing_credit': closing_credit,
            }
        if not per_account:
            return self._hierarchical_only_unaffected(unaffected)

        # Resolve group path per account. Reuses the same logic as
        # the sectioned helper.
        Account = self.env['account.account'].sudo()
        Group = self.env['account.group'].sudo()
        accounts = Account.browse(list(per_account.keys()))
        group_paths = {}
        for acc in accounts:
            chain = []
            grp = acc.group_id
            while grp:
                chain.append(grp.id)
                grp = grp.parent_id
            chain.reverse()
            group_paths[acc.id] = tuple(chain)

        # Aggregate per group (every prefix of every account's path).
        zero = {
            'opening_debit': 0.0, 'opening_credit': 0.0,
            'period_debit': 0.0, 'period_credit': 0.0,
            'closing_debit': 0.0, 'closing_credit': 0.0,
        }
        group_totals = {}
        accounts_by_group = {}
        for acc_id, vals in per_account.items():
            path = group_paths[acc_id]
            cumulative = ()
            for gid in path:
                cumulative = cumulative + (gid,)
                bucket = group_totals.setdefault(cumulative, dict(zero))
                for k in zero:
                    bucket[k] += vals[k]
            accounts_by_group.setdefault(path, []).append(acc_id)

        # Path identifiers and ordering.
        section_id = 'trial_balance'

        def _line_id_for_path(path_tuple):
            if not path_tuple:
                return "section-%s-header" % section_id
            return "section-%s-group-%s" % (
                section_id,
                "_".join(str(g) for g in path_tuple),
            )

        all_paths = set()
        for parent_path in accounts_by_group:
            cumulative = ()
            for g in parent_path:
                cumulative = cumulative + (g,)
                all_paths.add(cumulative)

        def _path_sort_key(p):
            keys = []
            for gid in p:
                grp = Group.browse(gid)
                keys.append((grp.code_prefix_start or '', gid))
            return keys
        ordered_paths = sorted(all_paths, key=_path_sort_key)

        lines = []
        # Ungrouped accounts attach directly under a synthetic header
        # at the top, matching the sectioned helper's behaviour.
        ungrouped = accounts_by_group.get((), [])
        if ungrouped:
            ungrouped.sort(key=lambda a: per_account[a]['code'] or '')
            for aid in ungrouped:
                vals = per_account[aid]
                lines.append(self._tb_account_line(
                    aid, vals, level=1, parent_id=None, options=options))

        for path in ordered_paths:
            grp = Group.browse(path[-1])
            depth = len(path)
            parent_id = _line_id_for_path(path[:-1]) if len(path) > 1 else None
            this_id = _line_id_for_path(path)
            unfolded = (not unfolded_ids) or this_id in unfolded_ids
            totals_at_path = group_totals[path]
            lines.append({
                'id': this_id,
                'name': "%s %s" % (
                    grp.code_prefix_start or '',
                    grp.display_name or grp.name or '',
                ),
                'level': depth,
                'parent_id': parent_id,
                'columns': [
                    {'expression_label': k,
                     'value': round(totals_at_path[k], 2)}
                    for k in ('opening_debit', 'opening_credit',
                              'period_debit', 'period_credit',
                              'closing_debit', 'closing_credit')
                ],
                'unfoldable': True,
                'unfolded': unfolded,
                'meta': {
                    'kind': 'account_group',
                    'group_id': grp.id,
                    'depth': depth,
                },
            })
            for aid in sorted(
                accounts_by_group.get(path, []),
                key=lambda a: per_account[a]['code'] or '',
            ):
                vals = per_account[aid]
                lines.append(self._tb_account_line(
                    aid, vals, level=depth + 1, parent_id=this_id,
                    options=options,
                ))

        # Top-level totals across every account in the report.
        totals = {k: 0.0 for k in zero}
        for vals in per_account.values():
            for k in totals:
                totals[k] += vals[k]

        # Unaffected-earnings line at the top level so the grand total foots
        # at a fiscal-year boundary. Placed under equity (ungrouped, top
        # level) to keep the hierarchical builder's parenting simple; the
        # contribution is added to the grand totals so flat and hierarchical
        # presentations agree.
        if round(float(unaffected or 0.0), 2) != 0.0:
            lines.append(self._unaffected_earnings_line(unaffected))
            contrib = self._unaffected_contribution(unaffected)
            for k in totals:
                totals[k] += contrib[k]

        totals = {k: round(v, 2) for k, v in totals.items()}
        return lines, totals

    def _hierarchical_only_unaffected(self, unaffected):
        """Return value when there are no account rows but prior-year P&L
        must still surface as the unaffected-earnings line.

        Keeps the empty-report shape (empty lines, zero totals) byte-identical
        when there is nothing to roll, so the existing no-data tests are
        unaffected.
        """
        zero = {
            'opening_debit': 0.0, 'opening_credit': 0.0,
            'period_debit': 0.0, 'period_credit': 0.0,
            'closing_debit': 0.0, 'closing_credit': 0.0,
        }
        if round(float(unaffected or 0.0), 2) == 0.0:
            return [], zero
        line = self._unaffected_earnings_line(unaffected)
        contrib = self._unaffected_contribution(unaffected)
        totals = {k: round(contrib[k], 2) for k in zero}
        return [line], totals

    def _tb_account_line(self, account_id, vals, level, parent_id,
                         options=None):
        line = {
            'id': "account-%s" % account_id,
            'name': "%s %s" % (vals['code'], vals['name']),
            'level': level,
            'columns': [
                {'expression_label': k, 'value': round(vals[k], 2)}
                for k in ('opening_debit', 'opening_credit',
                          'period_debit', 'period_credit',
                          'closing_debit', 'closing_credit')
            ],
            'unfoldable': False,
            'meta': {
                'account_id': account_id,
                'account_code': vals['code'],
            },
        }
        if parent_id:
            line['parent_id'] = parent_id
        if options is not None:
            self._eh_apply_leaf_lazy_flags(line, options)
        return line
