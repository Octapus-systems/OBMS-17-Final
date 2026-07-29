# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move live wiring for journal-entry anomaly detection.

Builds the Odoo-independent JournalEntrySummary records the detector
expects, runs the deterministic rules (optionally LLM-augmented when a
provider is configured), and persists each Finding as an
eh.account.anomaly.finding row. A scheduled scan sweeps recent posted
moves per company with savepoint isolation so one bad company never
freezes the queue.
"""

import logging
from datetime import timedelta

from odoo import _, api, fields, models

from odoo.addons.eh_account_ai_agent.tools import anomaly_detector

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_anomaly_finding_ids = fields.One2many(
        'eh.account.anomaly.finding', 'move_id',
        string="Anomaly findings", readonly=True,
    )
    eh_anomaly_count = fields.Integer(
        compute='_compute_eh_anomaly_count',
    )
    eh_anomaly_open_count = fields.Integer(
        compute='_compute_eh_anomaly_count',
    )

    @api.depends('eh_anomaly_finding_ids.state')
    def _compute_eh_anomaly_count(self):
        for move in self:
            findings = move.eh_anomaly_finding_ids
            move.eh_anomaly_count = len(findings)
            move.eh_anomaly_open_count = len(
                findings.filtered(lambda f: f.state == 'open')
            )

    # ---- summary builder ----

    def _eh_build_anomaly_summary(self):
        """Build one JournalEntrySummary for this move.

        Magnitude is the sum of debit legs, which is meaningful for
        every move type (invoices, bills, and pure journal entries
        alike), unlike amount_total which is zero for misc entries.
        """
        self.ensure_one()
        magnitude = sum(self.line_ids.mapped('debit'))
        return anomaly_detector.JournalEntrySummary(
            id=self.id,
            date=self.date,
            amount=magnitude,
            posted_by=(self.create_uid.login or self.create_uid.name or ''),
            account_codes=tuple(
                code for code in self.line_ids.account_id.mapped('code')
                if code
            ),
            reference=self.ref or self.name or '',
        )

    # ---- scan ----

    def eh_scan_anomalies(self):
        """Run the detector over this recordset and persist new findings.

        Idempotent: a finding already stored for a (move, rule) pair is
        left untouched, so re-scanning never duplicates. Returns the
        findings created on this pass.
        """
        Finding = self.env['eh.account.anomaly.finding']
        created = Finding
        by_company = {}
        for move in self.filtered(lambda m: m.state == 'posted'):
            by_company.setdefault(move.company_id, self.browse())
            by_company[move.company_id] |= move
        for company, moves in by_company.items():
            summaries = [m._eh_build_anomaly_summary() for m in moves]
            findings = anomaly_detector.detect(
                summaries,
                approval_threshold=company.eh_ai_anomaly_approval_threshold,
                median_multiplier=(
                    company.eh_ai_anomaly_median_multiplier or 3.0
                ),
                provider_key=company.eh_ai_provider_key or 'manual',
                provider_config=company.sudo().eh_ai_provider_config or None,
            )
            if not findings:
                continue
            existing = Finding.search([
                ('move_id', 'in', moves.ids),
            ])
            seen = {(f.move_id.id, f.rule) for f in existing}
            vals = [
                {
                    'move_id': f.move_id,
                    'rule': f.rule,
                    'severity': f.severity,
                    'description': f.description,
                    'amount': f.amount,
                    'date': f.date,
                }
                for f in findings
                if (f.move_id, f.rule) not in seen
            ]
            if vals:
                created |= Finding.with_context(
                    eh_anomaly_scan=True
                ).create(vals)
        return created

    def action_eh_scan_anomalies(self):
        """Manual scan button: scan the selection, then show findings."""
        self.eh_scan_anomalies()
        action = self.env['ir.actions.actions']._for_xml_id(
            'eh_account_ai_agent.action_eh_account_anomaly_finding'
        )
        action['domain'] = [('move_id', 'in', self.ids)]
        action['context'] = {'search_default_open': 1}
        return action

    def action_eh_view_anomalies(self):
        """Smart-button: open this move's findings."""
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id(
            'eh_account_ai_agent.action_eh_account_anomaly_finding'
        )
        action['domain'] = [('move_id', '=', self.id)]
        action['context'] = {'default_move_id': self.id}
        return action

    # ---- cron ----

    @api.model
    def _cron_scan_anomalies(self):
        """Scheduled sweep: scan each company's recent posted moves.

        Each company runs in its own savepoint so a failure on one
        company is logged and skipped without aborting the rest.
        """
        today = fields.Date.context_today(self)
        companies = self.env['res.company'].search([])
        for company in companies:
            try:
                with self.env.cr.savepoint():
                    lookback = company.eh_ai_anomaly_lookback_days or 30
                    since = today - timedelta(days=lookback)
                    moves = self.search([
                        ('company_id', '=', company.id),
                        ('state', '=', 'posted'),
                        ('date', '>=', since),
                    ])
                    moves.eh_scan_anomalies()
            except Exception as exc:  # noqa: BLE001 - isolate per company
                _logger.warning(
                    "Anomaly scan failed for company %s: %s",
                    company.id, exc,
                )
        return True
