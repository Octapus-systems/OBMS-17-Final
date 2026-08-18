# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The controls (an inalterable hash
# chain over posted entries, a period integrity scan, and a segregated
# sign-off) are general audit-assurance controls; no code or comments derive
# from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'Audit Pack & Integrity',
    'summary': 'Audit-grade period close for Odoo 17 Community that enables the inalterable hash chain over posted journal entries, runs a blocking integrity scan, and requires a segregated manager sign-off that advances the fiscal-year lock date. odoo 19 audit trail, hash chain inalterable, period integrity scan, secure posting, fiscal year lock date, sign off segregation of duties, audit pack period close, tamper evident ledger.',  # noqa: E501
    'description': "Audit Pack and Integrity hardens the period close so an auditor can trust the ledger. It switches Odoo's inalterable hash chain (restrict mode) on the company's sale, purchase, general, bank and cash journals, then scans the period for four integrity controls: no draft entries, every posted entry balanced to zero, every posted entry on a hash-restricted journal carrying its hash, and no open bank or cash suspense. A period is signed off only by an EH Accounting Manager who is not the preparer, and sign-off advances the fiscal-year lock date so prior-period entries cannot be edited. The gate is recomputed live at sign-off and the hash chain is re-verified through core, so a stale or tampered check row cannot vouch a period that no longer passes.",  # noqa: E501
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
        'views/audit_pack_views.xml',
        'data/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/audit_pack_01_checks.png'],
}
