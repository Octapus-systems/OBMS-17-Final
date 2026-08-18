# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Cash Flow Statement handler (direct method).

Classifies cash movements into operating, investing, and financing
activities by inspecting the non cash counterparts of each cash affecting
move. The direct method was chosen for v1 because it is more transparent
to SMB readers than the indirect method (which starts from net income and
adjusts), and because it composes naturally with the SQL builder.

Algorithm:

1. Find all moves that have at least one cash account line in the period.
2. For those moves, aggregate non cash line balances by account_type. The
   cash impact attributable to each account_type is SUM(-balance). Because
   each move balances, the sum of -balance over non cash lines equals the
   sum of balance over cash lines.
3. Group account_type buckets into the three activity sections.
4. Add a Net Change line, Opening Cash, Closing Cash, and a Balance Check.

The Balance Check verifies the identity:

    Closing Cash = Opening Cash + Net Change in Cash + FX Effect

If non zero, the underlying ledger has unbalanced postings or there is a
filter inconsistency, and the line surfaces this immediately.

FX effect on cash (IAS 7.28): journal entries posted in the company's
designated exchange difference journal that revalue cash accounts are not
activity; they restate the measurement of cash already held. Those moves
are pulled out of the activity sections and presented on their own
"Effect of exchange rate changes on cash" line, after the three activity
sections and before the closing cash balance. When the books carry no
such postings the line is zero and hidden, and the output is identical
to the pre-FX behaviour.

Cash and cash equivalents (IAS 7.6/7.46): companies can mark short term,
highly liquid investment accounts as cash equivalents on res.company
(eh_cash_equivalent_account_ids). Those accounts are then treated as cash
throughout: movements between real cash and equivalents net out as pure
cash transfers, and equivalent balances count towards opening and closing
cash. Left empty (the default), only `asset_cash` accounts are cash.

Indirect method (IAS 7.18(b)/7.20): the operating section is derived, not
classified. It starts from profit before tax (the period total of every
P&L account, plus back the income tax expense identified by the company's
Tax Expense account mapping and/or the EH Income Tax Paid tag), adjusts
for finance costs and investment income (so the disclosed interest and
dividend flows can be presented gross further down), adds back non-cash
charges resolved from the EH Non-Cash account tags (depreciation /
amortisation, impairment, provisions, unrealised FX, fair value), applies
the working-capital deltas measured from the balance-sheet movement of the
receivable / payable / other-current groups, and closes with the IAS
7.31-35 disclosed flows (interest paid, income taxes paid, and whichever
disclosed items the presentation policy assigns to operating).

The add-back engine is exact, not approximate: for every non-cash journal
entry carrying a tagged (or `expense_depreciation`-typed) P&L line, the
add-back equals that entry's movement on non-current balance-sheet
accounts. An accrual whose counterpart is itself a working-capital account
(a provision credited to a current liability, say) therefore contributes
no add-back - its cash effect is already inside the working-capital delta
- and the operating total of the indirect method ties exactly to the
direct method's operating total on the same ledger. That tie is asserted
as a test invariant.

IAS 7.31-35 disclosures: interest paid / received, dividends paid /
received and income taxes paid are measured from the tagged lines (EH
Interest Paid, EH Interest Received, EH Dividends Paid, EH Dividends
Received, EH Income Tax Paid) of cash-affecting entries, presented on
dedicated lines, and removed from the aggregate account-type buckets so
nothing is counted twice. Where a flow settles through an accrual account
(interest payable, income tax payable), tag that clearing account too:
the report reads the actual cash settlement off it and excludes it from
the working-capital deltas. When no account carries the EH Income Tax
Paid tag, an OPT-IN fallback (company flag eh_cf_tax_fallback, or the
cf_income_tax_fallback render option) measures the line from the
accounts named as tax repartition targets (the tax-authority payables
that core Odoo posts tax to), counting settlement lines only - lines of
cash-affecting entries that reduce the payable - so tax accrued inside a
cash entry (a POS session closing, a cash sale with tax) never surfaces
as a disclosed amount. The fallback cannot distinguish income tax from
indirect taxes (VAT / GST remittances qualify), applies to the direct
method's presentation only (see Limitations), and is off by default so
an untagged book renders byte-identically to a build without the
feature. Entities wanting income tax isolated should tag explicitly.
The presentation section per item follows IAS 7.31's policy choices,
defaulted on the company (interest paid: operating; interest / dividends
received: operating; dividends paid: financing) and overridable per
render via options; income taxes paid is always operating (IAS 7.35).

Non-cash transactions (IAS 7.43): entries recorded in the
eh.noncash.transaction register are listed in a memo section at the foot
of the statement for every period containing their date. Memo only: the
amounts never feed the net change in cash.

Limitations:

* Credit cards (`liability_credit_card`) are treated as financing rather
  than cash.
* The FX effect line applies to the direct method only. Under the
  indirect method the FX gain or loss already flows through net income,
  so isolating it there would double count.
* The indirect/direct operating tie assumes non-cash P&L charges against
  non-current balance-sheet accounts are tagged. An untagged non-cash
  entry crossing P&L and a non-current account (or a credit purchase of
  a fixed asset - itself an IAS 7.43 register candidate) surfaces as a
  difference between the two methods.
* The opt-in income-taxes-paid fallback (tax repartition targets) is a
  direct-method presentation aid only: it cannot distinguish income tax
  from indirect taxes, and the indirect method deliberately ignores it -
  no disclosed line, and the fallback accounts stay inside the
  working-capital deltas - so the operating total keeps its exact tie to
  the direct method (excluding them while still deducting the disclosed
  settlement would break the tie by the remittance amount, since a
  VAT-shaped accrual never crosses P&L). Tag the income tax accounts
  explicitly for a disclosed line under the indirect method.
