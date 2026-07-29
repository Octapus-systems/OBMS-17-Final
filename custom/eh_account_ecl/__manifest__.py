# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The expected-credit-loss mechanics
# (a provision matrix of ageing buckets and loss rates under the simplified
# approach, a loss allowance carried as a contra-asset, the movement to
# impairment loss) are IFRS 9 as published; no code or comments derive from
# any proprietary or third-party module.
#
##############################################################################
{
    'name': 'Expected Credit Loss (IFRS 9)',
    'summary': 'IFRS 9 expected credit loss for Odoo 17 Community, built as a provision matrix of ageing buckets and loss rates that posts only the movement in the loss allowance to impairment loss. Search: odoo 19 expected credit loss, IFRS 9 ECL, provision matrix, simplified approach, loss allowance, impairment of receivables, bad debt allowance, ageing buckets loss rate, general 3-stage model, EAD LGD PD, roll-forward loss allowance.',
    'description': "Expected Credit Loss measures the loss allowance on trade receivables under IFRS 9. A run carries a provision matrix of ageing buckets, each with a days past due range and a loss rate, and the gross carrying amount per bucket can be entered by hand or populated from the company's open, unreconciled receivables aged at the reporting date. It computes the closing allowance, and posting recognises only the movement from the opening allowance in one balanced entry: an increase debits impairment loss and credits a loss-allowance contra account, a decrease reverses it (IFRS 9.5.5.8). A general 3-stage model (EAD, LGD, 12-month and lifetime PD) and optional present-value discounting are also supported. Beyond open receivables, a run can also ingest credit exposures reported by any installed provider that implements the documented exposure hook (for example presented post-dated cheques whose receivable has been reconciled into a bank suspense holding), aged into the same provision matrix. Posting, reversal and cancellation are gated to the EH Accounting Manager group, and a posted run's inputs are frozen so the recognised figure cannot drift from the ledger.",
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
        'views/ecl_run_views.xml',
        'views/ecl_engine_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_ecl/static/tests/tours/ecl_test_tour.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/ecl_01_run.png'],
}
