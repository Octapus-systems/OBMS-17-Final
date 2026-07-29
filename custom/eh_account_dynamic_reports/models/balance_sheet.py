# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Balance Sheet handler.

Cumulative point in time snapshot at date_to. Three sections (Assets,
Liabilities, Equity) plus three computed lines (Current Year Earnings,
Total Liabilities and Equity, Balance Check).

date_from is intentionally ignored: a balance sheet is a snapshot, not a
period activity report. Only date_to drives the cumulative aggregation.
The wizard still supplies date_from because the same wizard form is shared
across reports; the handler simply does not use it.

Sign convention:

* Asset accounts: debit balance is positive. sign=+1.
* Liability accounts: credit balance is positive in display. sign=-1.
* Equity accounts: credit balance is positive in display. sign=-1.
* Current Year Earnings: sum of -balance over income+expense accounts up
  to date_to. Equivalent to (income - expenses) before year end close.

Balance Check identity:

    Total Assets = Total Liabilities + Total Equity + Current Year Earnings

If Balance Check is not zero, the ledger has unbalanced postings or there
is a date or filter mismatch. The line is shown to the user so any drift
is immediately visible.

Localizations can _inherit this handler to add subsections (Current
Assets, Non Current Assets, etc.) or override the account_type tuples.
"""

from odoo import _, api, fields, models
from odoo.tools.translate import _lt



class EhBalanceSheetHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.balance_sheet'
    _inherit = 'eh.account.dynamic.report.handler.sectioned'
    _description = "Balance Sheet report handler"

    REPORT_CODE = 'balance_sheet'
    REPORT_NAME = _lt("Balance Sheet")

    # IAS 1.60/66 current vs non-current split. The grand ASSET_TYPES /
    # LIABILITY_TYPES tuples remain the union of the two subsections so the
    # grand totals and any localization that reads them are unaffected.
    CURRENT_ASSET_TYPES = (
        'asset_receivable', 'asset_cash', 'asset_current',
        'asset_prepayments',
    )
    NON_CURRENT_ASSET_TYPES = ('asset_non_current', 'asset_fixed')
    ASSET_TYPES = CURRENT_ASSET_TYPES + NON_CURRENT_ASSET_TYPES

    CURRENT_LIABILITY_TYPES = (
        'liability_payable', 'liability_credit_card', 'liability_current',
    )
    NON_CURRENT_LIABILITY_TYPES = ('liability_non_current',)
    LIABILITY_TYPES = CURRENT_LIABILITY_TYPES + NON_CURRENT_LIABILITY_TYPES

    EQUITY_TYPES = ('equity', 'equity_unaffected')
    INCOME_TYPES = ('income', 'income_other')
    EXPENSE_TYPES = ('expense', 'expense_depreciation', 'expense_direct_cost')

    @api.model
    def expand_account_line(self, options, line_id, offset=0, limit=None):
        """Balance-Sheet override: thread the as-of [epoch, date_to] window.

        A balance sheet is a snapshot, so its account cell is SUM(balance)
        for every line up to date_to (no lower bound). The shared engine
        windows on [date_from, date_to]; here we force date_from to the
        epoch so the paged children reconcile EXACTLY to the as-of cell
        regardless of whatever date_from the shared wizard supplied. Falls
        back to the base behaviour if the date block is malformed.
        """
        try:
            date_block = dict(options.get('date') or {})
            date_block['date_from'] = '0001-01-01'
            options = dict(options, date=date_block)
        except Exception:  # pragma: no cover - defensive
            pass
        return super().expand_account_line(
            options, line_id, offset=offset, limit=limit)

    @api.model
    def compute(self, options):
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        show_zero = bool(options.get('show_zero', False))
        comparison = options.get('comparison') or 'none'

        lines, totals = self._build_snapshot_lines(
            options=options, company_ids=company_ids, date_to=date_to,
            posted_only=posted_only, show_zero=show_zero,
        )
        meta = {
            'report_code': self.REPORT_CODE,
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
            'show_zero': show_zero,
            'comparison': comparison,
        }

        if comparison and comparison != 'none':
            # For a snapshot report we compare against an as-of point in
            # the past: end of prior period (period mode) or one year
            # earlier (year mode). date_from is unused so we treat
            # date_to as both ends of a zero-width window for the
            # date-shift maths.
            prior_from, prior_to, prior_label = self._resolve_comparison_dates(
                comparison, date_to, date_to,
            )
            if prior_to:
                prior_lines, prior_totals = self._build_snapshot_lines(
                    options=options, company_ids=company_ids,
                    date_to=prior_to,
                    posted_only=posted_only, show_zero=show_zero,
                )
                merged = self.merge_comparative_lines(lines, prior_lines)
                meta['prior_date_to'] = self._iso_date(prior_to)
                meta['comparison_label'] = prior_label
                return {
                    'columns': self._build_comparative_column_layout(
                        label_name=_("Account"),
                        current_label=_("As at %s") % self._iso_date(date_to),
                        prior_label=_("As at %s") % self._iso_date(prior_to),
                    ),
                    'lines': merged,
                    'totals': dict(totals, **{
                        'prior_assets': prior_totals['assets'],
                        'prior_liabilities': prior_totals['liabilities'],
                        'prior_equity': prior_totals['equity'],
                    }),
                    'generated_at': fields.Datetime.now().isoformat(),
                    'meta': meta,
                }

        return {
            'columns': self._build_two_column_layout(),
            'lines': lines,
            'totals': totals,
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }

    @api.model
    def _build_snapshot_lines(
        self, options, company_ids, date_to, posted_only, show_zero,
    ):
        current_asset_rows = self._fetch_grouped_account_totals(
            account_types=self.CURRENT_ASSET_TYPES, sign=+1,
            company_ids=company_ids,
            date_to=date_to,
            posted_only=posted_only, options=options,
        )
        non_current_asset_rows = self._fetch_grouped_account_totals(
            account_types=self.NON_CURRENT_ASSET_TYPES, sign=+1,
            company_ids=company_ids,
            date_to=date_to,
            posted_only=posted_only, options=options,
        )
        current_liability_rows = self._fetch_grouped_account_totals(
            account_types=self.CURRENT_LIABILITY_TYPES, sign=-1,
            company_ids=company_ids,
            date_to=date_to,
            posted_only=posted_only, options=options,
        )
        non_current_liability_rows = self._fetch_grouped_account_totals(
            account_types=self.NON_CURRENT_LIABILITY_TYPES, sign=-1,
            company_ids=company_ids,
            date_to=date_to,
            posted_only=posted_only, options=options,
        )
        equity_rows = self._fetch_grouped_account_totals(
            account_types=self.EQUITY_TYPES, sign=-1,
            company_ids=company_ids,
            date_to=date_to,
            posted_only=posted_only, options=options,
        )
        current_year_earnings = self._fetch_aggregate_balance(
            account_types=self.INCOME_TYPES + self.EXPENSE_TYPES,
            company_ids=company_ids,
            date_to=date_to,
            posted_only=posted_only, options=options,
            sign=-1,
        )

        current_asset_total = round(
            sum(r['amount'] for r in current_asset_rows), 2)
        non_current_asset_total = round(
            sum(r['amount'] for r in non_current_asset_rows), 2)
        asset_total = round(current_asset_total + non_current_asset_total, 2)
        current_liability_total = round(
            sum(r['amount'] for r in current_liability_rows), 2)
        non_current_liability_total = round(
            sum(r['amount'] for r in non_current_liability_rows), 2)
        liability_total = round(
            current_liability_total + non_current_liability_total, 2)
        equity_total = round(sum(r['amount'] for r in equity_rows), 2)
        current_year_earnings = round(current_year_earnings, 2)
        total_equity_liabilities = round(
            liability_total + equity_total + current_year_earnings, 2,
        )
        balance_check = round(asset_total - total_equity_liabilities, 2)

        hierarchical = bool(options.get('hierarchical_groups', True))
        unfolded_ids = set(options.get('unfolded_lines') or [])

        def _render(rows, section_id):
            if hierarchical:
                return self._render_account_lines_grouped(
                    rows, section_id=section_id, show_zero=show_zero,
                    unfolded_ids=unfolded_ids, options=options,
                )
            return self._render_account_lines(
                rows, show_zero, options=options)

        lines = []
        # ---- Assets: current vs non-current (IAS 1.60/66) ----
        lines.append(self._section_header_line(_("Assets"), section_id='assets'))
        lines.append(self._section_header_line(
            _("Current Assets"), section_id='assets_current',
        ))
        lines.extend(_render(current_asset_rows, 'assets_current'))
        lines.append(self._section_total_line(
            _("Total Current Assets"), current_asset_total,
            section_id='assets_current',
        ))
        lines.append(self._section_header_line(
            _("Non-Current Assets"), section_id='assets_non_current',
        ))
        lines.extend(_render(non_current_asset_rows, 'assets_non_current'))
        lines.append(self._section_total_line(
            _("Total Non-Current Assets"), non_current_asset_total,
            section_id='assets_non_current',
        ))
        lines.append(self._section_total_line(
            _("Total Assets"), asset_total, section_id='assets',
        ))
        # ---- Liabilities: current vs non-current (IAS 1.60/69) ----
        lines.append(self._section_header_line(
            _("Liabilities"), section_id='liabilities',
        ))
        lines.append(self._section_header_line(
            _("Current Liabilities"), section_id='liabilities_current',
        ))
        lines.extend(_render(current_liability_rows, 'liabilities_current'))
        lines.append(self._section_total_line(
            _("Total Current Liabilities"), current_liability_total,
            section_id='liabilities_current',
        ))
        lines.append(self._section_header_line(
            _("Non-Current Liabilities"),
            section_id='liabilities_non_current',
        ))
        lines.extend(_render(
            non_current_liability_rows, 'liabilities_non_current'))
        lines.append(self._section_total_line(
            _("Total Non-Current Liabilities"), non_current_liability_total,
            section_id='liabilities_non_current',
        ))
        lines.append(self._section_total_line(
            _("Total Liabilities"), liability_total, section_id='liabilities',
        ))
        lines.append(self._section_header_line(_("Equity"), section_id='equity'))
        lines.extend(_render(equity_rows, 'equity'))
        lines.append(self._section_total_line(
            _("Total Equity"), equity_total, section_id='equity',
        ))
        lines.append(self._computed_line(
            'current_year_earnings', _("Current Year Earnings"),
            current_year_earnings, kind='current_year_earnings',
        ))
        lines.append(self._computed_line(
            'total_equity_liabilities', _("Total Liabilities and Equity"),
            total_equity_liabilities, kind='computed_total',
        ))
        lines.append(self._computed_line(
            'balance_check', _("Balance Check"),
            balance_check, kind='balance_check',
        ))
        return lines, {
            'assets': asset_total,
            'current_assets': current_asset_total,
            'non_current_assets': non_current_asset_total,
            'liabilities': liability_total,
            'current_liabilities': current_liability_total,
            'non_current_liabilities': non_current_liability_total,
            'equity': equity_total,
            'current_year_earnings': current_year_earnings,
            'total_equity_liabilities': total_equity_liabilities,
            'balance_check': balance_check,
        }
