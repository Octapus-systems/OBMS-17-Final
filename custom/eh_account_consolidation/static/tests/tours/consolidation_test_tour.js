/** @odoo-module **/

/**
 * Automated verification tour for the consolidation entity register
 * (IFRS 10/10 program, UI layer). Unlike onboarding tours under
 * static/src, this file ships in web.assets_tests only: it never loads
 * for real users and exists solely for HttpCase.start_tour runs in the
 * version matrix.
 *
 * KEEP THE STEP SHAPE MECHANICAL. tools/backport_account.py rewrites test
 * tours for the older series with a line-based transform, so every step
 * must stay in the exact  { trigger: "...", run: "..." }  form below:
 *   - selectors: double-quoted, one per step, prefer [data-menu-xmlid=...]
 *   - actions:   run: "click" | "edit <text>" | () => {}
 * The 16 target converts  registry -> tour.register  and  edit -> text;
 * 16/17 targets convert the /odoo url to /web.
 */

import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("eh_consolidation_test_tour", {
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
            trigger: "[data-menu-xmlid='account.menu_finance_reports']",
            run: "click",
        },
        {
            trigger: ".dropdown-item[data-menu-xmlid='eh_account_consolidation.menu_eh_consol_entity']",
            run: "click",
        },
        {
            trigger: ".o_list_button_add",
            run: "click",
        },
        {
            trigger: ".o_field_widget[name='name'] input",
            run: "text Tour Group Consolidated",
        },
        {
            trigger: ".o_field_widget[name='code'] input",
            run: "text tour_group",
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
