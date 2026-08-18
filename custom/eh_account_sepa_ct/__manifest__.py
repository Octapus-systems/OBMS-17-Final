# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The module is built directly
# against the public ISO 20022 PAIN.001.001.03 specification published
# at https://www.iso20022.org/. No code or comments derive from any
# proprietary or third-party Odoo module.
#
##############################################################################
{
    'name': 'SEPA Credit Transfer',
    'summary': 'Generate ISO 20022 SEPA Credit Transfer XML (PAIN.001.001.03 and PAIN.001.001.09) from a batch of posted vendor payments in Odoo 17 Community. Pure Python IBAN mod-97 and BIC validation with no external library, single currency EUR enforcement, SEPA Latin character sanitisation, and a per export SHA-256 audit record. SEPA credit transfer Odoo 17, PAIN.001.001.03 generator, PAIN.001.001.09 ISO 20022, SEPA XML batch vendor payment, IBAN BIC validation, EUR pay run bank upload file.',  # noqa: E501
    'description': """SEPA Credit Transfer builds the ISO 20022 credit transfer file that EU bank portals accept for paying many vendors in one upload. You assemble a batch of posted payments, pick the originator (your debtor IBAN, BIC and name), and generate the XML. The file is produced with lxml against the published PAIN.001.001.03 and PAIN.001.001.09 namespaces, and the originator record lets you choose which version a given bank expects.  # noqa: E501

Validation happens before the file is written, not after the bank rejects it. IBAN is checked with a pure Python mod-97 (ISO 13616) routine that also verifies the country is in the SEPA zone and the length matches that country exactly. BIC is checked against ISO 9362 structure rules, with 8 character codes padded to 11 with the XXX branch. There is no external validation library to install. If a batch mixes currencies, generation refuses and names the offending currencies so you split the batch before the bank does.  # noqa: E501

Names and remittance text are run through a SEPA Latin character set pass that transliterates accents and strips characters the scheme does not allow, so a supplier name with diacritics does not bounce at the gateway. Each transaction renders as its own entry for cleaner reconciliation on the bank side.  # noqa: E501

Every export writes an audit record stamped with the generating user, timestamp, message ID, transaction count, control sum and a SHA-256 file hash. Regenerating a batch supersedes the prior file rather than deleting it, and the stored hash lets an auditor spot a duplicate generation. State moves from generated to downloaded when you pull the file.  # noqa: E501

Built directly from the public ISO 20022 specification. Original work, not a derivative. Pairs with the batch payment module: build the batch, generate the SEPA file, upload to the bank.""",  # noqa: E501
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.1.5',
    'depends': ['eh_account_base', 'eh_account_batch_payment', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/email_templates.xml',
        'views/sepa_originator_views.xml',
        'views/batch_payment_views.xml',
        'views/sepa_export_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_backend': ['eh_account_sepa_ct/static/src/js/tours/sepa_ct_tour.js'],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
