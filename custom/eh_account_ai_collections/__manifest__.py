# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'AI Collections Next Action',
    'summary': 'Live deterministic next-action suggestions on collections cases for Odoo 17 Community. Each case shows a suggested action (contact, escalate, demand letter, agency referral, write-off), a priority level, and a plain-English rationale, computed on the fly from days overdue, contact history, and promise-to-pay state. An active promise always suppresses escalation; a broken promise escalates immediately. Runs with zero API keys by default. When a company configures an AI provider on the AI agent layer, the same hook hands the case snapshot to the provider for a richer narrative and falls back to the deterministic suggestion on any error. Keywords: collections next action, dunning ladder, promise to pay, collections automation, demand letter, agency referral, write-off guidance, collections case management, Odoo collections, deterministic escalation.',  # noqa: E501
    'description': "A bridge module that wires the deterministic collections next-action engine live onto eh.collections.case records. Every case gains three computed fields: a suggested action, a priority level (low/medium/high), and a plain-English rationale. The suggestion is recomputed on every read so it always reflects the current case state without stale stored values. The deterministic dunning ladder follows eight rules: broken promises escalate immediately to manager review; active promises suppress all escalation and signal monitor mode; cases at 180+ days route to write-off review; cases at 120+ days with a demand letter sent route to agency referral; cases at 90+ days without a payment plan route to demand letter; cases with prior contact at 45+ days route to phone call; cases with no contact at 30+ days route to first-contact email; everything else routes to manual review. The suggestion runs with zero network access by default. When a company configures an AI provider on the company's AI agent settings, the same hook sends the case snapshot to that provider for an optional richer rationale, and gracefully falls back to the deterministic suggestion on any provider error or bad credential.",  # noqa: E501
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.0.0',
    'depends': ['eh_account_ai_agent', 'eh_account_collections'],
    'data': ['views/collections_case_views.xml'],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif'],
}
