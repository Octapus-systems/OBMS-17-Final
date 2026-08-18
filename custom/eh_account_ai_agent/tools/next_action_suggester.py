# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Collections case next-best-action suggester.

The deterministic default produces an action suggestion based on the
case's days-overdue, promise-to-pay state, and follow-up history:

* No contact yet AND days_overdue >= 30 -> 'first_contact_email'
* Contacted but no promise AND days_overdue >= 45 -> 'phone_call'
* Promise to pay broken (lapsed) -> 'escalate_to_manager'
* Days overdue >= 90 AND no payment plan -> 'send_demand_letter'
* Days overdue >= 120 AND demand letter sent -> 'refer_to_collections_agency'
* Days overdue >= 180 -> 'consider_write_off'
* Promise active AND not yet at promise date -> 'monitor'
* Otherwise -> 'manual_review'

The LLM hook can produce a custom action recommendation that
considers case history nuance (customer responsiveness pattern,
prior payment behaviour). Stub providers raise ProviderError; the
deterministic action is returned.
"""

import dataclasses
import datetime  # noqa: F401
from typing import Optional

from .provider_registry import (
    get_provider, has_provider, ProviderError,
)
from .llm_parsing import extract_json_object


@dataclasses.dataclass
class CaseSnapshot:
    """Plain transport record describing a collections case."""
    days_overdue: int
    total_overdue: float
    contact_count: int = 0
    has_active_promise: bool = False
    has_broken_promise: bool = False
    last_action_type: str = ''  # 'email' | 'call' | 'letter' | ''
    last_action_days_ago: int = 0
    has_demand_letter: bool = False
    has_payment_plan: bool = False
    customer_segment: str = ''  # 'enterprise' | 'mid' | 'smb'


@dataclasses.dataclass
class Suggestion:
    action: str           # short action key
    rationale: str        # human-readable
    priority: str         # 'low' | 'medium' | 'high'


def suggest(
    case: CaseSnapshot,
    provider_key: str = 'manual',
    provider_config: Optional[dict] = None,
) -> Suggestion:
    """Return the deterministic suggestion; optionally augment via LLM.

    :param case: a CaseSnapshot.
    :param provider_key: registered provider key. Manual or missing
        returns the deterministic suggestion.
    :return: Suggestion record.
    """
    suggestion = _deterministic_suggest(case)
    if provider_key and provider_key != 'manual':
        try:
            if has_provider(provider_key):
                provider = get_provider(provider_key, provider_config)
                if not getattr(provider, 'is_manual', False):
                    enriched = _augment_via_llm(
                        provider, case, suggestion,
                    )
                    if enriched:
                        return enriched
        except ProviderError:
            pass
    return suggestion


def _deterministic_suggest(case: CaseSnapshot) -> Suggestion:
    if case.has_broken_promise:
        return Suggestion(
            action='escalate_to_manager',
            rationale=(
                "Promise to pay lapsed; escalate per the dunning "
                "ladder so the manager applies pressure beyond the "
                "collector's reach."
            ),
            priority='high',
        )
    if case.has_active_promise:
        return Suggestion(
            action='monitor',
            rationale=(
                "Active promise to pay in place; do not contact "
                "until the promise date passes. Re-evaluate then."
            ),
            priority='low',
        )
    if case.days_overdue >= 180:
        return Suggestion(
            action='consider_write_off',
            rationale=(
                "Over 180 days overdue (%d); recovery probability "
                "is below 25%% statistically. Consider partial or "
                "full write-off after one final demand."
                % case.days_overdue
            ),
            priority='high',
        )
    if case.days_overdue >= 120 and case.has_demand_letter:
        return Suggestion(
            action='refer_to_collections_agency',
            rationale=(
                "Demand letter sent and no response in 30+ days; "
                "external collections agency is the next escalation."
            ),
            priority='high',
        )
    if case.days_overdue >= 90 and not case.has_payment_plan:
        return Suggestion(
            action='send_demand_letter',
            rationale=(
                "Over 90 days overdue and no payment plan; send "
                "formal demand letter to start the legal-action "
                "clock."
            ),
            priority='high',
        )
    if case.contact_count > 0 and case.days_overdue >= 45:
        return Suggestion(
            action='phone_call',
            rationale=(
                "Email contacted but no promise after %d days; a "
                "phone call typically converts at 3x the email "
                "response rate."
                % case.days_overdue
            ),
            priority='medium',
        )
    if case.contact_count == 0 and case.days_overdue >= 30:
        return Suggestion(
            action='first_contact_email',
            rationale=(
                "No contact attempts yet on a %d-day overdue case; "
                "send the standard first-contact email today."
                % case.days_overdue
            ),
            priority='medium',
        )
    return Suggestion(
        action='manual_review',
        rationale=(
            "Case state does not match a default rule; collector "
            "should review manually."
        ),
        priority='low',
    )


def _augment_via_llm(provider, case, deterministic):
    summary = (
        "days_overdue=%d total=%.2f contacts=%d "
        "active_promise=%s broken_promise=%s "
        "last_action=%s last_action_days_ago=%d "
        "demand_letter=%s payment_plan=%s segment=%s"
    ) % (
        case.days_overdue, case.total_overdue, case.contact_count,
        case.has_active_promise, case.has_broken_promise,
        case.last_action_type or 'none', case.last_action_days_ago,
        case.has_demand_letter, case.has_payment_plan,
        case.customer_segment or 'unknown',
    )
    messages = [
        {
            'role': 'system',
            'content': (
                "You are a collections specialist. Given a case "
                "snapshot and the deterministic next-action "
                "suggestion, return a JSON object with three keys: "
                "action (short snake_case identifier), rationale "
                "(plain English), priority (low | medium | high). "
                "Override the deterministic suggestion only when "
                "the case nuance warrants it; otherwise echo it."
            ),
        },
        {
            'role': 'user',
            'content': (
                "Deterministic suggestion: action=%s priority=%s\n"
                "Snapshot: %s"
            ) % (
                deterministic.action, deterministic.priority, summary,
            ),
        },
    ]
    # Real provider returns JSON we parse into a Suggestion; stub
    # providers raise ProviderError handled by the caller. A malformed
    # or partial response yields None so the deterministic suggestion
    # is used unchanged.
    raw = provider.chat(messages)
    data = extract_json_object(raw)
    if not data:
        return None
    action = data.get('action')
    priority = data.get('priority')
    if not action or priority not in ('low', 'medium', 'high'):
        return None
    return Suggestion(
        action=str(action),
        rationale=str(data.get('rationale') or deterministic.rationale),
        priority=priority,
    )
