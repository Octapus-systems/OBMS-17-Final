# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
res.config.settings: central settings hub for the ERP Heritage suite.

The settings page mirrors company-scoped fields declared on res.company
in this module and across the suite. The pattern is:

* Add the persistent field to res.company in the owning module
  (eh_account_base for engine-level settings; module-specific for the
  module's own knobs).
* Mirror it here on res.config.settings using related='company_id.<f>'
  with readonly=False and config_parameter='module.field' for module
  defaults.

Why per-company: a deployment with multiple companies typically wants
each company to set its own GL row cap, default useful life, default
forecast horizon, etc. config_parameter is reserved for genuinely
global defaults (no per-company differentiation).
"""

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ------------------------------------------------------------------
    # Reporting engine (eh_account_base)
    # ------------------------------------------------------------------
    eh_gl_row_limit = fields.Integer(
        related='company_id.eh_gl_row_limit',
        readonly=False,
        string="GL Row Limit",
        help=(
            "Maximum journal-item rows the General Ledger and similar "
            "row-driven reports will materialise before refusing to render. "
            "Higher values render larger reports but use more memory."
        ),
    )
    eh_dashboard_lookback_days = fields.Integer(
        related='company_id.eh_dashboard_lookback_days',
        readonly=False,
        string="Dashboard Lookback (days)",
        help=(
            "How many days of history the financial dashboard scans for "
            "AR / AP overdue and recent activity tiles. Smaller values "
            "make the dashboard refresh faster."
        ),
    )

    # ------------------------------------------------------------------
    # Reports Pro (eh_account_dynamic_reports_pro)
    # ------------------------------------------------------------------
    eh_forecast_default_horizon = fields.Integer(
        related='company_id.eh_forecast_default_horizon',
        readonly=False,
        string="Default Forecast Horizon (months)",
        help="New forecast scenarios default to this horizon length.",
    )
    eh_forecast_default_history_months = fields.Integer(
        related='company_id.eh_forecast_default_history_months',
        readonly=False,
        string="Default Forecast History (months)",
        help="New forecast scenarios default to this many months of input series.",
    )

    # ------------------------------------------------------------------
    # Assets and Leases (eh_account_assets_pro)
    # ------------------------------------------------------------------
    eh_asset_default_useful_life_months = fields.Integer(
        related='company_id.eh_asset_default_useful_life_months',
        readonly=False,
        string="Default Useful Life (months)",
        help="New fixed-asset records start with this depreciation term.",
    )
    eh_lease_default_term_months = fields.Integer(
        related='company_id.eh_lease_default_term_months',
        readonly=False,
        string="Default Lease Term (months)",
        help="New IFRS 16 lease contracts start with this term.",
    )

    # ------------------------------------------------------------------
    # Collections (eh_account_collections)
    # ------------------------------------------------------------------
    eh_collections_grace_days = fields.Integer(
        related='company_id.eh_collections_grace_days',
        readonly=False,
        string="Collections Grace Days",
        help="Default grace period (days) before an invoice opens a collection case.",
    )

    # ------------------------------------------------------------------
    # AP Automation (eh_account_ap_automation)
    # ------------------------------------------------------------------
    eh_ap_invoice_ref_regex = fields.Char(
        related='company_id.eh_ap_invoice_ref_regex',
        readonly=False,
        string="Invoice Ref Regex",
        help="Default regex pattern used by the bill-intake parser to extract the invoice number.",
    )
    eh_ap_total_regex = fields.Char(
        related='company_id.eh_ap_total_regex',
        readonly=False,
        string="Total Amount Regex",
        help="Default regex pattern used by the bill-intake parser to extract the total amount.",
    )

    # ------------------------------------------------------------------
    # SEPA Direct Debit (eh_account_sepa_dd)
    # ------------------------------------------------------------------
    # Selection list inherited from the related field on res.company.
    eh_sepa_dd_default_instrument = fields.Selection(
        related='company_id.eh_sepa_dd_default_instrument',
        readonly=False,
        string="Default SEPA DD Local Instrument",
        help="Default local instrument applied to new SEPA Direct Debit creditors and mandates.",
    )

    # ------------------------------------------------------------------
    # Approval (eh_account_approval)
    # ------------------------------------------------------------------
    eh_approval_material_change_pct = fields.Float(
        related='company_id.eh_approval_material_change_pct',
        readonly=False,
        string="Approval Material Change Threshold (%)",
        help=(
            "Percentage change to a request amount that triggers a "
            "re-approval (rolls the workflow back to step zero)."
        ),
    )
