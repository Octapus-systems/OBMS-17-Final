# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The closing entry mechanics
# (zero income and expense accounts, push the net to retained earnings)
# are textbook double-entry close per any introductory accounting
# textbook; no code or comments derive from any proprietary or
# third-party Odoo module.
#
##############################################################################
{
    'name': 'Year-End Closing',
    'summary': 'Fiscal year-end closing for Odoo 17 Community: auto-compute net profit from posted journal entries, review the per-account profit-and-loss breakdown, then post one balanced closing entry that zeroes every income and expense account against retained earnings while sweeping each OCI component into its own accumulated-OCI (AOCI) sub-reserve per IAS 1. Advances the fiscal-year lock date by default (disabling requires a documented reason), enforces oldest-first close chronology, and supports a manager-only next-day reversal. Search: odoo 19 year end closing, fiscal year close, closing entry retained earnings, net profit to retained earnings, AOCI sub reserves, OCI reserve mapping, IAS 1 equity roll, fiscal year lock date, reverse closing entry, multi-company year end close, period close accounting.',
    'description': """Year-End Closing computes the net result of a fiscal year from its posted journal entries, then generates one balanced closing entry that zeroes every income and expense account against a configured retained earnings account. The run is computed first, so the operator reviews the per-account profit-and-loss breakdown before anything posts. Posting and reversal are gated to the EH Accounting Manager group, and every step (computed, posted, reversed) is stamped with the user and timestamp and tracked on the mail.thread chatter.

Accumulated OCI is carried in per-component sub-reserves (IAS 1.106): a company-level mapping links each OCI component (foreign currency translation, revaluation surplus, FVOCI debt, FVOCI equity, defined benefit remeasurement, other) to its flow accounts and a dedicated AOCI sub-reserve equity account, and the closing entry reclassifies each flow account's net period movement into its sub-reserve. Retained earnings receives only the profit-and-loss result, never an OCI component. Net movement only ever moves, so amounts recycled to profit or loss during the year are never double-moved. Known OCI accounts without a complete mapping are listed on the run and block posting unless a manager overrides with a documented reason, and a discovery action seeds the mapping from the installed modules. With no mapping configured the close stays a pure profit-and-loss close.

The closing entry balances by construction across every case: net profit, net loss, and a net-zero year that produces no retained-earnings line at all. Reverse posts the symmetric inverse entry dated the day after fiscal year end and flips the run to a terminal Reversed state, preserving both the original entry and its reversal by record linkage. Computing is idempotent, re-running unlinks and rebuilds the breakdown without duplicating, and the lock-date advance (on by default; switching it off requires a documented reason logged in the chatter) never moves the lock backwards. Posting is refused while a later fiscal year already stands closed, so the equity roll always builds oldest-first. One run record per company per fiscal year. No silent fallbacks: a missing retained earnings account, a missing journal, and an invalid run state each surface an explicit message naming the problem.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.1.0',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/year_end_run_views.xml',
        'views/aoci_reserve_map_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_year_end/static/tests/tours/year_end_test_tour.js',
        ],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
