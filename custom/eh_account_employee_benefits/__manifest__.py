# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The defined-benefit mechanics (net
# interest on the opening net position, service cost and settlement results
# to profit or loss, remeasurements to other comprehensive income without
# recycling, the asset ceiling test, and the obligation and plan-asset
# rollforward disclosures) are IAS 19 as published; no code or comments
# derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'Employee Benefits (IAS 19)',
    'summary': 'IAS 19 employee benefits accounting layer for Odoo 17 Community: import the actuarial valuation of each defined benefit plan per period, and the module computes net interest on the opening net position, routes service cost and settlement gains to profit or loss and remeasurements to non-recycling OCI, applies the asset ceiling, enforces obligation and plan-asset rollforward ties, and posts one sealed journal entry per valuation, with a defined contribution accrual for completeness. Search: odoo 19 IAS 19, defined benefit accounting, DBO rollforward, net interest defined benefit, remeasurement OCI, asset ceiling IAS 19.64, past service cost, settlement gain, defined contribution accrual, pension accounting odoo.',  # noqa: E501
    'description': 'The IAS 19 defined benefit ACCOUNTING layer for Odoo 17 Community. Actuarial results (defined benefit obligation, plan assets, service cost, actuarial gains and losses, asset ceiling) are IMPORTED INPUTS keyed from the actuary\'s report per plan per period; the module never computes them and contains no actuarial engine (no mortality tables, no salary projection). What the module owns is the ledger mechanics IAS 19 requires of the books: net interest on the opening net defined benefit liability or asset at the discount rate, current and past service cost and settlement gains or losses to profit or loss, remeasurements (actuarial gains and losses, excess return on plan assets, asset ceiling changes) to other comprehensive income with no recycling, the asset ceiling test capping a surplus at the available refunds, and one balanced sealed journal entry per posted valuation. Rollforward tie constraints refuse a keyed closing obligation or closing plan asset figure that does not reconcile to the movement analysis, and each valuation\'s opening figures are chained to the prior posted valuation, so the DBO and plan asset disclosure schedules per IAS 19.140-141 build straight from the posted ledger. A defined contribution accrual (IAS 19.51) completes the scope. Posting is gated to the EH Accounting Manager group and every generated journal entry is sealed against edit.',  # noqa: E501
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.0.0',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/benefit_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_employee_benefits/static/tests/tours/benefit_test_tour.js',
        ],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
