# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The government-grant mechanics (grants
# recognised over the periods that match the related costs, an asset grant
# presented as deferred income or as a deduction from the asset, a
# non-monetary grant recognised at the fair value of the asset received,
# recognition only on reasonable assurance of compliance with conditions, and
# a repayment adjusted prospectively) are IAS 20 as published; no code or
# comments derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'Government Grants (IAS 20)',
    'summary': 'IAS 20 government grants for Odoo 17 Community that recognise monetary and non-monetary grants on receipt, defer income until attached conditions are met, amortise to income over the periods that match the related costs, and accrue breach clawbacks, odoo 19 government grants, IAS 20 deferred income grant, non-monetary grant fair value, grant conditions register, grant clawback liability, asset related and income related grant, grant amortisation schedule, deduction from asset carrying amount, grant repayment prospective, deferred grant income accounting',
    'description': "Government Grants (eh.gov.grant) recognises and amortises government grants under IAS 20. An income-related grant, and an asset-related grant on the deferred-income basis, is credited to a deferred-income liability on receipt and released to grant income over the periods that match the related costs (IAS 20.12), with each release posting on its own earning-period date rather than the grant's original period. An asset-related grant on the netting basis is instead deducted from the carrying amount of the asset on receipt, which reduces later depreciation, and its lifecycle closes immediately with no separate amortisation. A non-monetary grant (land, equipment) is recognised at the fair value of the asset received (IAS 20.23): receipt debits the received asset at fair value and every later release or clawback flows off that fair-value base. Each grant carries a register of attached conditions (open, fulfilled, breached) and can be set to defer income until the conditions are met (IAS 20.7/8), which blocks every release while any condition is open or breached; the flag is off by default so existing grants keep their behaviour. Breaching a condition accrues the clawback before cash moves: the repayment obligation first reverses any unamortised deferred income, charges the excess straight to profit or loss, and credits a clawback liability that the Repay action later settles against cash (IAS 20.32). A repayment without a prior breach accrual follows the same prospective order directly against cash. Every posting action is gated to the EH Accounting Manager group, each entry is sealed, and the amount, fair value, accounts and measurement basis freeze once recognition begins.",
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
        'views/grant_views.xml',
        'data/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/grants_01_grant.png'],
}
