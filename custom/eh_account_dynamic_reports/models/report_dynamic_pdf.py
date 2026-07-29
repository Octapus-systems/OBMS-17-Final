# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
PDF render values and helpers for dynamic report PDFs.

Two pieces in this module:

1. An AbstractModel `report.eh_account_dynamic_reports.report_dynamic_pdf_template`
   that Odoo's reporting machinery picks up automatically when the Qweb
   template of the same name runs. Its `_get_report_values` returns the
   structure the template iterates over.

2. A model extension on `eh.account.dynamic.report` that adds `render_pdf`
   and `export_pdf_attachment`. These mirror the XLSX equivalents and let
   the OWL viewer (or any other caller) get a printable artifact.

The shared formatting helpers (_format_value, _line_css_class) are kept on
the AbstractModel so they are easy to override per locality if needed.
Concrete handlers do not need to know about PDF rendering at all; they
return the standard payload shape, and the template plus this helper do
the rest.
"""

import base64

from odoo import api, models
from odoo.exceptions import UserError


_NUMERIC_FIGURE_TYPES = frozenset({
    'monetary', 'integer', 'float', 'percentage',
})


class EhDynamicReportPdf(models.AbstractModel):
    _name = 'report.eh_account_dynamic_reports.report_dynamic_pdf_template'
    _description = "Render values for the universal dynamic report PDF"

    @api.model
    def _get_report_values(self, docids, data=None):
        data = data or {}
        options = data.get('options') or {}
        # A PDF export must render the full detail, not the lazy on-demand
        # skeleton the OWL viewer requests (GL / Partner Ledger otherwise emit
        # headers and totals with zero transaction lines). Force eager.
        options = dict(options, eager_expand=True)
        options.pop('lazy_expand', None)
        DynamicReport = self.env['eh.account.dynamic.report']
        docs = DynamicReport.browse(docids)
        rendered = []
        for doc in docs:
            payload = doc.render(options)
            rendered.append({
                'doc': doc,
                'payload': payload,
                'lines': self._render_lines(payload),
            })
        return {
            'docs': docs,
            'doc_ids': docids,
            'doc_model': 'eh.account.dynamic.report',
            'data': data,
            'rendered': rendered,
        }

    @api.model
    def _render_lines(self, payload):
        columns = payload.get('columns') or []
        currency = payload.get('currency') or {}
        rendered = []
        for line in payload.get('lines') or []:
            cells = []
            line_columns = line.get('columns') or []
            for i, line_col in enumerate(line_columns):
                col_def = columns[i + 1] if (i + 1) < len(columns) else {}
                figure_type = col_def.get('figure_type', 'string')
                cells.append({
                    'value': line_col.get('value') if line_col else None,
                    'display': self._format_value(
                        line_col.get('value') if line_col else None,
                        figure_type,
                        currency=currency,
                    ),
                    'align_right': figure_type in _NUMERIC_FIGURE_TYPES,
                })
            rendered.append({
                'id': line.get('id'),
                'name': line.get('name', ''),
                'level': int(line.get('level') or 0),
                'kind': (line.get('meta') or {}).get('kind', ''),
                'cells': cells,
                'css_class': self._line_css_class(line),
            })
        return rendered

    @api.model
    def _format_value(self, value, figure_type, currency=None):
        """Return a display string for a value, applying accounting
        conventions: comma thousands, decimals from the currency block
        (default 2) for monetary, parentheses around negatives, currency
        symbol on monetary cells when the scope is single currency.
        Empty when value is None or empty string.
        """
        currency = currency or {}
        if value is None or value == '':
            return ''
        if figure_type == 'monetary':
            if isinstance(value, (int, float)):
                decimals = int(currency.get('decimal_places') or 2)
                abs_val = abs(value)
                fmt = "{:,.%df}" % decimals
                formatted = fmt.format(abs_val)
                symbol = currency.get('symbol') or ''
                multi = currency.get('multi_currency')
                if symbol and not multi:
                    if currency.get('position') == 'before':
                        body = "%s %s" % (symbol, formatted)
                    else:
                        body = "%s %s" % (formatted, symbol)
                else:
                    body = formatted
                return ("(%s)" % body) if value < 0 else body
            return str(value)
        if figure_type == 'integer':
            if isinstance(value, (int, float)):
                return "{:,}".format(int(value))
            return str(value)
        if figure_type == 'float':
            if isinstance(value, (int, float)):
                return "{:,.4f}".format(value)
            return str(value)
        if figure_type == 'percentage':
            if isinstance(value, (int, float)):
                return "{:.2%}".format(value)
            return str(value)
        return str(value)

    @api.model
    def _line_css_class(self, line):
        """Return the space separated CSS class string for a row, based on
        line level and meta.kind. The Qweb template uses these classes to
        style headers, totals, computed lines, and balance checks.
        """
        classes = []
        level = int(line.get('level') or 0)
        if level == 0:
            classes.append('eh_pdf_section_row')
        else:
            classes.append('eh_pdf_data_row')
        kind = (line.get('meta') or {}).get('kind')
        if kind == 'section_header':
            classes.append('eh_pdf_section_header')
        elif kind == 'section_total':
            classes.append('eh_pdf_section_total')
        elif kind == 'account_header':
            classes.append('eh_pdf_account_header')
        elif kind == 'account_total':
            classes.append('eh_pdf_account_total')
        elif kind == 'partner_header':
            classes.append('eh_pdf_partner_header')
        elif kind == 'partner_total':
            classes.append('eh_pdf_partner_total')
        elif kind in ('net_profit', 'net_change',
                      'computed_total', 'current_year_earnings'):
            classes.append('eh_pdf_computed')
        elif kind == 'balance_check':
            classes.append('eh_pdf_balance_check')
        elif kind == 'opening_balance':
            classes.append('eh_pdf_opening')
        elif kind == 'cash_balance':
            classes.append('eh_pdf_cash_balance')
        return ' '.join(classes)


class EhAccountDynamicReportPdf(models.Model):
    _inherit = 'eh.account.dynamic.report'

    def render_pdf(self, options):
        """Render the report to PDF bytes.

        Resolves the universal PDF action, delegates to Odoo's standard
        Qweb to PDF pipeline, returning the bytes blob.
        """
        self.ensure_one()
        action = self.env.ref(
            'eh_account_dynamic_reports.action_report_dynamic_pdf',
            raise_if_not_found=False,
        )
        if not action:
            raise UserError(
                "PDF report action not found. Reinstall eh_account_dynamic_reports."
            )
        data = {'options': options}
        # Odoo's report rendering expects a list of docids; we pass our
        # report id and the abstract model picks it up in _get_report_values.
        report_ref = action.report_name
        rendering = self.env['ir.actions.report']._render_qweb_pdf(
            report_ref, [self.id], data=data,
        )
        # _render_qweb_pdf returns a tuple (bytes, content_type) on Odoo 17+.
        # In --test-enable mode Odoo 19 returns HTML bytes rather than
        # invoking wkhtmltopdf; both are valid for our callers because
        # downstream uses the bytes as a binary blob.
        if isinstance(rendering, tuple):
            return rendering[0]
        return rendering

    def export_pdf_attachment(self, options):
        """Render to PDF, persist as ir.attachment, return a download action.

        Mirrors export_xlsx_attachment so the OWL viewer can hand the user
        a downloadable file with one RPC call.
        """
        self.ensure_one()
        content = self.render_pdf(options)
        date_block = options.get('date') or {}
        filename = "%s_%s_to_%s.pdf" % (
            self.code,
            date_block.get('date_from') or '',
            date_block.get('date_to') or '',
        )
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(content),
            'mimetype': 'application/pdf',
            'res_model': self._name,
            'res_id': self.id,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'download',
        }
