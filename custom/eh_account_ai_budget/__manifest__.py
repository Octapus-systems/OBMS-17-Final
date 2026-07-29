# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'AI Budget Variance Commentary',
    'summary': 'Budget variance commentary for period close packs, deterministic template with live LLM augmentation option, headline plus largest movers, zero API keys by default, Odoo 17 Community accounting.',
    'description': 'Wires deterministic budget variance commentary live onto budgets in Odoo 17 Community. Each budget record gains a non-stored computed field that produces plain-English analysis of budget-vs-actual performance: a direction-aware headline stating whether the period overran, underran, or matched budget (with total variance and percentage), followed by the top three largest absolute variances by line. The commentary is generated on every read and always produces output, even for empty or all-zero periods. It requires zero API keys and zero network access by default. When a company configures an AI provider on the agent layer, the same hook hands the snapshot to the provider for a richer paragraph and gracefully falls back to the deterministic text on any provider error. The field is read-only on the budget form and includes a manual refresh button.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.0.1',
    'depends': ['eh_account_ai_agent', 'eh_account_budget_pro'],
    'data': ['views/budget_views.xml'],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif'],
}
