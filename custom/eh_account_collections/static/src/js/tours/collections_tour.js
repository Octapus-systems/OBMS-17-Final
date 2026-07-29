/** @odoo-module **/

/**
 * Onboarding tour for the ERP Heritage Collections Workbench.
 *
 * 5-step walkthrough covering the operations that distinguish this
 * module from a plain follow-up list:
 *
 *   1. Open the Collections kanban — cases grouped by stage.
 *   2. Explain that cases auto-create from overdue AR via the
 *      nightly cron; the operator usually doesn't create cases by
 *      hand.
 *   3. Show the action history on a case form (every email sent,
 *      every promise-to-pay, every late-fee invoice issued).
 *   4. Show the Refresh button + the daily aging cron.
 *   5. Point at the "Late fee" wizard which issues a real invoice
 *      rather than a memo entry.
 *
 * Trigger: ?tour=eh_collections_setup_tour or via Tutorials menu.
 */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { markup } from "@odoo/owl";

registry.category("web_tour.tours").add("eh_collections_setup_tour", {
    url: "/odoo",
    steps: () => [
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "Welcome to <b>Collections Workbench</b>. " +
                "This walkthrough shows what the workbench does " +
                "differently from a follow-up list, and how to " +
                "use it day-to-day."
            )),
            run: () => {},
        },
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "Cases <b>auto-create</b> from overdue customer " +
                "AR via the nightly cron. You almost never create " +
                "a case manually — the system opens one when a " +
                "partner crosses the configured grace days, and " +
                "closes it when the AR clears."
            )),
            run: () => {},
        },
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "On each case, the <b>Action History</b> log is the " +
                "audit trail: every email sent, every phone-call " +
                "logged, every promise-to-pay captured, every " +
                "late-fee invoice issued. Filter the case list by " +
                "Last Action to find stale follow-ups."
            )),
            run: () => {},
        },
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "Click <b>Late Fee</b> on any case to issue a " +
                "customer invoice for the configured fee — flat or " +
                "percentage of the overdue. The wizard posts a " +
                "real invoice, not a memo entry, so your AR " +
                "balances stay correct."
            )),
            run: () => {},
        },
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "The Configuration menu has <b>Stages</b> and " +
                "<b>Follow-up Levels</b>. Stages drive the kanban " +
                "columns; follow-up levels schedule recurring " +
                "reminder emails based on days overdue."
            )),
            run: () => {},
        },
    ],
});
