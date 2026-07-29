# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Live wiring of the deterministic collections next-action engine onto
eh.collections.case.

The suggestion is a non-stored computed field: it is recomputed on
every read so it always reflects the current case state (days overdue,
promises, contact history) without a stale stored value or a refresh
cron. The heavy lifting lives in the Odoo-independent
next_action_suggester helper; this model only builds the snapshot and
surfaces the result.

The optional LLM-enriched narrative is NEVER produced on read. A compute
must not perform synchronous network I/O: with a live provider configured
that would fire a blocking outbound HTTPS POST on every collections case
form/list access and starve the HTTP workers. The provider is therefore
invoked only from the explicit action_eh_ai_refresh_suggestion button,
which writes the result to a stored field.
"""

from odoo import api, fields, models

from odoo.addons.eh_account_ai_agent.tools import next_action_suggester


class EhCollectionsCase(models.Model):
    _inherit = 'eh.collections.case'

    eh_ai_suggested_action = fields.Char(
        string="AI suggested action",
        compute='_compute_eh_ai_suggestion', store=False,
    )
    eh_ai_suggested_priority = fields.Selection(
        [('low', "Low"), ('medium', "Medium"), ('high', "High")],
        string="AI priority",
        compute='_compute_eh_ai_suggestion', store=False,
    )
    eh_ai_suggestion_rationale = fields.Text(
        string="AI rationale",
        compute='_compute_eh_ai_suggestion', store=False,
    )
    eh_ai_suggestion_narrative = fields.Text(
        string="AI suggestion narrative",
        readonly=True, copy=False,
        help="Provider-enriched next-action narrative produced by the "
             "explicit Refresh AI suggestion button. Empty until the "
             "button is pressed; never generated on read so opening a "
             "collections case never triggers a synchronous LLM call.",
    )

    @api.depends(
        'days_overdue_max', 'total_overdue_amount',
        'has_active_promise', 'broken_promise',
        'action_ids', 'action_ids.contact_made',
        'action_ids.action_type', 'action_ids.action_date',
        'is_resolved',
    )
    def _compute_eh_ai_suggestion(self):
        # DETERMINISTIC ONLY. Force the manual provider so the dunning
        # ladder renders on every read/list without any network I/O; the
        # richer provider narrative is produced only by the explicit
        # action_eh_ai_refresh_suggestion button below. Passing the live
        # company provider here would fire a blocking outbound HTTPS POST
        # on every case form/list access and starve the HTTP workers.
        for case in self:
            snap = case._eh_build_case_snapshot()
            suggestion = next_action_suggester.suggest(
                snap, provider_key='manual',
            )
            case.eh_ai_suggested_action = suggestion.action
            case.eh_ai_suggested_priority = suggestion.priority
            case.eh_ai_suggestion_rationale = suggestion.rationale

    def _eh_build_case_snapshot(self):
        """Map the case onto the helper's CaseSnapshot transport record."""
        self.ensure_one()
        today = fields.Date.context_today(self)
        contacts = self.action_ids.filtered(lambda a: a.contact_made)
        last_action = self.action_ids.sorted(
            key=lambda a: a.action_date or fields.Datetime.now(),
            reverse=True,
        )[:1]
        last_days = 0
        if last_action and last_action.action_date:
            last_days = (today - last_action.action_date.date()).days
        return next_action_suggester.CaseSnapshot(
            days_overdue=self.days_overdue_max or 0,
            total_overdue=self.total_overdue_amount or 0.0,
            contact_count=len(contacts),
            has_active_promise=bool(self.has_active_promise),
            has_broken_promise=bool(self.broken_promise),
            last_action_type=(last_action.action_type or '') if last_action else '',
            last_action_days_ago=last_days,
            has_demand_letter=any(
                a.action_type == 'letter' for a in self.action_ids
            ),
            has_payment_plan=False,
            customer_segment='',
        )

    def action_eh_ai_refresh_suggestion(self):
        """Explicit user gesture that runs the configured AI provider.

        This is the ONLY place the provider is invoked, because the
        provider may perform outbound network I/O (a blocking HTTPS
        POST). Running it here, on a deliberate button press, keeps that
        call off the read path: collections case forms and lists render
        the deterministic compute fields without ever touching the
        network. The provider-enriched result is written to the stored
        eh_ai_suggestion_narrative field; next_action_suggester.suggest
        falls back to the deterministic suggestion on any provider error,
        so the field is always populated with something useful.
        """
        for case in self:
            snap = case._eh_build_case_snapshot()
            company = case.company_id
            suggestion = next_action_suggester.suggest(
                snap,
                provider_key=company.eh_ai_provider_key or 'manual',
                provider_config=company.sudo().eh_ai_provider_config or None,
            )
            case.eh_ai_suggestion_narrative = "%s (%s): %s" % (
                suggestion.action, suggestion.priority, suggestion.rationale,
            )
        return True
