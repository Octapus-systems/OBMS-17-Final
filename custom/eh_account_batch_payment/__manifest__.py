# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Batch Payment',
    'summary': 'Group inbound or outbound payments into a single posted batch on Odoo 17 Community, with per-partner aggregation, CSV export for bank-portal upload, manager-only post and cancel, and per-payment savepoint isolation so one bad payment never freezes the run. Search terms: Odoo 17 batch payment, Odoo Community vendor payment run, batch vendor payments, group customer receipts, per-partner payment aggregation, AP payment run, batch payment CSV export bank portal, manager approval payment posting, bulk account.payment, payment batch audit trail.',  # noqa: E501
    'description': """Run a single batch instead of N individual payments. Open the build wizard, pick the open invoices or vendor bills to clear, choose whether to aggregate per partner, and the wizard creates one draft account.payment per source (or one summed payment per partner). Pre-existing draft payments join a batch by setting the Batch field on the payment record.  # noqa: E501

Posting the batch posts every member payment in one pass, with per-payment savepoint isolation: a single validation failure (a closed period, a missing journal payment method, anything the underlying posting rejects) is caught for that one payment, written to the batch chatter, and the rest of the batch still posts. If every payment in the batch fails, the batch is deliberately left where it is rather than marked posted, so it can never become a stranded posted batch with zero movements. A posted-but-empty batch can be reset to draft and retried.  # noqa: E501

Output is plain standard account.payment records, with only a nullable batch link added, so reconciliation, the vendor ledger, follow-ups and reporting all see the payments natively. The build wizard refuses to mix companies in one batch, blocks a source document with no partner, and warns on the chatter when a document looks already settled by a previous batch. Manager-only guards protect post and cancel. The confirmed, posted and cancelled transitions each stamp the acting user and timestamp, and every transition is tracked on the chatter.  # noqa: E501

CSV export writes one row per payment (reference, partner, bank account, amount, currency, payment date, memo), ordered by partner, as a downloadable attachment for upload to your bank portal. The export layout can be subclassed for banks that need a different column set.  # noqa: E501

Pairs naturally with the post-dated cheque module: cheques can be presented on their value date and then batched.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.1.4',
    'depends': ['eh_account_base', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/batch_payment_views.xml',
        'wizards/build_batch_wizard_views.xml',
        'views/account_payment_views.xml',
        'data/menus.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
