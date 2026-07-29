# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Dynamic Reports: Budget Columns',
    'summary': 'Live budget-vs-actual P&L comparison, variance analysis, column-based performance tracking, management reporting, variance columns, budget comparison, Community accounting.',
    'description': 'Extends the dynamic Profit and Loss report with budget-versus-actual columns. When you select a budget in the report options, the module adds a Budget column and a Budget Variance column to every account line, to the income and expense section totals, and to the Net Profit line. Budget figures are summed from the budget lines per account; section totals and net profit are derived by account type, so all figures reconcile with actual results. The module keeps the core reporting engine free of budget dependencies and can be installed independently.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.0.0',
    'depends': ['eh_account_dynamic_reports', 'eh_account_budget_pro'],
    'data': [],
    'assets': {
        'web.assets_backend': [
            'eh_account_dynamic_reports_budget/static/src/budget_filter.js',
            'eh_account_dynamic_reports_budget/static/src/budget_filter.xml',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif'],
}
