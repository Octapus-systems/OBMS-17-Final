# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Profit and Loss handler.

Period income statement with two sections (Income, Expenses) and a Net
Profit row. Inherits the sectioned handler base, so query, line, and
section formatting come for free.

Sign convention:

* Income accounts carry credit balances (negative). The sectioned base
  query is called with sign=-1 to flip the display to a positive amount.
* Expense accounts carry debit balances (positive); sign=+1.
* Net Profit = Income - Expenses, displayed as a single computed line at
  the bottom.

Section structure:

* Income (account_type in 'income', 'income_other')
* Expenses (account_type in 'expense', 'expense_depreciation',
  'expense_direct_cost')
* Net Profit (computed)

Localizations can _inherit this handler to add more sections (Cost of
Sales, Other Income, etc.) or override the account_type tuples to match
local chart of accounts conventions.
"""

from odoo import _, api, fields, models
from odoo.tools.translate import _lt


class EhProfitAndLossHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.profit_and_loss'
    _inherit = 'eh.account.dynamic.report.handler.sectioned'
    _description = "Profit and Loss report handler"

    REPORT_CODE = 'profit_and_loss'
    REPORT_NAME = _lt("Profit and Loss")

    INCOME_TYPES = ('income', 'income_other')
    EXPENSE_TYPES = ('expense', 'expense_depreciation', 'expense_direct_cost')

    # By-function (IAS 1.103) presentation splits expenses by role. Cost of
    # Sales is the direct-cost bucket; Operating Expenses is the remaining
    # overhead. Finance Costs and Tax Expense have no dedicated account_type
    # and are resolved from the per-company account mappings on res.company
    # (eh_pnl_finance_cost_account_ids / eh_pnl_tax_expense_account_ids).
    COST_OF_SALES_TYPES = ('expense_direct_cost',)
    OPERATING_EXPENSE_TYPES = ('expense', 'expense_depreciation')

    @api.model
    def compute(self, options):
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        show_zero = bool(options.get('show_zero', False))
        comparison = options.get('comparison') or 'none'
        comparison_number = int(options.get('comparison_number') or 1)

        lines, totals = self._build_period_lines(
            options=options,
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, show_zero=show_zero,
        )

        meta = {
            'report_code': self.REPORT_CODE,
            'date_from': self._iso_date(date_from),
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
            'show_zero': show_zero,
            'comparison': comparison,
        }

        # N-period (more than one prior period): side-by-side amount
        # columns rather than the single current/prior/variance layout.
        if comparison != 'none' and comparison_number > 1:
            periods = self._resolve_comparison_periods(
                comparison, date_from, date_to, comparison_number)
            if periods:
                prior_line_lists = []
                period_labels = []
                prior_totals_by_index = {}
                for index, (prior_from, prior_to, _plabel) in enumerate(
                        periods, start=1):
                    prior_lines, prior_totals = self._build_period_lines(
                        options=options, company_ids=company_ids,
                        date_from=prior_from, date_to=prior_to,
                        posted_only=posted_only, show_zero=show_zero,
                    )
                    prior_line_lists.append(prior_lines)
                    period_labels.append("%s to %s" % (
                        self._iso_date(prior_from),
                        self._iso_date(prior_to)))
                    prior_totals_by_index[
                        'prior_%d' % index] = prior_totals['net_profit']
                merged = self.merge_n_period_lines(lines, prior_line_lists)
                meta['comparison_number'] = comparison_number
                meta['comparison_periods'] = [
                    {'from': self._iso_date(pf), 'to': self._iso_date(pt)}
                    for pf, pt, _l in periods
                ]
                totals_payload = {
                    'income': totals['income'],
                    'expenses': totals['expenses'],
                    'net_profit': totals['net_profit'],
                    'amount': totals['net_profit'],
                }
                totals_payload.update(prior_totals_by_index)
                return {
                    'columns': self._build_n_period_column_layout(
                        _("%s to %s") % (
                            self._iso_date(date_from),
                            self._iso_date(date_to)),
                        period_labels,
                    ),
                    'lines': merged,
                    'totals': totals_payload,
                    'generated_at': fields.Datetime.now().isoformat(),
                    'meta': meta,
                }

        # Horizontal column groups: one amount column per company.
        if options.get('horizontal_group_by') == 'company' and (
                len(company_ids) > 1):
            group_line_lists = []
            group_labels = []
            group_totals = {}
            for index, company_id in enumerate(company_ids, start=1):
                group_lines, group_t = self._build_period_lines(
                    options=options, company_ids=[company_id],
                    date_from=date_from, date_to=date_to,
                    posted_only=posted_only, show_zero=show_zero,
                )
                group_line_lists.append(group_lines)
                group_labels.append(
                    self.env['res.company'].browse(company_id).name)
                group_totals['group_%d' % index] = group_t['net_profit']
            merged = self.merge_horizontal_groups(group_line_lists)
            meta['horizontal_group_by'] = 'company'
            totals_payload = {
                'net_profit': totals['net_profit'],
                'amount': totals['net_profit'],
            }
            totals_payload.update(group_totals)
            return {
                'columns': self._build_horizontal_column_layout(group_labels),
                'lines': merged,
                'totals': totals_payload,
                'generated_at': fields.Datetime.now().isoformat(),
                'meta': meta,
            }

        if comparison and comparison != 'none':
            prior_from, prior_to, prior_label = self._resolve_comparison_dates(
                comparison, date_from, date_to,
            )
            if prior_from and prior_to:
                prior_lines, prior_totals = self._build_period_lines(
                    options=options,
                    company_ids=company_ids,
                    date_from=prior_from, date_to=prior_to,
                    posted_only=posted_only, show_zero=show_zero,
                )
                merged = self.merge_comparative_lines(lines, prior_lines)
                meta['prior_date_from'] = self._iso_date(prior_from)
                meta['prior_date_to'] = self._iso_date(prior_to)
                meta['comparison_label'] = prior_label
                return {
                    'columns': self._build_comparative_column_layout(
                        label_name=_("Account"),
                        current_label=_("%s to %s") % (
                            self._iso_date(date_from),
                            self._iso_date(date_to),
                        ),
                        prior_label=_("%s to %s") % (
                            self._iso_date(prior_from),
                            self._iso_date(prior_to),
                        ),
                    ),
                    'lines': merged,
                    'totals': {
                        'income': totals['income'],
                        'expenses': totals['expenses'],
                        'net_profit': totals['net_profit'],
                        'amount': totals['net_profit'],
                        'prior_income': prior_totals['income'],
                        'prior_expenses': prior_totals['expenses'],
                        'prior_net_profit': prior_totals['net_profit'],
                    },
                    'generated_at': fields.Datetime.now().isoformat(),
                    'meta': meta,
                }

        # Presentation-currency translation is applied centrally in
        # eh.account.dynamic.report.render so every report behaves the same;
        # the handler returns figures in the company currency.
        return {
            'columns': self._build_two_column_layout(),
            'lines': lines,
            'totals': {
                'income': totals['income'],
                'expenses': totals['expenses'],
                'net_profit': totals['net_profit'],
                'amount': totals['net_profit'],
            },
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
        }

    @api.model
    def _build_period_lines(
        self, options, company_ids, date_from, date_to,
        posted_only, show_zero,
    ):
        """Compute one period's section lines and totals.

        Dispatches on options['pnl_presentation']:

        * 'by_nature' (the default) keeps the classic Income / Expenses /
          Net Profit layout unchanged.
        * 'by_function' emits the IAS 1.82/85 subtotals (Gross Profit,
          Operating Profit, Profit Before Tax, Profit for the Period).

        Both branches return the same totals keys ('income', 'expenses',
        'net_profit') so the comparison, N-period, and horizontal-group
        code paths in compute() are presentation-agnostic.
        """
        presentation = options.get('pnl_presentation') or 'by_nature'
        if presentation == 'by_function':
            return self._build_period_lines_by_function(
                options=options, company_ids=company_ids,
                date_from=date_from, date_to=date_to,
                posted_only=posted_only, show_zero=show_zero,
            )
        return self._build_period_lines_by_nature(
            options=options, company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, show_zero=show_zero,
        )

    @api.model
    def _build_period_lines_by_nature(
        self, options, company_ids, date_from, date_to,
        posted_only, show_zero,
    ):
        """Compute one period's section lines and totals."""
        income_rows = self._fetch_grouped_account_totals(
            account_types=self.INCOME_TYPES, sign=-1,
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        expense_rows = self._fetch_grouped_account_totals(
            account_types=self.EXPENSE_TYPES, sign=+1,
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )

        income_total = round(sum(r['amount'] for r in income_rows), 2)
        expense_total = round(sum(r['amount'] for r in expense_rows), 2)
        net_profit = round(income_total - expense_total, 2)

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

        def _tag_higher_is_better(line_list, flag):
            # Optional directional hint for the WS5 viewer's variance
            # colouring: income lines read favourable when they RISE
            # (higher_is_better=True), expense lines when they FALL
            # (higher_is_better=False). Stamped on meta so the client can
            # colour a comparison column by favourability rather than raw
            # sign; absent it, the client falls back to sign-only (never
            # worse than before). Additive: never removes existing meta keys.
            for ln in line_list:
                meta = ln.setdefault('meta', {})
                meta['higher_is_better'] = flag
            return line_list

        income_lines = _tag_higher_is_better(_render(income_rows, 'income'), True)
        expense_lines = _tag_higher_is_better(_render(expense_rows, 'expenses'), False)

        lines = []
        income_header = self._section_header_line(_("Income"), section_id='income')
        income_header.setdefault('meta', {})['higher_is_better'] = True
        lines.append(income_header)
        lines.extend(income_lines)
        income_total_line = self._section_total_line(
            _("Total Income"), income_total, section_id='income',
        )
        income_total_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(income_total_line)
        expense_header = self._section_header_line(
            _("Expenses"), section_id='expenses',
        )
        expense_header.setdefault('meta', {})['higher_is_better'] = False
        lines.append(expense_header)
        lines.extend(expense_lines)
        expense_total_line = self._section_total_line(
            _("Total Expenses"), expense_total, section_id='expenses',
        )
        expense_total_line.setdefault('meta', {})['higher_is_better'] = False
        lines.append(expense_total_line)
        net_line = self._computed_line(
            'net_profit', _("Net Profit"), net_profit, kind='net_profit',
        )
        # A higher net profit is favourable.
        net_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(net_line)
        return lines, {
            'income': income_total,
            'expenses': expense_total,
            'net_profit': net_profit,
        }

    @api.model
    def _build_period_lines_by_function(
        self, options, company_ids, date_from, date_to,
        posted_only, show_zero,
    ):
        """By-function income statement with IAS 1.82/85 subtotals.

        Section order:

        * Revenue (INCOME_TYPES)
        * Cost of Sales (expense_direct_cost)
        * Gross Profit = Revenue - Cost of Sales (computed)
        * Operating Expenses (expense, expense_depreciation) less any
          accounts mapped to Finance Costs or Tax Expense
        * Operating Profit = Gross Profit - Operating Expenses (computed)
        * Finance Costs (per-company mapping; zero when unmapped)
        * Profit Before Tax = Operating Profit - Finance Costs (computed)
        * Tax Expense (per-company mapping; zero when unmapped). When any
          deferred-tax account is mapped
          (res.company.eh_pnl_deferred_tax_account_ids), this is split into
          a Current Tax and a Deferred Tax line (IAS 1.82 / IAS 12.81(c))
          that sum to the total tax; otherwise a single Tax Expense line
          is shown.
        * Profit for the Period = Profit Before Tax - Tax Expense

        Finance Costs and Tax Expense are carved out of Operating Expenses
        so nothing is double counted, which keeps Profit for the Period
        identical to the by-nature Net Profit
        (Revenue - all expenses). The totals payload mirrors the by-nature
        branch (income / expenses / net_profit) so compute() is agnostic.
        """
        income_rows = self._fetch_grouped_account_totals(
            account_types=self.INCOME_TYPES, sign=-1,
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        cos_rows = self._fetch_grouped_account_totals(
            account_types=self.COST_OF_SALES_TYPES, sign=+1,
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )
        opex_candidate_rows = self._fetch_grouped_account_totals(
            account_types=self.OPERATING_EXPENSE_TYPES, sign=+1,
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
        )

        # Resolve the Finance Cost / Tax Expense account carve-outs from the
        # per-company mapping. With more than one company in scope, the union
        # of every in-scope company's mapping is used; unmapped -> empty set.
        companies = self.env['res.company'].sudo().browse(
            [int(c) for c in company_ids])
        finance_ids = set(companies.mapped(
            'eh_pnl_finance_cost_account_ids').ids)
        tax_ids = set(companies.mapped('eh_pnl_tax_expense_account_ids').ids)
        # Deferred-tax accounts are a subset of the tax mapping (IAS 12.81(c)):
        # the deferred portion of Tax Expense. Finance Costs is carved out
        # first, so an account mapped to both finance and deferred tax is a
        # finance cost only, never counted as tax.
        deferred_tax_ids = set(companies.mapped(
            'eh_pnl_deferred_tax_account_ids').ids) & tax_ids
        deferred_tax_ids -= finance_ids
        # An account mapped to both buckets must land in exactly one, or it is
        # subtracted twice (finance_total and tax_total), double counted in
        # expense_total, and emits a duplicate 'account-<id>' line. Finance
        # Costs wins so the account is carved out once; Tax Expense drops it.
        tax_ids -= finance_ids
        # Current tax = the tax mapping less the deferred-tax subset.
        current_tax_ids = tax_ids - deferred_tax_ids

        finance_rows = [
            r for r in opex_candidate_rows if r['account_id'] in finance_ids]
        tax_rows = [
            r for r in opex_candidate_rows if r['account_id'] in tax_ids]
        deferred_tax_rows = [
            r for r in tax_rows if r['account_id'] in deferred_tax_ids]
        current_tax_rows = [
            r for r in tax_rows if r['account_id'] in current_tax_ids]
        # Operating Expenses excludes anything mapped to Finance or Tax so
        # the subtotals do not overlap.
        carved_ids = finance_ids | tax_ids
        opex_rows = [
            r for r in opex_candidate_rows
            if r['account_id'] not in carved_ids]

        revenue_total = round(sum(r['amount'] for r in income_rows), 2)
        cos_total = round(sum(r['amount'] for r in cos_rows), 2)
        opex_total = round(sum(r['amount'] for r in opex_rows), 2)
        finance_total = round(sum(r['amount'] for r in finance_rows), 2)
        tax_total = round(sum(r['amount'] for r in tax_rows), 2)
        deferred_tax_total = round(
            sum(r['amount'] for r in deferred_tax_rows), 2)
        current_tax_total = round(
            sum(r['amount'] for r in current_tax_rows), 2)

        gross_profit = round(revenue_total - cos_total, 2)
        operating_profit = round(gross_profit - opex_total, 2)
        profit_before_tax = round(operating_profit - finance_total, 2)
        profit_for_period = round(profit_before_tax - tax_total, 2)

        # Total expenses of every kind, for the totals payload parity with
        # the by-nature branch (Net Profit == Revenue - all expenses).
        expense_total = round(
            cos_total + opex_total + finance_total + tax_total, 2)

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

        def _tag(line_list, flag):
            for ln in line_list:
                ln.setdefault('meta', {})['higher_is_better'] = flag
            return line_list

        lines = []

        # Revenue.
        revenue_header = self._section_header_line(
            _("Revenue"), section_id='income')
        revenue_header.setdefault('meta', {})['higher_is_better'] = True
        lines.append(revenue_header)
        lines.extend(_tag(_render(income_rows, 'income'), True))
        revenue_total_line = self._section_total_line(
            _("Total Revenue"), revenue_total, section_id='income')
        revenue_total_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(revenue_total_line)

        # Cost of Sales.
        cos_header = self._section_header_line(
            _("Cost of Sales"), section_id='cost_of_sales')
        cos_header.setdefault('meta', {})['higher_is_better'] = False
        lines.append(cos_header)
        lines.extend(_tag(_render(cos_rows, 'cost_of_sales'), False))
        cos_total_line = self._section_total_line(
            _("Total Cost of Sales"), cos_total, section_id='cost_of_sales')
        cos_total_line.setdefault('meta', {})['higher_is_better'] = False
        lines.append(cos_total_line)

        # Gross Profit.
        gross_line = self._computed_line(
            'gross_profit', _("Gross Profit"), gross_profit,
            kind='subtotal')
        gross_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(gross_line)

        # Operating Expenses.
        opex_header = self._section_header_line(
            _("Operating Expenses"), section_id='operating_expenses')
        opex_header.setdefault('meta', {})['higher_is_better'] = False
        lines.append(opex_header)
        lines.extend(_tag(_render(opex_rows, 'operating_expenses'), False))
        opex_total_line = self._section_total_line(
            _("Total Operating Expenses"), opex_total,
            section_id='operating_expenses')
        opex_total_line.setdefault('meta', {})['higher_is_better'] = False
        lines.append(opex_total_line)

        # Operating Profit.
        operating_line = self._computed_line(
            'operating_profit', _("Operating Profit"), operating_profit,
            kind='subtotal')
        operating_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(operating_line)

        # Finance Costs.
        finance_header = self._section_header_line(
            _("Finance Costs"), section_id='finance_costs')
        finance_header.setdefault('meta', {})['higher_is_better'] = False
        lines.append(finance_header)
        lines.extend(_tag(_render(finance_rows, 'finance_costs'), False))
        finance_total_line = self._section_total_line(
            _("Total Finance Costs"), finance_total,
            section_id='finance_costs')
        finance_total_line.setdefault('meta', {})['higher_is_better'] = False
        lines.append(finance_total_line)

        # Profit Before Tax.
        pbt_line = self._computed_line(
            'profit_before_tax', _("Profit Before Tax"), profit_before_tax,
            kind='subtotal')
        pbt_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(pbt_line)

        # Tax Expense (IAS 1.82 / IAS 12.81(c)). When any deferred-tax account
        # is mapped, the Tax Expense subtotal is split into Current Tax and
        # Deferred Tax lines that sum to the total tax; otherwise a single Tax
        # Expense line is shown exactly as before. Both presentations carve the
        # same accounts out of Operating Expenses, so Profit for the Period is
        # identical either way.
        tax_header = self._section_header_line(
            _("Tax Expense"), section_id='tax_expense')
        tax_header.setdefault('meta', {})['higher_is_better'] = False
        lines.append(tax_header)
        if deferred_tax_ids:
            # Current Tax.
            lines.extend(_tag(
                _render(current_tax_rows, 'current_tax'), False))
            current_tax_line = self._section_total_line(
                _("Current Tax"), current_tax_total,
                section_id='current_tax')
            current_tax_line.setdefault(
                'meta', {})['higher_is_better'] = False
            lines.append(current_tax_line)
            # Deferred Tax.
            lines.extend(_tag(
                _render(deferred_tax_rows, 'deferred_tax'), False))
            deferred_tax_line = self._section_total_line(
                _("Deferred Tax"), deferred_tax_total,
                section_id='deferred_tax')
            deferred_tax_line.setdefault(
                'meta', {})['higher_is_better'] = False
            lines.append(deferred_tax_line)
        else:
            lines.extend(_tag(_render(tax_rows, 'tax_expense'), False))
        tax_total_line = self._section_total_line(
            _("Total Tax Expense"), tax_total, section_id='tax_expense')
        tax_total_line.setdefault('meta', {})['higher_is_better'] = False
        lines.append(tax_total_line)

        # Profit for the Period. Kept under the 'net_profit' id so any
        # downstream consumer keyed on that id (annotations, drill-down
        # guards, exports) keeps working across both presentations.
        net_line = self._computed_line(
            'net_profit', _("Profit for the Period"), profit_for_period,
            kind='net_profit')
        net_line.setdefault('meta', {})['higher_is_better'] = True
        lines.append(net_line)

        return lines, {
            'income': revenue_total,
            'expenses': expense_total,
            'net_profit': profit_for_period,
            'revenue': revenue_total,
            'cost_of_sales': cos_total,
            'gross_profit': gross_profit,
            'operating_expenses': opex_total,
            'operating_profit': operating_profit,
            'finance_costs': finance_total,
            'profit_before_tax': profit_before_tax,
            'tax_expense': tax_total,
            'current_tax': current_tax_total,
            'deferred_tax': deferred_tax_total,
        }
