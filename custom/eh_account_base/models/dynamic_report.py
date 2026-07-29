# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.account.dynamic.report: the orchestrator model.

One record per concrete report (Trial Balance, P&L, Balance Sheet, ...).
The handler_model field references the AbstractModel that knows how to
compute that report. The render() method:

1. Computes the cache key from the options dict.
2. Looks up a prior successful execution with the same key. If the move
   version counter is unchanged since that execution, the cached payload
   is fresh and is returned directly.
3. On cache miss, instantiates the handler, runs compute(), persists the
   payload on a fresh execution row, and returns it.

Both paths return the same shape so callers cannot tell hit from miss
unless they inspect the from_cache flag.
"""

import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.eh_account_base.tools.payload_codec import (
    compress_payload, decompress_payload,
)
from odoo.addons.eh_account_base.tools.xlsx_writer import XlsxReportWriter

_logger = logging.getLogger(__name__)


class EhAccountDynamicReport(models.Model):
    _name = 'eh.account.dynamic.report'
    _description = "Dynamic accounting report"
    _order = 'sequence, name'

    code = fields.Char(required=True, copy=False, index=True)
    name = fields.Char(required=True, translate=True)
    handler_model = fields.Char(
        required=True,
        help="Odoo abstract model name implementing the report handler.",
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text(translate=True)

    _sql_constraints = [
        ('unique_code', 'unique(code)', 'Report code must be unique.'),
    ]

    @api.constrains('handler_model')
    def _check_handler_model(self):
        for rec in self:
            if not rec.handler_model:
                raise ValidationError(_(
                    "Handler model is required for report %(code)r.",
                    code=rec.code,
                ))
            if rec.handler_model not in self.env.registry.models:
                raise ValidationError(_(
                    "Unknown handler model %(model)r for report %(code)r. "
                    "Did you forget to install the addon that provides "
                    "the handler?",
                    model=rec.handler_model,
                    code=rec.code,
                ))

    @api.model
    def get_by_code(self, code):
        report = self.search([('code', '=', code)], limit=1)
        if not report:
            raise UserError(_("Unknown report code: %s") % code)
        return report

    def get_default_options(self):
        self.ensure_one()
        return self.env[self.handler_model].build_default_options()

    def _eh_apply_presentation_currency(self, payload, options, company_ids):
        """Restate every monetary cell of a computed payload into the
        currency named by options['presentation_currency_id'].

        Only cells whose column is figure_type 'monetary' are converted,
        so day counts, percentages and labels are left intact. Conversion
        uses the company-to-target rate at the period end (date_to), or
        today when the report carries no end date. No-op when no currency
        is chosen or it equals the company currency.
        """
        target_id = options.get('presentation_currency_id')
        if not target_id:
            return payload
        target = self.env['res.currency'].browse(int(target_id)).exists()
        if not target:
            return payload
        company = (
            self.env['res.company'].browse(company_ids[0])
            if company_ids else self.env.company
        )
        source = company.currency_id
        if not source or target == source:
            return payload

        date_block = options.get('date') or {}
        date_to = date_block.get('date_to')
        if isinstance(date_to, str):
            date_to = fields.Date.from_string(date_to)
        date_to = date_to or fields.Date.context_today(self)

        def convert(value):
            return round(source._convert(value, target, company, date_to), 2)

        monetary = {
            col.get('expression_label')
            for col in payload.get('columns', [])
            if col.get('figure_type') == 'monetary'
        }
        for line in payload.get('lines', []):
            for col in line.get('columns', []):
                if col.get('expression_label') in monetary and isinstance(
                        col.get('value'), (int, float)):
                    col['value'] = convert(col['value'])
        totals = payload.get('totals', {})
        for key, value in list(totals.items()):
            if isinstance(value, (int, float)) and not key.endswith('_pct'):
                totals[key] = convert(value)
        payload['currency'] = {
            'id': target.id, 'name': target.name, 'symbol': target.symbol,
            'position': target.position,
            'decimal_places': target.decimal_places,
        }
        payload.setdefault('meta', {})['presentation_currency_id'] = target.id
        return payload

    def _eh_normalize_fold(self, payload, options):
        """Enforce one fold invariant across every report, in place.

        With options['lazy_expand'] (and NOT eager_expand), apply uniformly
        to every line so a caret appears IFF the row has something to expand:

          * lazy leaf (line['lazy'] is True): left as-is. It carries no
            in-payload children but expands on demand, so it stays
            unfoldable and collapsed.
          * structural group (the line's id is some other line's parent_id):
            unfoldable=True, and unfolded defaults to True (open) when the
            handler did not already set it. A row with real children always
            gets a caret and starts open.
          * everything else: unfoldable=False. Strips stray carets from flat
            rows (cash-flow / executive-summary / bank-reconciliation section
            headers, an empty bank-rec section line, partner/opening/total
            rows) that have nothing to expand.

        Backward compatible on two axes, so the eager / export / non-lazy
        callers and the existing suite see byte-identical lines:

          1. No-op unless options['lazy_expand'] is truthy and eager_expand
             is falsy (the OWL screen path only).
          2. Best-effort: any malformed payload degrades to leaving the lines
             untouched rather than raising (a normalization failure must
             never break a render).
        """
        self.ensure_one()
        options = options or {}
        if not options.get('lazy_expand') or options.get('eager_expand'):
            return payload
        try:
            lines = payload.get('lines')
            if not isinstance(lines, list):
                return payload
            # The set of ids that are some line's parent_id, i.e. ids that
            # actually have >= 1 child line materialised in this payload.
            parents_with_children = set()
            for line in lines:
                if not isinstance(line, dict):
                    continue
                parent_id = line.get('parent_id')
                if parent_id:
                    parents_with_children.add(parent_id)
            for line in lines:
                if not isinstance(line, dict):
                    continue
                if line.get('lazy') is True:
                    # Lazy leaf: expands on demand; leave its flags as the
                    # handler stamped them (unfoldable True, collapsed).
                    continue
                if line.get('id') in parents_with_children:
                    line['unfoldable'] = True
                    if 'unfolded' not in line:
                        line['unfolded'] = True
                else:
                    line['unfoldable'] = False
        except Exception:  # pragma: no cover - normalization is best-effort
            _logger.exception(
                "fold normalization failed for report %s; lines unchanged",
                self.code,
            )
        return payload

    def _eh_clamp_company_ids(self, company_ids):
        """Restrict a report's company scope to companies the acting user
        may access.

        The reporting engine reads ledgers through the raw-SQL builder and
        sudo()'d searches, which bypass the multi-company ``ir.rule``. That
        makes this the ONLY place multi-company isolation is enforced for
        reports, so a caller-supplied ``options['company_ids']`` must never
        be trusted verbatim: any requested company outside the acting
        user's own ``company_ids`` is refused. Scheduled or background
        renders must switch to the owning user (``with_user``) so this
        clamp applies to that user rather than the cron's root context.
        """
        allowed = set(self.env.user.company_ids.ids)
        seen = set()
        requested = []
        for c in company_ids or ():
            cid = int(c)
            if cid not in seen:
                seen.add(cid)
                requested.append(cid)
        forbidden = [c for c in requested if c not in allowed]
        if forbidden:
            raise AccessError(_(
                "Report %(code)s was requested for companies you are not "
                "allowed to access (%(ids)s).",
                code=self.code,
                ids=', '.join(str(c) for c in forbidden),
            ))
        return requested or [self.env.company.id]

    def render(self, options, result_format='json', use_cache=True):
        """Run the report. Returns the same shape on hit and miss.

        Result dict keys:

        * columns, lines, totals, generated_at: from handler.compute().
        * execution_id: id of the eh.account.report.execution row.
        * from_cache: True when served from a prior execution payload.

        On error, the execution row is marked 'error' with the exception
        message, and the exception is re raised so the caller can surface it.
        """
        self.ensure_one()
        Execution = self.env['eh.account.report.execution']

        company_ids = (
            options.get('company_ids')
            or list(self.env.context.get(
                'allowed_company_ids', [self.env.company.id],
            ))
        )
        company_ids = self._eh_clamp_company_ids(company_ids)

        canonical = Execution._canonicalise_options(options)
        options_hash = Execution._hash_string(
            json.dumps(canonical, sort_keys=True, default=str)
        )

        if use_cache:
            cached = Execution.find_cached(
                self.code, options_hash, company_ids,
            )
            if cached and cached.result_payload:
                payload = decompress_payload(cached.result_payload)
                if payload is not None:
                    # Cache hit still gets its own audit row. The
                    # compliance promise is "every render is recorded";
                    # serving from cache without a row would lose every
                    # render after the first. The new row points at the
                    # cached execution via served_from_execution_id, so
                    # forensic replay can trace back to the source
                    # without storing duplicate payload bytes.
                    audit = Execution.start_execution(
                        report_code=self.code,
                        name=self.name,
                        options=options,
                        company_ids=company_ids,
                        result_format=result_format,
                    )
                    audit.complete_execution(
                        row_count=len(payload.get('lines', [])),
                        result_hash=cached.result_hash or False,
                    )
                    audit.with_context(
                        eh_internal_audit_write=True,
                    ).write({'served_from_execution_id': cached.id})
                    _logger.info(
                        "Report %s cache HIT served_by=%s source=%s",
                        self.code, audit.id, cached.id,
                    )
                    payload['execution_id'] = audit.id
                    payload['from_cache'] = True
                    self._eh_apply_annotations(payload, company_ids, options)
                    return payload

        execution = Execution.start_execution(
            report_code=self.code,
            name=self.name,
            options=options,
            company_ids=company_ids,
            result_format=result_format,
        )

        try:
            # Pass the report code via context so generic handlers (like
            # the custom report builder) can resolve which definition to
            # interpret without polluting the options dict.
            handler = self.env[self.handler_model].with_context(
                eh_report_code=self.code,
            )
            payload = handler.compute(options)
            # Uniform fold normalization: enforce, identically across every
            # report, the invariant "a caret appears IFF the row has something
            # to expand". Runs right after the handler computes lines and
            # before caching, so the cached payload already carries the
            # normalised flags. Gated on lazy_expand (and off for eager/export)
            # so direct compute() callers, export, and the existing suite see
            # byte-identical lines.
            self._eh_normalize_fold(payload, options)
            # Universal presentation-currency translation: any report can be
            # restated into a chosen currency without each handler knowing.
            # Runs before caching so the cached payload is already in the
            # selected currency (the options hash includes it).
            self._eh_apply_presentation_currency(payload, options, company_ids)
            # Attach currency info to every payload so the OWL viewer and
            # the XLSX writer can render amounts with the right symbol /
            # decimal places without each handler having to remember.
            if 'currency' not in payload:
                payload['currency'] = handler.resolve_currency_info(options)
            row_count = len(payload.get('lines', []))
            compressed = compress_payload(payload)
            execution.complete_execution(
                row_count=row_count,
                result_payload=compressed,
            )
            payload['execution_id'] = execution.id
            payload['from_cache'] = False
            self._eh_apply_annotations(payload, company_ids, options)
            return payload
        except Exception as exc:
            execution.fail_execution(str(exc))
            raise

    def _eh_apply_annotations(self, payload, company_ids, options=None):
        """Attach annotations to the payload's lines/cells, in place.

        Applied after caching so notes are always live and never frozen
        into the cached result. A note with no expression_label is
        attached to the line's meta; one with a label is attached to the
        matching column dict. Each note carries its author, create date and
        a can_delete flag (manager-only) so the viewer can show the date and
        gate the delete affordance without eroding the append-only posture.

        Passing options with show_annotations=False suppresses the pass
        entirely (notes are hidden, not lost). options is optional so older
        callers keep working unchanged.
        """
        self.ensure_one()
        from collections import defaultdict
        # Opt-out: a user can hide notes for a clean print/screenshot without
        # losing them. Absent / truthy keeps the historical behaviour.
        if 'show_annotations' in (options or {}) and not (options or {}).get(
                'show_annotations', True):
            return payload
        annotations = self.env['eh.account.report.annotation'].search([
            ('report_code', '=', self.code),
            ('company_id', 'in', list(company_ids)),
        ])
        if not annotations:
            return payload
        # Manager-gated delete: the user group has create+write but NOT
        # unlink on eh.account.report.annotation (append-only audit posture),
        # so only managers see the delete affordance. Resolved once, not
        # per-note.
        can_delete = self.env.user.has_group(
            'eh_account_base.group_eh_manager')
        by_key = defaultdict(list)
        for ann in annotations:
            by_key[(ann.line_id, ann.expression_label or False)].append({
                'id': ann.id,
                'text': ann.text,
                'author': ann.create_uid.name,
                'date': (ann.create_date.isoformat()
                         if ann.create_date else False),
                'can_delete': can_delete,
            })
        for line in payload.get('lines', []):
            line_id = line.get('id')
            if not line_id:
                continue
            row_notes = by_key.get((line_id, False))
            if row_notes:
                line.setdefault('meta', {})['annotations'] = row_notes
            for col in line.get('columns', []):
                cell_notes = by_key.get(
                    (line_id, col.get('expression_label')))
                if cell_notes:
                    col['annotations'] = cell_notes
        return payload

    def add_annotation(self, line_id, text, expression_label=False):
        """Create an annotation on this report for the given line/cell."""
        self.ensure_one()
        return self.env['eh.account.report.annotation'].create({
            'report_code': self.code,
            'line_id': line_id,
            'expression_label': expression_label or False,
            'text': text,
            'company_id': self.env.company.id,
        })

    def delete_annotation(self, annotation_id):
        """Remove a single annotation from this report.

        Manager-gated: the user group has create+write but NOT unlink on
        eh.account.report.annotation, so a non-manager call raises an
        AccessError from the ORM, preserving the append-only audit posture.
        Scoped defensively to this report's code and an allowed company so a
        note can never be deleted from the wrong report or a company the
        user is not in. Returns True on a successful unlink, False when the
        id does not resolve to a note on this report (no raise, so a stale
        UI id never errors the viewer).
        """
        self.ensure_one()
        try:
            ann = self.env['eh.account.report.annotation'].browse(
                int(annotation_id)).exists()
        except (TypeError, ValueError):
            return False
        if not ann or ann.report_code != self.code:
            return False
        allowed_companies = list(self.env.context.get(
            'allowed_company_ids', self.env.company.ids))
        if ann.company_id.id not in allowed_companies:
            return False
        # unlink() enforces the manager-only ACL; we deliberately do not
        # sudo() so the audit posture (non-managers cannot delete) holds.
        ann.unlink()
        return True

    def render_xlsx(self, options, use_cache=True):
        """Render the report and return XLSX bytes.

        Equivalent to render() followed by passing the JSON payload to the
        XLSX writer. Cache hits short circuit recomputation, the writer
        runs against the cached payload directly.
        """
        self.ensure_one()
        # An export must contain the full detail, not the lazy on-demand
        # skeleton the OWL viewer requests: General Ledger / Partner Ledger
        # gate their aml rows on lazy_expand, so a straight-through export of
        # the viewer's options produces a workbook with headers and totals but
        # zero transaction lines. Force eager expansion here.
        options = dict(options, eager_expand=True)
        options.pop('lazy_expand', None)
        payload = self.render(options, use_cache=use_cache)
        writer = XlsxReportWriter(report_name=self.name)
        return writer.write_payload(payload)

    def export_xlsx_attachment(self, options):
        """Render to XLSX, persist as ir.attachment, and return a download
        action.

        The OWL viewer calls this from the Export to Excel button. It exists
        because passing raw bytes through OWL's RPC layer is awkward; an
        attachment plus an act_url action is the conventional Odoo path.
        """
        import base64
        self.ensure_one()
        content = self.render_xlsx(options)
        date_block = options.get('date') or {}
        filename = "%s_%s_to_%s.xlsx" % (
            self.code,
            date_block.get('date_from') or '',
            date_block.get('date_to') or '',
        )
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(content),
            'mimetype': (
                'application/vnd.openxmlformats-officedocument'
                '.spreadsheetml.sheet'
            ),
            'res_model': self._name,
            'res_id': self.id,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'download',
        }

    def expand_line(self, options, line_id, offset=0, limit=None):
        """RPC entry point: fetch one lazy account leaf's child page.

        Resolves the handler (mirroring get_drilldown_for_line), delegates
        to handler.expand_account_line, then applies the same
        presentation-currency restatement and annotations to the returned
        child_lines that the main payload receives, so paged children honour
        currency and notes identically. Does NOT create a cached payload
        row: children are sub-slices of an already-audited render.

        Fallback: any failure returns an empty, collapsed page rather than
        raising, so a failed expand leaves the row collapsed (the §2
        invariant: a broken expand never fans out or crashes the report).
        """
        self.ensure_one()
        empty = {
            'child_lines': [], 'has_more': False,
            'next_offset': int(offset or 0), 'total_count': 0,
        }
        try:
            # SECURITY: clamp the requested company scope BEFORE delegating.
            # The handler reads ledgers via the raw-SQL builder / sudo searches
            # that bypass ir.rule, so an unclamped drill-down RPC would leak
            # another company's journal items even though render() clamps. A
            # forbidden request raises inside this try and returns an empty
            # (collapsed) page, matching the "expand never crashes" contract.
            options = dict(options or {})
            options['company_ids'] = self._eh_clamp_company_ids(
                options.get('company_ids')
                or list(self.env.context.get(
                    'allowed_company_ids', [self.env.company.id])))
            handler = self.env[self.handler_model].with_context(
                eh_report_code=self.code,
            )
            result = handler.expand_account_line(
                options, line_id, offset=offset, limit=limit,
            )
            if not isinstance(result, dict):
                return empty
            child_lines = result.get('child_lines') or []
            company_ids = (
                options.get('company_ids')
                or list(self.env.context.get(
                    'allowed_company_ids', [self.env.company.id],
                ))
            )
            # Restate child monetary cells into the presentation currency
            # and attach annotations, reusing the same helpers the main
            # payload goes through. Wrapped in a sub-payload so the helpers
            # (which expect {columns, lines}) operate over the children.
            try:
                columns = []
                # The handler exposes the host report's columns via compute,
                # but we avoid recomputing; child cells already carry the
                # host expression_labels, so we drive the monetary set from
                # the handler's _build_columns when available.
                if hasattr(handler, '_build_columns'):
                    columns = handler._build_columns() or []
                sub_payload = {'columns': columns, 'lines': child_lines,
                               'totals': {}}
                self._eh_apply_presentation_currency(
                    sub_payload, options, company_ids)
                self._eh_apply_annotations(sub_payload, company_ids, options)
            except Exception:  # pragma: no cover - presentation is best-effort
                _logger.exception(
                    "expand_line presentation/annotation failed for %s %s",
                    self.code, line_id,
                )
            return {
                'child_lines': child_lines,
                'has_more': bool(result.get('has_more')),
                'next_offset': int(result.get('next_offset') or 0),
                'total_count': int(result.get('total_count') or 0),
            }
        except Exception:
            _logger.exception(
                "expand_line failed for report %s line %s; row stays collapsed",
                self.code, line_id,
            )
            return empty

    def get_drilldown_for_line(self, options, line_id):
        """Return the handler's drill down action for a given line id.

        Thin RPC wrapper around the handler's get_drilldown_action so the
        OWL viewer can invoke it without holding a handler reference.
        Returns None when the line has no drill down (the OWL viewer
        treats falsy responses as a no op click).
        """
        self.ensure_one()
        handler = self.env[self.handler_model]
        return handler.get_drilldown_action(options, line_id)
