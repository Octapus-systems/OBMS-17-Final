# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The treatments (a change in accounting
# policy or the correction of a prior-period error applied retrospectively by
# restating comparatives, a change in estimate applied prospectively, and an
# adjusting versus non-adjusting event after the reporting period) are IAS 8
# and IAS 10 as published; no code or comments derive from any proprietary or
# third-party module.
#
##############################################################################
{
    'name': 'Accounting Changes & Events (IAS 8 / IAS 10)',
    'summary': 'Keep IAS 8 accounting changes and IAS 10 subsequent events on a controlled register in Odoo 17 Community, with retrospective restatement of comparatives and optional general ledger posting. Search terms: odoo 19 IAS 8, prior period error correction, change in accounting policy, retrospective restatement of comparatives, as previously reported adjustment as restated, change in accounting estimate prospective, opening retained earnings restatement, IAS 10 events after the reporting period, adjusting non-adjusting event, authorised for issue date, dividend declared after year end.',
    'description': 'This module maintains two linked registers. The IAS 8 register records a change in accounting policy, a change in accounting estimate, or the correction of a prior-period error, deriving retrospective versus prospective application from the change type, and building a per-line restatement trail of as previously reported, the adjustment, and the restated amount that totals into the opening retained-earnings impact. The IAS 10 register records events after the reporting period, computing an adjusting or disclose-only treatment from whether the event evidences conditions that existed at the reporting date, and holding the estimated financial effect. Both registers can optionally post to the general ledger: a retrospective change books an opening retained-earnings restatement (either a single net figure or a per-account trail across prior periods), and an adjusting event books its balanced entry to the reporting date. Each event also carries the date the financial statements were authorised for issue, defaulting from the latest posted year-end close where that module is installed: an adjusting event dated after authorisation is blocked from booking because it belongs to the next reporting period, and later events are flagged on a next-period disclosure filter. Every posting is restricted to an EH Accounting Manager and is frozen once posted, correctable only through a Reset to Draft that reverses the entry.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.2.0',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/event_views.xml',
        'data/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/events_01_restatement.png'],
}
