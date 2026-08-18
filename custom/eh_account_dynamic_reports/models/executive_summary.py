# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Executive Summary handler (board-pack KPI / ratio statement).

A single as-of statement that restates the live dashboard numbers into a
printable period summary: profitability, cash and liquidity, and the core
balance-sheet ratios. It reads the same ledger the Profit and Loss, Balance
Sheet and dashboard read, so the figures reconcile to those reports by
construction.

Sign conventions (authored from double-entry first principles):

* Income accounts carry credit balances (negative); revenue and other
  income are flipped with sign=-1 so they present positive.
* Expense accounts carry debit balances (positive); sign=+1.
* Cash and receivable balances are cumulative debit balances up to
  date_to (sign=+1); payables are cumulative credit balances flipped to a
  positive "owed" figure (sign=-1).

Ratio rows mix figure types (monetary, percentage, days). The shared
PDF/XLSX renderer keys figure_type off the COLUMN, not the row, so a single
value column cannot carry mixed types. v1 renders every value as a
pre-formatted string in one column and stashes the raw number in meta for
downstream export; a per-row figure_type column is a scoped follow-up.

Profitability is a period flow (date_from..date_to). Cash, receivables and
payables are point-in-time balances cumulative to date_to. This mirrors
dashboard._compute_period_pl (period flow) and the cumulative balance reads
the Balance Sheet uses, so Executive Summary reconciles to the board.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _lt


class EhExecutiveSummaryHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.executive_summary'
    _inherit = 'eh.account.dynamic.report.handler.sectioned'
    _description = "Executive Summary report handler"

    REPORT_CODE = 'executive_summary'
    REPORT_NAME = _lt("Executive Summary")

    INCOME_TYPES = ('income', 'income_other')
    EXPENSE_TYPES = ('expense', 'expense_depreciation', 'expense_direct_cost')
    # Direct costs / cost of sales drive the gross-margin split. Authored
    # from the standard income-statement layout, not transcribed.
    COST_OF_SALES_TYPES = ('expense_direct_cost',)
    OPERATING_EXPENSE_TYPES = ('expense',)
    CASH_TYPES = ('asset_cash',)
    RECEIVABLE_TYPES = ('asset_receivable',)
    PAYABLE_TYPES = ('liability_payable',)
    CURRENT_ASSET_TYPES = (
        'asset_cash', 'asset_receivable', 'asset_current', 'asset_prepayments',
    )
    CURRENT_LIABILITY_TYPES = (
        'liability_payable', 'liability_current', 'liability_credit_card',
    )
    QUICK_ASSET_TYPES = ('asset_cash', 'asset_receivable', 'asset_current')
    TOTAL_ASSET_TYPES = (
        'asset_cash', 'asset_receivable', 'asset_current',
        'asset_prepayments', 'asset_non_current', 'asset_fixed',
    )

    # ---- ratio helpers ----

    @staticmethod
    def _safe_ratio(numerator, denominator):
        """Divide guarding against a zero/absent denominator.

        Returns None (not 0.0) when the ratio is undefined so the caller
        can render 'n/a' rather than a misleading zero. Never raises
        ZeroDivisionError.
        """
        try:
            if not denominator:
                return None
            return float(numerator) / float(denominator)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    @staticmethod
    def _fmt_money(value):
        return "%0.2f" % round(float(value or 0.0), 2)

    @staticmethod
    def _fmt_ratio(value):
        if value is None:
            return _("n/a")
        return "%0.2f" % value

    @staticmethod
    def _fmt_pct(value):
        if value is None:
            return _("n/a")
        return "%0.1f%%" % (value * 100.0)

    @staticmethod
    def _fmt_days(value):
        if value is None:
            return _("n/a")
        return _("%s days") % ("%0.0f" % value)

    @api.model
    def compute(self, options):
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        comparison = options.get('comparison') or 'none'

        current = self._compute_scalars(
            options=options, company_ids=company_ids,
            date_from=date_from, date_to=date_to, posted_only=posted_only,
        )

        meta = {
            'report_code': self.REPORT_CODE,
            'date_from': self._iso_date(date_from),
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
            'comparison': comparison,
        }

        prior = None
        prior_label = ''
        if comparison and comparison != 'none':
            prior_from, prior_to, prior_label = self._resolve_comparison_dates(
                comparison, date_from, date_to,
            )
            if prior_from and prior_to:
                prior = self._compute_scalars(
                    options=options, company_ids=company_ids,
                    date_from=prior_from, date_to=prior_to,
                    posted_only=posted_only,
                )
                meta['prior_date_from'] = self._iso_date(prior_from)
                meta['prior_date_to'] = self._iso_date(prior_to)
                meta['comparison_label'] = prior_label

        lines = self._build_lines(current, prior)
        columns = self._build_columns(
            current_label=_("%s to %s") % (
                self._iso_date(date_from), self._iso_date(date_to)),
            prior_label=prior_label if prior else None,
        )

        return {
            'columns': columns,
            'lines': lines,
            'totals': {
                'revenue': current['revenue'],
                'net_profit': current['net_profit'],
                'amount': current['net_profit'],
            },
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }

    # ---- column layout ----
    #
    # Built manually (not _build_two_column_layout) because every value cell
    # is a pre-formatted string carrying a mix of monetary / percentage /
    # days figures. figure_type is 'string' so the renderer prints the
    # string we computed without re-formatting it.

    @api.model
    def _build_columns(self, current_label, prior_label=None):
        columns = [
            {'expression_label': 'metric', 'name': _("Metric"),
             'figure_type': 'string'},
            {'expression_label': 'value',
             'name': current_label or _("Value"),
             'figure_type': 'string'},
        ]
        if prior_label:
            columns.append({
                'expression_label': 'prior_value',
                'name': prior_label, 'figure_type': 'string',
            })
        return columns

    # ---- scalar reads ----

    @api.model
    def _compute_scalars(
        self, options, company_ids, date_from, date_to, posted_only,
    ):
        """Single-pass SUM aggregates, one per KPI. O(1) in result rows.

        Period flow figures (revenue, expenses) use the date window;
        balance figures (cash, AR, AP) are cumulative to date_to via a
        date_to-only window, mirroring the Balance Sheet's snapshot read.
        """
        revenue = self._fetch_aggregate_balance(
            account_types=self.INCOME_TYPES, sign=-1,
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        cost_of_sales = self._fetch_aggregate_balance(
            account_types=self.COST_OF_SALES_TYPES, sign=+1,
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        operating_expense = self._fetch_aggregate_balance(
            account_types=self.OPERATING_EXPENSE_TYPES, sign=+1,
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        total_expense = self._fetch_aggregate_balance(
            account_types=self.EXPENSE_TYPES, sign=+1,
            company_ids=company_ids, date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )

        # Balances cumulative to date_to (no date_from). Mirrors the
        # Balance Sheet snapshot read so the figures reconcile.
        cash = self._fetch_aggregate_balance(
            account_types=self.CASH_TYPES, sign=+1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        receivables = self._fetch_aggregate_balance(
            account_types=self.RECEIVABLE_TYPES, sign=+1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        payables = self._fetch_aggregate_balance(
            account_types=self.PAYABLE_TYPES, sign=-1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        current_assets = self._fetch_aggregate_balance(
            account_types=self.CURRENT_ASSET_TYPES, sign=+1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        current_liabilities = self._fetch_aggregate_balance(
            account_types=self.CURRENT_LIABILITY_TYPES, sign=-1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        quick_assets = self._fetch_aggregate_balance(
            account_types=self.QUICK_ASSET_TYPES, sign=+1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        total_assets = self._fetch_aggregate_balance(
            account_types=self.TOTAL_ASSET_TYPES, sign=+1,
            company_ids=company_ids, date_from=None, date_to=date_to,
            posted_only=posted_only, options=options,
        )

        gross_profit = round(revenue - cost_of_sales, 2)
        operating_profit = round(gross_profit - operating_expense, 2)
        net_profit = round(revenue - total_expense, 2)
        working_capital = round(current_assets - current_liabilities, 2)

        # Average period length in days for DSO / DPO. Inclusive of both
        # endpoints, floored at 1 to avoid a zero-length window.
        try:
            period_days = max(1, (date_to - date_from).days + 1)
        except TypeError:
            period_days = 1

        # DSO / DPO: balance / period-flow * days. Guard the divisions.
        dso = self._safe_ratio(receivables * period_days, revenue)
        # Purchases proxy = total expenses recognised in the period.
        dpo = self._safe_ratio(payables * period_days, total_expense)

        return {
            'revenue': round(revenue, 2),
            'cost_of_sales': round(cost_of_sales, 2),
            'gross_profit': gross_profit,
            'operating_profit': operating_profit,
            'total_expense': round(total_expense, 2),
            'net_profit': net_profit,
            'gross_margin': self._safe_ratio(gross_profit, revenue),
            'operating_margin': self._safe_ratio(operating_profit, revenue),
            'net_margin': self._safe_ratio(net_profit, revenue),
            'cash': round(cash, 2),
            'receivables': round(receivables, 2),
            'payables': round(payables, 2),
            'working_capital': working_capital,
            'current_ratio': self._safe_ratio(
                current_assets, current_liabilities),
            'quick_ratio': self._safe_ratio(
                quick_assets, current_liabilities),
            'dso': dso,
            'dpo': dpo,
            'return_on_assets': self._safe_ratio(net_profit, total_assets),
        }

    # ---- line factories ----

    @api.model
    def _build_ratio_rows(self):
        """Return the row spec: (id, label, key, formatter, kind).

        Authored from the standard KPI set (IAS 1 presentation + common
        liquidity / efficiency ratios). formatter names map to the _fmt_*
        helpers; kind drives styling and which raw value is exported.
        """
        return [
            ('section_profitability', _("Profitability"), None, None,
             'section_header'),
            ('revenue', _("Revenue"), 'revenue', 'money', 'metric'),
            ('gross_profit', _("Gross Profit"), 'gross_profit', 'money',
             'metric'),
            ('operating_profit', _("Operating Profit"), 'operating_profit',
             'money', 'metric'),
            ('net_profit', _("Net Profit"), 'net_profit', 'money', 'metric'),
            ('gross_margin', _("Gross Margin"), 'gross_margin', 'pct',
             'ratio'),
            ('operating_margin', _("Operating Margin"), 'operating_margin',
             'pct', 'ratio'),
            ('net_margin', _("Net Margin"), 'net_margin', 'pct', 'ratio'),

            ('section_liquidity', _("Cash & Liquidity"), None, None,
             'section_header'),
            ('cash', _("Cash Position"), 'cash', 'money', 'metric'),
            ('receivables', _("Receivables"), 'receivables', 'money',
             'metric'),
            ('payables', _("Payables"), 'payables', 'money', 'metric'),
            ('working_capital', _("Net Working Capital"), 'working_capital',
             'money', 'metric'),

            ('section_ratios', _("Ratios"), None, None, 'section_header'),
            ('current_ratio', _("Current Ratio"), 'current_ratio', 'ratio',
             'ratio'),
            ('quick_ratio', _("Quick Ratio"), 'quick_ratio', 'ratio',
             'ratio'),
            ('dso', _("Days Sales Outstanding (DSO)"), 'dso', 'days',
             'ratio'),
            ('dpo', _("Days Payable Outstanding (DPO)"), 'dpo', 'days',
             'ratio'),
            ('return_on_assets', _("Return on Assets"), 'return_on_assets',
             'pct', 'ratio'),
        ]

    @api.model
    def _format_cell(self, formatter, raw):
        if formatter == 'money':
            return self._fmt_money(raw)
        if formatter == 'pct':
            return self._fmt_pct(raw)
        if formatter == 'days':
            return self._fmt_days(raw)
        # default: ratio / float
        return self._fmt_ratio(raw)

    @api.model
    def _build_lines(self, current, prior):
        lines = []
        for row_id, label, key, formatter, kind in self._build_ratio_rows():
            if kind == 'section_header':
                lines.append({
                    'id': "section-%s" % row_id,
                    'name': label,
                    'level': 0,
                    'columns': self._blank_columns(prior is not None),
                    'unfoldable': False,
                    'meta': {'kind': 'section_header', 'section_id': row_id},
                })
                continue
            raw = current.get(key)
            value_str = self._format_cell(formatter, raw)
            columns = [{'expression_label': 'value', 'value': value_str}]
            line_meta = {
                'kind': kind,
                'metric': key,
                # Raw number stashed for export / a future numeric column.
                'raw_value': raw,
            }
            if prior is not None:
                prior_raw = prior.get(key)
                columns.append({
                    'expression_label': 'prior_value',
                    'value': self._format_cell(formatter, prior_raw),
                })
                line_meta['prior_raw_value'] = prior_raw
            lines.append({
                'id': "exec-%s" % row_id,
                'name': label,
                'level': 1,
                'columns': columns,
                'unfoldable': False,
                'meta': line_meta,
            })
        return lines

    @staticmethod
    def _blank_columns(has_prior):
        cols = [{'expression_label': 'value', 'value': ''}]
        if has_prior:
            cols.append({'expression_label': 'prior_value', 'value': ''})
        return cols

    # ---- drilldown ----

    @api.model
    def get_drilldown_action(self, options, line_id):
        """Cash / AR / AP metric rows drill to the underlying journal items
        by account_type and date. Section headers, ratios and margins are
        aggregates and do not drill.
        """
        if not line_id or not isinstance(line_id, str):
            return None
        drill_map = {
            'exec-cash': self.CASH_TYPES,
            'exec-receivables': self.RECEIVABLE_TYPES,
            'exec-payables': self.PAYABLE_TYPES,
        }
        account_types = drill_map.get(line_id)
        if not account_types:
            return None
        try:
            date_to = self._extract_date(options, 'date_to')
        except UserError:
            return None
        company_ids = options.get('company_ids') or [self.env.company.id]
        domain = [
            ('account_id.account_type', 'in', list(account_types)),
            ('company_id', 'in', list(company_ids)),
            ('date', '<=', self._iso_date(date_to)),
        ]
        if options.get('posted_only', True):
            domain.append(('parent_state', '=', 'posted'))
        domain += self._eh_drilldown_filter_domain(options)
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Items"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': domain,
            'context': {'search_default_group_move': 1},
        }
