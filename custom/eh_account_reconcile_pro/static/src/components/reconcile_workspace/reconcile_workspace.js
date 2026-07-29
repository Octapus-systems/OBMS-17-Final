/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================
//
// Bank reconciliation workspace.
//
// A two pane OWL component that drives the entire reconciliation flow:
//   * Top: journal picker + session counters + Close session button.
//   * Left pane: list of unreconciled statement lines for the selected
//     journal. Click to select.
//   * Right pane: scored suggestions for the selected line, plus action
//     buttons (Match, Skip, Write off).
//
// All state is local to the component; backend calls go through the
// session model's load_workspace, get_suggestions_for_line, apply_match,
// apply_skip methods. After every decision the workspace re-fetches the
// list so the just-matched line drops out and counters update.

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { session } from "@web/session";
const user = { context: (session && session.user_context) || {}, userId: session && session.uid };

function fmtMoney(value) {
    if (value === null || value === undefined || value === "") return "";
    if (typeof value !== "number") return String(value);
    const abs = Math.abs(value).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    });
    return value < 0 ? "(" + abs + ")" : abs;
}

function fmtPercent(value) {
    if (typeof value !== "number") return "";
    return Math.round(value * 100) + "%";
}

export class EhReconcileWorkspace extends Component {
    static template = "eh_account_reconcile_pro.ReconcileWorkspace";
    static props = { "*": true };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        // Odoo 19 removed the "user" service.
        this.user = user;

        this.state = useState({
            loading: true,
            error: null,
            journals: [],
            selectedJournalId: null,
            session: null,
            statementLines: [],
            selectedLineId: null,
            suggestions: [],
            suggestionsLoading: false,
            // aml ids ticked for a bulk apply, and the drop-target highlight
            // flag for drag and drop.
            selectedAmlIds: [],
            dragActive: false,
        });

