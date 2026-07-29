# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.company extension: per company move version counter.

The reporting cache uses (company_id, eh_move_version) as a freshness signal.
Every account.move state transition bumps this counter atomically. When a
cache lookup sees a different counter than the one stored at execution time,
the cached result is stale and recomputation runs.
"""

from odoo import fields, models
from odoo.tools import SQL


class ResCompany(models.Model):
    _inherit = 'res.company'

    eh_move_version = fields.Integer(
        string="Move Version Counter",
        default=0,
        copy=False,
        readonly=True,
        help=(
            "Internal counter incremented on every account.move state change. "
            "Used by the ERP Heritage reporting cache to detect staleness. "
            "Do not edit manually."
        ),
    )

    # ------------------------------------------------------------------
    # Settings persisted on the company so they survive across sessions.
    # The corresponding res.config.settings fields mirror these via
    # related/readonly=False so the operator edits them on the
    # standard Settings page.
    # ------------------------------------------------------------------

    # Reporting engine
    eh_gl_row_limit = fields.Integer(
        string="GL Row Limit",
        default=10000,
        help="Maximum row materialisation cap for row-driven reports.",
    )
    eh_expand_page_size = fields.Integer(
        string="Lazy Expand Page Size",
        default=80,
        help=(
            "Number of journal items fetched per page when an account "
            "line is expanded on demand in a dynamic report. Lower values "
            "feel snappier on huge accounts; higher values page less often."
        ),
    )
    eh_dashboard_lookback_days = fields.Integer(
        string="Dashboard Lookback (days)",
        default=180,
        help="History window scanned by the financial dashboard tiles.",
    )

    # Reports Pro: forecasting defaults
    eh_forecast_default_horizon = fields.Integer(
        string="Default Forecast Horizon (months)",
        default=12,
    )
    eh_forecast_default_history_months = fields.Integer(
        string="Default Forecast History (months)",
        default=24,
    )

    # Assets and leases
    eh_asset_default_useful_life_months = fields.Integer(
        string="Default Asset Useful Life (months)",
        default=60,
    )
    eh_lease_default_term_months = fields.Integer(
        string="Default Lease Term (months)",
        default=36,
    )

    # Collections
    eh_collections_grace_days = fields.Integer(
        string="Collections Grace Days",
        default=14,
    )

    # AP Automation: parser defaults
    eh_ap_invoice_ref_regex = fields.Char(
        string="AP Invoice Ref Regex",
        default=r'(?im)Invoice[:#\s]+([A-Z0-9\-]+)',
    )
    eh_ap_total_regex = fields.Char(
        string="AP Total Amount Regex",
        default=r'(?im)Total[:\s]+([0-9][0-9,\.]*)',
    )

    # SEPA Direct Debit
    eh_sepa_dd_default_instrument = fields.Selection(
        [('CORE', "CORE (consumer)"),
         ('B2B', "B2B (business-to-business)"),
         ('COR1', "COR1 (one-day legacy)")],
        string="Default SEPA DD Local Instrument",
        default='CORE',
    )

    # Approval workflow
    eh_approval_material_change_pct = fields.Float(
        string="Approval Material Change %",
        default=10.0,
        help=(
            "If a request amount changes by more than this percentage "
            "after first approval, the workflow re-approves from step zero."
        ),
    )

    # Profit and Loss by-function presentation (IAS 1.82/85).
    #
    # Finance Costs and Tax Expense have no dedicated Odoo account_type, so
    # the by-function income statement resolves them from these explicit
    # per-company account mappings. Left empty, both subtotals are zero and
    # Profit for the Period ties to the by-nature Net Profit unchanged.
    eh_pnl_finance_cost_account_ids = fields.Many2many(
        comodel_name='account.account',
        relation='eh_company_pnl_finance_cost_account_rel',
        column1='company_id',
        column2='account_id',
        string="Finance Cost Accounts",
        help=(
            "Expense accounts presented on the Finance Costs line of the "
            "by-function Profit and Loss. These are excluded from Operating "
            "Expenses so nothing is counted twice."
        ),
    )
    # Cash Flow Statement: cash and cash equivalents (IAS 7.6/7.46).
    #
    # By default only `asset_cash` accounts are treated as cash. Companies
    # holding short term, highly liquid investments (money market funds,
    # term deposits under three months) can mark those accounts here so
    # the Cash Flow Statement treats them as cash equivalents: transfers
    # between cash and these accounts are excluded from the activity
    # sections, and their balances count towards opening and closing cash.
    # Left empty, behaviour is unchanged.
    eh_cash_equivalent_account_ids = fields.Many2many(
        comodel_name='account.account',
        relation='eh_company_cash_equivalent_account_rel',
        column1='company_id',
        column2='account_id',
        string="Cash Equivalent Accounts",
        help=(
            "Accounts treated as cash equivalents on the Cash Flow "
            "Statement, alongside Bank and Cash accounts. Movements "
            "between cash and these accounts are presented as pure cash "
            "transfers (no activity), and their balances are included in "
            "the opening and closing cash position."
        ),
    )
    eh_pnl_tax_expense_account_ids = fields.Many2many(
        comodel_name='account.account',
        relation='eh_company_pnl_tax_expense_account_rel',
        column1='company_id',
        column2='account_id',
        string="Tax Expense Accounts",
        help=(
            "Expense accounts presented on the Tax Expense line of the "
            "by-function Profit and Loss. These are excluded from Operating "
            "Expenses so nothing is counted twice."
        ),
    )
    # Profit and Loss by-function: deferred tax split (IAS 1.82 / IAS 12.81(c)).
    #
    # The income statement must present current tax and deferred tax as
    # distinct amounts. Accounts marked here are the deferred-tax portion of
    # the Tax Expense mapping; the remainder is the current-tax portion. Left
    # empty, the by-function Profit and Loss shows a single Tax Expense line
    # exactly as before, so existing output is unchanged.
    eh_pnl_deferred_tax_account_ids = fields.Many2many(
        comodel_name='account.account',
        relation='eh_company_pnl_deferred_tax_account_rel',
        column1='company_id',
        column2='account_id',
        string="Deferred Tax Accounts",
        help=(
            "Tax-expense accounts whose movement is deferred tax. On the "
            "by-function Profit and Loss the Tax Expense subtotal is split "
            "into a Current Tax line and a Deferred Tax line; these accounts "
            "form the Deferred Tax line, and the remaining Tax Expense "
            "accounts form the Current Tax line. Left empty, a single Tax "
            "Expense line is shown."
        ),
    )
    # Cash Flow Statement: additional exchange-difference journal (IAS 7.28).
    #
    # The FX effect on cash held is detected from moves posted in the
    # standard currency_exchange_journal_id. Some deployments post bank /
    # cash revaluation entries in a dedicated journal instead (for example a
    # month-end foreign-currency revaluation run). Set that journal here so
    # its cash-touching moves are recognised as exchange-rate effects rather
    # than leaking into the opening-to-closing difference. Left empty, the
    # detection seam and report output are unchanged.
    eh_cash_fx_revaluation_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string="Cash Revaluation Journal",
        help=(
            "Additional journal whose cash-touching entries revalue cash "
            "held for exchange-rate changes, presented on the Effect of "
            "exchange rate changes on cash line of the Cash Flow Statement, "
            "alongside the standard exchange difference journal."
        ),
    )
    # Cash Flow Statement: IAS 7.31/7.33/7.34 presentation policy.
    #
    # IAS 7.31 requires interest and dividends received and paid to be
    # disclosed separately, each classified in a consistent manner from
    # period to period. The standard allows a choice of section: interest
    # paid is usually operating but may be presented as financing; interest
    # and dividends received are usually operating but may be presented as
    # investing; dividends paid are usually financing but may be presented
    # as operating. These company-level defaults drive where the dedicated
    # disclosure lines appear on the Cash Flow Statement; a per-render
    # override is available through the report options. Income taxes paid
    # have no policy field: IAS 7.35 classifies them as operating unless
    # they can be specifically identified with financing or investing
    # activities, which this report does not attempt automatically.
    eh_cf_interest_paid_section = fields.Selection(
        [('operating', "Operating activities"),
         ('financing', "Financing activities")],
        string="Interest Paid Presentation",
        default='operating',
        help=(
            "Cash Flow Statement section carrying the Interest Paid "
            "disclosure line (IAS 7.31/7.33)."
        ),
    )
    eh_cf_interest_received_section = fields.Selection(
        [('operating', "Operating activities"),
         ('investing', "Investing activities")],
        string="Interest Received Presentation",
        default='operating',
        help=(
            "Cash Flow Statement section carrying the Interest Received "
            "disclosure line (IAS 7.31/7.33)."
        ),
    )
    eh_cf_dividends_paid_section = fields.Selection(
        [('financing', "Financing activities"),
         ('operating', "Operating activities")],
        string="Dividends Paid Presentation",
        default='financing',
        help=(
            "Cash Flow Statement section carrying the Dividends Paid "
            "disclosure line (IAS 7.31/7.34)."
        ),
    )
    eh_cf_dividends_received_section = fields.Selection(
        [('operating', "Operating activities"),
         ('investing', "Investing activities")],
        string="Dividends Received Presentation",
        default='operating',
        help=(
            "Cash Flow Statement section carrying the Dividends Received "
            "disclosure line (IAS 7.31/7.33)."
        ),
    )
    # Cash Flow Statement: income-taxes-paid fallback measurement (IAS 7.35).
    #
    # With no account tagged EH Income Tax Paid the report can fall back to
    # measuring the Income Taxes Paid line from cash settlements against the
    # accounts named as tax repartition targets (the tax-authority payables
    # core Odoo posts tax to). That measurement cannot distinguish income
    # tax from indirect taxes (VAT / GST remittances qualify), so it is
    # strictly opt-in: left off (the default) an untagged book shows no
    # Income Taxes Paid line and the statement is unchanged. Tagging the
    # income tax accounts is always the accurate configuration; this switch
    # is for books that want a taxes-paid line without tagging and accept
    # the mixed measurement.
    eh_cf_tax_fallback = fields.Boolean(
        string="Taxes Paid Fallback",
        default=False,
        help=(
            "Without any account tagged EH Income Tax Paid, show an Income "
            "Taxes Paid line on the Cash Flow Statement measured from cash "
            "settlements against the tax repartition target accounts. "
            "Includes indirect tax (VAT/GST) remittances; tag the income "
            "tax accounts instead for an exact line."
        ),
    )

    # Report presentation-policy fields. A change to any of these changes
    # report output (by-function P&L mapping, cash-flow sectioning, cash-
    # equivalent set), yet none of them move a journal entry, so without a
    # bump here the reporting cache would serve figures/labels computed under
    # the old configuration until an unrelated move posts.
    _EH_REPORT_CONFIG_FIELDS = frozenset({
        'eh_pnl_finance_cost_account_ids', 'eh_pnl_tax_expense_account_ids',
        'eh_pnl_deferred_tax_account_ids', 'eh_cash_equivalent_account_ids',
        'eh_cf_interest_paid_section', 'eh_cf_interest_received_section',
        'eh_cf_dividends_paid_section', 'eh_cf_dividends_received_section',
        'eh_cf_tax_fallback',
    })

    def write(self, vals):
        res = super().write(vals)
        if self._EH_REPORT_CONFIG_FIELDS.intersection(vals):
            self._eh_bump_move_version(self.ids)
        return res

    def _eh_bump_move_version(self, company_ids):
        """Atomically increment the counter for the given companies.

        Uses a direct SQL UPDATE so concurrent posts cannot race the
        counter out of sequence. Caller passes an iterable of company
        ids (not records) so the bump can fire from contexts where
        browsing the company would be unnecessary overhead. The cache
        is invalidated only on the affected company rows, not on every
        company in the registry.
        """
        ids = tuple(int(c) for c in company_ids)
        if not ids:
            return
        self.env.cr.execute(SQL(
            "UPDATE res_company SET eh_move_version = eh_move_version + 1 "
            "WHERE id IN %s",
            ids,
        ))
        self.env['res.company'].browse(ids).invalidate_recordset(
            ['eh_move_version'],
        )
