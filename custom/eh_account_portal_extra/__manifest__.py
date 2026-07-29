# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The customer portal extension
# uses Odoo's public portal infrastructure (portal mixin, /my routes,
# QWeb templates) to surface the suite's existing customer statement
# generator. No code derives from any proprietary or third-party Odoo
# module beyond the public framework conventions.
#
##############################################################################
{
    'name': 'Customer Portal Extra',
    'summary': 'Self-service accounts receivable for the Odoo 17 Community customer portal. Customers download a period-range statement PDF in one click and see a live open-balance card on their portal home, with no back-office round trip. Odoo 17 customer portal statement download, customer portal account statement PDF, portal AR balance card, self-service customer statement, portal open invoices and overdue summary, accounts receivable portal, partner scoped portal routes.',
    'description': """Customer Portal Extra turns the standard Odoo 17 Community customer portal into a self-service accounts receivable corner. It adds two things and does them carefully.

First, a one-click statement PDF download. The logged-in customer picks a period range and gets a customer statement rendered on the spot. The download runs through the same compute pipeline as the internal back-office statement (the same report handler, the same get_default_options and render_pdf path), so the numbers a customer sees always agree with what the accounts team sees. Because that handler rewinds partial reconciliations created after the chosen end date, a back-dated statement reflects what was actually outstanding then, not today's residual.

Second, a live AR balance card on the portal home. It shows open invoice count, total overdue, the oldest unpaid due date, and a days-overdue counter, all computed server side in a single SQL pass so the card stays cheap even for a partner with many open lines.

Access is strictly partner scoped. Every route resolves the logged-in user's commercial_partner_id and accepts no partner_id override, so no customer can read another customer's data. Both routes require an authenticated portal user. Every failure mode (no linked partner, unparseable or inverted dates, a missing report registration, a render error) returns a clear branded page instead of a stack trace.

The installed portal pages add no analytics tag, no tracking pixel, no external CDN, and no JavaScript beyond Odoo's standard portal layout. No new ORM models, no new report definitions. It reuses the customer statement generator from eh_account_dynamic_reports.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.0.4',
    'depends': [
        'eh_account_base',
        'eh_account_dynamic_reports',
        'portal',
        'account',
    ],
    'data': ['views/portal_templates.xml'],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