        onWillStart(async () => await this.bootstrap());
    }

    async bootstrap() {
        this.state.loading = true;
        try {
            const journals = await this.orm.searchRead(
                "account.journal",
                [["type", "in", ["bank", "cash"]]],
                ["id", "name", "code", "type"],
                { order: "sequence asc, name asc" },
            );
            this.state.journals = journals;
            if (journals.length === 1) {
                await this.selectJournal(journals[0].id);
            } else {
                this.state.loading = false;
            }
        } catch (err) {
            this.state.error = (err && err.message) || String(err);
            this.state.loading = false;
        }
    }

    async selectJournal(journalId) {
        this.state.loading = true;
        this.state.error = null;
        this.state.selectedJournalId = journalId;
        this.state.selectedLineId = null;
        this.state.suggestions = [];
        this.state.selectedAmlIds = [];
        this.state.dragActive = false;
        try {
            const data = await this.orm.call(
                "eh.reconciliation.session",
                "load_workspace",
                [journalId],
            );
            this.state.session = data.session;
            this.state.statementLines = data.statement_lines;
        } catch (err) {
            this.state.error = (err && err.message) || String(err);
        } finally {
            this.state.loading = false;
        }
    }

    async selectLine(lineId) {
        if (this.state.selectedLineId === lineId) {
            return;
        }
        this.state.selectedLineId = lineId;
        this.state.suggestionsLoading = true;
        this.state.suggestions = [];
        // A bulk selection only makes sense for one statement line; reset
        // it whenever the user moves to a different line.
        this.state.selectedAmlIds = [];
        try {
            const suggestions = await this.orm.call(
                "eh.reconciliation.session",
                "get_suggestions_for_line",
                [lineId],
            );
            this.state.suggestions = suggestions;
        } catch (err) {
            this.notification.add(
                (err && err.message) || String(err),
                { type: "danger" },
            );
        } finally {
            this.state.suggestionsLoading = false;
        }
    }

    async match(amlId, source) {
        if (!this.state.selectedLineId || !this.state.session) {
            return;
        }
        const sessionId = this.state.session.id;
        try {
            await this.orm.call(
                "eh.reconciliation.session",
                "apply_match",
                [[sessionId], this.state.selectedLineId, [amlId], source || "manual"],
            );
            this.notification.add(
                "Matched (source: " + (source || "manual") + ")",
                { type: "success" },
            );
            await this.refreshAfterDecision();
        } catch (err) {
            this.notification.add(
                (err && err.message) || String(err),
                { type: "danger" },
            );
        }
    }

    // ---- bulk selection ----

    isAmlSelected(amlId) {
        return this.state.selectedAmlIds.includes(amlId);
    }

    selectedAmlCount() {
        return this.state.selectedAmlIds.length;
    }

    toggleAmlSelection(amlId) {
        // Reassign a fresh array so the OWL reactivity proxy observes the
        // change; mutating in place with push/splice is not reliably seen.
        if (this.state.selectedAmlIds.includes(amlId)) {
            this.state.selectedAmlIds = this.state.selectedAmlIds.filter(
                (id) => id !== amlId,
            );
        } else {
            this.state.selectedAmlIds = [...this.state.selectedAmlIds, amlId];
        }
    }

    async matchSelected() {
        // Bulk match: reconcile the selected statement line against every
        // ticked candidate in ONE apply_match call (source "bulk"), instead
        // of one RPC per candidate.
        if (!this.state.selectedLineId || !this.state.session) {
            return;
        }
        const amlIds = this.state.selectedAmlIds;
        if (!amlIds.length) {
            return;
        }
        const sessionId = this.state.session.id;
        try {
            await this.orm.call(
                "eh.reconciliation.session",
                "apply_match",
                [[sessionId], this.state.selectedLineId, amlIds, "bulk"],
            );
            this.notification.add(
                "Matched " + amlIds.length + " item(s) (source: bulk)",
                { type: "success" },
            );
            this.state.selectedAmlIds = [];
            await this.refreshAfterDecision();
        } catch (err) {
            this.notification.add(
                (err && err.message) || String(err),
                { type: "danger" },
            );
        }
    }

    // ---- drag and drop ----

    onSuggestionDragStart(event, amlId) {
        // Carry the candidate AML id in the drag payload. text/plain has the
        // widest browser support.
        event.dataTransfer.setData("text/plain", String(amlId));
        event.dataTransfer.effectAllowed = "move";
    }

    onDropZoneDragOver(event) {
        // preventDefault is required for the element to accept a drop.
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        if (!this.state.dragActive) {
            this.state.dragActive = true;
        }
    }

    onDropZoneDragLeave() {
        this.state.dragActive = false;
    }

    onDropZoneDrop(event) {
        event.preventDefault();
        this.state.dragActive = false;
        const amlId = parseInt(event.dataTransfer.getData("text/plain"), 10);
        if (!isNaN(amlId)) {
            this.match(amlId, "drag_drop");
        }
    }

    async skip() {
        if (!this.state.selectedLineId || !this.state.session) {
            return;
        }
        const sessionId = this.state.session.id;
        try {
            await this.orm.call(
                "eh.reconciliation.session",
                "apply_skip",
                [[sessionId], this.state.selectedLineId],
            );
            this.notification.add("Skipped for now", { type: "info" });
            await this.refreshAfterDecision();
        } catch (err) {
            this.notification.add(
                (err && err.message) || String(err),
                { type: "danger" },
            );
        }
    }

    async closeSession() {
        if (!this.state.session) return;
        const sessionId = this.state.session.id;
        try {
            await this.orm.call(
                "eh.reconciliation.session",
                "action_close",
                [[sessionId]],
            );
            this.notification.add("Session closed", { type: "success" });
            this.state.session = null;
            this.state.statementLines = [];
            this.state.selectedLineId = null;
            this.state.suggestions = [];
            this.state.selectedJournalId = null;
            await this.bootstrap();
        } catch (err) {
            this.notification.add(
                (err && err.message) || String(err),
                { type: "danger" },
            );
        }
    }

    async refreshAfterDecision() {
        if (this.state.selectedJournalId) {
            await this.selectJournal(this.state.selectedJournalId);
        }
    }

    onJournalChange(event) {
        const id = parseInt(event.target.value, 10);
        if (!isNaN(id)) {
            this.selectJournal(id);
        }
    }

    // ---- presentation helpers ----

    fmtMoney(v) { return fmtMoney(v); }
    fmtPercent(v) { return fmtPercent(v); }

    selectedLine() {
        const id = this.state.selectedLineId;
        if (!id) return null;
        return this.state.statementLines.find((sl) => sl.id === id) || null;
    }

    rulesFiredText(rules) {
        return Array.isArray(rules) ? rules.join(", ") : "";
    }

    confidenceColor(score) {
        if (score >= 0.85) return "eh_rec_conf_high";
        if (score >= 0.6) return "eh_rec_conf_mid";
        return "eh_rec_conf_low";
    }
}

registry.category("actions").add(
    "eh_reconcile_workspace", EhReconcileWorkspace,
);
