# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'AI Agent Layer',
    'summary': 'Deterministic, no-API-key AI helpers for Odoo 17 Community accounting: journal entry anomaly detection, budget-vs-actual variance commentary, and a collections next-action dunning ladder. Odoo journal entry anomaly detection, weekend posting control, just-under-threshold structuring detection, reversal-pair wash candidate flagging, budget vs actual variance commentary, collections dunning next action, pluggable LLM provider registry, bring-your-own-key cloud and self-hosted local LLM support, rule-based fraud heuristics, deterministic AI with provider fallback.',
    'description': """A reusable AI helper layer for the ERP Heritage accounting suite. It ships three accounting-specific capabilities, each with a working deterministic default that runs with zero API keys and zero network access, plus an optional provider hook for sites that later want live language-model output.

Anomaly detection on journal entries runs four deterministic rules: round-number outliers (exact thousand multiples above 3x the period median), weekend posts (entries dated Saturday or Sunday), just-under-threshold amounts within 5 percent below the configured approval threshold (a standard structuring control, opt-in via the threshold), and reversal-pair candidates that group same-user, same-account, same-amount, same-day entries as possible wash or duplicate posts. Each rule has self-suppression guards so it does not fire on meaningless input.

Variance commentary produces a plain-English headline for the close pack from a budget-vs-actual snapshot: it names the direction of the total variance (overran, underran, or on budget), highlights the top-N largest movers by absolute variance, and always emits a line even on an empty or all-zero period. With a live provider configured, this hook genuinely swaps in the provider's natural-language paragraph in place of the template.

Next-best-action suggestion picks the next collections step from an eight-branch deterministic dunning ladder, where an active promise-to-pay takes precedence over the days-overdue escalation so collections never get over-aggressive against a partner who has committed to pay.

A pluggable provider registry selects the provider per company through a configuration field (default manual). Provider stubs for hosted cloud LLM APIs and for self-hosted, OpenAI-compatible local endpoints self-register at import, validate their credentials, and refuse live calls until a separate paid provider extension re-registers under the same key. Every capability catches a provider failure and falls back to its deterministic result, so a missing or bad key never breaks the call site.

Anomaly detection is wired live into Odoo: each finding is stored against the offending journal entry as an eh.account.anomaly.finding record with an immutable evidence trail and an open / reviewed / dismissed triage state. A Scan anomalies button on the entry, a list-view bulk action, and a daily per-company scheduled scan (savepoint isolated) keep the findings current. Variance commentary and the collections next-action ladder remain reusable helper functions for now, surfaced by their consuming suite modules rather than wired here.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.2.1',
    'depends': ['eh_account_base', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/ir_cron.xml',
        'views/anomaly_finding_views.xml',
        'views/account_move_views.xml',
        'views/res_company_views.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
