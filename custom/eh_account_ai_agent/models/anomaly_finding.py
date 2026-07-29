# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.account.anomaly.finding: persisted output of the journal-entry
anomaly scan.

The detection logic itself lives in tools/anomaly_detector.py as a
pure, Odoo-independent function. This model is the live wiring: each
deterministic Finding the detector returns is stored as one record
against the offending account.move so accountants triage it from the
UI, the cron can run unattended, and the result is queryable for KPIs.

The detected evidence (rule, description, amount, date) is immutable
after creation; only the triage fields (state, reviewer, review note)
can change. Re-scanning a move never duplicates a finding: the unique
(move, rule) constraint plus a create-if-absent upsert make the scan
idempotent.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_EVIDENCE_FIELDS = frozenset(
    {'move_id', 'rule', 'severity', 'description', 'amount', 'date'}
)


class EhAccountAnomalyFinding(models.Model):
    _name = 'eh.account.anomaly.finding'
    _description = "Journal entry anomaly finding"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'detected_at desc, id desc'
    _rec_name = 'description'

    move_id = fields.Many2one(
        'account.move', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        related='move_id.company_id', store=True, readonly=True, index=True,
    )
    currency_id = fields.Many2one(
        related='move_id.company_id.currency_id', readonly=True,
    )
    rule = fields.Char(required=True, readonly=True)
    severity = fields.Selection(
        [('low', "Low"), ('medium', "Medium"), ('high', "High")],
        required=True, readonly=True, index=True,
    )
    description = fields.Text(required=True, readonly=True)
    amount = fields.Monetary(
        currency_field='currency_id', readonly=True,
    )
    date = fields.Date(readonly=True)
    detected_at = fields.Datetime(
        readonly=True, default=fields.Datetime.now,
    )

    state = fields.Selection(
        [
            ('open', "Open"),
            ('reviewed', "Reviewed"),
            ('dismissed', "Dismissed"),
        ],
        default='open', required=True, index=True, tracking=True,
    )
    reviewed_by_id = fields.Many2one('res.users', readonly=True)
    reviewed_at = fields.Datetime(readonly=True)
    review_note = fields.Text()

    _sql_constraints = [
        ('unique_move_rule', 'unique(move_id, rule)', 'A move can carry at most one finding per rule.'),
    ]

    # ---- triage actions ----

    def action_mark_reviewed(self):
        self.write({
            'state': 'reviewed',
            'reviewed_by_id': self.env.user.id,
            'reviewed_at': fields.Datetime.now(),
        })

    def action_dismiss(self):
        self.write({
            'state': 'dismissed',
            'reviewed_by_id': self.env.user.id,
            'reviewed_at': fields.Datetime.now(),
        })

    def action_reopen(self):
        self.write({
            'state': 'open',
            'reviewed_by_id': False,
            'reviewed_at': False,
        })

    def action_open_move(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    # ---- immutability guard ----

    def write(self, vals):
        if not self.env.context.get('eh_anomaly_scan'):
            touched = _EVIDENCE_FIELDS.intersection(vals)
            if touched:
                raise UserError(_(
                    "Anomaly evidence is immutable; cannot change %s. "
                    "Only the review state and note can be edited.",
                    ', '.join(sorted(touched)),
                ))
        return super().write(vals)
