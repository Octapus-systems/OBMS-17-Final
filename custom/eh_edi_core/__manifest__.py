# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'post_init_hook': 'post_init_hook',
    'name': 'EH EDI Core',
    'version': '17.0.1.1.1',
    'summary': 'Shared EDI foundation for the ERP Heritage e-invoicing modules: one AES-256-GCM credential vault and one canonical EN16931 invoice model and mapper. EN16931 invoice model Odoo, AES-256-GCM credential vault, encrypt API secrets at rest, authenticated encryption, account.move to EN16931 mapper, Factur-X CII adapter, Peppol BIS Billing UBL, UNTDID 5305 tax category, e-invoicing shared library Odoo 17.',
    'description': """Internal technical dependency for the ERP Heritage e-invoicing modules (Mauritius MRA, France PDP / PPF Factur-X, Peppol BIS Billing). It gives the e-invoicing line two shared foundations so each format module stays small, consistent, and audited in one place.

AES-256-GCM credential vault. One audited authenticated-encryption implementation, parametrised per module by a key namespace, so API secrets for each tax authority or access point are encrypted once instead of a copy of the crypto per module. The master key resolves from one of three sources in order (environment variable, a 0600 key file, or a system config parameter), and the vault refuses to operate when no master key resolves. A master-key file with group or other permissions is rejected outright (chmod 0600 required), the config-parameter source emits a loud least-secure warning because the key would sit beside the ciphertext, and tampered, truncated, wrong-key, or non-base64 ciphertext raises a vault error rather than returning garbage. Empty or unset credential fields pass through cleanly so they never raise.

Canonical EN16931 invoice model and account.move mapper. One mapper reads an account.move into a single EN16931 semantic model with BT-code-annotated fields (parties, lines, multi-rate tax breakdown, totals, references, UNTDID 5305 tax categories). It is the sole reader of the move for outbound e-invoicing. The model exposes to_cii_input and to_ubl_inputs adapters; each format module serialises that one shared model to its target syntax (Factur-X CII, Peppol UBL) instead of re-reading the move. Product-line detection works across Odoo versions, multi-rate tax buckets keep a deterministic first-seen order, credit notes map to the correct document type, and optional cross-module fields are read defensively so the mapper runs whether or not those modules are installed.

Not an end-user application. No user interface, no menu, no configuration of its own. Installed automatically as a dependency of the e-invoicing modules. Free and licensed LGPL-3.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'category': 'Accounting/Accounting',
    'license': 'LGPL-3',
    'depends': ['base'],
    'external_dependencies': {
        'python': ['cryptography'],
    },
    'data': [],
    'images': ['static/description/banner.gif'],
    'application': False,
    'installable': True,
    'auto_install': False,
}
