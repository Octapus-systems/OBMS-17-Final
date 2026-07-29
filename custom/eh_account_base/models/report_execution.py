# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.account.report.execution: durable audit log for every report render.

Why this exists:

* Compliance. Auditors need to reproduce the exact report a user printed on a
  given day. We persist the full options dict and the result hash so any prior
  execution can be replayed deterministically.
* Cache key. The (report_code, options_hash, sum(eh_move_version)) tuple is
  the freshness key used by the in process cache layer. Looking up a prior
  successful execution with a matching key short circuits computation.
* Performance telemetry. duration_ms and row_count let us track regressions
  over time and flag reports drifting outside their service level objective.
"""

import hashlib
import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class EhAccountReportExecution(models.Model):
    _name = 'eh.account.report.execution'
    _description = "Accounting report execution audit log"
    _order = 'executed_at desc'
    _rec_name = 'display_name'

    display_name = fields.Char(
        compute='_compute_display_name',
        store=True,
    )
    report_code = fields.Char(
        required=True,
        index=True,
        help="Stable identifier for the report definition (for example 'profit_loss').",
    )
    name = fields.Char(required=True)

    executed_by = fields.Many2one(
        'res.users',
        required=True,
        default=lambda self: self.env.user,
        index=True,
    )
    executed_at = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        index=True,
    )

    company_ids = fields.Many2many('res.company', required=True)
    company_ids_key = fields.Char(
        compute='_compute_company_ids_key',
        store=True,
        index=True,
        help=(
            "Sorted comma separated representation of company_ids. Used "
            "for strict equality in the cache lookup; an m2m IN filter "
            "would match overlapping sets and return wrong scoped rows."
        ),
    )

    options_snapshot = fields.Text(
        required=True,
        help="JSON serialised options dict at execution time.",
    )
    options_hash = fields.Char(
        size=64,
        required=True,
        index=True,
        help="SHA-256 of the canonicalised options dict.",
    )

    result_format = fields.Selection(
        [
            ('json', "JSON"),
            ('xlsx', "XLSX"),
            ('pdf', "PDF"),
        ],
        required=True,
        default='json',
    )

    state = fields.Selection(
        [
            ('running', "Running"),
            ('done', "Done"),
            ('error', "Error"),
        ],
        required=True,
        default='running',
        index=True,
    )

    duration_ms = fields.Integer(default=0)
    row_count = fields.Integer(default=0)
    result_hash = fields.Char(
        size=64,
        help="SHA-256 of the rendered output for xlsx and pdf formats.",
    )
    error_message = fields.Text()

    move_version_at_start = fields.Integer(
        help=(
            "Sum of res_company.eh_move_version at execution start across the "
            "company_ids. Component of the cache key."
        ),
    )

    result_payload = fields.Binary(
        attachment=True,
        help=(
            "Compressed serialised report result. Used to serve cache hits "
            "without recomputation. See eh_account_base.tools.payload_codec."
        ),
    )
    served_from_execution_id = fields.Many2one(
        'eh.account.report.execution',
        index=True, ondelete='set null',
        help=(
            "When this execution served a result from a prior cached "
            "execution, this is a pointer to that prior execution. The "
            "audit log keeps one row per actual user request even when "
            "the underlying compute was reused, so compliance reporting "
            "lists every render."
        ),
    )

    @api.depends('company_ids')
    def _compute_company_ids_key(self):
        for rec in self:
            ids = sorted(rec.company_ids.ids)
            rec.company_ids_key = ','.join(str(i) for i in ids)

    @api.depends('report_code', 'name', 'executed_at')
    def _compute_display_name(self):
        for rec in self:
            ts = rec.executed_at and fields.Datetime.to_string(rec.executed_at) or ''
            base = rec.name or rec.report_code or 'Report'
            rec.display_name = f"{base} ({ts})" if ts else base

    @staticmethod
    def _company_ids_key_for(company_ids):
        ids = sorted(set(int(c) for c in company_ids))
        return ','.join(str(i) for i in ids)

    @api.model
    def start_execution(self, report_code, name, options, company_ids, result_format='json'):
        """Begin a report execution and return the audit row.

        :param report_code: short stable identifier, for example 'profit_loss'.
        :param name: human readable report name.
        :param options: dict of all parameters used to render the report.
        :param company_ids: iterable of company ids in scope.
        :param result_format: 'json', 'xlsx', or 'pdf'.
        :return: created eh.account.report.execution record.
        """
        canonical = self._canonicalise_options(options)
        canonical_json = json.dumps(canonical, sort_keys=True, default=str)
        options_hash = self._hash_string(canonical_json)
        company_ids_list = sorted(set(int(c) for c in company_ids))
        if not company_ids_list:
            raise ValueError("start_execution requires at least one company id")
        company_recs = self.env['res.company'].sudo().browse(company_ids_list)
        move_version_total = sum(company_recs.mapped('eh_move_version'))
        return self.create({
            'report_code': report_code,
            'name': name,
            'company_ids': [(6, 0, company_ids_list)],
            'options_snapshot': json.dumps(options, sort_keys=True, default=str, indent=2),
            'options_hash': options_hash,
            'result_format': result_format,
            'state': 'running',
            'move_version_at_start': move_version_total,
        })

    # ----------- audit-log immutability -----------
    # Allow only the lifecycle methods (start_execution + this update)
    # to write. Block direct external writes and any unlink. Cascade
    # FKs on this model are absent, so no parent can erase a row.
    _EH_INTERNAL_WRITE_FIELDS = frozenset({
        'state', 'duration_ms', 'row_count', 'result_hash',
        'result_payload', 'error_message',
        'served_from_execution_id',  # set on cache-hit row to point at source
    })

    def write(self, vals):
        # Tamper-evidence: the compliance fields (state, result_hash,
        # result_payload, error_message) must be writable ONLY by the engine's
        # own methods, never by a user over RPC. The prior whitelist let any
        # user with write ACL rewrite result_hash / result_payload / state of
        # a historical row, defeating the "append-only, hashed" promise. Now
        # EVERY non-superuser write must carry the internal context flag that
        # only complete_execution / fail_execution / the cache-hit writer set.
        if not self.env.su and not self.env.context.get(
                'eh_internal_audit_write'):
            raise UserError(_(
                "Report execution audit rows are append-only and may only be "
                "written by the reporting engine. Forbidden field(s): %s",
            ) % ', '.join(sorted(vals)))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_immutable_audit(self):
        if not self.env.context.get('eh_internal_audit_unlink'):
            raise UserError(_(
                "Report execution audit rows cannot be deleted. "
                "Reason: durable compliance trail."
            ))

    def complete_execution(self, row_count=0, result_hash=None, result_payload=None):
        """Mark this execution as done and record duration based on executed_at.

        :param row_count: number of rows in the rendered report.
        :param result_hash: optional SHA-256 of the rendered output.
        :param result_payload: optional compressed bytes blob produced by
            eh_account_base.tools.payload_codec.compress_payload(). When
            provided, the next find_cached() call with a matching cache key
            can skip recomputation entirely and return this stored payload.
        """
        import base64
        for rec in self:
            duration_ms = self._compute_elapsed_ms(rec)
            vals = {
                'state': 'done',
                'duration_ms': duration_ms,
                'row_count': int(row_count or 0),
                'result_hash': result_hash or False,
            }
            if result_payload is not None:
                # Odoo 19 Binary fields require base64-encoded bytes on write.
                vals['result_payload'] = base64.b64encode(result_payload)
            rec.with_context(eh_internal_audit_write=True).write(vals)
        return True

    def fail_execution(self, error_message):
        """Mark this execution as failed and log the error."""
        msg = str(error_message)[:8000]
        for rec in self:
            duration_ms = self._compute_elapsed_ms(rec)
            rec.with_context(eh_internal_audit_write=True).write({
                'state': 'error',
                'duration_ms': duration_ms,
                'error_message': msg,
            })
            _logger.warning(
                "Report execution failed: report_code=%s execution_id=%s error=%s",
                rec.report_code, rec.id, msg,
            )
        return True

    @staticmethod
    def _compute_elapsed_ms(record):
        if not record.executed_at:
            return 0
        delta = fields.Datetime.now() - record.executed_at
        return int(delta.total_seconds() * 1000)

    @api.model
    def find_cached(self, report_code, options_hash, company_ids):
        """Look up a recent successful execution that matches the cache key.

        Strict equality on company scope: an m2m 'in' would match
        overlapping sets, so a render for [1, 2] could pick up a cached
        row computed for [1] alone (and vice versa). Using the canonical
        sorted-comma key forces exact set equality. Combined with the
        move-version freshness check this guarantees the cached payload
        was computed on the same ledger snapshot for the same company
        scope as the current request.
        """
        company_ids_list = sorted(set(int(c) for c in company_ids))
        if not company_ids_list:
            return self.browse()
        company_recs = self.env['res.company'].sudo().browse(company_ids_list)
        current_version = sum(company_recs.mapped('eh_move_version'))
        scope_key = self._company_ids_key_for(company_ids_list)
        return self.search(
            [
                ('report_code', '=', report_code),
                ('options_hash', '=', options_hash),
                ('state', '=', 'done'),
                ('move_version_at_start', '=', current_version),
                ('company_ids_key', '=', scope_key),
            ],
            limit=1,
            order='executed_at desc',
        )

    # Option keys that are conceptually SETS of ids: their order carries no
    # meaning, so they MUST be canonicalised order-insensitively or the
    # cache fragments (the same unfold set expanded in a different order
    # would hash differently) or, worse, an un-normalised key serves a
    # stale no-children payload. unfolded_lines is the load-bearing case
    # (different unfold sets are different payloads); the id-list filters
    # are peers whose order never changes the figures.
    _ORDER_INSENSITIVE_OPTION_KEYS = frozenset({
        'unfolded_lines',
        'company_ids',
        'journal_ids',
        'partner_ids',
        'account_ids',
        'account_type_ids',
        'analytic_account_ids',
        'analytic_plan_ids',
    })

    @staticmethod
    def _canonicalise_options(options, _key=None):
        """Produce a deterministic representation of options for hashing.

        Dicts are sorted by key. Sets and frozensets are sorted. Lists and
        tuples keep order EXCEPT for the order-insensitive id-list keys
        (unfolded_lines and peer filters), which are sorted so the same set
        of ids hashes identically regardless of selection order. Scalar
        types pass through; non-JSON values become str() downstream.
        """
        cls = EhAccountReportExecution
        if isinstance(options, dict):
            return {
                k: cls._canonicalise_options(v, _key=k)
                for k, v in sorted(options.items(), key=lambda kv: str(kv[0]))
            }
        if isinstance(options, (list, tuple)):
            items = [cls._canonicalise_options(v) for v in options]
            if _key in cls._ORDER_INSENSITIVE_OPTION_KEYS:
                # Sort by a stable string projection so heterogeneous /
                # unorderable members never raise during canonicalisation.
                try:
                    return sorted(items, key=lambda v: (str(type(v)), str(v)))
                except Exception:  # pragma: no cover - defensive
                    return items
            return items
        if isinstance(options, (set, frozenset)):
            return sorted(
                cls._canonicalise_options(v) for v in options
            )
        return options

    @staticmethod
    def _hash_string(s):
        return hashlib.sha256(s.encode('utf-8')).hexdigest()
