# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Live wiring of the budget-variance commentary onto eh.budget.budget.

The deterministic commentary is a non-stored computed field recomputed
on read so it always reflects the current line figures. The Odoo-
independent variance_commenter helper does the prose; this model only
builds the per-line snapshot and resolves the income flag from the
account type.

The optional LLM-enriched narrative is NEVER produced on read. A compute
must not perform synchronous network I/O: with a live provider configured
that would fire a blocking outbound HTTPS POST on every budget form/list
access and starve the HTTP workers. The provider is therefore invoked
only from the explicit action_eh_ai_refresh_commentary button, which
writes the result to a stored field.
"""

from odoo import api, fields, models

from odoo.addons.eh_account_ai_agent.tools import variance_commenter

# Mirrors the income classification used by the P&L report handler.
_INCOME_TYPES = ('income', 'income_other')


class EhBudget(models.Model):
    _inherit = 'eh.budget.budget'

    eh_ai_variance_commentary = fields.Text(
        string="Variance commentary",
        compute='_compute_eh_ai_variance_commentary', store=False,
    )
    eh_ai_variance_narrative = fields.Text(
        string="AI variance narrative",
        readonly=True, copy=False,
        help="Provider-enriched narrative produced by the explicit "
             "Refresh AI commentary button. Empty until the button is "
             "pressed; never generated on read so budget access never "
             "triggers a synchronous LLM call.",
    )

    @api.depends(
        'name', 'line_ids',
        'line_ids.budgeted_amount', 'line_ids.actual_amount',
        'line_ids.account_id',
    )
    def _compute_eh_ai_variance_commentary(self):
        # DETERMINISTIC ONLY. Force the manual provider so the template
        # renders on every read/list without any network I/O; the
        # richer provider narrative is produced only by the explicit
        # action_eh_ai_refresh_commentary button below.
        for budget in self:
            snapshot = budget._eh_build_budget_snapshot()
            budget.eh_ai_variance_commentary = variance_commenter.comment(
                snapshot,
                period_label=budget.name or '',
                provider_key='manual',
            )

    def _eh_build_budget_snapshot(self):
        """Map budget lines onto the helper's BudgetLineSnapshot records.

        is_income is resolved from the line account's account_type so
        the commentary describes an income shortfall and an expense
        overrun correctly rather than treating every variance the same.
        """
        self.ensure_one()
        snapshot = []
        for line in self.line_ids:
            account = line.account_id
            if not account:
                continue
            # Use the SIGN-NORMALISED actual, not the raw Odoo-signed balance.
            # actual_amount is a credit-negative SUM(balance); feeding it raw
            # made a fully-earned revenue line read as a ~200% miss
            # (-10000 - 10000). _normalised_actual() flips credit-natured
            # accounts so the figure reads in the same direction as the budget.
            snapshot.append(variance_commenter.BudgetLineSnapshot(
                label=account.display_name,
                budget=line.budgeted_amount or 0.0,
                actual=line._normalised_actual(),
                is_income=account.account_type in _INCOME_TYPES,
            ))
        return snapshot

    def action_eh_ai_refresh_commentary(self):
        """Explicit user gesture that runs the configured AI provider.

        This is the ONLY place the provider is invoked, because the
        provider may perform outbound network I/O (a blocking HTTPS
        POST). Running it here, on a deliberate button press, keeps that
        call off the read path: budget forms and lists render the
        deterministic compute field without ever touching the network.
        The provider-enriched text is written to the stored
        eh_ai_variance_narrative field; variance_commenter.comment falls
        back to the deterministic template on any provider error, so the
        field is always populated with something useful.
        """
        for budget in self:
            snapshot = budget._eh_build_budget_snapshot()
            company = budget.company_id
            budget.eh_ai_variance_narrative = variance_commenter.comment(
                snapshot,
                period_label=budget.name or '',
                provider_key=company.eh_ai_provider_key or 'manual',
                provider_config=company.sudo().eh_ai_provider_config or None,
            )
        return True
