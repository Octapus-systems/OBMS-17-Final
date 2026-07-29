# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Fixed Assets and Leases',
    'summary': 'Fixed asset register and IFRS 16 lease accounting for Odoo 17 Community with real persisted depreciation schedules and journal posting. Odoo Community asset depreciation, four depreciation methods (straight line, reducing balance, AU prime cost, AU diminishing value) plus manual, IAS 36 impairment with reversal cap, right of use asset and lease liability amortisation, lease modification and termination, asset disposal gain loss, deferred revenue and deferred expense recognition, asset under construction CIP, Australian low-value pool, multi-book tax and statutory reporting schedules. No Enterprise modules required.',
    'description': """A first class fixed asset register and IFRS 16 lessee accounting engine for Odoo 17 Community. No paid Enterprise modules required. Every schedule is a real persisted record, one line per period with computed depreciation, accumulated balance and net book value, queryable and exportable for audit.

Asset register. Asset categories carry default useful life, salvage rate, depreciation method and the asset, depreciation expense and accumulated depreciation accounts plus journal. The depreciation engine supports four working methods that generate GL postable schedules: straight line, reducing balance (declining) with an automatic switch to straight line on the remaining balance, AU prime cost, and AU diminishing value, plus a manual schedule. Units of production is a preview. First period proration by days in service is a switch on the asset. The schedule reaches salvage exactly at end of life through a last row rounding true-up.

Lifecycle and disposal. Draft, running, paused, fully depreciated and disposed states. A posted asset cannot return to draft and a disposed asset cannot reopen. Dispose at a price posts a balanced gain or loss entry, transfers accumulated depreciation and terminates the schedule. Disposing with zero proceeds writes off the remaining book value as a loss.

IAS 36 impairment. Impairment posts with a dual reversal ceiling: a reversal is blocked when it exceeds cumulative charges and also when it would lift the carrying amount above the depreciated historical cost the asset would have had with no impairment. After an impairment charge or reversal posts, the remaining schedule is rebuilt over the same number of remaining periods on the post event net book value (IAS 36.63).

IFRS 16 lessee. A lease contract carries lessor, term, payment cadence (monthly, quarterly, semi annual, annual), advance or arrears timing, fixed payment, incremental borrowing rate and optional initial direct costs and prepaid payments. On activation the present value of payments is computed, the ROU asset and lease liability are created and the opening entry posts. The amortisation schedule splits each payment into principal and interest so that principal plus interest equals the payment, with ROU depreciation straight line over the term and a last row true-up that lands the liability on zero. The lease term must be a whole multiple of the payment cadence or activation raises a clear error. The modification wizard remeasures the liability at the revised rate, adjusts the ROU by the same delta and rebuilds the remaining schedule. The termination wizard derecognises the ROU and remaining liability and posts the gain or loss.

Deferred revenue and expense. The same schedule engine, exposed through a single deferred_type switch on the record, releases prepaid revenue into income or prepaid expense into the P/L over the schedule, posting a balanced two leg entry each period.

Specialised regimes. Asset under construction accumulates cost with no schedule, then a capitalise action posts a balanced transfer entry, sets the in service date and starts depreciation. Instant asset write-off books a one shot full write-off in the first period regardless of useful life, salvage honoured. An IAS 16 parent and child component roll-up and an AU low-value pool with idempotent annual depreciation are included.

Operations. A daily cron posts each running asset's due depreciation lines, isolating every asset under its own savepoint so one bad asset never freezes the batch. A daily cron posts each active lease's due amortisation entries, isolating each lease with per record try and except. A manual Post Due Lines action force posts overdue lines on demand.

Pairs with eh_account_close_workflow for period close validation and with eh_account_dynamic_reports for asset register reports.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.5.0',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'data/cron.xml',
        'views/account_account_views.xml',
        'views/asset_category_views.xml',
        'views/asset_views.xml',
        'views/asset_depreciation_line_views.xml',
        'views/asset_impairment_views.xml',
        'views/asset_cgu_views.xml',
        'views/asset_lvp_pool_views.xml',
        'views/lease_contract_views.xml',
        'views/lease_schedule_line_views.xml',
        'views/res_partner_views.xml',
        'views/res_config_settings_views.xml',
        'wizards/asset_dispose_wizard_views.xml',
        'wizards/asset_revalue_wizard_views.xml',
        'wizards/lease_modify_wizard_views.xml',
        'wizards/lease_terminate_wizard_views.xml',
        'report/asset_register_report.xml',
        'data/menus.xml',
    ],
    'demo': ['demo/asset_demo.xml'],
    'assets': {
        'web.assets_tests': [
            'eh_account_assets_pro/static/tests/tours/asset_test_tour.js',
        ],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
