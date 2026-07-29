# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The module is built directly
# against the public ISO 20022 PAIN.008.001.02 specification published
# at https://www.iso20022.org/ and the SEPA Direct Debit Scheme
# Rulebook published by the European Payments Council. No code or
# comments derive from any proprietary or third-party Odoo module.
#
##############################################################################
{
    'name': 'SEPA Direct Debit',
    'summary': 'SEPA Direct Debit collection for Odoo 17 Community. Generate ISO 20022 PAIN.008.001.02 XML written from the public spec, run the full FRST, RCUR, FNAL, OOFF mandate lifecycle with an atomic sequence counter, and block non-compliant exports before they reach the bank. Search terms: SEPA direct debit Odoo 17, PAIN.008.001.02 XML generator, SEPA mandate management, FRST RCUR FNAL OOFF sequence type, SEPA creditor identifier, CORE B2B COR1 direct debit scheme, 36-month mandate dormancy, recurring direct debit collection, IBAN BIC validation, SEPA direct debit Community edition.',
    'description': """SEPA Direct Debit for Odoo 17 Community collects EUR payments from customer bank accounts via the SEPA Direct Debit scheme. It pairs with the Recurring Invoices module for subscription collection and with Batch Payment for ad-hoc collection runs.

The PAIN.008.001.02 XML is original work, written strictly from the public ISO 20022 specification and the SEPA Direct Debit Scheme Rulebook. Tests parse the generated XML back with lxml and assert the scheme structure: namespace, sequence type, local instrument, the mandate-related block, creditor identifier, charge bearer, and control sum.

The mandate state machine encodes the FRST, RCUR, FNAL, OOFF transitions exactly as the rulebook describes. The first collection on a mandate renders as FRST, and an atomic SQL counter increment flips it to RCUR automatically thereafter, so concurrent collection attempts cannot race the transition. Mandate states are Draft, Active, Completed, Revoked, and Expired.

The scheme's 36-month dormancy rule is double-enforced: on the collection path and on a daily cron that processes each mandate inside its own savepoint, so one bad record never aborts the batch.

IBAN and BIC are validated locally against ISO 13616 and ISO 9362, with no external library dependency. CORE, B2B, and COR1 local instruments are supported. Each creditor carries a default local instrument that the exporter writes into every file. Per-company creditor configuration holds the creditor identifier and the pre-notification window.

Every generated file carries a SHA-256 fingerprint. Re-generating a file for the same batch and sequence type marks the prior file as superseded, so the audit trail always shows which file is current.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.0.9',
    'depends': [
        'eh_account_base',
        'eh_account_batch_payment',
        'eh_account_sepa_ct',
        'account',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'data/cron.xml',
        'views/sepa_creditor_views.xml',
        'views/sepa_mandate_views.xml',
        'views/batch_payment_views.xml',
        'views/sepa_dd_export_views.xml',
        'data/menus.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
