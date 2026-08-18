# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Post Dated Cheques (PDC)',
    'summary': 'First-class post dated cheque management for Odoo 17 Community. One cheque register handles both issued (payable) and received (receivable) cheques through a guarded draft, registered, presented, cleared, bounced, replaced and cancelled state machine, with cheque books, concurrency-safe serial allocation, balanced double-entry posting through a suspense account, a bounce and replace workflow, and a daily auto-present cron. Search terms: post dated cheque management odoo, PDC odoo 19 community, cheque register odoo, cheque book serial tracking odoo, issued and received cheque tracking, cheque bounce and replace workflow, post dated cheque accounting, cheque clearing suspense account odoo, cheque lifecycle state machine odoo.',  # noqa: E501
    'description': """Post dated cheques are part of normal trade in many markets, and Odoo Community has no native handling. This module makes the cheque a real record, not a memo note on a payment. Issued cheques (vendor pay) and received cheques (customer collect) live on one model with a direction flag and a guarded lifecycle: draft, registered, presented, cleared, bounced, replaced, cancelled. Each transition stamps the user and timestamp and is blocked unless the state machine allows it.  # noqa: E501

Cheque books are tracked per bank journal with start and end serials, a next-serial pointer and a remaining count. Registration of an issued cheque takes a row lock on the book so two concurrent registrations queue instead of racing the pointer, rejects any serial that is not the next one in line, and flips the book to exhausted automatically when the last serial is consumed. A unique constraint on book and cheque number is the safety net.  # noqa: E501

Present, clear and bounce each post a real balanced journal entry through a suspense account. Foreign-currency cheques convert the cheque amount into the company currency for the debit and credit while carrying the signed foreign amount in amount_currency, so FX cheques post balanced and stay reconcilable. The account resolver hard-fails with a named error when a suspense, default, receivable or payable account is missing, rather than posting a one-sided entry.  # noqa: E501

The bounce wizard records a configurable reason (insufficient funds, signature, stop payment, account closed, post dated, technical, amount mismatch, other), the bank dishonour date and a bank-charge amount. The bounce reversal is dated at the dishonour date, not the day the operator recorded it, validated against the presentation date and the accounting lock dates; when the dishonour period is locked, a Post at Current Date option books the reversal in the current period while the dishonour date stays on the record for disclosure. Nonzero bounce charges post a second entry, debit bounce charges expense and credit bank, at the same date; the expense account is configured on the bank journal with a company level fallback in Settings. The reversal fires only when the present entry was actually posted, so it never double-reverses. A bounced cheque can be re-banked through the replace wizard, which chains a new cheque carrying a new value date and moves the original to replaced. A daily cron auto-presents every registered cheque, issued or received, once its value date arrives, with each row wrapped in its own savepoint so one failing cheque never aborts the batch.  # noqa: E501

IFRS 9 recognition mapping. Registered: the entity becomes party to the contractual provisions of the instrument; no entry, the underlying receivable or payable stays recognised. Presented: no derecognition yet; the deposit entry moves the exposure from the receivable into the bank suspense account, an internal control choice reflecting that the cheque is with the bank but cash is not yet received. Cleared: derecognition on settlement, suspense transfers to bank. Bounced: reinstatement, the present entry is reversed at the dishonour date and dishonour charges are expensed at the same date.  # noqa: E501

Foreign currency cheques are monetary items: the present and clear entries carry the signed foreign amount in amount_currency on every line, so period end FX revaluation (eh_account_fx_revaluation) can retranslate the open suspense holding at the closing rate. Flag the bank journal suspense account as revaluable there, since suspense accounts are current assets and are not auto-flagged. For loss allowances, each cheque exposes days outstanding and an eh_ecl_exposure_lines() provider hook returning the open suspense exposures of presented incoming cheques, whose receivable was reconciled at deposit and would otherwise fall out of the ECL receivables population.  # noqa: E501

Pairs with eh_account_collections for follow up on bounced customer cheques.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.2.0',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/bounce_reasons.xml',
        'data/cron.xml',
        'views/cheque_book_views.xml',
        'views/cheque_views.xml',
        'views/bounce_reason_views.xml',
        'views/res_partner_views.xml',
        'views/account_move_views.xml',
        'views/account_journal_views.xml',
        'views/res_config_settings_views.xml',
        'wizards/replace_cheque_wizard_views.xml',
        'wizards/bounce_cheque_wizard_views.xml',
        'report/cheque_register_report.xml',
        'report/cheque_print_report.xml',
        'data/menus.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
