# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Abstract base for dynamic report handlers.

Concrete handlers _inherit this model and override compute(). The
orchestrator (eh.account.dynamic.report) instantiates the handler by model
name and calls compute() with the options dict.

Why an Odoo AbstractModel and not a plain Python class:

* Localizations and add ons can extend a handler with _inherit, override
  compute(), and add new lines or columns without touching the base addon.
* Discovery through self.env[handler_model] is one line and consistent with
  the rest of the framework.
* Inheritance composition (multiple addons stacking on the same handler)
  works out of the box.

Shared helpers live here so concrete handlers stay small and focused on
report specific math:

* _extract_date(options, key): pulls and parses a date from options['date'].
* _iso_date(value): renders a date or string back to ISO format.
* get_drilldown_action(options, line_id): default account drill down to
  filtered journal items. Concrete handlers override only when their
  line_id scheme differs.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery
from odoo.addons.eh_account_base.tools.currency_table import CurrencyTable


class EhAccountDynamicReportHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler'
    _description = "Base for ERP Heritage dynamic report handlers"

    REPORT_CODE = ''
    REPORT_NAME = ""

    @api.model
    def build_default_options(self):
        """Return the default options dict for this report.

        Subclasses may override to add report specific defaults. Always call
        super() and merge.
        """
        today = fields.Date.today()
        first_of_month = today.replace(day=1)
        return {
            'date': {
                'mode': 'range',
                'date_from': first_of_month.isoformat(),
                'date_to': today.isoformat(),
            },
            'company_ids': list(
                self.env.context.get(
                    'allowed_company_ids',
                    [self.env.company.id],
                )
            ),
            'journal_ids': [],
            'partner_ids': [],
            'account_ids': [],
            'analytic_account_ids': [],
            'analytic_plan_ids': [],
            'unfolded_lines': [],
            'show_zero': False,
            'posted_only': True,
            'comparative': None,
            'currency_display': 'symbol',
        }

    @api.model
    def compute(self, options):
        """Compute the report data.

        Returns a dict with keys:

        * columns: list of dicts with name, expression_label, figure_type
          (string, monetary, percentage, integer, float, date, boolean).
        * lines: list of dicts with id, name, level, columns (list of value
          dicts), unfoldable (bool), parent_id (optional), meta (optional).
        * totals: optional dict mapping expression_label to summary value.
        * generated_at: ISO datetime string.
        """
        raise NotImplementedError(
            "Concrete handlers must override compute(options)."
        )

    @api.model
    def resolve_currency_info(self, options):
        """Resolve the currency block to attach to a report payload.

        When a single company is in scope, the company's currency is the
        report currency; when multiple companies share the same currency,
        the same applies. When the scope spans companies with different
        currencies the report cannot be expressed in a single currency
        without conversion, so we mark the payload as multi_currency and
        leave amount formatting to use no symbol.

        Returns a dict with keys: id, name, symbol, position, decimal_places,
        multi_currency.
        """
        company_ids = (
            options.get('company_ids')
            or list(self.env.context.get(
                'allowed_company_ids', [self.env.company.id],
            ))
        )
        companies = self.env['res.company'].sudo().browse(company_ids)
        currencies = companies.mapped('currency_id')
        unique = currencies.filtered(lambda c: c)
        if len(unique) <= 1 and unique:
            currency = unique[:1]
            return {
                'id': currency.id,
                'name': currency.name,
                'symbol': currency.symbol,
                'position': currency.position,
                'decimal_places': currency.decimal_places,
                'multi_currency': False,
            }
        return {
            'id': False,
            'name': '',
            'symbol': '',
            'position': 'after',
            'decimal_places': 2,
            'multi_currency': True,
        }

    @api.model
    def apply_common_filters(self, query, options):
        """Apply the standard option filters (journal_ids, partner_ids,
        account_ids, analytic_account_ids, analytic_plan_ids) to a
        MoveLineQuery. Centralised so adding a new dimension lands in one
        place instead of every concrete handler.
        """
        if options.get('journal_ids'):
            query.where_journals(options['journal_ids'])
        if options.get('partner_ids'):
            query.where_partners(options['partner_ids'])
        if options.get('account_ids'):
            query.where_accounts(options['account_ids'])
        if options.get('analytic_account_ids'):
            query.where_analytic_accounts(options['analytic_account_ids'])
        if options.get('analytic_plan_ids'):
            query.where_analytic_plans(options['analytic_plan_ids'])
        return query

    # ---- fiscal-year + multi-currency consolidation (WS4) ----

    @api.model
    def _resolve_currency_table(self, options, company_ids):
        """Build the CurrencyTable for a consolidated report run.

        Presentation currency = options['presentation_currency_id'] when set,
        else the active company's currency. The table is seeded as of the
        report's date_to (a spot-rate-as-of-close consolidation; per-aml
        date-effective rates are a documented follow-up). In the common
        single-company / single-currency case the returned table reports
        is_monocurrency True and threads through MoveLineQuery with zero
        SQL effect, so the hot path is unchanged.

        Never raises: any failure resolving the as-of date or constructing
        the table degrades to None, and every caller treats None exactly
        like a monocurrency table (raw sum).
        """
        options = options or {}
        try:
            company_ids = [
                int(c) for c in (company_ids or [self.env.company.id])
            ]
        except (TypeError, ValueError):  # pragma: no cover - defensive
            company_ids = [self.env.company.id]
        presentation_currency_id = options.get('presentation_currency_id')
        as_of = None
        try:
            as_of = self._extract_date(options, 'date_to')
        except Exception:  # pragma: no cover - date_to optional here
            as_of = None
        try:
            return CurrencyTable(
                self.env,
                company_ids=company_ids,
                presentation_currency_id=presentation_currency_id,
                as_of_date=as_of,
            )
        except Exception:  # pragma: no cover - defensive
            return None

    @api.model
    def _fiscalyear_start_for(self, company, date):
        """Return the first day of the fiscal year of `company` containing
        `date`.

        Wraps res.company.compute_fiscalyear_dates so GL, TB and the
        sectioned reports share one fiscal-year resolution. Honours a
        staggered (non-calendar) fiscal year automatically because the core
        helper already handles fiscalyear_last_day / fiscalyear_last_month.

        FALLBACK: if the company has no usable fiscal-year configuration, or
        the core helper raises, degrade to the calendar-year start
        (1 January of `date`'s year) rather than raising. A missing fiscal
        year must never break a report.
        """
        try:
            company = company.sudo()
            company.ensure_one()
            fy = company.compute_fiscalyear_dates(date)
            start = fy.get('date_from')
            if start is not None:
                return start
        except Exception:  # pragma: no cover - defensive
            pass
        # Calendar-year fallback.
        try:
            return date.replace(month=1, day=1)
        except Exception:  # pragma: no cover - defensive
            return date

    @api.model
    def _fiscalyear_starts(self, company_ids, date):
        """Map {company_id: fiscal_year_start_date} for `date`.

        O(companies) Python calls, each yielding at most one start date, so
        callers can bind them as a small CASE keyed on aml.company_id rather
        than issuing a query per company. Degrades per company to the
        calendar-year start via _fiscalyear_start_for.
        """
        starts = {}
        companies = self.env['res.company'].sudo().browse([
            int(c) for c in (company_ids or [])
        ])
        for company in companies:
            starts[company.id] = self._fiscalyear_start_for(company, date)
        return starts

    @api.model
    def _fiscalyear_start_case(self, fy_starts, default_date):
        """SQL CASE mapping aml.company_id -> that company's fiscal-year start.

        O(companies) bound constants, not O(rows), so the fiscal-year split
        stays a single SQL pass even when consolidated companies run on
        different fiscal calendars. A company absent from the map (should not
        happen) falls back to default_date (the report's date_from), which
        collapses its P&L current-FY-to-date contribution to zero rather than
        mis-rolling. Shared on the base so GL, TB and later sectioned reports
        bind the identical expression.
        """
        if not fy_starts:
            return SQL("%s::date", default_date)
        whens = []
        for company_id, start in fy_starts.items():
            whens.append(
                SQL("WHEN %s THEN %s::date", int(company_id), start))
        return SQL(
            "CASE aml.company_id %s ELSE %s::date END",
            SQL(" ").join(whens), default_date,
        )

    @api.model
    def get_drilldown_action(self, options, line_id):
        """Default drill down: open filtered journal items when line_id is
        of the form 'account-N'. Concrete handlers override only when their
        line_id scheme differs or when they want to disable drill down.
        """
        if not line_id or not isinstance(line_id, str):
            return None
        if not line_id.startswith('account-'):
            return None
        try:
            account_id = int(line_id.split('-', 1)[1])
        except ValueError:
            return None
        try:
            date_from = self._extract_date(options, 'date_from')
            date_to = self._extract_date(options, 'date_to')
        except UserError:
            return None
        company_ids = options.get('company_ids') or [self.env.company.id]
        domain = [
            ('account_id', '=', account_id),
            ('company_id', 'in', list(company_ids)),
            ('date', '>=', self._iso_date(date_from)),
            ('date', '<=', self._iso_date(date_to)),
        ]
        if options.get('posted_only', True):
            domain.append(('parent_state', '=', 'posted'))
        # Audit-cell fidelity: fold the same journal / partner filters the
        # report applied so the opened journal items reconstruct exactly
        # the figure in the cell, not a broader account total.
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

    @api.model
    def _eh_drilldown_filter_domain(self, options):
        """Domain fragments mirroring the report's common filters, so a
        drilldown matches the filtered cell. Journal and partner filters
        translate cleanly to an account.move.line domain; analytic
        filters are left to the SQL path (their distribution match does
        not express as a simple domain)."""
        extra = []
        journal_ids = options.get('journal_ids')
        if journal_ids:
            extra.append(('journal_id', 'in', list(journal_ids)))
        partner_ids = options.get('partner_ids')
        if partner_ids:
            extra.append(('partner_id', 'in', list(partner_ids)))
        return extra

    # ---- lazy expand engine (Wave 0 / Part A engine contract) ----

    @api.model
    def _resolve_expand_page_size(self, options):
        """Default page size for a lazy expand page.

        Reads res.company.eh_expand_page_size (Integer, default 80);
        options['expand_page_size'] overrides when present and valid.
        Mirrors the GL _resolve_row_limit shape. Degrades to a safe
        constant if the company field is absent (older schema) so the
        engine never raises here.
        """
        try:
            company_default = self.env.company.eh_expand_page_size or 80
        except Exception:  # pragma: no cover - schema fallback
            company_default = 80
        requested = options.get('expand_page_size')
        if requested is None:
            return company_default
        try:
            requested = int(requested)
        except (TypeError, ValueError):
            return company_default
        if requested <= 0:
            return company_default
        return requested

    @api.model
    def _account_line_is_expandable(self, line):
        """Decide whether an account leaf is a lazy-expandable line.

        Returns True for an account leaf (a line carrying
        meta.account_id) in a single-current-period mode. Returns False
        in any multi-column mode (comparison / N-period / horizontal
        pivot) because a single journal item cannot fill the prior /
        group / total columns, which would mis-align the positional
        column slicing on the client.

        The mode is read from the line's own options snapshot when the
        caller stashes one in meta['_expand_options']; otherwise the
        decision is made purely on the presence of meta.account_id and
        callers gate the multi-column case before constructing the leaf.
        """
        meta = (line or {}).get('meta') or {}
        if not meta.get('account_id'):
            return False
        opts = meta.get('_expand_options') or {}
        if self._eh_options_are_multi_column(opts):
            return False
        return True

    @api.model
    def _eh_options_are_multi_column(self, options):
        """True when options describe a layout a single aml cannot fill."""
        options = options or {}
        comparison = options.get('comparison') or 'none'
        if comparison and comparison != 'none':
            return True
        try:
            if int(options.get('comparison_number') or 1) > 1:
                return True
        except (TypeError, ValueError):
            pass
        if options.get('horizontal_group_by'):
            return True
        return False

    @api.model
    def _eh_apply_leaf_lazy_flags(self, line, options):
        """Stamp the lazy/unfoldable flags on an account leaf in place.

        Backward compatible on two axes:

        1. The lazy path is opt-in via options['lazy_expand'] (set by the
           OWL viewer). Without it the leaf keeps its legacy
           unfoldable: False shape, so direct compute() callers, export,
           and the existing test suite see byte-identical leaves.
        2. Even with the flag, a non-expandable leaf (multi-column mode,
           or no account_id) keeps unfoldable: False.
        """
        options = options or {}
        if not options.get('lazy_expand') or options.get('eager_expand'):
            return line
        meta = line.setdefault('meta', {})
        meta['_expand_options'] = options or {}
        expandable = self._account_line_is_expandable(line)
        # The transient options snapshot must not leak into the payload
        # (it bloats the cache key surface and is not serialisable-stable).
        meta.pop('_expand_options', None)
        if expandable:
            line['unfoldable'] = True
            line['unfolded'] = False
            line['lazy'] = True
            line['has_more'] = False
            meta['expandable'] = True
        return line

    @api.model
    def _expand_account_id_from_line_id(self, line_id):
        """Parse 'account-N' into N. Returns None on any other shape."""
        if not line_id or not isinstance(line_id, str):
            return None
        if not line_id.startswith('account-'):
            return None
        tail = line_id.split('-', 1)[1]
        try:
            return int(tail)
        except (ValueError, TypeError):
            return None

    @api.model
    def _expand_build_page_query(
        self, options, account_id, date_from, date_to,
    ):
        """Build the MoveLineQuery for one account's journal-item page.

        Reuses apply_common_filters and the report's [date_from, date_to]
        window so the page reconstructs EXACTLY the figure in the parent
        cell. Returns a query with all the columns _expand_child_columns
        may read; callers add offset/limit/order.
        """
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_date_range(date_from=date_from, date_to=date_to)
        query.where_accounts([account_id])
        if posted_only:
            query.where_posted_only()
        self.apply_common_filters(query, options)
        return query

    @api.model
    def _expand_select_columns(self, query):
        """Add the canonical aml column projection to an expand query."""
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
        query.join_journal()
        query.select(SQL("aj.code"), 'journal_code')
        query.join_partner()
        query.select(SQL("p.name"), 'partner_name')
        query.select(SQL("am.name"), 'move_name')
        return query

    @api.model
    def expand_account_line(self, options, line_id, offset=0, limit=None):
        """Shared server engine: fetch one account's journal-item page.

        Returns {child_lines, has_more, next_offset, total_count}.

        Parses 'account-N', builds a single-account MoveLineQuery over the
        report window with the report's common filters applied (so the page
        reconciles exactly to the cell), runs a count-only pre-flight for
        total_count, then fetches a (limit + 1) page ordered by (date, id);
        the +1 row is the has-more probe and is dropped before mapping.
        Each fetched row is projected through _expand_child_columns.
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

        # Count-only pre-flight: same filters, single COUNT.
        count_query = self._expand_build_page_query(
            options, account_id, date_from, date_to)
        count_query.select_count(alias='row_count')
        count_rows = count_query.execute()
        total_count = int(count_rows[0]['row_count']) if count_rows else 0

        page_query = self._expand_build_page_query(
            options, account_id, date_from, date_to)
        self._expand_select_columns(page_query)
        page_query.order_by('date', 'ASC')
        page_query.order_by('id', 'ASC')
        page_query.offset(offset)
        page_query.limit(limit + 1)
        rows = page_query.execute()

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        child_lines = []
        for aml_row in rows:
            child_lines.extend(self._expand_child_columns(options, aml_row))

        return {
            'child_lines': child_lines,
            'has_more': has_more,
            'next_offset': offset + len(rows),
            'total_count': total_count,
        }

    @api.model
    def _expand_child_columns(self, options, aml_row):
        """Per-report column projection for one fetched journal item.

        Base default: a single child line carrying date / move / partner /
        label and one signed amount in a 'balance' (fallback 'amount')
        column. Concrete handlers override to map onto the host report's
        own expression_labels. Returns a LIST of line dicts (one per aml
        in the base case) so an override may, in principle, emit several.
        """
        amount = round(float(aml_row.get('balance') or 0.0), 2)
        date_val = aml_row.get('date')
        return [{
            'id': "aml-%s" % aml_row.get('aml_id'),
            'name': aml_row.get('ref') or aml_row.get('line_label') or '',
            'level': 2,
            'columns': self._expand_default_columns(aml_row, amount),
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

    @api.model
    def _expand_default_columns(self, aml_row, amount):
        """Default single-amount column projection.

        Emits one cell per non-label column of the host report. Because the
        base default cannot know the host report's columns, it emits a
        single 'amount' cell; overrides replace this with the host layout.
        """
        return [{'expression_label': 'amount', 'value': amount}]

    # ---- shared helpers used by concrete handlers ----

    def _extract_date(self, options, key):
        """Pull options['date'][key] and return a Python date.

        Raises UserError if the date is missing. The error message includes
        the report's display name so the user can identify which report
        rejected the input.
        """
        date_block = options.get('date') or {}
        value = date_block.get(key)
        if not value:
            raise UserError(_(
                "%(report)s requires options['date'][%(key)s].",
                report=self.REPORT_NAME or "Report",
                key=repr(key),
            ))
        if isinstance(value, str):
            return fields.Date.from_string(value)
        return value

    @staticmethod
    def _iso_date(value):
        """Render a date or compatible value back to ISO format."""
        return value.isoformat() if hasattr(value, 'isoformat') else str(value)
