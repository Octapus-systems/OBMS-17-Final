# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The goodwill formula (consideration
# transferred plus non-controlling interest plus any previously-held interest
# less the fair value of identifiable net assets acquired) is IFRS 3, and the
# equity-method carrying roll-forward (cost plus share of profit less
# dividends and impairment) is IAS 28, both as published; no code or comments
# derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'Business Combinations & Associates (IFRS 3 / IAS 28)',
    'summary': 'IFRS 3 goodwill and IAS 28 equity method for Odoo 17 Community, with a full purchase price allocation, deferred tax on the fair-value step-up, step-acquisition remeasurement, measurement-period adjustments, and contingent consideration. Search terms: odoo 19 goodwill, IFRS 3 business combination, purchase price allocation, bargain purchase gain, non-controlling interest at fair value or proportionate share, previously-held interest step acquisition remeasurement, measurement period adjustment, contingent consideration fair value, IAS 28 equity method, investment in associate, joint venture, share of profit pickup, IAS 12 deferred tax on acquisition.',
    'description': "This module adds two IFRS mechanics a plain ledger does not carry. On the IFRS 3 side it computes goodwill as consideration transferred (including the acquisition-date fair value of contingent consideration) plus non-controlling interest plus any previously-held interest less the fair value of identifiable net assets acquired, recognises a bargain purchase gain when that arithmetic is negative, and can post either a simple goodwill entry or a full purchase price allocation that debits each identifiable asset, credits each liability assumed, raises deferred tax on the fair-value step-up, and credits consideration, contingent consideration, and non-controlling interest. Step acquisitions remeasure the previously-held interest to fair value with the gain or loss posted to profit or loss. Measurement-period adjustments restate provisional amounts against goodwill within the 12-month window and are blocked after it; liability-classified contingent consideration is remeasured to fair value through profit or loss while equity-classified contingent consideration is never remeasured. On the IAS 28 side it carries an investment in an associate or joint venture at cost and rolls it forward for the investor's share of profit or loss, dividends received, impairment, and disposal, posting a balanced journal entry at each step. Posting actions are gated to the EH Accounting Manager group, entries are sealed, and every missing account, journal, or invalid state raises an explicit message instead of a silent fallback.",
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
        'views/business_combination_views.xml',
        'views/equity_investment_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_business_combination/static/tests/tours/business_combination_test_tour.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/business_combination_01.png'],
}
