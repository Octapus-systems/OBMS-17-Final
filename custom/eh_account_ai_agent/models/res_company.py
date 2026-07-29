# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.company extension: AI provider configuration.

Per-company selection of which AI provider drives the capability
hooks (anomaly detection, variance commentary, next-action). The
default is 'manual', meaning the deterministic capability output is
returned without any LLM call. Sites that want LLM augmentation
configure a provider key and the corresponding credentials via the
provider-specific extension module.
"""

from odoo import _, api, fields, models

from odoo.addons.eh_account_ai_agent.tools import provider_registry


class ResCompany(models.Model):
    _inherit = 'res.company'

    eh_ai_provider_key = fields.Char(
        string="AI provider",
        default='manual',
        help=(
            "Identifier of the AI provider the capability hooks use. "
            "Defaults to 'manual', which routes every capability to "
            "its deterministic default. Set this to 'claude', "
            "'openai', 'local', or any third-party adapter key to "
            "enable LLM augmentation; install the corresponding "
            "extension module first."
        ),
    )
    eh_ai_provider_config = fields.Text(
        string="AI provider config (JSON)",
        groups="base.group_system",
        help=(
            "JSON-encoded credentials handed to the provider "
            "factory. Shape varies per provider (api_key + model "
            "for hosted, endpoint + model for local). Stored as "
            "text rather than per-field so the same column carries "
            "every provider's distinct shape; provider modules can "
            "add structured fields when they prefer. Restricted to "
            "system administrators because it holds the API key; the "
            "capability layer reads it via sudo so AI still works for "
            "ordinary users without exposing the secret to them."
        ),
    )

    eh_ai_anomaly_lookback_days = fields.Integer(
        string="Anomaly scan window (days)",
        default=30,
        help=(
            "How far back the scheduled anomaly scan looks for posted "
            "journal entries. The manual scan on a selection ignores "
            "this and scans exactly what is selected."
        ),
    )
    eh_ai_anomaly_median_multiplier = fields.Float(
        string="Round-number outlier factor",
        default=3.0,
        help=(
            "A round-number entry larger than this multiple of the "
            "period median is flagged. Higher values flag fewer "
            "entries. Default 3.0."
        ),
    )
    eh_ai_anomaly_approval_threshold = fields.Float(
        string="Structuring threshold",
        default=0.0,
        help=(
            "Entries posted just under this amount are flagged as "
            "possible threshold structuring. Set to your approval "
            "ceiling. 0 disables the check."
        ),
    )

    @api.model
    def _eh_available_ai_providers(self):
        """Return [(key, label)] of registered providers.

        Used by the company config dropdown so the operator only
        sees providers actually installed on this deployment.
        """
        return [
            (key, key.title())
            for key in provider_registry.list_providers()
        ]
