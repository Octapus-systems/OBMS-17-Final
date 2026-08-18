# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The dashboard composes KPIs
# computed via the suite's existing SQL builder against standard
# Odoo accounting tables. No layout, naming, or template derives
# from any proprietary or third-party Odoo module.
#
##############################################################################
{
    'name': 'Financial Dashboard',
    'summary': 'A per-company financial KPI dashboard for Odoo 17 Community that puts cash position, AR and AP aging, and period revenue, expense and net on one screen, every tile computed with single parameterised SQL passes off the same engine as the suite reports so figures match the trial balance to the cent. Odoo 17 Community financial dashboard, accounting KPI dashboard, cash position dashboard, accounts receivable and payable aging, period P and L, CFO finance dashboard, cash flow sparkline trend, drill-down accounting dashboard, multi-company KPI dashboard.',  # noqa: E501
    'description': """The single screen that summarises a company's financial state and links straight to the detail behind every number. Six core tiles always show: cash position, receivables (total and overdue, plus the age of the oldest open item), payables (total and overdue), and period revenue, expense and net. Up to eight more tiles light up automatically when the matching suite modules are installed, covering pending approvals, active collections cases, budgets in overrun, credit-limit signals, SEPA mandate dormancy, and year-end, period-close and FX revaluation runs.  # noqa: E501

Every KPI is computed with single parameterised SQL passes per tile (no per-row ORM access), using the same MoveLineQuery engine that drives the suite's trial balance and financial reports. That shared engine is why the dashboard figures reconcile to the trial balance to the cent rather than approximating them.  # noqa: E501

A "needs attention" rail sits beside the trend charts and surfaces day-to-day accounting hygiene from standard Odoo tables: overdue customer invoices and vendor bills, bank statement lines still to reconcile, posted entries flagged for review, the draft invoice and bill backlog, sequence gaps in posted journals, and entries posted without an inalterable hash. Each row that maps to records opens them in one click.  # noqa: E501

Pick a period (month, quarter, year to date, trailing 30 or 90 days, or a custom range) and a posted-only toggle, and every figure re-runs. Drill-downs preserve the period context: a revenue, expense or credit-override drill scopes to the dashboard's date window, not all-time. Thirty-day cash, revenue and expense area sparklines sit alongside vs-prior-period delta badges on each money KPI, computed over an equal-length trailing window.  # noqa: E501

The layout is an original dark command-centre design built as an Owl client action: a near-black ground, a single mint accent, monospaced numerals, and a strict spacing grid. It does not derive its layout, naming, or markup from any stock Odoo dashboard.  # noqa: E501

This module is read-only. It posts no journal entries and changes no accounting state. It reads standard Odoo accounting tables and your installed suite modules, and shows you what is there.""",  # noqa: E501
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.5.0',
    'depends': ['eh_account_base', 'eh_account_dynamic_reports', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'views/dashboard_views.xml',
        'views/res_company_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'eh_account_dashboard/static/src/dashboard/dashboard.scss',
            'eh_account_dashboard/static/src/dashboard/sparkline.js',
            'eh_account_dashboard/static/src/dashboard/kpi_tile.js',
            'eh_account_dashboard/static/src/dashboard/dashboard.js',
            'eh_account_dashboard/static/src/dashboard/dashboard.xml',
        ],
    },
    'images': [
        'static/description/banner.gif',
        'static/description/dashboard_01_overview.png',
        'static/description/dashboard_02_overdue_card.png',
        'static/description/dashboard_03_control_signals.png',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
