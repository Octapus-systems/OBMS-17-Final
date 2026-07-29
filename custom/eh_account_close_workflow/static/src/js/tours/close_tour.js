/** @odoo-module **/

/**
 * Onboarding tour for the ERP Heritage Period Close Workflow.
 *
 * 5-step walkthrough explaining the four lifecycle stages, the
 * task-template-driven seeding, and the post-close reopen path.
 *
 * Trigger: ?tour=eh_close_setup_tour or via Tutorials menu.
 */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { markup } from "@odoo/owl";

registry.category("web_tour.tours").add("eh_close_setup_tour", {
    url: "/odoo",
    steps: () => [
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "Welcome to <b>Period Close Workflow</b>. " +
                "This walkthrough shows the lifecycle and what to " +
                "configure before your first close."
            )),
            run: () => {},
        },
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "Configure <b>Task Templates</b> first. Each " +
                "template seeds a checklist task on every new run: " +
                "reconcile bank, post depreciation, run revaluation, " +
                "review aged AR, etc. Set the responsible role on " +
                "each so the right person gets assigned at run-time."
            )),
            run: () => {},
        },
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "Each <b>run</b> walks four stages: Open → In " +
                "Progress → Pending Approval → Closed. The kanban " +
                "view shows progress per run and flags blocked " +
                "tasks in red. A run can be reopened from Closed " +
                "if a late entry needs to land in the period."
            )),
            run: () => {},
        },
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "On a run, the <b>tasks tab</b> is your checklist. " +
                "Each task carries an assignee, due date, and a " +
                "Done/Skipped/Blocked status. Pending Approval " +
                "blocks until every required task is complete."
            )),
            run: () => {},
        },
        {
            trigger: ".o_main_navbar",
            content: markup(_t(
                "Once Closed, the run is locked but the period " +
                "isn't. Use <b>Reopen</b> to roll back to In " +
                "Progress when a missed entry needs to land. " +
                "Reopens are stamped with user + reason in " +
                "chatter for audit."
            )),
            run: () => {},
        },
    ],
});
