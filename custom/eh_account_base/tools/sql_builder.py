# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
SQL builder for account.move.line aggregation queries.

The reporting engine's hot path is here. All large reports compose a
MoveLineQuery, then call .execute() to fetch aggregated rows. The class is a
plain Python class (not an ORM model) so it can be unit tested without the
full Odoo registry.

Design rules:

1. No user supplied data ever interpolated into the SQL string. Every value
   binds via the SQL primitive's parameter mechanism.
2. Identifiers (column names, table aliases) come from a fixed whitelist
   in this module. Adding entries requires code changes, not configuration.
3. Multi company scope is mandatory; build() always emits a company_id filter.
4. Cancelled moves are excluded by default.
5. build() returns the composed SQL primitive without executing, so callers
   (and tests) can inspect the rendered query before deciding to run it.
"""

import re

from odoo.release import version_info
from odoo.tools import SQL

# From Odoo 17, account.account.code is company-dependent (stored as a jsonb
# 'code_store' keyed by company_id) and translated fields like `name` are
# jsonb keyed by language. On Odoo 16 both are plain columns, so the jsonb
# ->> operator does not apply and the bare column is used instead.
_ACCOUNT_CODE_JSONB = version_info[0] >= 17
_ACCOUNT_NAME_JSONB = version_info[0] >= 17


_AML_FIELDS = frozenset({
    'id', 'move_id', 'account_id', 'journal_id', 'partner_id',
    'company_id', 'currency_id', 'date', 'date_maturity',
    'debit', 'credit', 'balance', 'amount_currency',
    'amount_residual', 'amount_residual_currency',
    'name', 'ref', 'sequence',
    'reconciled', 'full_reconcile_id', 'matching_number',
    'tax_line_id', 'analytic_distribution',
    'parent_state',
})

_ACCOUNT_FIELDS = frozenset({
    'id', 'code', 'name', 'account_type', 'reconcile',
})

_MOVE_FIELDS = frozenset({
    'id', 'state', 'move_type', 'date', 'partner_id',
    'invoice_date', 'invoice_date_due', 'name', 'ref',
})

_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class MoveLineQueryError(ValueError):
    """Raised when MoveLineQuery rejects an input as unsafe or invalid."""


class MoveLineQuery:
    """Composable aggregation query on account.move.line.

    Usage:

        query = MoveLineQuery(env, company_ids=[1, 2])
        query.where_date_range('2026-01-01', '2026-12-31')
        query.where_account_types(['income', 'expense'])
        query.select_balance_sum(alias='balance')
        query.select_field('account_id')
        query.group_by('account_id')
        rows = query.execute()
        # rows: [{'account_id': 5, 'balance': -1234.56}, ...]
    """

    def __init__(self, env, company_ids, currency_table=None,
                 presentation_currency_id=None):
        if not company_ids:
            raise MoveLineQueryError(
                "MoveLineQuery requires at least one company id"
            )
        self.env = env
        self.company_ids = tuple(int(c) for c in company_ids)
        self._select_exprs = []
        self._select_aliases = []
        self._wheres = []
        self._joined_tables = set()
        self._group_by_exprs = []
        self._order_by_exprs = []
        self._limit = None
        self._offset = 0
        self._include_cancelled = False
        self._posted_only = False
        # Optional multi-currency consolidation (WS4). When a CurrencyTable
        # is attached AND it is in multicurrency mode, build() emits a
        # per-company rate LEFT JOIN and the converted-sum selectors multiply
        # each monetary column by the rate. With no table, or a monocurrency
        # table, every rendered fragment is byte-for-byte identical to the
        # legacy form, so existing callers and tests are unaffected.
        self.currency_table = currency_table
        self.presentation_currency_id = presentation_currency_id

    # ---- selection ----

    def select(self, expr, alias):
        """Add a column to the SELECT clause.

        :param expr: SQL primitive instance (use SQL("...") for raw SQL).
        :param alias: alphanumeric+underscore identifier starting with letter
            or underscore. Validated here.
        """
        if not isinstance(expr, SQL):
            raise MoveLineQueryError(
                "select(expr, alias) expects expr to be a SQL instance"
            )
        if not isinstance(alias, str) or not _IDENTIFIER_RE.match(alias):
            raise MoveLineQueryError(
                f"alias must be alphanumeric and underscore starting with a "
                f"letter or underscore, got {alias!r}"
            )
        self._select_exprs.append(expr)
        self._select_aliases.append((alias, SQL.identifier(alias)))
        return self

    def select_field(self, field_name, alias=None):
        if field_name not in _AML_FIELDS:
            raise MoveLineQueryError(
                f"unknown account_move_line field {field_name!r}"
            )
        return self.select(SQL("aml.%s" % field_name), alias or field_name)

    def select_account_field(self, field_name, alias=None):
        if field_name not in _ACCOUNT_FIELDS:
            raise MoveLineQueryError(
                f"unknown account_account field {field_name!r}"
            )
        self._joined_tables.add('account_account')
        # In Odoo 19, account.account.code is per company and stored as a
        # jsonb column 'code_store' keyed by company_id (as text); name
        # is a translated jsonb keyed by language code. Resolve at SQL time.
        if field_name == 'code':
            expr = self._account_code_sql()
        elif field_name == 'name':
            expr = self._translated_account_name_sql()
        else:
            expr = SQL("acc.%s" % field_name)
        return self.select(expr, alias or f"account_{field_name}")

    # ---- explicit joins (for callers that select via raw SQL) ----

    def join_account(self):
        """Ensure JOIN account_account acc is emitted in the FROM clause.

        Once joined, callers can reference ``acc.<column>`` in their own
        SQL fragments via select() and where_raw().
        """
        self._joined_tables.add('account_account')
        return self

    def join_journal(self):
        """Ensure JOIN account_journal aj is emitted.

        After this, callers can reference ``aj.<column>`` in their SQL.
        """
        self._joined_tables.add('account_journal')
        return self

    def join_partner(self):
        """Ensure LEFT JOIN res_partner p is emitted.

        LEFT JOIN because account_move_line.partner_id is nullable. After
        this, callers can reference ``p.<column>`` in their SQL.
        """
        self._joined_tables.add('res_partner')
        return self

    def select_balance_sum(self, alias='balance'):
        return self.select(SQL("SUM(aml.balance)"), alias)

    def select_debit_sum(self, alias='debit'):
        return self.select(SQL("SUM(aml.debit)"), alias)

    def select_credit_sum(self, alias='credit'):
        return self.select(SQL("SUM(aml.credit)"), alias)

    def select_count(self, alias='line_count'):
        return self.select(SQL("COUNT(aml.id)"), alias)

    # ---- currency-aware sums (WS4) ----
    #
    # When a multicurrency CurrencyTable is attached these emit
    # ``SUM((aml.<col>) * COALESCE(ct.rate, 1))`` and build() adds the rate
    # join. With no table / a monocurrency table the rate expression folds to
    # ``* 1`` is NOT emitted at all: _balance_expr returns the bare
    # ``aml.balance`` so the rendered SQL is identical to select_balance_sum.
    # This keeps the single-company / single-currency hot path byte-identical.

    def _has_currency_conversion(self):
        ct = self.currency_table
        return bool(ct is not None and not ct.is_monocurrency)

    def _balance_expr(self):
        if self._has_currency_conversion():
            return SQL("(aml.balance) * %s", self.currency_table.rate_expr())
        return SQL("aml.balance")

    def _debit_expr(self):
        if self._has_currency_conversion():
            return SQL("(aml.debit) * %s", self.currency_table.rate_expr())
        return SQL("aml.debit")

    def _credit_expr(self):
        if self._has_currency_conversion():
            return SQL("(aml.credit) * %s", self.currency_table.rate_expr())
        return SQL("aml.credit")

    def select_balance_sum_converted(self, alias='balance'):
        """Currency-converted SUM(balance).

        Identical to select_balance_sum when no multicurrency table is
        attached, so it is a safe drop-in everywhere the raw sum was used.
        """
        return self.select(SQL("SUM(%s)", self._balance_expr()), alias)

    def select_debit_sum_converted(self, alias='debit'):
        return self.select(SQL("SUM(%s)", self._debit_expr()), alias)

    def select_credit_sum_converted(self, alias='credit'):
        return self.select(SQL("SUM(%s)", self._credit_expr()), alias)

    # ---- where filters ----

    def where_date_range(self, date_from=None, date_to=None):
        if date_from:
            self._wheres.append(SQL("aml.date >= %s", date_from))
        if date_to:
            self._wheres.append(SQL("aml.date <= %s", date_to))
        return self

    def where_journals(self, journal_ids):
        ids = tuple(int(i) for i in (journal_ids or ()))
        if ids:
            self._wheres.append(SQL("aml.journal_id IN %s", ids))
        return self

    def where_accounts(self, account_ids):
        ids = tuple(int(i) for i in (account_ids or ()))
        if ids:
            self._wheres.append(SQL("aml.account_id IN %s", ids))
        return self

    def where_account_codes(self, prefixes):
        prefixes = list(prefixes or ())
        if not prefixes:
            return self
        self._joined_tables.add('account_account')
        # Odoo 19: code is per company in acc.code_store jsonb.
        clauses = [
            self._account_code_like_sql(f"{p}%")
            for p in prefixes
        ]
        joined = SQL(" OR ").join(clauses)
        self._wheres.append(SQL("(%s)", joined))
        return self

    def where_account_types(self, types):
        types = tuple(types or ())
        if types:
            self._joined_tables.add('account_account')
            self._wheres.append(SQL("acc.account_type IN %s", types))
        return self

    def where_partners(self, partner_ids):
        ids = tuple(int(i) for i in (partner_ids or ()))
        if ids:
            self._wheres.append(SQL("aml.partner_id IN %s", ids))
        return self

    def where_analytic_accounts(self, analytic_account_ids):
        """Restrict to journal items whose analytic_distribution references
        any of the given analytic account ids.

        analytic_distribution is a jsonb keyed by analytic_account_id (as
        string) carrying the percentage allocated. We match presence of any
        of the supplied keys; the percentage threshold check is left to
        callers that need it via where_raw().
        """
        ids = tuple(int(i) for i in (analytic_account_ids or ()))
        if not ids:
            return self
        keys = [str(i) for i in ids]
        self._wheres.append(SQL(
            "aml.analytic_distribution ?| %s", keys,
        ))
        return self

    def where_analytic_plans(self, plan_ids):
        """Restrict to journal items whose analytic_distribution references
        an analytic account belonging to any of the given plans.

        Resolves plan -> account ids via account_analytic_account.plan_id
        and passes through where_analytic_accounts.
        """
        ids = tuple(int(i) for i in (plan_ids or ()))
        if not ids:
            return self
        cr = self.env.cr
        cr.execute(
            "SELECT id FROM account_analytic_account WHERE plan_id IN %s",
            (ids,),
        )
        account_ids = [r[0] for r in cr.fetchall()]
        if not account_ids:
            self._wheres.append(SQL("FALSE"))
            return self
        return self.where_analytic_accounts(account_ids)

    def where_posted_only(self, posted=True):
        self._posted_only = bool(posted)
        return self

    def where_include_cancelled(self, include=True):
        """By default cancelled moves are excluded. Call this to override."""
        self._include_cancelled = bool(include)
        return self

    def where_raw(self, sql_fragment):
        """Escape hatch for callers that need a custom WHERE clause.

        sql_fragment must be a SQL primitive. Use sparingly; prefer the
        named filters when the case fits.
        """
        if not isinstance(sql_fragment, SQL):
            raise MoveLineQueryError("where_raw expects a SQL instance")
        self._wheres.append(sql_fragment)
        return self

    # ---- group / order / limit ----

    def group_by(self, *fields):
        for f in fields:
            if isinstance(f, str):
                if f not in _AML_FIELDS:
                    raise MoveLineQueryError(f"unknown groupable field {f!r}")
                self._group_by_exprs.append(SQL("aml.%s" % f))
            elif isinstance(f, SQL):
                self._group_by_exprs.append(f)
            else:
                raise MoveLineQueryError(
                    f"group_by expects str or SQL, got {type(f).__name__}"
                )
        return self

    def order_by(self, expr, direction='ASC'):
        direction = (direction or 'ASC').upper()
        if direction not in ('ASC', 'DESC'):
            raise MoveLineQueryError(
                f"direction must be ASC or DESC, got {direction!r}"
            )
        if isinstance(expr, str):
            if expr not in _AML_FIELDS:
                raise MoveLineQueryError(f"unknown orderable field {expr!r}")
            expr_sql = SQL("aml.%s" % expr)
        elif isinstance(expr, SQL):
            expr_sql = expr
        else:
            raise MoveLineQueryError(
                f"order_by expects str or SQL, got {type(expr).__name__}"
            )
        # direction is whitelisted, safe to splice into the format string.
        self._order_by_exprs.append(SQL("%s " + direction, expr_sql))
        return self

    def order_by_account_field(self, field_name, direction='ASC'):
        """Convenience: order by a column on the joined account_account table."""
        if field_name not in _ACCOUNT_FIELDS:
            raise MoveLineQueryError(
                f"unknown account_account field {field_name!r}"
            )
        self._joined_tables.add('account_account')
        if field_name == 'code':
            expr = self._account_code_sql()
        elif field_name == 'name':
            expr = self._translated_account_name_sql()
        else:
            expr = SQL("acc.%s" % field_name)
        return self.order_by(expr, direction)

    def group_by_account_field(self, field_name):
        """Group by a column on the joined account_account table.

        Mirrors the expression that select_account_field / order_by_account_field
        emit for the same field name. This matters for translated jsonb columns
        like ``name``: SELECT and ORDER BY resolve the user's lang via COALESCE,
        so GROUP BY must use the identical expression or PostgreSQL rejects the
        query with "must appear in the GROUP BY clause" when env.lang is not
        en_US. Reusing this helper keeps the three call sites in lock-step.
        """
        if field_name not in _ACCOUNT_FIELDS:
            raise MoveLineQueryError(
                f"unknown account_account field {field_name!r}"
            )
        self._joined_tables.add('account_account')
        if field_name == 'code':
            expr = self._account_code_sql()
        elif field_name == 'name':
            expr = self._translated_account_name_sql()
        else:
            expr = SQL("acc.%s" % field_name)
        self._group_by_exprs.append(expr)
        return self

    def _account_code_sql(self):
        """SQL for account.account.code. Company-dependent jsonb on Odoo 17+,
        a plain column on Odoo 16."""
        if _ACCOUNT_CODE_JSONB:
            return SQL("(acc.code)")
        return SQL("acc.code")

    def _account_code_like_sql(self, pattern):
        """A ``code LIKE pattern`` clause, version-aware (see
        _account_code_sql)."""
        if _ACCOUNT_CODE_JSONB:
            return SQL(
                "(acc.code) LIKE %s", pattern,
            )
        return SQL("acc.code LIKE %s", pattern)

    def _translated_account_name_sql(self):
        """Resolve acc.name to the user's language with an en_US fallback.

        Odoo 17+ stores translated fields as a jsonb keyed by language code;
        the user's active language comes from env.lang, with a fallback to the
        base 'en_US' entry Odoo always populates. Odoo 16 keeps `name` as a
        plain column (translations live in ir_translation), so the bare column
        is used.
        """
        if not _ACCOUNT_NAME_JSONB:
            return SQL("acc.name")
        lang = self.env.lang or 'en_US'
        if lang == 'en_US':
            return SQL("(acc.name ->> 'en_US')")
        return SQL(
            "COALESCE(acc.name ->> %s, acc.name ->> 'en_US')", lang,
        )

    def limit(self, n):
        if n is not None:
            self._limit = int(n)
        return self

    def offset(self, n):
        if n:
            self._offset = int(n)
        return self

    # ---- build / execute ----

    def build(self):
        """Compose the final SQL primitive without executing it."""
        if not self._select_exprs:
            raise MoveLineQueryError(
                "MoveLineQuery requires at least one select() before build()"
            )

        # SELECT clause: "expr AS alias, expr AS alias, ...".
        select_parts = []
        for expr, (_alias_str, alias_sql) in zip(
            self._select_exprs, self._select_aliases,
        ):
            select_parts.append(SQL("%s AS %s", expr, alias_sql))
        select_clause = SQL(", ").join(select_parts)

        # JOIN clauses (account_move is mandatory because we always filter on state).
        joins = []
        if 'account_account' in self._joined_tables:
            joins.append(SQL("JOIN account_account acc ON acc.id = aml.account_id"))
        joins.append(SQL("JOIN account_move am ON am.id = aml.move_id"))
        if 'account_journal' in self._joined_tables:
            joins.append(SQL("JOIN account_journal aj ON aj.id = aml.journal_id"))
        if 'res_partner' in self._joined_tables:
            joins.append(SQL("LEFT JOIN res_partner p ON p.id = aml.partner_id"))
        # Currency-conversion rate join (WS4). Emitted ONLY when a
        # multicurrency CurrencyTable is attached; monocurrency / no table
        # yields an empty fragment, so the FROM/JOIN clause is byte-identical
        # to the legacy query for the single-currency hot path.
        if self._has_currency_conversion():
            ct_join = self.currency_table.join_sql('aml')
            if ct_join.code.strip():
                joins.append(ct_join)
        joins_clause = SQL(" ").join(joins)

        # WHERE clauses. Mandatory: company scope. State filter:
        # posted_only is the strictest filter, so it shadows the cancel
        # exclusion when set. Otherwise the default behaviour is to exclude
        # cancelled moves; callers can opt out via where_include_cancelled().
        wheres = [SQL("aml.company_id IN %s", self.company_ids)]
        if self._posted_only:
            wheres.append(SQL("am.state = %s", 'posted'))
        elif not self._include_cancelled:
            wheres.append(SQL("am.state != %s", 'cancel'))
        wheres.extend(self._wheres)
        where_clause = SQL(" AND ").join(wheres)

        sql = SQL(
            "SELECT %s FROM account_move_line aml %s WHERE %s",
            select_clause, joins_clause, where_clause,
        )

        if self._group_by_exprs:
            sql = SQL(
                "%s GROUP BY %s",
                sql, SQL(", ").join(self._group_by_exprs),
            )
        if self._order_by_exprs:
            sql = SQL(
                "%s ORDER BY %s",
                sql, SQL(", ").join(self._order_by_exprs),
            )
        if self._limit is not None:
            sql = SQL("%s LIMIT %s", sql, self._limit)
        if self._offset:
            sql = SQL("%s OFFSET %s", sql, self._offset)

        return sql

    def execute(self):
        """Run the composed query and return rows as list of dicts.

        Flushes pending ORM writes first so raw SQL sees the latest
        state of account.move, account.move.line and any joined
        tables (Odoo 19 defers stored writes until flush).
        """
        self.env.flush_all()
        sql = self.build()
        cr = self.env.cr
        cr.execute(sql)
        column_keys = [alias for alias, _ in self._select_aliases]
        return [dict(zip(column_keys, row)) for row in cr.fetchall()]
