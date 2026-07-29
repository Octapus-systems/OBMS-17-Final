# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Journal-entry anomaly detection.

The deterministic default flags moves on rule-based heuristics that
match the patterns auditors most commonly investigate:

* Round-number outlier: amount is an exact thousand multiple AND
  is more than 3x the median posted amount in the same period.
* Weekend post: move dated on Saturday or Sunday.
* Just-under-threshold: amount within 5% below a configurable
  approval threshold (suggests structuring to evade approval).
* Reversal pair: a debit and matching credit on the same account
  posted by the same user within the same period (suggests an
  in-and-out wash entry).

The LLM hook (when a non-manual provider is configured) calls
chat() with the deterministic flags as input and asks for a
prioritised summary; the LLM output augments rather than replaces
the deterministic findings, so a missing or failing provider never
causes a capability call to error out.

The detector returns a list of Finding records:

    Finding(
        move_id, severity, rule, description, amount, date
    )

Callers (a dashboard tile, a close-workflow validator, a chatter-
posting cron) consume the list however they want.
"""

import dataclasses
import datetime
import statistics
from typing import Iterable, List, Optional

from .provider_registry import (
    get_provider, has_provider, ProviderError,
)
from .llm_parsing import extract_json_array


@dataclasses.dataclass
class JournalEntrySummary:
    """Plain transport record describing one journal entry.

    The detector consumes these summaries rather than account.move
    records so it stays Odoo-independent and unit-testable. Callers
    inside Odoo build summaries from account.move; tests can build
    them from literals.
    """
    id: int
    date: datetime.date
    amount: float                 # absolute amount, signed positive
    posted_by: str = ''           # user login or display name
    account_codes: tuple = ()     # accounts touched
    reference: str = ''


@dataclasses.dataclass
class Finding:
    move_id: int
    severity: str                 # 'low' | 'medium' | 'high'
    rule: str                     # rule key
    description: str              # human-readable
    amount: float
    date: datetime.date


_SEVERITY_BY_RULE = {
    'round_outlier': 'medium',
    'weekend_post': 'low',
    'just_under_threshold': 'high',
    'reversal_pair': 'medium',
}


def detect(
    entries: Iterable[JournalEntrySummary],
    approval_threshold: float = 0.0,
    median_multiplier: float = 3.0,
    provider_key: str = 'manual',
    provider_config: Optional[dict] = None,
) -> List[Finding]:
    """Run the deterministic anomaly rules; optionally augment via LLM.

    :param entries: iterable of JournalEntrySummary records.
    :param approval_threshold: configured approval threshold (0.0
        disables the just-under-threshold rule).
    :param median_multiplier: factor over the period median to
        trigger round-outlier (default 3.0).
    :param provider_key: registered provider key. 'manual' (or
        any provider whose `is_manual` attribute is True) skips
        the LLM call and returns the deterministic findings only.
    :param provider_config: dict / JSON string handed to the
        provider factory. Ignored when provider is manual.
    :return: list of Finding records.
    """
    entries = list(entries)
    findings = []
    findings.extend(_round_outlier(entries, median_multiplier))
    findings.extend(_weekend_post(entries))
    if approval_threshold > 0:
        findings.extend(
            _just_under_threshold(entries, approval_threshold),
        )
    findings.extend(_reversal_pair(entries))
    # LLM augmentation: skipped when manual or when the provider is
    # not registered; falls back silently to the deterministic
    # output so the capability call never fails on missing config.
    if provider_key and provider_key != 'manual':
        try:
            if has_provider(provider_key):
                provider = get_provider(provider_key, provider_config)
                if not getattr(provider, 'is_manual', False):
                    # Real provider would augment here; the stubs
                    # raise ProviderError, which we swallow so the
                    # deterministic findings still flow back to the
                    # caller.
                    _augment_via_llm(provider, entries, findings)
        except ProviderError:
            pass
    return findings


def _round_outlier(entries, multiplier):
    if not entries:
        return []
    amounts = [e.amount for e in entries if e.amount > 0]
    if len(amounts) < 3:
        return []
    median = statistics.median(amounts) or 1.0
    out = []
    for e in entries:
        if e.amount <= 0:
            continue
        if e.amount % 1000 != 0:
            continue
        if e.amount < median * multiplier:
            continue
        out.append(Finding(
            move_id=e.id, severity=_SEVERITY_BY_RULE['round_outlier'],
            rule='round_outlier',
            description=(
                "Round-number entry of %.2f is more than %dx the "
                "period median of %.2f."
                % (e.amount, int(multiplier), median)
            ),
            amount=e.amount, date=e.date,
        ))
    return out


def _weekend_post(entries):
    out = []
    for e in entries:
        if e.date and e.date.weekday() >= 5:
            out.append(Finding(
                move_id=e.id,
                severity=_SEVERITY_BY_RULE['weekend_post'],
                rule='weekend_post',
                description=(
                    "Posted on %s (%s); review whether out-of-"
                    "hours posting is policy-compliant."
                    % (e.date, e.date.strftime('%A'))
                ),
                amount=e.amount, date=e.date,
            ))
    return out


def _just_under_threshold(entries, threshold):
    out = []
    band_low = threshold * 0.95
    for e in entries:
        if band_low <= e.amount < threshold:
            out.append(Finding(
                move_id=e.id,
                severity=_SEVERITY_BY_RULE['just_under_threshold'],
                rule='just_under_threshold',
                description=(
                    "Amount %.2f is within 5%% below the approval "
                    "threshold %.2f. Possible structuring to evade "
                    "approval; verify no related entries make up "
                    "a single split transaction."
                    % (e.amount, threshold)
                ),
                amount=e.amount, date=e.date,
            ))
    return out


def _reversal_pair(entries):
    """Flag debit + credit pairs on the same account by the same user
    in the same period for the same amount.

    The reversal-pair check is conservative on the summaries it sees:
    it considers entries that share account_codes, date (same day),
    posted_by, and amount. Real implementations inside Odoo would
    join through account.move.line; this version assumes the caller
    already groups summaries appropriately.
    """
    by_key = {}
    for e in entries:
        if not e.posted_by or not e.account_codes:
            continue
        key = (
            e.posted_by, tuple(sorted(e.account_codes)),
            e.date, round(e.amount, 2),
        )
        by_key.setdefault(key, []).append(e)
    out = []
    for entries_at_key in by_key.values():
        if len(entries_at_key) >= 2:
            for e in entries_at_key:
                out.append(Finding(
                    move_id=e.id,
                    severity=_SEVERITY_BY_RULE['reversal_pair'],
                    rule='reversal_pair',
                    description=(
                        "Same user posted %d entries at the same "
                        "amount on the same accounts on %s; review "
                        "whether they form a wash pair."
                        % (len(entries_at_key), e.date)
                    ),
                    amount=e.amount, date=e.date,
                ))
    return out


def _augment_via_llm(provider, entries, findings):
    """Call the provider with the deterministic findings so it can
    add prioritisation or detect patterns the rules miss.

    Stub providers raise ProviderError, which the caller swallows.
    Real provider implementations parse the response and append
    additional Finding records to the list in place.
    """
    if not entries:
        return
    # Run even when the deterministic rules found nothing: the whole
    # point of LLM augmentation is to catch patterns the rules miss.
    if findings:
        summary = "\n".join(
            "- %s on %s: %s (%s)" % (
                f.severity.upper(), f.date, f.description, f.rule,
            )
            for f in findings[:50]
        )
    else:
        summary = "No rule-based flags were raised."
    messages = [
        {
            'role': 'system',
            'content': (
                "You are an accounting auditor. Given a list of "
                "anomaly flags raised by a rule-based detector, "
                "comment on which are most likely false positives "
                "vs real issues, and add any pattern you spot that "
                "the rules missed. Be terse."
            ),
        },
        {
            'role': 'user',
            'content': (
                "Period summary follows. Findings:\n%s" % summary
            ),
        },
    ]
    # Real provider returns a JSON array of extra findings we parse and
    # append. Stubs raise ProviderError; the caller handles it. We only
    # accept findings that reference a move_id actually in this batch,
    # so a hallucinated entry cannot invent a finding against a move
    # that was never scanned. All LLM-sourced findings share the
    # 'llm_flag' rule so the live upsert stores at most one per move.
    raw = provider.chat(messages)
    items = extract_json_array(raw)
    if not items:
        return
    amount_by_id = {e.id: e.amount for e in entries}
    date_by_id = {e.id: e.date for e in entries}
    existing = {(f.move_id, f.rule) for f in findings}
    for item in items:
        if not isinstance(item, dict):
            continue
        move_id = item.get('move_id')
        if move_id not in amount_by_id:
            continue
        description = str(item.get('description') or '').strip()
        if not description:
            continue
        severity = item.get('severity')
        if severity not in ('low', 'medium', 'high'):
            severity = 'medium'
        if (move_id, 'llm_flag') in existing:
            continue
        existing.add((move_id, 'llm_flag'))
        findings.append(Finding(
            move_id=move_id, severity=severity, rule='llm_flag',
            description=description,
            amount=amount_by_id[move_id], date=date_by_id[move_id],
        ))