"""

from odoo import _, api, fields, models
from odoo.tools import SQL
from odoo.tools.translate import _lt

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery


class EhCashFlowHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.cash_flow'
    _inherit = 'eh.account.dynamic.report.handler.sectioned'
    _description = "Cash Flow Statement report handler"

    REPORT_CODE = 'cash_flow'
    REPORT_NAME = _lt("Cash Flow Statement")

    CASH_TYPES = ('asset_cash',)

    OPERATING_TYPES = (
        'income', 'income_other',
        'expense', 'expense_depreciation', 'expense_direct_cost',
        'asset_receivable', 'liability_payable',
        'asset_current', 'liability_current',
    )
    INVESTING_TYPES = (
        'asset_fixed', 'asset_non_current', 'asset_prepayments',
    )
    FINANCING_TYPES = (
        'liability_non_current', 'liability_credit_card',
        'equity', 'equity_unaffected',
    )

    # P&L account types: the profit-before-tax base of the indirect method.
    PL_TYPES = (
        'income', 'income_other',
        'expense', 'expense_depreciation', 'expense_direct_cost',
    )
    # Working-capital balance-sheet groups whose period movement forms the
    # indirect method's delta lines. Deliberately identical to the direct
    # method's operating balance-sheet types so the two methods measure the
    # same operating perimeter (prepayments stay investing in both, per the
    # direct classification above).
    WC_TYPES = (
        'asset_receivable', 'liability_payable',
        'asset_current', 'liability_current',
    )

    # ---- IAS 7 tag registry ----

    NONCASH_ORDER = (
        'depreciation', 'impairment', 'provisions', 'fx', 'fair_value',
    )
    NONCASH_TAG_XMLIDS = {
        'depreciation':
            'eh_account_dynamic_reports.account_tag_noncash_depreciation',
        'impairment':
            'eh_account_dynamic_reports.account_tag_noncash_impairment',
        'provisions':
            'eh_account_dynamic_reports.account_tag_noncash_provisions',
        'fx': 'eh_account_dynamic_reports.account_tag_noncash_fx',
        'fair_value':
            'eh_account_dynamic_reports.account_tag_noncash_fair_value',
    }

    DISCLOSURE_ORDER = (
        'interest_paid', 'interest_received',
        'dividends_paid', 'dividends_received', 'income_tax_paid',
    )
    DISCLOSURE_TAG_XMLIDS = {
        'interest_paid':
            'eh_account_dynamic_reports.account_tag_interest_paid',
        'interest_received':
            'eh_account_dynamic_reports.account_tag_interest_received',
        'dividends_paid':
            'eh_account_dynamic_reports.account_tag_dividends_paid',
        'dividends_received':
            'eh_account_dynamic_reports.account_tag_dividends_received',
        'income_tax_paid':
            'eh_account_dynamic_reports.account_tag_income_tax_paid',
    }
    # item -> (default section, allowed sections, company policy field).
    # Income taxes paid carries no choice: IAS 7.35 classifies them as
    # operating unless specifically identifiable with financing or
    # investing activities, which this report does not attempt.
    DISCLOSURE_POLICY = {
        'interest_paid': (
            'operating', ('operating', 'financing'),
            'eh_cf_interest_paid_section'),
        'interest_received': (
            'operating', ('operating', 'investing'),
            'eh_cf_interest_received_section'),
        'dividends_paid': (
            'financing', ('financing', 'operating'),
            'eh_cf_dividends_paid_section'),
        'dividends_received': (
            'operating', ('operating', 'investing'),
            'eh_cf_dividends_received_section'),
        'income_tax_paid': ('operating', ('operating',), None),
    }

    @api.model
    def _get_account_type_labels(self):
        """Return the per-account-type display label map.

        Returned as a method (not a class attr) so each call resolves
        translations against the active language. A class-level dict
        literal would freeze English strings at module load.
        """
        return {
            'income': _("Income"),
            'income_other': _("Other Income"),
            'expense': _("Expenses"),
            'expense_depreciation': _("Depreciation"),
            'expense_direct_cost': _("Direct Costs"),
            'asset_receivable': _("Receivables"),
            'liability_payable': _("Payables"),
            'asset_current': _("Other Current Assets"),
            'liability_current': _("Other Current Liabilities"),
            'asset_fixed': _("Fixed Assets"),
            'asset_non_current': _("Non Current Assets"),
            'asset_prepayments': _("Prepayments"),
            'liability_non_current': _("Long Term Liabilities"),
            'liability_credit_card': _("Credit Cards"),
            'equity': _("Equity"),
            'equity_unaffected': _("Current Year Earnings"),
        }

    @api.model
    def compute(self, options):
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        show_zero = bool(options.get('show_zero', False))
        method = (options.get('cash_flow_method') or 'direct').lower()
        if method not in ('direct', 'indirect'):
            method = 'direct'

        equivalent_ids = self._get_cash_equivalent_ids(company_ids)
        cash_move_ids = self._fetch_cash_active_move_ids(
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
            equivalent_ids=equivalent_ids,
        )
        # IAS 7.28: exchange rate changes on cash held are not activity.
        # Cash-touching moves posted in the exchange difference journal, or
        # carrying an exchange gain/loss counterpart line, are isolated onto
        # their own line (direct method only; under the indirect method the
        # FX gain/loss already flows via net income).
        fx_effect = 0.0
        if method == 'direct':
            fx_effect, fx_move_ids = self._fetch_fx_effect(
                company_ids=company_ids,
                date_from=date_from, date_to=date_to,
                posted_only=posted_only, options=options,
                equivalent_ids=equivalent_ids,
            )
            if fx_move_ids:
                fx_move_set = set(fx_move_ids)
                cash_move_ids = [
                    m for m in cash_move_ids if m not in fx_move_set
                ]
        reconciled = bool(options.get('cash_flow_reconciled'))
        if not cash_move_ids:
            impacts_by_type = {}
        elif method == 'direct' and reconciled:
            impacts_by_type = self._fetch_cash_impacts_reconciled(
                move_ids=cash_move_ids,
                company_ids=company_ids,
                posted_only=posted_only, options=options,
                equivalent_ids=equivalent_ids,
            )
        else:
            impacts_by_type = self._fetch_cash_impacts(
                move_ids=cash_move_ids,
                company_ids=company_ids,
                posted_only=posted_only, options=options,
                equivalent_ids=equivalent_ids,
            )

        # IAS 7.31-35: disclosed flows (interest / dividends / income tax)
        # measured off the tagged lines of cash-affecting entries, then
        # re-attributed OUT of the aggregate account-type buckets so the
        # dedicated disclosure line and the bucket never count the same
        # cash twice. With no tagged accounts (and the opt-in tax fallback
        # off, its default) every map below is empty and the arithmetic
        # (and payload) is byte-identical to before.
        disclosure_accounts, fallback_items = (
            self._resolve_disclosure_accounts(company_ids, options))
        policy = self._resolve_disclosure_policy(options, company_ids)
        disclosures = self._fetch_disclosure_flows(
            cash_move_ids=cash_move_ids, company_ids=company_ids,
            posted_only=posted_only, options=options,
            disclosure_accounts=disclosure_accounts,
            fallback_items=fallback_items,
        )
        if method == 'indirect' and fallback_items:
            # Fallback-derived amounts are a direct-method presentation
            # aid only. Under the indirect method the settlements must
            # stay inside the working-capital deltas (so the fallback
            # accounts are NOT excluded from them) and no disclosed line
            # is subtracted; excluding the accounts while still deducting
            # the disclosed settlement would erase the accrual side from
            # the deltas yet keep the deduction, breaking the operating
            # tie to the direct method by exactly the remittance amount
            # (a VAT-shaped accrual has no P&L tax bridge). Tagged
            # accounts are unaffected.
            for item in fallback_items:
                disclosures.pop(item, None)
                disclosure_accounts.pop(item, None)
        for data in disclosures.values():
            for acc_type, amount in data['by_type'].items():
                impacts_by_type[acc_type] = (
                    impacts_by_type.get(acc_type, 0.0) - amount)
        # Assign each disclosed item to its presentation section per the
        # IAS 7.31 policy (income taxes paid is always operating, IAS 7.35).
        disclosure_rows = {'operating': [], 'investing': [], 'financing': []}
        for item in self.DISCLOSURE_ORDER:
            data = disclosures.get(item)
            if not data:
                continue
            disclosure_rows[policy[item]].append((item, data['total']))

        investing_total = round(
            self._sum_types(impacts_by_type, self.INVESTING_TYPES)
            + sum(a for _item, a in disclosure_rows['investing']), 2,
        )
        financing_total = round(
            self._sum_types(impacts_by_type, self.FINANCING_TYPES)
            + sum(a for _item, a in disclosure_rows['financing']), 2,
        )

        if method == 'indirect':
            indirect_breakdown = self._compute_indirect_operating(
                company_ids=company_ids,
                date_from=date_from, date_to=date_to,
                posted_only=posted_only, options=options,
                cash_move_ids=cash_move_ids,
                equivalent_ids=equivalent_ids,
                disclosure_accounts=disclosure_accounts,
                operating_disclosures=disclosure_rows['operating'],
            )
            operating_total = round(indirect_breakdown['total'], 2)
        else:
            indirect_breakdown = None
            operating_total = round(
                self._sum_types(impacts_by_type, self.OPERATING_TYPES)
                + sum(a for _item, a in disclosure_rows['operating']), 2,
            )
        net_change = round(
            operating_total + investing_total + financing_total, 2,
        )

        opening_cash = round(self._fetch_cash_balance(
            company_ids=company_ids,
            cutoff_date=date_from, posted_only=posted_only, before=True,
            equivalent_ids=equivalent_ids, options=options,
        ), 2)
        closing_cash = round(self._fetch_cash_balance(
            company_ids=company_ids,
            cutoff_date=date_to, posted_only=posted_only, before=False,
            equivalent_ids=equivalent_ids, options=options,
        ), 2)
        # Extended identity (IAS 7.28):
        # Closing == Opening + Net Change from activities + FX effect.
        balance_check = round(
            closing_cash - opening_cash - net_change - fx_effect, 2,
        )

        lines = []
        if method == 'indirect':
            lines.extend(self._render_indirect_operating_section(
                indirect_breakdown, operating_total, show_zero,
            ))
        else:
            lines.extend(self._render_section(
                _("Operating Activities"), 'operating',
                self.OPERATING_TYPES, impacts_by_type,
                section_total=operating_total, show_zero=show_zero,
                extra_rows=disclosure_rows['operating'],
            ))
        lines.extend(self._render_section(
            _("Investing Activities"), 'investing',
            self.INVESTING_TYPES, impacts_by_type,
            section_total=investing_total, show_zero=show_zero,
            extra_rows=disclosure_rows['investing'],
        ))
        lines.extend(self._render_section(
            _("Financing Activities"), 'financing',
            self.FINANCING_TYPES, impacts_by_type,
            section_total=financing_total, show_zero=show_zero,
            extra_rows=disclosure_rows['financing'],
        ))
        lines.append(self._computed_line(
            'net_change_in_cash', _("Net Change in Cash"),
            net_change, kind='net_change',
        ))
        # Shown only when non zero so books with no FX activity render
        # byte-identically to the pre-FX layout.
        if fx_effect:
            lines.append(self._computed_line(
                'fx_effect_on_cash',
                _("Effect of exchange rate changes on cash"),
                fx_effect, kind='fx_effect',
            ))
        lines.append(self._computed_line(
            'opening_cash_balance', _("Opening Cash Balance"),
            opening_cash, kind='cash_balance',
        ))
        lines.append(self._computed_line(
            'closing_cash_balance', _("Closing Cash Balance"),
            closing_cash, kind='cash_balance',
        ))
        lines.append(self._computed_line(
            'cash_balance_check', _("Balance Check"),
            balance_check, kind='balance_check',
        ))
        # IAS 7.43 memo section: non-cash investing and financing
        # transactions recorded in the register. Rendered at the foot of
        # the statement, after the cash identity block, because the
        # amounts are disclosure only and never feed net change. Absent
        # entirely when the register holds nothing for the period, so the
        # legacy payload shape is untouched on existing books.
        register_lines, register_total = self._render_noncash_register(
            company_ids, date_from, date_to,
        )
        if register_lines:
            lines.extend(register_lines)

        totals = {
            'operating': operating_total,
            'investing': investing_total,
            'financing': financing_total,
            'net_change_in_cash': net_change,
            'opening_cash_balance': opening_cash,
            'closing_cash_balance': closing_cash,
            'balance_check': balance_check,
        }
        # Emitted only when non zero: keeps the totals payload (and any
        # cached copy of it) unchanged for books with no FX activity.
        if fx_effect:
            totals['fx_effect_on_cash'] = fx_effect
        # Same emit-only-when-present rule for the new IAS 7 blocks.
        if disclosures:
            totals['disclosures'] = {
                item: data['total'] for item, data in disclosures.items()
            }
        if register_lines:
            totals['noncash_register'] = register_total
        if indirect_breakdown is not None:
            totals['cash_generated_from_operations'] = (
                indirect_breakdown['cgo'])
        return {
            'columns': self._build_two_column_layout(),
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
                'method': method,
                'reconciled': reconciled,
                # IAS 7.31 presentation policy actually applied to this
                # render, so the viewer / export can caption the disclosed
                # lines. Constant-by-default (company defaults), so cached
                # payloads for untagged books remain shape-stable.
                'disclosure_policy': dict(policy),
            },
        }

    # ---- internal helpers ----

    def _get_cash_equivalent_ids(self, company_ids):
        """Configured cash-and-cash-equivalent account ids (IAS 7.6).

        Read with sudo like the other per-company report mappings: the
        report user may lack write access on res.company but the mapping
        is plain configuration. Returns a tuple; empty when unconfigured,
        which keeps every code path byte-identical to the pre-feature
        behaviour.
        """
        companies = self.env['res.company'].sudo().browse(
            [int(c) for c in company_ids])
        return tuple(
            companies.mapped('eh_cash_equivalent_account_ids').ids)

    def _apply_cash_account_filter(self, query, equivalent_ids):
        """Constrain a MoveLineQuery to cash accounts.

        Cash = accounts of the CASH_TYPES account types, plus any
        explicitly configured cash-equivalent accounts. With no
        equivalents this renders the exact same SQL as before
        (where_account_types on CASH_TYPES).
        """
        if equivalent_ids:
            query.join_account()
            query.where_raw(SQL(
                "(acc.account_type IN %s OR aml.account_id IN %s)",
                tuple(self.CASH_TYPES), tuple(equivalent_ids),
            ))
        else:
            query.where_account_types(self.CASH_TYPES)
        return query

    def _fetch_cash_active_move_ids(
        self, company_ids, date_from, date_to, posted_only, options,
        equivalent_ids=(),
    ):
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_date_range(date_from=date_from, date_to=date_to)
        self._apply_cash_account_filter(query, equivalent_ids)
        if posted_only:
            query.where_posted_only()
        self.apply_common_filters(query, options)
        # GROUP BY produces unique move_ids without needing DISTINCT.
        query.select_field('move_id')
        query.group_by('move_id')
        return [r['move_id'] for r in query.execute()]

    def _fetch_fx_effect(
        self, company_ids, date_from, date_to, posted_only, options,
        equivalent_ids=(),
    ):
        """IAS 7.28: effect of exchange rate changes on cash held.

        Detection is the union of two independent seams; a move qualifies if
        EITHER matches, so a book can be picked up by nominated journal, by
        counterpart account, or by both without double counting.

        1. Journal seam: journal entries posted in the company's designated
           exchange difference journal (currency_exchange_journal_id), plus
           any configured cash revaluation journal
           (eh_cash_fx_revaluation_journal_id), that carry at least one cash
           line in the period. The extra revaluation journal is included
           because core Odoo posts no cash line into
           currency_exchange_journal_id on AR/AP settlement, so a bank / cash
           revaluation run posted elsewhere would otherwise be missed and leak
           into the opening-to-closing difference while the FX line stayed
           zero. Left unconfigured, only currency_exchange_journal_id is used.

        2. Counterpart seam (journal-agnostic): moves that carry both a cash
           line AND a line on one of the company's exchange gain/loss accounts
           (income_currency_exchange_account_id /
           expense_currency_exchange_account_id). This catches a stock-Odoo
           bank revaluation posted to an ordinary bank journal, which the
           journal seam alone misses, without requiring any nominated journal.

        For every qualifying move the FX effect is the sum of that move's
        cash-line balances; the move ids are returned so the caller can pull
        them out of the activity sections.

        Returns (fx_effect, fx_move_ids). Companies with neither an exchange
        journal nor exchange gain/loss accounts configured, or with no such
        postings, yield (0.0, []) and the report is unchanged.
        """
        companies = self.env['res.company'].sudo().browse(
            [int(c) for c in company_ids])
        fx_journal_ids = []
        if 'currency_exchange_journal_id' in companies._fields:
            fx_journal_ids += companies.mapped(
                'currency_exchange_journal_id').ids
        if 'eh_cash_fx_revaluation_journal_id' in companies._fields:
            fx_journal_ids += companies.mapped(
                'eh_cash_fx_revaluation_journal_id').ids
        # Dedupe while keeping order stable.
        fx_journal_ids = list(dict.fromkeys(fx_journal_ids))

        # Exchange gain/loss counterpart accounts (guarded: these fields
        # exist on core res.company across 16-19, but stay defensive so the
        # handler never raises on a stripped-down company model).
        fx_account_ids = []
        for fname in ('income_currency_exchange_account_id',
                      'expense_currency_exchange_account_id'):
            if fname in companies._fields:
                fx_account_ids += companies.mapped(fname).ids
        fx_account_ids = list(dict.fromkeys(fx_account_ids))

        if not fx_journal_ids and not fx_account_ids:
            return 0.0, []

        # Identify the qualifying move set first. A move qualifies if it is
        # posted in a nominated FX journal, OR it carries a line on an
        # exchange gain/loss account. Both branches are scoped by the same
        # common option filters so the fx move set is always a subset of the
        # cash-active move set; combining them at move granularity means a
        # move matched by both seams is counted exactly once.
        fx_move_ids = set()
        if fx_journal_ids:
            fx_move_ids |= set(self._fetch_fx_move_ids_by_predicate(
                company_ids=company_ids,
                date_from=date_from, date_to=date_to,
                posted_only=posted_only, options=options,
                equivalent_ids=equivalent_ids,
                predicate=SQL("aml.journal_id IN %s", tuple(fx_journal_ids)),
            ))
        if fx_account_ids:
            fx_move_ids |= set(self._fetch_fx_move_ids_by_predicate(
                company_ids=company_ids,
                date_from=date_from, date_to=date_to,
                posted_only=posted_only, options=options,
                equivalent_ids=equivalent_ids,
                predicate=SQL(
                    "aml.move_id IN ("
                    "SELECT move_id FROM account_move_line "
                    "WHERE account_id IN %s)",
                    tuple(fx_account_ids),
                ),
            ))
        if not fx_move_ids:
            return 0.0, []

        # The FX effect is the sum of the cash-line balances of those moves.
        # Summed here (not in the per-seam queries) so a move matched by both
        # seams contributes its cash balance once.
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_date_range(date_from=date_from, date_to=date_to)
        self._apply_cash_account_filter(query, equivalent_ids)
        if posted_only:
            query.where_posted_only()
        query.where_raw(SQL("aml.move_id IN %s", tuple(fx_move_ids)))
        self.apply_common_filters(query, options)
        query.select(SQL("SUM(aml.balance)"), 'fx_balance')
        rows = query.execute()
        fx_effect = sum(float(r['fx_balance'] or 0.0) for r in rows)
        return round(fx_effect, 2), sorted(fx_move_ids)

    def _fetch_fx_move_ids_by_predicate(
        self, company_ids, date_from, date_to, posted_only, options,
        equivalent_ids, predicate,
    ):
        """Cash-active move ids matching a raw FX predicate.

        Shared body for both FX detection seams: constrained to cash lines in
        the period under the same common option filters as
        _fetch_cash_active_move_ids, plus the supplied predicate. Returns a
        list of move ids (deduped by GROUP BY).
        """
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_date_range(date_from=date_from, date_to=date_to)
        self._apply_cash_account_filter(query, equivalent_ids)
        if posted_only:
            query.where_posted_only()
        query.where_raw(predicate)
        self.apply_common_filters(query, options)
        query.select_field('move_id')
        query.group_by('move_id')
        return [r['move_id'] for r in query.execute()]

    def _fetch_cash_impacts(
        self, move_ids, company_ids, posted_only, options,
        equivalent_ids=(),
    ):
        if not move_ids:
            return {}
        query = MoveLineQuery(self.env, company_ids=company_ids)
        if posted_only:
            query.where_posted_only()
        query.join_account()
        if equivalent_ids:
            # Counterpart lines: everything that is not cash, where cash
            # includes the configured equivalents. A transfer between real
            # cash and an equivalent therefore has no counterpart at all
            # (pure cash transfer), exactly like a bank-to-bank transfer.
            query.where_raw(SQL(
                "(acc.account_type NOT IN %s AND aml.account_id NOT IN %s)",
                tuple(self.CASH_TYPES), tuple(equivalent_ids),
            ))
        else:
            query.where_raw(SQL("acc.account_type != %s", 'asset_cash'))
        query.where_raw(SQL("aml.move_id IN %s", tuple(move_ids)))
        if options.get('journal_ids'):
            query.where_journals(options['journal_ids'])
        if options.get('partner_ids'):
            query.where_partners(options['partner_ids'])
        if options.get('analytic_account_ids'):
            query.where_analytic_accounts(options['analytic_account_ids'])
        if options.get('analytic_plan_ids'):
            query.where_analytic_plans(options['analytic_plan_ids'])

        query.select_account_field('account_type', alias='account_type')
        query.select(SQL("SUM(-aml.balance)"), 'cash_impact')
        query.group_by(SQL("acc.account_type"))

        rows = query.execute()
        return {
            r['account_type']: float(r['cash_impact'] or 0.0)
            for r in rows
        }

    # ---- reconciliation-accurate direct method ----

    NON_OPERATING_COUNTERPART = ('asset_cash', 'asset_receivable',
                                 'liability_payable')

    def _fetch_cash_impacts_reconciled(
        self, move_ids, company_ids, posted_only, options,
        equivalent_ids=(),
    ):
        """Reconciliation-accurate cash impacts.

        Same total as the coarse direct method, but a cash movement that
        settles a receivable / payable is attributed to what that AR/AP
        actually settled. The settling line's reconciliation partials are
        followed to the invoice/bill, whose income/expense (and tax)
        breakdown distributes the cash impact across the real activity
        buckets. AR/AP with no reconciliation falls back to its own type.
        Analytic option filters are not applied here (they do not express
        on the traced counterpart); journal/partner filters are.
        """
        from collections import defaultdict
        domain = [
            ('move_id', 'in', list(move_ids)),
            ('account_id.account_type', '!=', 'asset_cash'),
            ('company_id', 'in', list(company_ids)),
        ]
        if equivalent_ids:
            # Configured cash equivalents are cash, never a counterpart.
            domain.append(('account_id', 'not in', list(equivalent_ids)))
        if posted_only:
            domain.append(('parent_state', '=', 'posted'))
        if options.get('journal_ids'):
            domain.append(('journal_id', 'in', list(options['journal_ids'])))
        if options.get('partner_ids'):
            domain.append(('partner_id', 'in', list(options['partner_ids'])))

        impacts = defaultdict(float)
        for line in self.env['account.move.line'].search(domain):
            cash_impact = -line.balance
            account_type = line.account_id.account_type
            if account_type in ('asset_receivable', 'liability_payable'):
                distribution = self._trace_reconciled_types(line)
                if distribution:
                    for traced_type, fraction in distribution.items():
                        impacts[traced_type] += cash_impact * fraction
                else:
                    impacts[account_type] += cash_impact
            else:
                impacts[account_type] += cash_impact
        return {t: round(v, 2) for t, v in impacts.items()}

    def _trace_reconciled_types(self, ar_ap_line):
        """Return {account_type: fraction} describing what the receivable /
        payable line settled, by reading its reconciliation partials back
        to the counterpart move's income/expense/tax lines. Empty dict if
        the line is unreconciled or the counterpart carries no activity."""
        from collections import defaultdict
        partials = ar_ap_line.matched_debit_ids | ar_ap_line.matched_credit_ids
        if not partials:
            return {}
        counterpart_lines = (
            partials.debit_move_id | partials.credit_move_id
        ) - ar_ap_line
        type_amounts = defaultdict(float)
        total = 0.0
        for counterpart in counterpart_lines:
            activity = counterpart.move_id.line_ids.filtered(
                lambda line_item: line_item.account_id.account_type
                not in self.NON_OPERATING_COUNTERPART)
            for activity_line in activity:
                weight = abs(activity_line.balance)
                if not weight:
                    continue
                type_amounts[activity_line.account_id.account_type] += weight
                total += weight
        if not total:
            return {}
        return {t: amt / total for t, amt in type_amounts.items()}

    def _fetch_cash_balance(
        self, company_ids, cutoff_date, posted_only, before,
        equivalent_ids=(), options=None,
    ):
        """Sum balance on cash accounts (including configured equivalents).

        before=True: lines strictly before cutoff_date (used for opening).
        before=False: lines up to and including cutoff_date (closing).

        The same common option filters (journal / partner / account /
        analytic) that scope net_change and fx are applied here, so opening
        and closing measure the identical filtered subset. Without this the
        activity sections would report a filtered subset of cash movement
        while the balances reported the whole ledger, and the Balance Check
        identity (Closing == Opening + Net Change + FX) would surface a
        spurious residual whenever the report is scoped to one journal or
        partner. With no filters set the SQL is unchanged.
        """
        query = MoveLineQuery(self.env, company_ids=company_ids)
        self._apply_cash_account_filter(query, equivalent_ids)
        if posted_only:
            query.where_posted_only()
        if before:
            query.where_raw(SQL("aml.date < %s", cutoff_date))
        else:
            query.where_date_range(date_to=cutoff_date)
        self.apply_common_filters(query, options or {})
        query.select(SQL("COALESCE(SUM(aml.balance), 0)"), 'balance')
        rows = query.execute()
        if not rows:
            return 0.0
        return float(rows[0].get('balance') or 0.0)

    @staticmethod
    def _sum_types(impacts_by_type, types):
        return sum(impacts_by_type.get(t, 0.0) for t in types)

    def _render_section(
        self, name, section_id, types, impacts_by_type,
        section_total, show_zero, extra_rows=None,
    ):
        lines = [self._section_header_line(name, section_id)]
        type_labels = self._get_account_type_labels()
        for t in types:
            amount = round(impacts_by_type.get(t, 0.0), 2)
            if not show_zero and amount == 0.0:
                continue
            label = type_labels.get(t, t.replace('_', ' ').title())
            lines.append({
                'id': "section-%s-line-%s" % (section_id, t),
                'name': label,
                'level': 1,
                'columns': [
                    {'expression_label': 'amount', 'value': amount},
                ],
                'unfoldable': False,
                'meta': {
                    'kind': 'section_line',
                    'section_id': section_id,
                    'account_type': t,
                },
            })
        # IAS 7.31-35 disclosure rows assigned to this section by policy,
        # rendered after the account-type buckets and inside the total.
        for item, amount in (extra_rows or []):
            line = self._disclosure_line(item, amount, section_id)
            if line is not None and (show_zero or line['columns'][0]['value']):
                lines.append(line)
        lines.append(self._section_total_line(
            _("Total %s") % name, section_total, section_id=section_id,
        ))
        return lines

    # ---- IAS 7.31-35 disclosure helpers ----

    @api.model
    def _get_disclosure_labels(self):
        """Disclosure display labels, resolved per call for translations."""
        return {
            'interest_paid': _("Interest paid"),
            'interest_received': _("Interest received"),
            'dividends_paid': _("Dividends paid"),
            'dividends_received': _("Dividends received"),
            'income_tax_paid': _("Income taxes paid"),
        }

    def _disclosure_line(self, item, amount, section_id):
        labels = self._get_disclosure_labels()
        if item not in labels:
            return None
        return {
            'id': "disclosure-%s" % item,
            'name': labels[item],
            'level': 1,
            'columns': [
                {'expression_label': 'amount', 'value': round(amount, 2)},
            ],
            'unfoldable': False,
            'meta': {
                'kind': 'disclosure_line',
                'section_id': section_id,
                'item': item,
            },
        }

    def _tagged_account_ids(self, xmlid):
        """Account ids carrying the tag registered under `xmlid`.

        Company scoping is unnecessary here: the ids only ever appear in
        WHERE clauses of queries already company-scoped by MoveLineQuery.
        Returns [] when the tag record is absent (partial data load).
        """
        tag = self.env.ref(xmlid, raise_if_not_found=False)
        if not tag:
            return []
        return self.env['account.account'].sudo().search(
            [('tag_ids', 'in', tag.ids)]).ids

    def _tax_authority_account_ids(self, company_ids):
        """Fallback identification of tax-authority payable accounts.

        Used for the IAS 7.35 income-taxes-paid line only when no account
        carries the EH Income Tax Paid tag AND the fallback is switched on
        (company eh_cf_tax_fallback / option cf_income_tax_fallback): the
        accounts named as tax repartition targets are where core Odoo
        posts amounts owed to the tax authority. Measurement downstream is
        restricted to settlement lines (payable-reducing lines of
        cash-affecting entries, see _fetch_disclosure_flows) and applies
        to the direct method's presentation only. Documented limitation:
        the amounts cannot distinguish income tax from indirect taxes
        (VAT/GST remittances qualify); tag the income tax accounts
        explicitly to isolate taxes on income.
        """
        if 'account.tax.repartition.line' not in self.env:
            return []
        Repartition = self.env['account.tax.repartition.line'].sudo()
        domain = [('account_id', '!=', False)]
        if 'company_id' in Repartition._fields:
            domain.append(('company_id', 'in', [int(c) for c in company_ids]))
        return list(set(Repartition.search(domain).mapped('account_id').ids))

    def _income_tax_fallback_enabled(self, options, company_ids):
        """Opt-in gate for the tax-repartition-target fallback.

        Off by default: with no account tagged EH Income Tax Paid an
        untagged book must render byte-identically to a build without
        the disclosure feature (no fallback-derived rows). Priority:
        the cf_income_tax_fallback render option when present (explicit
        True/False both honoured), else the eh_cf_tax_fallback company
        flag (enabled when any company in scope enables it), else off.
        """
        requested = (options or {}).get('cf_income_tax_fallback')
        if requested is not None:
            return bool(requested)
        companies = self.env['res.company'].sudo().browse(
            [int(c) for c in company_ids])
        if 'eh_cf_tax_fallback' in companies._fields:
            return any(companies.mapped('eh_cf_tax_fallback'))
        return False

    def _resolve_disclosure_accounts(self, company_ids, options=None):
        """Map disclosure item -> account ids measured for it.

        Tagged accounts per item; income_tax_paid falls back to the tax
        repartition targets when untagged, but only when the fallback is
        opted into (_income_tax_fallback_enabled). Resolved once per
        compute and threaded everywhere (extraction, bucket
        re-attribution, and the indirect method's working-capital
        exclusions) so every consumer sees the identical account set.

        Returns (accounts, fallback_items): fallback_items names the
        items whose accounts came from the fallback rather than tags, so
        the callers can restrict their measurement to settlement lines
        and keep the indirect method's tie intact.
        """
        accounts = {}
        fallback_items = set()
        for item in self.DISCLOSURE_ORDER:
            ids = self._tagged_account_ids(self.DISCLOSURE_TAG_XMLIDS[item])
            if (item == 'income_tax_paid' and not ids
                    and self._income_tax_fallback_enabled(
                        options, company_ids)):
                ids = self._tax_authority_account_ids(company_ids)
                if ids:
                    fallback_items.add(item)
            if ids:
                accounts[item] = ids
        return accounts, fallback_items

    def _resolve_disclosure_policy(self, options, company_ids):
        """Presentation section per disclosed item (IAS 7.31/7.35).

        Priority: options['cf_<item>_section'] when valid, else the
        company policy field (first company in scope), else the default.
        """
        company = self.env['res.company'].sudo().browse(
            int(company_ids[0])) if company_ids else self.env.company.sudo()
        policy = {}
        for item, (default, allowed, field_name) in (
                self.DISCLOSURE_POLICY.items()):
            section = default
            if field_name and field_name in company._fields:
                configured = company[field_name]
                if configured in allowed:
                    section = configured
            requested = (options or {}).get('cf_%s_section' % item)
            if requested in allowed:
                section = requested
            policy[item] = section
        return policy

    def _fetch_disclosure_flows(
        self, cash_move_ids, company_ids, posted_only, options,
        disclosure_accounts, fallback_items=(),
    ):
        """Measure each disclosed flow off tagged lines of cash entries.

        For every disclosure item with resolved accounts, sums the cash
        impact (-balance, the direct method's sign convention: inflow
        positive) of the journal items on those accounts within the
        cash-affecting move set, grouped by account_type so the caller can
        re-attribute the amounts out of the aggregate buckets exactly.
        Same option filters as _fetch_cash_impacts so bucket adjustment
        and bucket computation always see the same scope.

        Items in fallback_items (income taxes paid measured off the tax
        repartition targets rather than tagged accounts) count settlement
        lines only: lines that reduce the payable (debit, balance > 0)
        inside the cash-affecting move set. Tax ACCRUED within a cash
        entry (a POS session closing, a bank-journal cash sale with tax)
        is a credit on those accounts; it stays inside its aggregate
        account-type bucket, so the fallback line is always an outflow
        and can never render a positive "taxes paid". Tagged accounts are
        measured in full, both signs, exactly as before.

        Returns {item: {'total': float, 'by_type': {account_type: float}}}
        with zero-flow items omitted.
        """
        flows = {}
        if not cash_move_ids:
            return flows
        for item in self.DISCLOSURE_ORDER:
            account_ids = disclosure_accounts.get(item)
            if not account_ids:
                continue
            query = MoveLineQuery(self.env, company_ids=company_ids)
            if posted_only:
                query.where_posted_only()
            query.join_account()
            query.where_raw(SQL(
                "aml.move_id IN %s", tuple(cash_move_ids)))
            query.where_raw(SQL(
                "aml.account_id IN %s", tuple(account_ids)))
            if item in fallback_items:
                # Settlement lines only (see docstring above).
                query.where_raw(SQL("aml.balance > 0"))
            if options.get('journal_ids'):
                query.where_journals(options['journal_ids'])
            if options.get('partner_ids'):
                query.where_partners(options['partner_ids'])
            if options.get('analytic_account_ids'):
                query.where_analytic_accounts(
                    options['analytic_account_ids'])
            if options.get('analytic_plan_ids'):
                query.where_analytic_plans(options['analytic_plan_ids'])
            query.select_account_field('account_type', alias='account_type')
            query.select(SQL("SUM(-aml.balance)"), 'cash_impact')
            query.group_by(SQL("acc.account_type"))
            by_type = {
                r['account_type']: float(r['cash_impact'] or 0.0)
                for r in query.execute()
            }
            total = round(sum(by_type.values()), 2)
            if by_type and total:
                flows[item] = {'total': total, 'by_type': by_type}
        return flows

    # ---- indirect-method engine (IAS 7.18(b)/7.20) ----

    INDIRECT_WORKING_CAPITAL_TYPES = (
        ('asset_receivable', _lt("Decrease (Increase) in Receivables")),
        ('liability_payable', _lt("Increase (Decrease) in Payables")),
        ('asset_current', _lt(
            "Decrease (Increase) in Other Current Assets",
        )),
        ('liability_current', _lt(
            "Increase (Decrease) in Other Current Liabilities",
        )),
    )

    @api.model
    def _get_noncash_addback_labels(self):
        """Non-cash adjustment labels, resolved per call for translations."""
        return {
            'depreciation': _("Depreciation and amortisation (non-cash)"),
            'impairment': _("Impairment losses (non-cash)"),
            'provisions': _("Provisions (non-cash)"),
            'fx': _("Unrealised foreign exchange (non-cash)"),
            'fair_value': _("Fair value adjustments (non-cash)"),
        }

    def _compute_indirect_operating(
        self, company_ids, date_from, date_to, posted_only, options,
        cash_move_ids, equivalent_ids, disclosure_accounts,
        operating_disclosures,
    ):
        """Derive the operating section from the ledger (indirect method).

        Presentation (each term its own row):

            Profit before tax
            + finance costs (interest expense add-back)
            - investment income (interest / dividend income backed out)
            + non-cash adjustments resolved from the EH Non-Cash tags
            +/- working-capital deltas per balance-sheet group
            = Cash generated from operations
            - interest paid / income taxes paid / other disclosed flows
              the IAS 7.31 policy assigns to operating
            = Net cash from operating activities

        Exactness contract (the tie invariant): under the default policy
        the total equals the direct method's operating total on the same
        ledger. Term by term:

        * Profit before tax = net income plus the income tax expense
          identified by the company Tax Expense mapping and/or tagged
          P&L accounts. The tax expense re-enters through the deltas /
          disclosed lines, so adding it here is presentation, not a gap.
        * The finance-cost and investment-income adjustments back the
          tagged interest / dividend P&L amounts out of profit; the cash
          portion re-enters through the disclosed lines, the accrued
          portion through nothing - its clearing accounts are excluded
          from the working-capital deltas below.
        * Non-cash adjustments: for every non-cash entry with a tagged
          (or expense_depreciation-typed) P&L line, the add-back is that
          entry's movement on non-current balance-sheet accounts - see
          _fetch_noncash_addbacks.
        * Working-capital deltas are the period balance movement per
          operating balance-sheet group, sign-corrected to cash effect,
          excluding accounts measured by a disclosed line.

        Returns the breakdown dict consumed by
        _render_indirect_operating_section, including 'cgo' and 'total'.
        """
        companies = self.env['res.company'].sudo().browse(
            [int(c) for c in company_ids])

        # Profit before tax.
        pl_balance = self._sum_balance_in_period(
            company_ids, self.PL_TYPES,
            date_from, date_to, posted_only, options,
        )
        net_income = -pl_balance
        tax_account_ids = set(disclosure_accounts.get('income_tax_paid', []))
        if 'eh_pnl_tax_expense_account_ids' in companies._fields:
            tax_account_ids |= set(
                companies.mapped('eh_pnl_tax_expense_account_ids').ids)
        tax_expense = self._sum_balance_on_accounts(
            company_ids, sorted(tax_account_ids),
            date_from, date_to, posted_only, options,
        )
        pbt = net_income + tax_expense

        # Finance costs added back / investment income backed out. The
        # P&L-only restriction inside _sum_balance_on_accounts means a
        # tagged clearing account (interest payable...) never lands here.
        fincost_ids = set(disclosure_accounts.get('interest_paid', []))
        if 'eh_pnl_finance_cost_account_ids' in companies._fields:
            fincost_ids |= set(
                companies.mapped('eh_pnl_finance_cost_account_ids').ids)
        adj_finance_costs = self._sum_balance_on_accounts(
            company_ids, sorted(fincost_ids),
            date_from, date_to, posted_only, options,
        )
        invinc_ids = (
            set(disclosure_accounts.get('interest_received', []))
            | set(disclosure_accounts.get('dividends_received', []))
        )
        adj_investment_income = self._sum_balance_on_accounts(
            company_ids, sorted(invinc_ids),
            date_from, date_to, posted_only, options,
        )

        # Non-cash add-backs from the tag registry.
        addbacks = self._fetch_noncash_addbacks(
            company_ids=company_ids,
            date_from=date_from, date_to=date_to,
            posted_only=posted_only, options=options,
            cash_move_ids=cash_move_ids, equivalent_ids=equivalent_ids,
        )
        noncash_lines = []
        noncash_total = 0.0
        labels = self._get_noncash_addback_labels()
        for key in self.NONCASH_ORDER:
            amount = addbacks.get(key, 0.0)
            noncash_lines.append({
                'key': key,
                'label': labels[key],
                'amount': round(amount, 2),
            })
            noncash_total += amount

        # Working-capital deltas. cash effect = -(period balance movement)
        # uniformly: an asset build-up (positive balance movement) uses
        # cash, a liability build-up (negative balance movement, credit)
        # provides it. Accounts measured by a disclosed line (tagged
        # interest / tax clearing accounts) are excluded: their
        # settlements appear on the disclosure rows instead. The
        # tax-authority fallback set never reaches here - compute() strips
        # fallback items from disclosure_accounts in indirect mode - so
        # those accounts stay inside the deltas and the tie to the direct
        # method holds without a P&L tax bridge.
        excluded_ids = sorted({
            account_id
            for ids in disclosure_accounts.values()
            for account_id in ids
        })
        wc_lines = []
        wc_total = 0.0
        for account_type, label in self.INDIRECT_WORKING_CAPITAL_TYPES:
            delta_balance = self._sum_balance_in_period(
                company_ids, (account_type,),
                date_from, date_to, posted_only, options,
                exclude_account_ids=excluded_ids,
            )
            cash_effect = -delta_balance
            wc_lines.append({
                'account_type': account_type,
                'label': str(label),
                'amount': round(cash_effect, 2),
            })
            wc_total += cash_effect

        cgo = (
            pbt + adj_finance_costs + adj_investment_income
            + noncash_total + wc_total
        )
        total = cgo + sum(
            amount for _item, amount in operating_disclosures)
        return {
            'net_income': round(net_income, 2),
            'tax_expense': round(tax_expense, 2),
            'pbt': round(pbt, 2),
            'adj_finance_costs': round(adj_finance_costs, 2),
            'adj_investment_income': round(adj_investment_income, 2),
            'noncash_lines': noncash_lines,
            'wc_lines': wc_lines,
            'working_capital': round(wc_total, 2),
            'cgo': round(cgo, 2),
            'operating_disclosures': list(operating_disclosures),
            'total': round(total, 2),
        }

    def _fetch_noncash_addbacks(
        self, company_ids, date_from, date_to, posted_only, options,
        cash_move_ids, equivalent_ids,
    ):
        """Exact non-cash add-backs per tag category.

        Trigger: a P&L line in the period on an account tagged with one of
        the EH Non-Cash tags (or, for depreciation, of account_type
        expense_depreciation - the implicit tag), in an entry that touches
        no cash. For every trigger entry the add-back is minus the entry's
        movement on accounts outside P&L, working capital and cash - i.e.
        exactly the non-current movement that neither profit nor the
        working-capital deltas can see.

        Why not simply the tagged expense amount: a tagged charge whose
        counterpart is itself a working-capital account (a provision
        credited to a current liability) is already neutralised by the
        delta lines; its entry has no non-current movement and correctly
        contributes zero here. This is what makes the indirect operating
        total tie exactly to the direct method.

        Entries carrying trigger lines of several categories allocate the
        add-back proportionally to the absolute tagged amounts. Returns
        {category: float} with zero categories omitted.
        """
        category_accounts = {
            key: tuple(self._tagged_account_ids(
                self.NONCASH_TAG_XMLIDS[key]))
            for key in self.NONCASH_ORDER
        }
        move_weights = {}
        for key in self.NONCASH_ORDER:
            account_ids = category_accounts[key]
            if key != 'depreciation' and not account_ids:
                continue
            query = MoveLineQuery(self.env, company_ids=company_ids)
            query.where_date_range(date_from=date_from, date_to=date_to)
            if posted_only:
                query.where_posted_only()
            query.join_account()
            # Trigger lines are P&L lines only: tagging the balance-sheet
            # side (accumulated depreciation) must not create a trigger.
            query.where_raw(SQL(
                "acc.account_type IN %s", tuple(self.PL_TYPES)))
            if key == 'depreciation':
                if account_ids:
                    query.where_raw(SQL(
                        "(aml.account_id IN %s OR acc.account_type = %s)",
                        account_ids, 'expense_depreciation'))
                else:
                    query.where_raw(SQL(
                        "acc.account_type = %s", 'expense_depreciation'))
            else:
                query.where_raw(SQL(
                    "aml.account_id IN %s", account_ids))
            if cash_move_ids:
                query.where_raw(SQL(
                    "aml.move_id NOT IN %s", tuple(cash_move_ids)))
            self.apply_common_filters(query, options)
            query.select_field('move_id')
            query.select(SQL("SUM(ABS(aml.balance))"), 'weight')
            query.group_by('move_id')
            for row in query.execute():
                weight = float(row['weight'] or 0.0)
                if weight:
                    move_weights.setdefault(
                        row['move_id'], {})[key] = weight

        if not move_weights:
            return {}

        # Non-current movement per trigger entry: everything outside P&L,
        # working capital and cash (configured cash equivalents included).
        visible_types = (
            tuple(self.PL_TYPES) + tuple(self.WC_TYPES)
            + tuple(self.CASH_TYPES)
        )
        query = MoveLineQuery(self.env, company_ids=company_ids)
        if posted_only:
            query.where_posted_only()
        query.join_account()
        query.where_raw(SQL(
            "aml.move_id IN %s", tuple(move_weights)))
        query.where_raw(SQL(
            "acc.account_type NOT IN %s", visible_types))
        if equivalent_ids:
            query.where_raw(SQL(
                "aml.account_id NOT IN %s", tuple(equivalent_ids)))
        query.select_field('move_id')
        query.select(SQL("SUM(aml.balance)"), 'balance')
        query.group_by('move_id')
        x_by_move = {
            r['move_id']: float(r['balance'] or 0.0)
            for r in query.execute()
        }

        addbacks = {}
        for move_id, weights in move_weights.items():
            addback = -x_by_move.get(move_id, 0.0)
            if not addback:
                continue
            weight_sum = sum(weights.values())
            for key, weight in weights.items():
                addbacks[key] = (
                    addbacks.get(key, 0.0)
                    + addback * (weight / weight_sum)
                )
        return addbacks

    def _sum_balance_in_period(
        self, company_ids, account_types, date_from, date_to,
        posted_only, options, exclude_account_ids=None,
    ):
        if not account_types:
            return 0.0
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_date_range(date_from=date_from, date_to=date_to)
        query.where_account_types(account_types)
        if posted_only:
            query.where_posted_only()
        if exclude_account_ids:
            query.where_raw(SQL(
                "aml.account_id NOT IN %s", tuple(exclude_account_ids)))
        self.apply_common_filters(query, options)
        query.select(SQL("COALESCE(SUM(aml.balance), 0)"), 'balance')
        rows = query.execute()
        return float(rows[0].get('balance') or 0.0) if rows else 0.0

    def _sum_balance_on_accounts(
        self, company_ids, account_ids, date_from, date_to,
        posted_only, options, pl_only=True,
    ):
        """Period balance sum over explicit accounts.

        pl_only (default) restricts to P&L account types so a tagged
        clearing account (interest payable, tax payable) contributes
        nothing to the profit-side adjustments even when the operator
        tags both sides of an accrual, as the tag help text instructs.
        """
        if not account_ids:
            return 0.0
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_date_range(date_from=date_from, date_to=date_to)
        if posted_only:
            query.where_posted_only()
        query.where_raw(SQL(
            "aml.account_id IN %s", tuple(account_ids)))
        if pl_only:
            query.join_account()
            query.where_raw(SQL(
                "acc.account_type IN %s", tuple(self.PL_TYPES)))
        self.apply_common_filters(query, options)
        query.select(SQL("COALESCE(SUM(aml.balance), 0)"), 'balance')
        rows = query.execute()
        return float(rows[0].get('balance') or 0.0) if rows else 0.0

    def _render_indirect_operating_section(
        self, breakdown, section_total, show_zero,
    ):
        section_id = 'operating'

        def _row(line_id, name, amount, kind='section_line', **meta_extra):
            meta = {'kind': kind, 'section_id': section_id}
            meta.update(meta_extra)
            return {
                'id': line_id,
                'name': name,
                'level': 1,
                'columns': [{
                    'expression_label': 'amount',
                    'value': amount,
                }],
                'unfoldable': False,
                'meta': meta,
            }

        lines = [self._section_header_line(
            _("Operating Activities (indirect method)"), section_id,
        )]
        lines.append(_row(
            'indirect-pbt', _("Profit before tax"), breakdown['pbt'],
        ))
        if show_zero or breakdown['adj_finance_costs'] != 0.0:
            lines.append(_row(
                'indirect-adj-finance-costs',
                _("Finance costs (interest expense)"),
                breakdown['adj_finance_costs'],
            ))
        if show_zero or breakdown['adj_investment_income'] != 0.0:
            lines.append(_row(
                'indirect-adj-investment-income',
                _("Investment income (interest and dividends)"),
                breakdown['adj_investment_income'],
            ))
        for noncash in breakdown['noncash_lines']:
            if not show_zero and noncash['amount'] == 0.0:
                continue
            lines.append(_row(
                'indirect-noncash-%s' % noncash['key'],
                noncash['label'], noncash['amount'],
                noncash_key=noncash['key'],
            ))
        for wc in breakdown['wc_lines']:
            if not show_zero and wc['amount'] == 0.0:
                continue
            lines.append(_row(
                'indirect-wc-%s' % wc['account_type'],
                wc['label'], wc['amount'],
                account_type=wc['account_type'],
            ))
        lines.append(_row(
            'indirect-cgo', _("Cash generated from operations"),
            breakdown['cgo'], kind='section_subtotal',
        ))
        for item, amount in breakdown['operating_disclosures']:
            line = self._disclosure_line(item, amount, section_id)
            if line is not None and (
                    show_zero or line['columns'][0]['value']):
                lines.append(line)
        lines.append(self._section_total_line(
            _("Net cash from operating activities"),
            section_total, section_id=section_id,
        ))
        return lines

    # ---- IAS 7.43 non-cash register ----

    def _render_noncash_register(self, company_ids, date_from, date_to):
        """Memo section listing the period's registered non-cash
        transactions. Returns (lines, total); ([], 0.0) when the register
        holds nothing for the period, so untouched books render exactly
        as before this section existed.
        """
        if 'eh.noncash.transaction' not in self.env:
            return [], 0.0
        records = self.env['eh.noncash.transaction'].search([
            ('company_id', 'in', [int(c) for c in company_ids]),
            ('date', '>=', self._iso_date(date_from)),
            ('date', '<=', self._iso_date(date_to)),
        ], order='date, id')
        if not records:
            return [], 0.0
        section_id = 'noncash_register'
        lines = [self._section_header_line(
            _("Non-cash investing and financing activities"), section_id,
        )]
        total = 0.0
        kind_labels = dict(
            records._fields['kind']._description_selection(self.env))
        for record in records:
            amount = round(record.amount, 2)
            total += amount
            lines.append({
                'id': "noncash-%s" % record.id,
                'name': record.name,
                'level': 1,
                'columns': [
                    {'expression_label': 'amount', 'value': amount},
                ],
                'unfoldable': False,
                'meta': {
                    'kind': 'noncash_line',
                    'section_id': section_id,
                    'noncash_kind': record.kind,
                    'noncash_kind_label': kind_labels.get(
                        record.kind, record.kind),
                    'date': self._iso_date(record.date),
                },
            })
        total = round(total, 2)
        lines.append(self._section_total_line(
            _("Total non-cash transactions"), total,
            section_id=section_id,
        ))
        return lines, total
