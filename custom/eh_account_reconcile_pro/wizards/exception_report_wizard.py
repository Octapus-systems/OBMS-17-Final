# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Reconciliation exception report wizard.

Operations teams need a daily snapshot of bank reconciliation health:
how many statement lines came in, how many are reconciled, how many
were written off, how many remain unmatched, and how old the oldest
unmatched line is. This wizard collects the date range + journal
selection, computes the dataset, and renders the suite-styled PDF.

The dataset is also returned by the model method
`compute_exception_data` so other tooling (custom reports, future
dashboards) can reuse it without going through the wizard.
"""

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhReconciliationExceptionWizard(models.TransientModel):
    _name = 'eh.reconciliation.exception.wizard'
    _description = "Reconciliation exception report wizard"

    date_from = fields.Date(
        required=True,
        default=lambda self: fields.Date.context_today(self) - timedelta(days=30),
    )
    date_to = fields.Date(
        required=True,
        default=lambda self: fields.Date.context_today(self),
    )
    journal_ids = fields.Many2many(
        'account.journal',
        string="Bank journals",
        domain="[('type', 'in', ('bank', 'cash'))]",
        help=(
            "Subset of bank/cash journals to report on. Leave empty to "
            "include every accessible bank/cash journal in the company."
        ),
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
    )
    show_zero_journals = fields.Boolean(
        default=False,
        help=(
            "Include journals with no statement lines in the period. "
            "Default off so the report stays focused on journals that "
            "actually saw activity."
        ),
    )

    def action_print(self):
        self.ensure_one()
        if self.date_from > self.date_to:
            raise UserError(_(
                "Date from must be earlier than or equal to date to.",
            ))
        return self.env.ref(
            'eh_account_reconcile_pro.action_report_reconciliation_exception'
        ).report_action(self)

    @api.model
    def _get_report_values(self, docids, data=None):
        """Standard hook so the QWeb action can fetch the dataset.

        Returns the dict that the report template iterates. Each
        wizard record yields one section keyed by journal.
        """
        wizards = self.browse(docids)
        return {
            'doc_ids': docids,
            'doc_model': self._name,
            'docs': wizards,
            'data': {
                wiz.id: wiz.compute_exception_data() for wiz in wizards
            },
        }

    def compute_exception_data(self):
        """Return per-journal exception aggregates for this wizard's
        scope. Result shape:

            {
              'date_from': iso,
              'date_to': iso,
              'company': str,
              'journals': [
                {
                  'name': journal.display_name,
                  'code': journal.code,
                  'currency': str,
                  'total_lines': int,
                  'reconciled_lines': int,
                  'unmatched_lines': int,
                  'unmatched_amount': float,
                  'oldest_unmatched_date': iso or None,
                  'oldest_unmatched_days': int,
                  'write_off_count': int,
                  'skip_count': int,
                  'reconciled_pct': float (0..1),
                },
                ...
              ],
              'totals': { same keys, summed }
            }
        """
        self.ensure_one()
        Journal = self.env['account.journal']
        SLine = self.env['account.bank.statement.line']
        Audit = self.env['eh.reconciliation.audit']

        if self.journal_ids:
            journals = self.journal_ids
        else:
            journals = Journal.search([
                ('type', 'in', ('bank', 'cash')),
                ('company_id', '=', self.company_id.id),
            ])

        # One search across every journal in scope, then group in
        # Python. Same pattern for the audit lookup. Replaces 2N
        # round-trips with 2.
        all_lines = SLine.search([
            ('journal_id', 'in', journals.ids),
            ('date', '>=', self.date_from),
            ('date', '<=', self.date_to),
        ])
        lines_by_journal = {}
        for line in all_lines:
            bag = lines_by_journal.get(line.journal_id.id, SLine)
            lines_by_journal[line.journal_id.id] = bag | line

        all_audits = Audit.search([
            ('statement_line_id', 'in', all_lines.ids),
            ('decided_at', '>=',
             fields.Datetime.to_datetime(self.date_from)),
            ('decided_at', '<=',
             fields.Datetime.to_datetime(self.date_to + timedelta(days=1))),
        ]) if all_lines else Audit
        # statement_line -> journal index, used to bucket audits by
        # journal without an extra read on the audit row.
        journal_id_by_line = {
            line.id: line.journal_id.id for line in all_lines
        }
        audits_by_journal = {}
        for audit in all_audits:
            j_id = journal_id_by_line.get(audit.statement_line_id.id)
            if j_id is None:
                continue
            bag = audits_by_journal.get(j_id, Audit)
            audits_by_journal[j_id] = bag | audit

        rows = []
        today = fields.Date.context_today(self)
        for journal in journals:
            lines = lines_by_journal.get(journal.id, SLine)
            if not lines and not self.show_zero_journals:
                continue
            unmatched = lines.filtered(
                lambda line_item: not getattr(line_item, 'is_reconciled', False),
            )
            audits = audits_by_journal.get(journal.id, Audit)
            reconciled_count = len(lines) - len(unmatched)
            unmatched_amount = sum(unmatched.mapped('amount') or [0.0])
            oldest_dt = min(unmatched.mapped('date'), default=None)
            oldest_days = (today - oldest_dt).days if oldest_dt else 0
            currency = (journal.currency_id
                        or journal.company_id.currency_id)
            rows.append({
                'journal_id': journal.id,
                'name': journal.display_name,
                'code': journal.code or '',
                'currency': currency.name if currency else '',
                'total_lines': len(lines),
                'reconciled_lines': reconciled_count,
                'unmatched_lines': len(unmatched),
                'unmatched_amount': float(unmatched_amount or 0.0),
                'oldest_unmatched_date':
                    oldest_dt.isoformat() if oldest_dt else None,
                'oldest_unmatched_days': oldest_days,
                'write_off_count': len(audits.filtered(
                    lambda a: a.decision == 'write_off',
                )),
                'skip_count': len(audits.filtered(
                    lambda a: a.decision == 'skip',
                )),
                'reconciled_pct': (
                    reconciled_count / len(lines) if lines else 0.0
                ),
            })

        totals = {
            'total_lines': sum(r['total_lines'] for r in rows),
            'reconciled_lines': sum(r['reconciled_lines'] for r in rows),
            'unmatched_lines': sum(r['unmatched_lines'] for r in rows),
            'unmatched_amount': sum(r['unmatched_amount'] for r in rows),
            'write_off_count': sum(r['write_off_count'] for r in rows),
            'skip_count': sum(r['skip_count'] for r in rows),
        }
        if totals['total_lines']:
            totals['reconciled_pct'] = (
                totals['reconciled_lines'] / totals['total_lines']
            )
        else:
            totals['reconciled_pct'] = 0.0

        return {
            'date_from': self.date_from.isoformat() if self.date_from else None,
            'date_to': self.date_to.isoformat() if self.date_to else None,
            'company': self.company_id.display_name,
            'journals': rows,
            'totals': totals,
        }


class ReportReconciliationException(models.AbstractModel):
    """Report-values model for the reconciliation-exception PDF.

    Odoo resolves a QWeb report's data through an AbstractModel named
    ``report.<report_name>``; a ``_get_report_values`` defined on the source
    wizard alone is never called, so without this class the template's
    ``data`` context key is missing and the render fails with KeyError. This
    hook rebuilds the same dataset the wizard exposes.
    """
    _name = 'report.eh_account_reconcile_pro.report_reconciliation_exception'
    _description = "Reconciliation exception report values"

    @api.model
    def _get_report_values(self, docids, data=None):
        wizards = self.env['eh.reconciliation.exception.wizard'].browse(docids)
        return {
            'doc_ids': docids,
            'doc_model': 'eh.reconciliation.exception.wizard',
            'docs': wizards,
            'data': {
                wiz.id: wiz.compute_exception_data() for wiz in wizards
            },
        }
