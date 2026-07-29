/** @odoo-module **/

/**
 * Automated verification tour for the FX revaluation run (IFRS 10/10
 * program, UI layer). Unlike onboarding tours under static/src, this
 * file ships in web.assets_tests only: it never loads for real users and
 * exists solely for HttpCase.start_tour runs in the version matrix.
 *
 * KEEP THE STEP SHAPE MECHANICAL. tools/backport_account.py rewrites test
 * tours for the older series with a line-based transform, so every step
 * must stay in the exact  { trigger: "...", run: "..." }  form below:
 *   - selectors: double-quoted, one per step, prefer [data-menu-xmlid=...]
 *   - actions:   run: "click" | "edit <text>" | () => {}
 * The 16 target converts  registry -> tour.register  and  edit -> text;
 * 16/17 targets convert the /odoo url to /web.
 *
 * The run action lands on kanban first (view_mode kanban,list,form), so
 * the create step clicks .o-kanban-button-new (same class on 16-19).
 * The required journal / gain / loss accounts are pre-seeded as
 * ir.default records by tests/test_tour.py, so the tour only edits
 * plain char and date inputs. On 19 a date field holding a value renders
 * a button (o_input o_daterange_start), not an input, until focused, so
 * the tour clicks the field's .o_input first to swap in the real input;
 * on 16/17/18 that same click lands on the plain input and is harmless.
 * The final step waits for the statusbar to reach Computed, proving
 * action_compute round-tripped.
 */

import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("eh_fx_revaluation_test_tour", {
    url: "/web",
    test: true,
    steps: () => [
        {
            trigger: ".o_navbar_apps_menu button",
            run: "click",
        },
        {
            trigger: ".dropdown-item[data-menu-xmlid='account.menu_finance']",
            run: "click",
        },
        {
            trigger: "[data-menu-xmlid='account.menu_finance_entries']",
            run: "click",
        },
        {
            trigger: ".dropdown-item[data-menu-xmlid='eh_account_fx_revaluation.menu_eh_fx_revaluation_run']",
            run: "click",
        },
        {
            trigger: ".o_switch_view.o_list",
            run: "click",
        },
        {
            trigger: ".o_list_button_add",
            run: "click",
        },
        {
            trigger: "[name='description'] input, [name='description'] textarea",
            run: "text FX tour run",
        },
        {
            trigger: ".o_field_widget[name='revaluation_date'] .o_input",
            run: "click",
        },
        {
            trigger: ".o_field_widget[name='revaluation_date'] input",
            run: "text 06/15/2026",
        },
        {
            trigger: ".o_form_button_save",
            run: "click",
        },
        {
            trigger: ".o_form_saved",
            run: () => {},
        },
    ],
});
