# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The held-for-sale mechanics (measure
# at the lower of carrying amount and fair value less costs to sell, cease
# depreciation on classification, present discontinued operations separately)
# are IFRS 5 as published; no code or comments derive from any proprietary or
# third-party module.
#
##############################################################################
{
    'name': 'Held for Sale & Discontinued (IFRS 5)',
    'summary': 'IFRS 5 non-current assets held for sale, disposal groups and discontinued operations for Odoo 17 Community, with lower-of-carrying-amount-and-fair-value-less-costs-to-sell remeasurement, group write-down allocation (goodwill first, pro rata, fair-value floors), cease-depreciation on classification, discontinued operations account tagging, twelve-month overdue flag, and posted disposal gain or loss. Search terms: odoo 19 held for sale, IFRS 5 Odoo, discontinued operations, fair value less costs to sell, disposal group accounting, disposal group write-down allocation, cease depreciation held for sale, non-current asset disposal, impairment write-down reversal cap.',
    'description': "This module implements IFRS 5 for non-current assets, disposal groups and discontinued operations in Odoo 17 Community. When you classify an item as held for sale it is remeasured to the lower of its carrying amount and fair value less costs to sell, and any shortfall is posted as an impairment write-down (IFRS 5.15); depreciation then ceases (IFRS 5.25). An item can optionally be linked to a fixed asset, in which case the carrying amount is seeded from the asset's net book value, the asset's depreciation is paused, and write-downs are routed through the asset's own impairment engine so the two subledgers stay reconciled. Disposal groups measure many members as one unit: assets, goodwill and directly associated liabilities are listed as member lines, the group is remeasured at group level, and any write-down posts as a single journal entry allocated in the IFRS 5.23 order (goodwill first, then pro rata over the members inside the measurement scope, never below a member's fair-value floor, with out-of-scope members such as financial assets, NRV inventories and deferred tax assets excluded). Reversals are capped at the cumulative non-goodwill write-down (IFRS 5.22, IAS 36.124). Flagging a record or group as a discontinued operation tags the selected profit-and-loss accounts with a per-company discontinued-operations account tag for separate presentation (IFRS 5.33), and a twelve-month overdue flag with an IFRS 5.9 extension marker keeps long-held classifications visible without ever auto-declassifying them. On sale the disposal posts proceeds against the carrying amount with the resulting gain or loss. Posting is gated to the EH Accounting Manager group and every entry is stamped for audit.",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.2.0.0',
    'depends': [
        'eh_account_base',
        'eh_account_assets_pro',
        'account',
        'mail',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/held_for_sale_views.xml',
        'views/disposal_group_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_held_for_sale/static/tests/tours/held_for_sale_test_tour.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/held_for_sale_01.png'],
}
