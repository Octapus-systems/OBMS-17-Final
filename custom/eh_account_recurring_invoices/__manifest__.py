# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Recurring Invoices Pro',
    'summary': 'True recurring customer invoices for Odoo 17 Community Edition, template based with a daily cron that issues on cadence. Recurring customer invoices, subscription billing, recurring invoice template, automatic invoice generation, daily weekly monthly quarterly yearly cadence, every N interval, end date or invoice count termination, optional auto post, mid-period proration credit note, draft by default account.move out_invoice.',  # noqa: E501
    'description': """Real recurring customer invoices for Odoo 17 Community Edition. Templates, not workarounds. Configure a billing arrangement once, and a daily cron generates invoices on the cadence you set, in draft for review or auto posted for hands-off operation.  # noqa: E501

The cadence engine runs an arbitrary N interval over five built-in units (day, week, month, quarter, year), so every 2 weeks or every 6 months is a setting, not a hack. Bound the schedule with an end date or a maximum invoice count, and the template finishes itself when the limit is reached.  # noqa: E501

The daily cron isolates every template in its own savepoint and self-heals. If one template fails to generate, the partial draft is rolled back, the schedule still advances, and the captured error is recorded on the template, so a single broken record never freezes the queue or causes the cron to retry it forever. Manual issuance takes a SELECT FOR UPDATE row lock before reading and advancing the next-run date, so a double-click or browser retry cannot double-bill the same period; the serialised second request issues the following period instead.  # noqa: E501

The proration wizard handles mid-cycle price changes as one atomic operation: a credit note, a new-price invoice, and the template reprice all commit under a single savepoint, on a calendar-day pro-rata basis. Ambiguous cases (multi-line templates, zero days remaining) are refused with explicit remedies rather than producing a wrong credit.  # noqa: E501

Generated invoices are standard account.move records of type out_invoice, so core Odoo reconciliation, the customer statement, and every downstream report already understand the output. This module adds the recurrence engine, not a parallel invoicing stack.  # noqa: E501

Requires eh_account_base and account (Community core).""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.0.7',
    'depends': ['eh_account_base', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/cron.xml',
        'views/template_views.xml',
        'wizards/proration_wizard_views.xml',
        'data/menus.xml',
    ],
    'demo': ['demo/recurring_demo.xml'],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
