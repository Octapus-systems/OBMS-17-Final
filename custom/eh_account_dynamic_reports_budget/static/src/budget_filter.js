/** @odoo-module **/

/**
 * Adds a Budget picker to the dynamic-report options panel for the P&L.
 *
 * Without it, the budget-vs-actual columns this module computes are
 * unreachable: the compute path only fires when options carries a
 * budget_id, and nothing in the base viewer sets it. This patch loads the
 * company's budgets and writes options.budget_id, so the feature the module
 * advertises is actually usable.
 */

import { patch } from "@web/core/utils/patch";
import { onWillStart } from "@odoo/owl";
import { EhDynamicReportViewer } from "@eh_account_dynamic_reports/components/dynamic_report/dynamic_report";

patch(EhDynamicReportViewer.prototype, {
    setup() {
        super.setup();
        this.state.budgetChoices = [];
        if (this.state.options && this.state.options.budget_id === undefined) {
            this.state.options.budget_id = false;
        }
        onWillStart(async () => {
            if (this.reportCode === "profit_and_loss") {
                this.state.budgetChoices = await this.orm.call(
                    "eh.budget.budget",
                    "eh_report_budget_choices",
                    [],
                );
            }
        });
    },

    onBudgetChange(ev) {
        const val = ev.target.value;
        this.state.options.budget_id = val ? parseInt(val, 10) : false;
        this.onRefresh();
    },
});
