# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Budget vs actual variance commentary.

Default implementation produces a structured commentary block from
the variance data using a deterministic template:

  "Total <category> overran budget by <pct>% (<amount> over). The
  three biggest variances are <line1> (+<X>%), <line2> (+<Y>%),
  <line3> (+<Z>%)."

The LLM hook runs after the template comment and asks the provider
for a more natural-language paragraph that incorporates trend and
context. Stub providers raise ProviderError; the deterministic
template is returned regardless so the capability call always
yields useful output.
"""

import dataclasses
from typing import Iterable, List, Optional  # noqa: F401

from .provider_registry import (
    get_provider, has_provider, ProviderError,
)


@dataclasses.dataclass
class BudgetLineSnapshot:
    """One line in a budget vs actual snapshot.

    Variance is computed as actual minus budget; a positive variance
    is an overrun for expense lines and a favourable result for
    income lines. The commentary respects the line's `is_income`
    flag when describing direction.
    """
    label: str
    budget: float
    actual: float
    is_income: bool = False

    @property
    def variance(self) -> float:
        return self.actual - self.budget

    @property
    def variance_pct(self) -> float:
        if not self.budget:
            return 0.0 if not self.actual else 100.0
        return (self.variance / self.budget) * 100.0


def comment(
    snapshot: Iterable[BudgetLineSnapshot],
    period_label: str = '',
    top_n: int = 3,
    provider_key: str = 'manual',
    provider_config: Optional[dict] = None,
) -> str:
    """Return a prose variance commentary string.

    :param snapshot: iterable of BudgetLineSnapshot.
    :param period_label: optional human label ('Q3 2026', 'May 2026').
    :param top_n: number of largest variances to highlight (default 3).
    :param provider_key: registered provider key. Manual or missing
        returns the deterministic template only; non-manual providers
        get a chance to produce a richer narrative.
    :return: commentary string. Always non-empty when there is at
        least one budgeted line; returns a placeholder when the
        snapshot is empty.
    """
    snap = [s for s in snapshot if s.budget or s.actual]
    if not snap:
        return _empty_period_comment(period_label)
    deterministic = _build_template_comment(snap, period_label, top_n)
    if provider_key and provider_key != 'manual':
        try:
            if has_provider(provider_key):
                provider = get_provider(provider_key, provider_config)
                if not getattr(provider, 'is_manual', False):
                    enriched = _augment_via_llm(
                        provider, snap, period_label, deterministic,
                    )
                    if enriched:
                        return enriched
        except ProviderError:
            pass
    return deterministic


def _build_template_comment(snap, period_label, top_n):
    total_budget = sum(s.budget for s in snap)
    total_actual = sum(s.actual for s in snap)
    total_variance = total_actual - total_budget
    total_pct = (
        (total_variance / total_budget * 100.0)
        if total_budget else 0.0
    )
    direction = (
        'overran budget' if total_variance > 0
        else 'underran budget' if total_variance < 0
        else 'matched budget'
    )
    period = (' for %s' % period_label) if period_label else ''
    headline = (
        "Period total%s %s by %.1f%% (%s%.2f vs %.2f)."
        % (
            period, direction, abs(total_pct),
            '+' if total_variance > 0 else '',
            total_actual, total_budget,
        )
    )
    # Largest absolute variances.
    by_abs = sorted(snap, key=lambda s: abs(s.variance), reverse=True)
    top = by_abs[:max(0, top_n)]
    if top:
        bullets = "\n".join(
            "  - %s: %s%.2f vs %.2f (%s%.1f%%)" % (
                s.label,
                '+' if s.variance > 0 else '',
                s.actual, s.budget,
                '+' if s.variance > 0 else '',
                s.variance_pct,
            )
            for s in top
        )
        body = "Largest variances:\n%s" % bullets
    else:
        body = ''
    if body:
        return "%s\n\n%s" % (headline, body)
    return headline


def _empty_period_comment(period_label):
    period = (' for %s' % period_label) if period_label else ''
    return "No budgeted or actual amounts%s; nothing to comment on." % period


def _augment_via_llm(provider, snap, period_label, deterministic):
    """Call the provider with the deterministic comment as context.

    Real provider implementations parse the chat() return into a
    polished narrative paragraph and return it. Stub providers raise
    ProviderError, which the caller catches; the deterministic
    template is returned instead.
    """
    summary_lines = "\n".join(
        "%s\tbudget=%.2f\tactual=%.2f\tvar=%.2f (%.1f%%)" % (
            s.label, s.budget, s.actual, s.variance, s.variance_pct,
        )
        for s in snap[:50]
    )
    messages = [
        {
            'role': 'system',
            'content': (
                "You are a financial controller. Given a budget vs "
                "actual snapshot, produce a one-paragraph "
                "executive commentary that explains the variance "
                "story in plain English. No bullet points; no "
                "markdown."
            ),
        },
        {
            'role': 'user',
            'content': (
                "Period: %s\nDeterministic commentary:\n%s\n\n"
                "Raw snapshot:\n%s"
            ) % (period_label or 'unspecified', deterministic, summary_lines),
        },
    ]
    return provider.chat(messages)
