# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'AU BAS Reporting',
    'summary': 'Quarterly Business Activity Statement worksheet for Odoo 17 Community Australian businesses, with the complete 33-row ATO label set (G, W, T, F, FTC, WET, LCT), a tag-driven single-SQL compute that locks once a run is lodged, GST control-account reconciliation against the 1A and 1B totals, and a mod-89 weighted ABN check-digit validator. Search terms: Odoo 17 Australian BAS, Business Activity Statement Odoo Community, Odoo GST BAS report Australia, ABN validator Odoo, ABN checksum validation, GST control reconciliation, quarterly BAS run, BAS label G1 1A 1B, PAYG withholding W1 W2, Australian tax localization Odoo Community.',
    'description': """The Business Activity Statement is the quarterly tax report Australian businesses lodge with the ATO. This module produces the figures the operator transfers onto the form and keeps a frozen, auditable record of each lodgement, on Odoo 17 Community.

It ships the canonical 33-row ATO worksheet (G, W, T, F, FTC, WET and LCT labels) as seeded records: G1 total sales, G2 export sales, G3 other GST-free sales, G10 and G11 acquisitions, 1A GST on sales, 1B GST on acquisitions, W1 and W2 PAYG withholding, and the income-tax instalment labels. One BAS run exists per company and quarter.

Each label maps to one or more account.tax tags and is summed by a single-SQL pass per label, so once the tag mapping is configured the run recomputes deterministically from posted journal items in the period. The aggregation is side-aware: base and tax sums are taken per ledger side, so a credit note and a sale are not conflated by an absolute-value shortcut. Out of the box every label ships as manual entry, so a fresh install gives you a structured worksheet you fill in. You configure an aggregation mode and the tax tags per label during onboarding to switch the relevant labels to auto-compute.

When a run is marked as lodged it becomes read-only. Recompute and reset-to-draft are both refused, lodged_at and lodged_by are stamped, and a re-run raises an explicit error rather than silently reproducing different numbers against an already-lodged figure. Every compute writes a start, complete or fail row to the shared report-execution audit trail; a mid-compute failure is recorded on the audit row and re-raised, never swallowed into a half-written state.

GST control reconciliation sums posted movement on the tax control accounts over the BAS period and flags any non-zero variance against the 1A and 1B line amounts before you lodge. The 1A (GST collected) side reconciles against the credit movement and is sound; the 1B (GST paid) side uses the gross debit movement and is approximate on accounts that carry both input and output tax. Partners carry a validated ABN field: the mod-89 weighted check digit is verified on save, and whitespace, hyphens and punctuation are normalised to the canonical 11-digit form on both create and write.

Out of scope: SBR lodgement transport (the module produces the figures, not the wire transmission to the ATO), and activity-statement variants beyond the quarterly BAS such as PAYG monthly forms and the IAS for non-GST registrants.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Localizations',
    'version': '17.0.1.1.3',
    'depends': ['eh_account_base', 'eh_account_dynamic_reports', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/bas_labels.xml',
        'data/dynamic_report.xml',
        'views/bas_run_views.xml',
        'views/gst_recon_views.xml',
        'views/res_partner_views.xml',
        'data/menus.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
