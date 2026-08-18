# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Dynamic Account Reports',
    'summary': 'Next generation dynamic financial reports for Odoo 17 Community, built on a SQL-direct aggregation engine that stays fast on large ledgers. Profit and Loss, Balance Sheet, Trial Balance, General Ledger, Partner Ledger, Aged Receivable, Aged Payable, Cash Flow (direct and indirect), plus Customer and Vendor Statements and Analytic Balance. Drill down to journal items, prior period and prior year comparatives, hierarchical chart of accounts nesting, currency-aware XLSX and PDF export, and an append-only audit log written on every render. Keywords: Odoo 17 dynamic financial reports, profit and loss report, balance sheet, trial balance, general ledger, partner ledger, aged receivable, aged payable, cash flow statement, drill down journal entries, accounting report XLSX PDF export, fast reports on large database.',  # noqa: E501
    'description': """The financial reporting layer of the ERP Heritage accounting suite for Odoo 17 Community. Eleven accountant reports are built on a single AbstractModel handler hierarchy and rendered through one OWL viewer, so the look and feel is uniform across every report. The hot path is plain Python SQL, not the ORM: a parameter-bound, posted-only, company-scoped query against account.move.line that stays responsive on large databases.  # noqa: E501

Reports included: Profit and Loss, Balance Sheet, Trial Balance, General Ledger, Partner Ledger, Aged Receivable, Aged Payable, Cash Flow (both direct and indirect methods), plus Customer and Vendor Statements and an Analytic Balance.  # noqa: E501

Engineering wedges that competitors gloss over:

A move-version cache. Each company carries an atomic SQL counter that is bumped only when an account.move state actually changes value. Cached results are keyed by the options hash and that version, with strict company-scope equality, so a render is never served stale and never leaks across overlapping company scopes.  # noqa: E501

An append-only audit log. Every render writes one row, including cache hits, and that row points back at the source compute it was served from. The model rejects edits and blocks deletion, so the trail cannot be altered even by an administrator.  # noqa: E501

Drill down. Data lines (accounts, partners, journal items) click through to the underlying journal items or the journal entry form. Section headers, subtotals and computed rows are aggregates and are not drillable.  # noqa: E501

Comparatives. Prior period and prior year columns are native, with divide-by-zero-safe variance percentages and leap-day-safe date shifting.  # noqa: E501

Currency-aware rendering. Every monetary cell renders with the company currency symbol, and the XLSX writer emits a matching Excel number-format string so the spreadsheet matches the screen cell for cell. Sectioned reports nest by account.group with per-user fold persistence.  # noqa: E501

Presentation currency. A currency selector restates any report into a chosen currency, converting every monetary cell and total at the period-end rate while leaving labels, dates and percentages untouched. For companies that post in more than one currency, the General Ledger and Partner Ledger add an "Amount in Currency" column that shows each line's original transaction amount and currency code next to the company-currency figures.  # noqa: E501

Multi-select filtering. Beside each single-pick filter, a "Pick more" button opens a searchable, checkbox records picker so you can scope a report to several partners, accounts or analytic accounts at once.  # noqa: E501

This is the free anchor for the suite. For a custom report builder and scheduled email delivery, see the Dynamic Reports Pro module that builds on top of this one.""",  # noqa: E501
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.6.0',
    'depends': ['eh_account_base'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/reports.xml',
        'data/account_tags.xml',
        'data/paperformat.xml',
        'data/report_pdf.xml',
        'report/report_dynamic_pdf_template.xml',
        'views/res_partner_views.xml',
        'views/noncash_transaction_views.xml',
        'data/menus.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'assets': {
        'web.assets_backend': ['eh_account_dynamic_reports/static/src/components/**/*'],
        # WS5 hoot suite: pure table-craft logic (windowing / search / variance
        # semantics) tested in isolation. Loaded only into the unit-test bundle.
        'web.assets_unit_tests': [
            'eh_account_dynamic_reports/static/tests/**/*',
        ],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': True,
    'auto_install': False,
}
