# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Group Consolidation',
    'summary': 'Multi-entity group consolidation for Odoo 17 Community that rolls the trial balance of a parent plus its subsidiaries into one consolidated reporting set. IAS 21 currency translation with a genuine time-weighted average rate versus closing rate, automatic non-controlling-interest carve-out below 100% ownership, balanced intercompany elimination journals, and a source-tagged consolidated run you can trace line by line. Search terms: Odoo 17 consolidation, Odoo Community group consolidation, multi-entity consolidation, IAS 21 currency translation, intercompany elimination, non-controlling interest NCI, consolidated trial balance, CTA translation reserve, multi-company financial consolidation, full equity and proportional method.',
    'description': """Define a consolidation entity once: presentation currency, root company, and the list of member companies with ownership percentage and method (full, equity, or proportional). Each period, run a consolidation. The engine pulls every member's posted balances, translates them to the presentation currency, eliminates intercompany positions, computes non-controlling interest where ownership is below 100 percent, and writes the IAS 21 currency-translation-adjustment difference so the consolidated set balances.

The translation is the real thing. Balance-sheet accounts convert at the closing rate at period end, while income and expense accounts convert at a time-weighted average rate that weights each spot rate by the number of days it was in effect across the period. The gap between the two rates is exactly what the CTA captures. A flat rate produces zero CTA, as it should, and a genuine FX movement produces a genuine adjustment.

Every consolidated number is traceable. Each run line is tagged by source (parent, subsidiary, elimination, CTA, or NCI), so you can see precisely where any figure came from. The CTA difference is recorded as a tagged kind=cta consolidation run line against the configured equity translation-reserve account. No journal entry is posted to your live ledger. Non-controlling interest is computed as subsidiary equity multiplied by one minus the ownership percentage and recorded as its own tagged line.

Intercompany elimination journals are balanced on post: debit must equal credit, or the post is refused. Only posted member moves and only posted eliminations feed a run, so draft and unposted entries never leak into the consolidated set.

The run has a draft to computed to reviewed to closed lifecycle. Recompute is idempotent: re-running Compute deletes the prior run lines and rebuilds them from scratch, so it is never additive. Closed runs are locked in the UI with all fields read-only and no further transitions, every state change is captured in the chatter audit trail, and reset-to-draft requires Accounting Manager privileges.

Method depth. Proportional members roll up at ownership share, applied after translation so the IAS 21 arithmetic is undistorted, and NCI configuration is blocked for them by constraint. Equity-method members are mandatory-config: a missing investment or share-of-profit account refuses the compute instead of silently dropping the associate, the IAS 28 pick-up is idempotent across recomputes, and the IAS 28.1A fair value option can be elected per member (no pick-up, a memo disclosure line records the election). The IFRS 3 investment elimination runs automatically for configured members: parent investment against acquisition-date equity (optionally translated at a historical rate), acquisition NCI on a proportionate or fair-value basis per member, goodwill or bargain-purchase residual carried on its own tagged line. The CTA plug is split per member and can carry a link to a CTA reserve position, and disposing a member recycles its accumulated translation reserve to profit or loss at the chosen percentage per IAS 21.48. IFRS 10.B87/B92-93 guards block a compute when a member reports more than three months off the group date or is not policy-aligned, with an audited manager override.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.2.0',
    'depends': [
        'eh_account_base',
        'eh_account_fx_revaluation',
        'account',
        'mail',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/consol_security.xml',
        'data/sequences.xml',
        'views/consol_entity_views.xml',
        'views/consol_run_views.xml',
        'views/consol_elimination_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_tests': [
            'eh_account_consolidation/static/tests/tours/consolidation_test_tour.js',
        ],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
