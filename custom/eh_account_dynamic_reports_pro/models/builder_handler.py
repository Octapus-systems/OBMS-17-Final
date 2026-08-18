# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.account.dynamic.report.handler.builder: generic handler that interprets
an eh.report.builder record at compute time.

The orchestrator passes the report code via context (eh_report_code).
This handler resolves the matching builder, walks its line_ids in
sequence, and produces a payload in the standard shape that the OWL
viewer, XLSX writer, and PDF Qweb template all already understand.

Line dispatch:

* section_header  -> section header line at level 0.
* account_aggregate -> SQL aggregation through MoveLineQuery using the
  line's account_codes or account_types and sign.
* formula -> safe_eval_formula() against accumulated line values.

Each line that has a code field set publishes its computed value into a
shared dict so subsequent formula lines can reference it.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery

from .report_builder import safe_eval_formula


class EhBuilderHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.builder'
    _inherit = 'eh.account.dynamic.report.handler.sectioned'
    _description = "Generic handler for builder defined custom reports"

    REPORT_CODE = ''
    REPORT_NAME = "Custom Report"

    @api.model
    def compute(self, options):
        builder = self._resolve_builder()
        date_from = self._extract_date(options, 'date_from')
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        show_zero = bool(options.get('show_zero', False))

        line_values = {}
        rendered_lines = []

        for builder_line in builder.line_ids.sorted(lambda line_item: line_item.sequence):
            if builder_line.line_type == 'section_header':
                rendered_lines.append(self._render_section_header(builder_line))
                continue

            if builder_line.line_type == 'account_aggregate':
                value = self._evaluate_account_aggregate(
                    builder_line=builder_line,
                    options=options,
                    date_from=date_from, date_to=date_to,
                    company_ids=company_ids,
                    posted_only=posted_only,
                )
                if builder_line.code:
                    line_values[builder_line.code] = value
                if not show_zero and round(value, 2) == 0.0:
                    continue
                rendered_lines.append(
                    self._render_account_aggregate(builder_line, value),
                )
                continue

            if builder_line.line_type == 'formula':
                value = safe_eval_formula(builder_line.formula, line_values)
                if builder_line.code:
                    line_values[builder_line.code] = value
                rendered_lines.append(
                    self._render_formula(builder_line, value),
                )
                continue

        return {
            'columns': [
                {
                    'expression_label': 'description',
                    'name': builder.label_column_name or "Description",
                    'figure_type': 'string',
                },
                {
                    'expression_label': 'amount',
                    'name': builder.amount_column_name or "Amount",
                    'figure_type': 'monetary',
                },
            ],
            'lines': rendered_lines,
            'totals': self._compute_totals(line_values),
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': {
                'report_code': builder.code,
                'builder_id': builder.id,
                'date_from': self._iso_date(date_from),
                'date_to': self._iso_date(date_to),
                'company_ids': sorted(int(c) for c in company_ids),
                'posted_only': posted_only,
                'show_zero': show_zero,
            },
        }

    # ---- internals ----

    @api.model
    def _resolve_builder(self):
        code = self.env.context.get('eh_report_code')
        if not code:
            raise UserError(_(
                "Builder handler invoked without a report code in context. "
                "Render through the eh.account.dynamic.report orchestrator."
            ))
        builder = self.env['eh.report.builder'].search(
            [('code', '=', code)], limit=1,
        )
        if not builder:
            raise UserError(_(
                "No builder record matches code %r. Was the builder "
                "deleted while the published report still references it?",
            ) % code)
        return builder

    def _evaluate_account_aggregate(self, builder_line, options,
                                    date_from, date_to,
                                    company_ids, posted_only):
        sign = 1.0 if builder_line.sign == '+' else -1.0
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_date_range(date_from=date_from, date_to=date_to)
        if posted_only:
            query.where_posted_only()
        if builder_line.account_scope == 'codes':
            codes = self._split_csv(builder_line.account_codes)
            if not codes:
                return 0.0
            query.where_account_codes(codes)
        else:
            types = self._split_csv(builder_line.account_types)
            if not types:
                return 0.0
            query.where_account_types(tuple(types))
        if options.get('journal_ids'):
            query.where_journals(options['journal_ids'])
        if options.get('partner_ids'):
            query.where_partners(options['partner_ids'])
        if options.get('account_ids'):
            query.where_accounts(options['account_ids'])

        query.select(SQL("SUM(aml.balance)"), 'balance')
        rows = query.execute()
        if not rows:
            return 0.0
        return float(rows[0].get('balance') or 0.0) * sign

    @staticmethod
    def _split_csv(value):
        return [v.strip() for v in (value or '').split(',') if v.strip()]

    def _render_section_header(self, builder_line):
        section_id = builder_line.code or ("line-%s" % builder_line.id)
        return {
            'id': "section-%s-header" % section_id,
            'name': builder_line.name,
            'level': 0,
            'columns': [
                {'expression_label': 'amount', 'value': ''},
            ],
            'unfoldable': False,
            'meta': {
                'kind': 'section_header',
                'section_id': section_id,
                'builder_line_id': builder_line.id,
            },
        }

    def _render_account_aggregate(self, builder_line, value):
        return {
            'id': "line-%s" % builder_line.id,
            'name': builder_line.name,
            'level': max(int(builder_line.level or 1), 1),
            'columns': [
                {'expression_label': 'amount', 'value': round(value, 2)},
            ],
            'unfoldable': False,
            'meta': {
                'kind': 'account_aggregate',
                'builder_line_id': builder_line.id,
                'builder_line_code': builder_line.code or '',
            },
        }

    def _render_formula(self, builder_line, value):
        kind = (
            'section_total'
            if builder_line.is_section_total
            else 'computed'
        )
        return {
            'id': "line-%s" % builder_line.id,
            'name': builder_line.name,
            'level': int(builder_line.level or 0),
            'columns': [
                {'expression_label': 'amount', 'value': round(value, 2)},
            ],
            'unfoldable': False,
            'meta': {
                'kind': kind,
                'builder_line_id': builder_line.id,
                'builder_line_code': builder_line.code or '',
            },
        }

    @staticmethod
    def _compute_totals(line_values):
        return {
            code: round(value, 2)
            for code, value in line_values.items()
            if isinstance(value, (int, float))
        }
