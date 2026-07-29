# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The statement structures (a statement
# of changes in equity reconciling opening to closing equity by component, and
# a statement of comprehensive income adding OCI to profit for total
# comprehensive income) are IAS 1 as published; no code or comments derive
# from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'IAS 1 Primary Statements',
    'summary': 'Prepare the two IAS 1 primary statements a plain ledger does not give you, a statement of changes in equity and a statement of comprehensive income, each tied back to the posted general ledger. Search: odoo 19 statement of changes in equity, IAS 1 primary statements, statement of comprehensive income, other comprehensive income OCI, total comprehensive income, equity reconciliation, SOCE SOCI, owners and non-controlling interests attribution, OCI recycling reclassification IAS 1.82A, interim financial reporting IAS 34 condensed statements.',
    'description': 'This module adds two IAS 1 primary statements that a standard chart of accounts and trial balance do not produce on their own. The statement of changes in equity reconciles the opening to closing balance of each equity component (share capital, share premium, retained earnings, revaluation and other reserves, translation reserve and non-controlling interests) across profit, other comprehensive income, dividends, share issues and other movements, and the statement of comprehensive income adds the OCI components to profit for the period to give total comprehensive income, split by whether items may later be reclassified to profit or loss and attributed to owners and NCI. The reclassification split is structural: two account tags classify the ledger OCI reserve accounts as recyclable or non-recyclable, default assignments are derived from the suite modules own OCI account settings (translation and hedge reserves recyclable, FVOCI-debt recyclable, FVOCI-equity, revaluation surplus and defined benefit remeasurements non-recyclable), and a manual override against the tag is flagged. Confirmation also enforces an IAS 1.60 completeness check that blocks while posted balances sit on accounts outside the recognised current and non-current classification sets, with a logged manager override, and when the consolidation module is installed the non-controlling interest figures prefill from a covering consolidation run with any manual divergence surfaced as a discrepancy. Statements can be marked as IAS 34 interim reports with prior interim and prior annual comparatives and a condensed presentation flag. Both statements are disclosure worksheets that post no journal entries; instead they derive figures from posted journal items and surface their own tie-out to the ledger so a preparer can see exactly where a worksheet disagrees. A separate cross-statement tie-out control checks that ledger net profit, the SoCI profit for the period, the SoCE profit movement and the balance sheet current-year-earnings movement all agree for one company and period.',
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
        'data/oci_tags.xml',
        'views/statement_views.xml',
        'views/tieout_views.xml',
        'data/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/statements_01_soci.png'],
}
