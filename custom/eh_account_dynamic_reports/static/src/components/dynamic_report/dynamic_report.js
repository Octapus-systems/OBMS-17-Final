/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================
//
// Dynamic Report viewer.
//
// A client action that renders any registered eh.account.dynamic.report
// interactively in the Odoo backend. The action context provides the
// report_code; the component fetches the report record, calls render() to
// get the JSON payload, and lays it out as a hierarchical table with a
// comprehensive filter pane above.
//
// Filters: date mode (range, as-of, this/last month/quarter/year),
// comparison toggle (none, previous_period, previous_year), companies,
// journals, partners, accounts, account types, analytic plans and
// analytic accounts. Posted-only and show-zero stay as quick checkboxes.
//
// Currency: every payload carries a currency block resolved server side
// from the company scope. The viewer renders amounts with the right
// symbol, decimal places, position. Multi-currency scopes mark the
// payload as such and the cells render numbers without a symbol.
//
// Hierarchy: lines flagged unfoldable can be expanded / collapsed; the
// component tracks the expanded set in state so re-renders preserve it.

import { Component, onWillStart, onMounted, useState, useRef } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { session } from "@web/session";
const user = { context: (session && session.user_context) || {}, userId: session && session.uid };
import {
    todayStr,
    firstOfMonthStr,
    PRESET_RANGES,
    ACCOUNT_TYPE_CHOICES,
    formatCurrency,
} from "./report_format";
import { EhCellEllipsis } from "./cell_ellipsis";
import {
    ROW_HEIGHT,
    OVERSCAN,
    DEFAULT_VIEWPORT_PX,
    computeFilterKeepSet,
    sliceWindow,
    variantCellRole,
} from "./report_table_logic";

// ---- virtual-scroll constants (WS5) ----
// ROW_HEIGHT / OVERSCAN / DEFAULT_VIEWPORT_PX are imported from the pure
// logic module (which the hoot suite tests in isolation). The .scss clamps
// every rendered row to ROW_HEIGHT so floor(scrollTop / ROW_HEIGHT) is exact
// and the window stays O(viewport) regardless of payload size.
//
// In-table search debounce. Client-side only (never hits the server), so it
// stays instant on a multi-year ledger; the debounce just avoids rebuilding
// the filtered set on every keystroke.
const SEARCH_DEBOUNCE_MS = 150;

// Virtual-scroll engages only when the visible row count exceeds this. Below
// it, the table renders as one plain <tbody> with no spacer rows, so the
// sticky <thead> pins reliably (spacer-row windowing was detaching the header
// into the body mid-scroll). The lazy engine keeps real reports well under
// this, so windowing is reserved for an exceptionally large expanded view.
const VIRTUAL_THRESHOLD = 4000;

export class EhDynamicReportViewer extends Component {
    static template = "eh_account_dynamic_reports.DynamicReportViewer";
    static components = { EhCellEllipsis };
    static props = { "*": true };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.dialog = useService("dialog");
        this.user = user;
        // The scroll container (.eh_dr_body). Its scrollTop / clientHeight
        // drive windowedLines(); we read them in onBodyScroll and on mount.
        this.bodyRef = useRef("body");
        // Fixed row height + overscan exposed to the template for spacer math.
        this.ROW_HEIGHT = ROW_HEIGHT;

        const ctx = (this.props.action && this.props.action.context) || {};
        this.reportCode = ctx.report_code;

        this.state = useState({
            loading: true,
            error: null,
            reportId: null,
            reportName: "",
            payload: null,
            filtersExpanded: false,
            expandedLines: [],
            // Lazy expand: per-line fetched children, keyed by line id.
            // { [lineId]: { lines: [...], hasMore, nextOffset, loading,
            //   totalCount } }. Cleared on every refresh so a new payload
            // never shows stale children, and never persisted (lazy leaves
            // always start collapsed on reload, per the §2 invariant).
            childLines: {},
            savedViews: [],
            currentSavedViewId: null,
            choices: {
                companies: [],
                journals: [],
                partners: [],
                accounts: [],
                accountTypes: ACCOUNT_TYPE_CHOICES,
                analyticPlans: [],
                analyticAccounts: [],
                currencies: [],
            },
            options: {
                date: {
                    mode: "range",
                    date_from: firstOfMonthStr(),
                    date_to: todayStr(),
                },
                company_ids: [],
                journal_ids: [],
                partner_ids: [],
                account_ids: [],
                account_type_ids: [],
                analytic_account_ids: [],
                analytic_plan_ids: [],
                posted_only: true,
                show_zero: false,
                comparison: "none",
                presentation_currency_id: null,
                // Aged-report config (interval/bucket count/basis) and the
                // reconcile-state filter. Only the aged handlers read these;
                // other reports ignore them. They are part of the options
                // hash so a different bucket grid / reconcile set caches and
                // re-serves separately.
                aging_interval: 30,
                aging_bucket_count: 4,
                aging_basis: "maturity",
                reconcile_state: "open",
                // Opt into the server's lazy expand-on-demand path. Account
                // leaves arrive collapsed; their journal items are fetched
                // only when expanded. Export/PDF render eagerly server-side
                // and ignore this flag, so paper output is unchanged.
                lazy_expand: true,
                // WS3 ghost-feature options. All four pass straight through
                // render()->compute() and only change behaviour when set, so
                // the default values reproduce today's single-period,
                // standard-layout, direct-coarse-cash-flow output exactly.
                // comparison_number>1 widens to N side-by-side periods;
                // horizontal_group_by='company' pivots to per-company columns;
                // cash_flow_method/cash_flow_reconciled select the cash-flow
                // attribution. They are part of the options hash so each
                // variant caches and re-serves separately.
                comparison_number: 1,
                horizontal_group_by: null,
                cash_flow_method: "direct",
                cash_flow_reconciled: false,
                // IAS 7.31 presentation overrides. Empty string means
                // "follow the company policy" - the server resolves the
                // eh_cf_*_section fields on res.company, so the default
                // render is policy-true without any option noise.
                cf_interest_paid_section: "",
                cf_dividends_paid_section: "",
            },
            // Annotation popover state: which line/cell currently has its
            // note popover open ({lineId,label}) and the in-flight draft.
            // null = closed. Kept out of options so it never affects the
            // render hash or a saved view.
            annotationOpen: null,
            annotationDraft: "",
            // ---- WS5 table craft ----
            // In-table search. Pure client-side filter over the already
            // loaded payload (and any spliced lazy children); never sent to
            // the server, so it never re-queries the ledger and never enters
            // the options hash. Empty string = no filter.
            tableQuery: "",
            // Virtual-scroll viewport tracking. scrollTop is the live scroll
            // offset of .eh_dr_body; viewportPx is its measured clientHeight.
            // Both feed windowedLines(); kept in state so a scroll re-renders
            // only the visible window. Defaults are pre-measure fallbacks.
            scrollTop: 0,
            viewportPx: DEFAULT_VIEWPORT_PX,
        });

        onWillStart(async () => {
            await this.bootstrap();
        });

        onMounted(() => {
            // Measure the real viewport once the scroll container exists so
            // the very first windowed slice is sized to the actual screen
            // rather than the pre-measure fallback. Guarded: if the ref is
            // missing we keep the fallback, never throwing.
            this._measureViewport();
        });
    }

    _measureViewport() {
        const el = this.bodyRef && this.bodyRef.el;
        if (el && el.clientHeight) {
            this.state.viewportPx = el.clientHeight;
        }
    }

    onBodyScroll(ev) {
        // Live scroll offset drives the window. Reading scrollTop off the
        // event target keeps it cheap; re-measuring clientHeight here too
        // covers a resize that happened since mount. Defensive: a missing
        // target leaves the prior offset untouched.
        const el = (ev && ev.target) || (this.bodyRef && this.bodyRef.el);
        if (!el) return;
        this.state.scrollTop = el.scrollTop || 0;
        if (el.clientHeight) {
            this.state.viewportPx = el.clientHeight;
        }
    }

    async bootstrap() {
        if (!this.reportCode) {
            this.state.loading = false;
            this.state.error = "No report_code in action context.";
            return;
        }
        const allowed = (this.user && this.user.context
            && this.user.context.allowed_company_ids) || [];
        if (allowed.length) {
            this.state.options.company_ids = allowed.slice();
        }
        const records = await this.orm.searchRead(
            "eh.account.dynamic.report",
            [["code", "=", this.reportCode]],
            ["id", "name"],
            { limit: 1 },
        );
        if (!records.length) {
            this.state.loading = false;
            this.state.error =
                "No registered report with code: " + this.reportCode;
            return;
        }
        this.state.reportId = records[0].id;
        this.state.reportName = records[0].name;
        await this.loadFilterChoices();
        await this.loadSavedViews();
        await this.refresh();
    }

    async loadSavedViews() {
        try {
            const views = await this.orm.call(
                "eh.account.report.saved_view", "list_for",
                [this.reportCode],
            );
            this.state.savedViews = views || [];
        } catch (e) {
            this.state.savedViews = [];
        }
    }

    async onSavedViewChange(event) {
        const viewId = parseInt(event.target.value, 10);
        if (!viewId) {
            this.state.currentSavedViewId = null;
            return;
        }
        try {
            const opts = await this.orm.call(
                "eh.account.report.saved_view", "load",
                [[viewId]],
            );
            if (opts) {
                // Merge: keep keys we know about, replace with the loaded
                // values; unknown keys in the saved view are dropped.
                const cur = this.state.options;
                for (const key of Object.keys(cur)) {
                    if (key in opts) {
                        cur[key] = opts[key];
                    }
                }
                this.state.currentSavedViewId = viewId;
                this.refresh();
            }
        } catch (e) {
            this.notification.add(
                "Failed to load saved view: "
                + ((e && e.message) || String(e)),
                { type: "danger" },
            );
        }
    }

    async onSaveCurrentView() {
        const name = window.prompt(
            "Save current filters as a view name:",
            "My filters",
        );
        if (!name) return;
        const shared = window.confirm(
            "Share with everyone in this company? Cancel for personal.",
        );
        try {
            const newId = await this.orm.call(
                "eh.account.report.saved_view", "save_view",
                [name, this.reportCode, this.state.options, shared],
            );
            this.state.currentSavedViewId = newId;
            this.notification.add(
                "Saved view: " + name, { type: "success" },
            );
            await this.loadSavedViews();
        } catch (e) {
            this.notification.add(
                (e && e.message) || String(e), { type: "danger" },
            );
        }
    }

    async onDeleteSavedView() {
        if (!this.state.currentSavedViewId) return;
        if (!window.confirm("Delete this saved view?")) return;
        try {
            await this.orm.unlink(
                "eh.account.report.saved_view",
                [this.state.currentSavedViewId],
            );
            this.state.currentSavedViewId = null;
            await this.loadSavedViews();
        } catch (e) {
            this.notification.add(
                (e && e.message) || String(e), { type: "danger" },
            );
        }
    }

    async loadFilterChoices() {
        // Each filter feed is independent; if one access-rule trips
        // we still want the rest of the picker to work. Use tryFetch
        // (already fault-tolerant) for every model and default to []
        // so downstream .map / .find calls never see undefined.
        const [companies, journals, partners, accounts, plans, analyticAccounts, currencies] = await Promise.all([
            this.tryFetch("res.company", [], ["id", "name"], { limit: 50, order: "name" }),
            this.tryFetch("account.journal", [], ["id", "name", "code"], { limit: 200, order: "name" }),
            this.tryFetch(
                "res.partner",
                [["customer_rank", ">", 0], ["parent_id", "=", false]],
                ["id", "name"],
                { limit: 100, order: "name" },
            ),
            this.tryFetch("account.account", [], ["id", "code", "name"], { limit: 200, order: "code" }),
            this.tryFetch("account.analytic.plan", [], ["id", "name"], { limit: 50, order: "name" }),
            this.tryFetch("account.analytic.account", [], ["id", "name"], { limit: 100, order: "name" }),
            this.tryFetch("res.currency", [["active", "=", true]], ["id", "name", "symbol"], { limit: 200, order: "name" }),
        ]);
        this.state.choices.currencies = currencies || [];
        this.state.choices.companies = companies || [];
        this.state.choices.journals = (journals || []).map((j) => ({
            ...j, name: j.code ? `${j.code} ${j.name}` : j.name,
        }));
        this.state.choices.partners = partners || [];
        this.state.choices.accounts = accounts || [];
        this.state.choices.analyticPlans = plans || [];
        this.state.choices.analyticAccounts = analyticAccounts || [];
    }

    async tryFetch(model, domain, fields, opts) {
        // Analytic models may not be installed in some setups; tolerate.
        try {
            return await this.orm.searchRead(model, domain, fields, opts);
        } catch (e) {
            return [];
        }
    }

    onAccountSearch(event) {
        // Debounced server-side search so an arbitrary account (for
        // example one beyond the initial code-ordered page, routine on a
        // large chart of accounts) can be located by code or name instead
        // of scrolled for in a capped flat list.
        const term = event.target.value;
        clearTimeout(this._accountSearchTimer);
        this._accountSearchTimer = setTimeout(() => {
            this.searchAccounts(term);
        }, 300);
    }

    async searchAccounts(rawTerm) {
        const term = (rawTerm || "").trim();
        const domain = term
            ? ["|", ["code", "ilike", term], ["name", "ilike", term]]
            : [];
        const found = await this.tryFetch(
            "account.account", domain, ["id", "code", "name"],
            { limit: 200, order: "code" },
        );
        // Keep already-selected accounts in the option list so picking a
        // further match does not drop earlier selections (the multi-select
        // reports only its rendered-and-selected options) and so their
        // chips keep resolving to code and name.
        const selected = this.state.options.account_ids || [];
        const missing = selected.filter(
            (id) => !found.some((a) => a.id === id),
        );
        let extras = [];
        if (missing.length) {
            extras = await this.tryFetch(
                "account.account", [["id", "in", missing]],
                ["id", "code", "name"], { order: "code" },
            );
        }
        this.state.choices.accounts = [...extras, ...found];
    }

    async refresh() {
        if (!this.state.reportId) {
            return;
        }
        this.state.loading = true;
        this.state.error = null;
        try {
            const payload = await this.orm.call(
                "eh.account.dynamic.report",
                "render",
                [[this.state.reportId], this.state.options],
            );
            this.state.payload = payload;
            // A fresh payload invalidates any previously fetched children:
            // ids may have changed and lazy leaves always start collapsed.
            this.state.childLines = {};
            // A new payload starts scrolled to the top so the window does not
            // point past the end of a shorter result; the prior in-table
            // search filter is also cleared (its matches referenced the old
            // line ids / names).
            this.state.scrollTop = 0;
            this.state.tableQuery = "";
            if (this.bodyRef && this.bodyRef.el) {
                this.bodyRef.el.scrollTop = 0;
            }
            // Per-user fold persistence: hydrate the user's saved
            // expand / collapse choices for this report so the next
            // render starts from the same shape as the previous
            // session. When no preferences exist yet (returns {}),
            // fall back to the default of "everything expanded".
            if (payload && Array.isArray(payload.lines)) {
                // Foldability is now decided server-side by the uniform fold
                // normalization (_eh_normalize_fold): a row is unfoldable IFF
                // it is a lazy leaf OR it has a child in the payload. The
                // viewer no longer flips section headers on the client, so a
                // flat header (cash-flow / executive-summary / an empty
                // bank-reconciliation section) gets NO stray caret while a
                // section that really nests rows stays foldable.
                const code = this.reportCode;
                let savedState = {};
                try {
                    if (code) {
                        savedState = await this.orm.call(
                            "eh.account.report.fold.state",
                            "get_for_user",
                            [code],
                        ) || {};
                    }
                } catch (exc) {
                    // Persistence is a nice-to-have; never block the
                    // render on a fold-state RPC failure.
                    savedState = {};
                }
                const expanded = [];
                for (const line of payload.lines) {
                    if (!line.unfoldable) continue;
                    // NEVER auto-restore a lazy leaf's expansion. Re-expanding
                    // every account on reload would fan out to a fetch per
                    // account (the §2 invariant forbids it), so lazy leaves
                    // always start collapsed regardless of saved fold state.
                    if (line.lazy) continue;
                    let isExpanded;
                    if (line.id in savedState) {
                        isExpanded = !!savedState[line.id];
                    } else {
                        // Default: respect the handler's per-line
                        // unfolded flag (defaults to true).
                        isExpanded = line.unfolded !== false;
                    }
                    if (isExpanded) expanded.push(line.id);
                }
                this.state.expandedLines = expanded;
            }
        } catch (exc) {
            this.state.error = (exc && exc.message) || String(exc);
        } finally {
            this.state.loading = false;
        }
    }

    async onRefresh() {
        await this.refresh();
    }

    async onExportXlsx() {
        if (!this.state.reportId) return;
        try {
            const action = await this.orm.call(
                "eh.account.dynamic.report",
                "export_xlsx_attachment",
                [[this.state.reportId], this.state.options],
            );
            await this.action.doAction(action);
        } catch (exc) {
            this.notification.add(
                (exc && exc.message) || String(exc), { type: "danger" },
            );
        }
    }

    async onPrintPdf() {
        if (!this.state.reportId) return;
        try {
            const action = await this.orm.call(
                "eh.account.dynamic.report",
                "export_pdf_attachment",
                [[this.state.reportId], this.state.options],
            );
            await this.action.doAction(action);
        } catch (exc) {
            this.notification.add(
                (exc && exc.message) || String(exc), { type: "danger" },
            );
        }
    }

    async onLineClick(line) {
        if (!this.state.reportId) return;
        try {
            const drillAction = await this.orm.call(
                "eh.account.dynamic.report",
                "get_drilldown_for_line",
                [[this.state.reportId], this.state.options, line.id],
            );
            if (drillAction) {
                await this.action.doAction(drillAction);
            }
        } catch (exc) {
            console.warn("eh_dynamic_report drilldown failed", exc);
        }
    }

    onAmountCellClick(line) {
        // WS1 gesture split: the legacy full-page drilldown (open the native
        // journal-items / move list) lives on the amount cell only. The caret
        // (t-on-click.stop) owns the inline lazy unfold, and a bare click on
        // the name cell does nothing, so the two gestures never collide.
        // Sentinel / load-more rows carry no drilldown.
        if (!line || (line.meta && line.meta.kind === "load_more")) return;
        return this.onLineClick(line);
    }

    // ---- filter handlers ----

    onToggleFilters() {
        this.state.filtersExpanded = !this.state.filtersExpanded;
    }

    onDateModeChange(event) {
        const mode = event.target.value;
        this.state.options.date.mode = mode;
        if (PRESET_RANGES[mode]) {
            const [from, to] = PRESET_RANGES[mode]();
            this.state.options.date.date_from = from;
            this.state.options.date.date_to = to;
            this.refresh();
        } else if (mode === "as_of") {
            this.state.options.date.date_from = "0001-01-01";
        }
    }

    onDateFromChange(event) {
        this.state.options.date.date_from = event.target.value;
    }

    onDateToChange(event) {
        this.state.options.date.date_to = event.target.value;
    }

    onComparisonChange(event) {
        this.state.options.comparison = event.target.value;
        // Dropping back to "no comparison" makes a multi-period request
        // meaningless; reset the period count so the stale N never lingers
        // in a saved view or chip.
        if (this.state.options.comparison === "none") {
            this.state.options.comparison_number = 1;
        }
        this.refresh();
    }

    // ---- WS3 ghost-feature controls ----

    // Which extra controls a report supports. Driven first by the live
    // payload (meta.supports, when a handler advertises it) and otherwise
    // by a static per-report map, so a report that has not implemented a
    // branch never shows a control that would silently no-op. Unknown
    // reports get nothing extra, degrading to today's behaviour.
    reportCapabilities() {
        const fromMeta = this.state.payload
            && this.state.payload.meta
            && this.state.payload.meta.supports;
        if (Array.isArray(fromMeta)) {
            return fromMeta;
        }
        const STATIC = {
            profit_and_loss: ["nperiod", "pivot"],
            cash_flow: ["recon", "method"],
        };
        return STATIC[this.reportCode] || [];
    }

    supportsNPeriod() {
        return this.reportCapabilities().includes("nperiod");
    }

    supportsPivot() {
        return this.reportCapabilities().includes("pivot");
    }

    get isCashFlow() {
        return this.reportCode === "cash_flow"
            && this.reportCapabilities().includes("recon");
    }

    // Pivot only makes sense when more than one company is in scope; the
    // server branch is guarded by len>1 and silently no-ops otherwise, so
    // we hide the control to avoid a "looks broken" moment.
    get pivotAvailable() {
        return this.supportsPivot()
            && (this.state.options.company_ids || []).length > 1;
    }

    onComparisonNumberChange(event) {
        const v = parseInt(event.target.value, 10);
        // Clamp 1..12: the server cost is linear in N and a 200-column
        // request would be abusive (see big-data note). Non-numeric falls
        // back to a single period.
        let n = Number.isFinite(v) ? v : 1;
        if (n < 1) n = 1;
        if (n > 12) n = 12;
        this.state.options.comparison_number = n;
        this.refresh();
    }

    onHorizontalGroupChange(event) {
        const val = event.target.value;
        this.state.options.horizontal_group_by =
            val === "company" ? "company" : null;
        this.refresh();
    }

    onCashFlowMethodChange(event) {
        const val = event.target.value;
        this.state.options.cash_flow_method =
            val === "indirect" ? "indirect" : "direct";
        this.refresh();
    }

    onCashFlowReconciledToggle(event) {
        // Opt-in only: the reconciliation-accurate path walks partials per
        // AR/AP line and is the expensive one, so it never defaults on.
        this.state.options.cash_flow_reconciled = !!event.target.checked;
        this.refresh();
    }

    onCfInterestPaidSectionChange(event) {
        // IAS 7.31 override: '' = follow the company policy field.
        const val = event.target.value;
        this.state.options.cf_interest_paid_section =
            val === "operating" || val === "financing" ? val : "";
        this.refresh();
    }

    onCfDividendsPaidSectionChange(event) {
        // IAS 7.34 override: '' = follow the company policy field.
        const val = event.target.value;
        this.state.options.cf_dividends_paid_section =
            val === "financing" || val === "operating" ? val : "";
        this.refresh();
    }

    // ---- WS3 annotations ----

    // The render payload already carries notes (server _eh_apply_annotations
    // stamps line.meta.annotations for row notes and col.annotations for
    // cell notes), so the viewer never fetches them separately. col===false
    // (or undefined) addresses the whole row; a column def addresses a cell
    // via its expression_label.

    annotationsFor(line, col) {
        if (!line) return [];
        if (col && col.expression_label) {
            const lineCol = (line.columns || []).find(
                (c) => c.expression_label === col.expression_label);
            return (lineCol && lineCol.annotations) || [];
        }
        return (line.meta && line.meta.annotations) || [];
    }

    hasAnnotation(line, col) {
        return this.annotationsFor(line, col).length > 0;
    }

    isAnnotationOpen(line, col) {
        const open = this.state.annotationOpen;
        if (!open || !line) return false;
        const label = (col && col.expression_label) || false;
        return open.lineId === line.id && open.label === label;
    }

    onOpenAnnotation(line, col) {
        if (!line) return;
        const label = (col && col.expression_label) || false;
        if (this.isAnnotationOpen(line, col)) {
            this.state.annotationOpen = null;
            return;
        }
        this.state.annotationOpen = { lineId: line.id, label };
        this.state.annotationDraft = "";
    }

    onCloseAnnotation() {
        this.state.annotationOpen = null;
        this.state.annotationDraft = "";
    }

    onAnnotationDraftInput(event) {
        this.state.annotationDraft = event.target.value;
    }

    async onCreateAnnotation() {
        const open = this.state.annotationOpen;
        const text = (this.state.annotationDraft || "").trim();
        if (!open || !text || !this.state.reportId) return;
        try {
            await this.orm.call(
                "eh.account.dynamic.report",
                "add_annotation",
                [[this.state.reportId], open.lineId, text, open.label || false],
            );
            this.state.annotationDraft = "";
            // A fresh render re-applies notes live (they are deliberately
            // injected after the cache lookup), so the new note appears.
            await this.refresh();
        } catch (exc) {
            // Never block the viewer on a note failure: surface and stay put.
            this.notification.add(
                (exc && exc.message) || String(exc), { type: "danger" },
            );
        }
    }

    async onDeleteAnnotation(annotationId) {
        if (!annotationId || !this.state.reportId) return;
        try {
            await this.orm.call(
                "eh.account.dynamic.report",
                "delete_annotation",
                [[this.state.reportId], annotationId],
            );
            await this.refresh();
        } catch (exc) {
            // Non-managers lack unlink (append-only posture); surface the
            // access error rather than silently swallowing it, but never
            // crash the render.
            this.notification.add(
                (exc && exc.message) || String(exc), { type: "danger" },
            );
        }
    }

    onCurrencyChange(event) {
        const val = event.target.value;
        this.state.options.presentation_currency_id = val ? parseInt(val, 10) : null;
        this.refresh();
    }

    onPostedOnlyToggle(event) {
        this.state.options.posted_only = event.target.checked;
        this.refresh();
    }

    onShowZeroToggle(event) {
        this.state.options.show_zero = event.target.checked;
        this.refresh();
    }

    // ---- WS5 in-table search ----

    onTableSearch(event) {
        // Debounced, purely client-side filter over the already-loaded
        // payload. No RPC, so it stays instant on a huge ledger; the debounce
        // only avoids rebuilding the match set on every keystroke. Scroll is
        // reset to the top so the window does not point past the (shorter)
        // filtered list.
        const term = event.target.value;
        clearTimeout(this._tableSearchTimer);
        this._tableSearchTimer = setTimeout(() => {
            this.state.tableQuery = term || "";
            this.state.scrollTop = 0;
            if (this.bodyRef && this.bodyRef.el) {
                this.bodyRef.el.scrollTop = 0;
            }
        }, SEARCH_DEBOUNCE_MS);
    }

    // The set of payload line ids that should stay on screen for the active
    // query: a line matches when its own name contains the term OR any
    // descendant matches (so a parent stays as context for a matched child)
    // OR any ancestor matches (so the children of a matched group stay).
    // Built once per query change and memoised on (payload, query) so a
    // scroll does not recompute it.
    get tableFilteredIds() {
        const q = (this.state.tableQuery || "").trim().toLowerCase();
        if (!q || !this.state.payload) {
            return null; // null = no active filter; show everything.
        }
        // Memoise: scrolling fires many re-renders but the match set only
        // depends on the payload identity and the query string. The match
        // walk itself lives in the pure (hoot-tested) logic module.
        if (this._filterCache
            && this._filterCache.payload === this.state.payload
            && this._filterCache.q === q) {
            return this._filterCache.ids;
        }
        const keep = computeFilterKeepSet(this.state.payload.lines || [], q);
        this._filterCache = { payload: this.state.payload, q, ids: keep };
        return keep;
    }

    // ---- aged-report config (WS2) ----

    get isAged() {
        return this.reportCode === "aged_receivable"
            || this.reportCode === "aged_payable";
    }

    onAgingIntervalChange(event) {
        const v = parseInt(event.target.value, 10);
        this.state.options.aging_interval = Number.isFinite(v) && v > 0 ? v : 30;
        this.refresh();
    }

    onAgingBucketCountChange(event) {
        const v = parseInt(event.target.value, 10);
        this.state.options.aging_bucket_count =
            Number.isFinite(v) && v > 0 ? v : 4;
        this.refresh();
    }

    onAgingBasisChange(event) {
        this.state.options.aging_basis = event.target.value || "maturity";
        this.refresh();
    }

    onReconcileStateChange(event) {
        this.state.options.reconcile_state = event.target.value || "open";
        this.refresh();
    }

    onMultiSelectChange(event, key) {
        const ids = Array.from(event.target.selectedOptions).map(
            (o) => parseInt(o.value, 10),
        );
        this.state.options[key] = ids;
        this.refresh();
    }

    onMultiSelectChangeStr(event, key) {
        const codes = Array.from(event.target.selectedOptions).map((o) => o.value);
        this.state.options[key] = codes;
        this.refresh();
    }

    onClearAllFilters() {
        this.state.options.journal_ids = [];
        this.state.options.partner_ids = [];
        this.state.options.account_ids = [];
        this.state.options.account_type_ids = [];
        this.state.options.analytic_account_ids = [];
        this.state.options.analytic_plan_ids = [];
        this.state.options.comparison = "none";
        this.state.options.comparison_number = 1;
        this.state.options.horizontal_group_by = null;
        this.state.options.cash_flow_method = "direct";
        this.state.options.cash_flow_reconciled = false;
        this.state.options.cf_interest_paid_section = "";
        this.state.options.cf_dividends_paid_section = "";
        this.refresh();
    }

    async openManyToManyPicker(model, optionKey, label) {
        // Multi-select records picker with checkboxes + a Select button.
        // SelectCreateDialog is imported lazily (not at module top level) so
        // a bundle-resolution hiccup can never stop this component from
        // registering; we fall back to the inline multi-select otherwise.
        try {
            const { SelectCreateDialog } = await odoo.loader.modules.get(
                "@web/views/view_dialogs/select_create_dialog");
            this.dialog.add(SelectCreateDialog, {
                title: "Pick " + label,
                resModel: model,
                multiSelect: true,
                domain: [],
                noCreate: false,
                onSelected: (resIds) => {
                    if (!resIds || !resIds.length) {
                        return;
                    }
                    const merged = new Set(
                        [...(this.state.options[optionKey] || []), ...resIds]
                            .map((i) => parseInt(i, 10)),
                    );
                    this.state.options[optionKey] = Array.from(merged);
                    this.refresh();
                },
            });
        } catch (e) {
            this.notification.add(
                "Use the multi-select list; full picker unavailable.",
                { type: "warning" },
            );
        }
    }

    activeFilterCount() {
        const opts = this.state.options;
        let n = 0;
        if (opts.journal_ids.length) n++;
        if (opts.partner_ids.length) n++;
        if (opts.account_ids.length) n++;
        if ((opts.account_type_ids || []).length) n++;
        if (opts.analytic_account_ids.length) n++;
        if (opts.analytic_plan_ids.length) n++;
        if (opts.comparison && opts.comparison !== "none") n++;
        if (opts.comparison_number && opts.comparison_number > 1) n++;
        if (opts.horizontal_group_by) n++;
        if (this.isCashFlow && opts.cash_flow_method
                && opts.cash_flow_method !== "direct") n++;
        if (this.isCashFlow && opts.cash_flow_reconciled) n++;
        if (this.isAged) {
            if (opts.reconcile_state && opts.reconcile_state !== "open") n++;
            if (opts.aging_basis && opts.aging_basis !== "maturity") n++;
            if (opts.aging_interval && opts.aging_interval !== 30) n++;
            if (opts.aging_bucket_count && opts.aging_bucket_count !== 4) n++;
        }
        return n;
    }

    activeChips() {
        const chips = [];
        const opts = this.state.options;
        if (opts.comparison && opts.comparison !== "none") {
            chips.push({
                label: "Compare: " + opts.comparison.replace("_", " "),
                key: "comparison",
            });
        }
        if (opts.comparison_number && opts.comparison_number > 1) {
            chips.push({
                label: "Periods: " + opts.comparison_number,
                key: "comparison_number",
            });
        }
        if (opts.horizontal_group_by === "company") {
            chips.push({ label: "Layout: By company", key: "horizontal_group_by" });
        }
        if (this.isCashFlow && opts.cash_flow_method === "indirect") {
            chips.push({ label: "Method: Indirect", key: "cash_flow_method" });
        }
        if (this.isCashFlow && opts.cash_flow_reconciled) {
            chips.push({
                label: "Reconciliation-accurate",
                key: "cash_flow_reconciled",
            });
        }
        const named = [
            ["journal_ids", this.state.choices.journals, "Journal"],
            ["partner_ids", this.state.choices.partners, "Partner"],
            ["account_ids", this.state.choices.accounts, "Account"],
            ["analytic_plan_ids", this.state.choices.analyticPlans, "Analytic plan"],
            ["analytic_account_ids", this.state.choices.analyticAccounts, "Analytic"],
        ];
        for (const [key, src, label] of named) {
            for (const id of opts[key] || []) {
                const rec = src.find((r) => r.id === id);
                chips.push({
                    label: `${label}: ${rec ? (rec.code ? rec.code + " " : "") + rec.name : id}`,
                    key, id,
                });
            }
        }
        for (const code of opts.account_type_ids || []) {
            const t = ACCOUNT_TYPE_CHOICES.find((x) => x.code === code);
            chips.push({
                label: "Type: " + (t ? t.label : code),
                key: "account_type_ids", id: code,
            });
        }
        if (this.isAged) {
            if (opts.reconcile_state && opts.reconcile_state !== "open") {
                chips.push({
                    label: "Reconcile: all (incl. reconciled)",
                    key: "reconcile_state",
                });
            }
            if (opts.aging_basis && opts.aging_basis !== "maturity") {
                chips.push({ label: "Basis: invoice date", key: "aging_basis" });
            }
            if (opts.aging_interval && opts.aging_interval !== 30) {
                chips.push({
                    label: "Interval: " + opts.aging_interval + "d",
                    key: "aging_interval",
                });
            }
            if (opts.aging_bucket_count && opts.aging_bucket_count !== 4) {
                chips.push({
                    label: "Buckets: " + opts.aging_bucket_count,
                    key: "aging_bucket_count",
                });
            }
        }
        return chips;
    }

    onRemoveChip(chip) {
        const agingDefaults = {
            reconcile_state: "open",
            aging_basis: "maturity",
            aging_interval: 30,
            aging_bucket_count: 4,
        };
        const ws3Defaults = {
            comparison_number: 1,
            horizontal_group_by: null,
            cash_flow_method: "direct",
            cash_flow_reconciled: false,
            cf_interest_paid_section: "",
            cf_dividends_paid_section: "",
        };
        if (chip.key === "comparison") {
            this.state.options.comparison = "none";
            // Dropping the comparison also drops a multi-period request.
            this.state.options.comparison_number = 1;
        } else if (chip.key in ws3Defaults && chip.id === undefined) {
            this.state.options[chip.key] = ws3Defaults[chip.key];
        } else if (chip.key in agingDefaults && chip.id === undefined) {
            this.state.options[chip.key] = agingDefaults[chip.key];
        } else if (chip.id !== undefined) {
            this.state.options[chip.key] = this.state.options[chip.key].filter(
                (x) => x !== chip.id,
            );
        }
        this.refresh();
    }

    // ---- presentation helpers ----

    valueColumnDefs() {
        // Drop the leading label column; the rest are the value columns. The
        // defs already carry expression_label (the sectioned handler stamps
        // 'amount' / 'prior_amount' / 'variance' / 'variance_pct'), which
        // cellClass() reads to tell a variance column from a plain amount.
        if (!this.state.payload) return [];
        return this.state.payload.columns.slice(1);
    }

    formatLineValue(line, valueIndex) {
        const colDef = this.valueColumnDefs()[valueIndex];
        if (!colDef) return "";
        const lineCol = line.columns ? line.columns[valueIndex] : null;
        if (!lineCol) return "";
        return formatCurrency(
            lineCol.value, this.state.payload.currency, colDef.figure_type,
        );
    }

    // True when a column expresses a CHANGE (variance / variance %) rather
    // than a raw balance. A change column is coloured by whether the move is
    // favourable, not merely by sign, so a cost reduction reads positive.
    _isComparisonColumn(colDef) {
        const label = colDef && colDef.expression_label;
        return label === "variance" || label === "variance_pct";
    }

    // Whether a higher number is "good" for this row. Income/revenue rows
    // want higher; expense/cost rows want lower. The server may stamp
    // line.meta.higher_is_better (P&L does, keyed on section); when it is
    // absent we return null so the caller falls back to sign-only colouring,
    // which is never worse than the previous all-negatives-red behaviour.
    _higherIsBetter(line) {
        const meta = line && line.meta;
        if (meta && typeof meta.higher_is_better === "boolean") {
            return meta.higher_is_better;
        }
        return null;
    }

    cellClass(line, valueIndex) {
        const colDef = this.valueColumnDefs()[valueIndex];
        const classes = [];
        const isNumeric = colDef && [
            "monetary", "integer", "float", "percentage",
        ].includes(colDef.figure_type);
        if (isNumeric) {
            classes.push("text-end");
            // Numeric cells render in mono with tabular figures so columns of
            // numbers line up digit-for-digit like the dashboard KPI tiles.
            classes.push("eh_dr_num");
        }
        const lineCol = line.columns ? line.columns[valueIndex] : null;
        const value = lineCol ? lineCol.value : null;
        if (!isNumeric || typeof value !== "number") {
            return classes.join(" ");
        }
        if (this._isComparisonColumn(colDef)) {
            // Semantic good/bad colouring on a change column. The role
            // (good/bad/muted by sign x direction) is computed by the pure,
            // hoot-tested variantCellRole(); higher_is_better=null falls back
            // to sign-only, never worse than today.
            const role = variantCellRole(value, this._higherIsBetter(line));
            const ROLE_CLASS = {
                good: "eh_dr_good", bad: "eh_dr_bad", muted: "eh_dr_muted",
            };
            if (role && ROLE_CLASS[role]) {
                classes.push(ROLE_CLASS[role]);
            }
        } else if (value < 0) {
            // Non-comparison column: keep the plain negative=warm-red rule.
            classes.push("eh_dr_bad");
        }
        return classes.join(" ");
    }

    rowClass(line) {
        const meta = line.meta || {};
        const classes = [];
        if (line.level === 0) classes.push("eh_dr_section_row");
        else classes.push("eh_dr_data_row");
        if (meta.kind === "section_header") classes.push("eh_dr_header");
        if (meta.kind === "section_total") classes.push("eh_dr_total");
        if (meta.kind === "balance_check") classes.push("eh_dr_check");
        if (meta.kind === "net_profit"
            || meta.kind === "net_change"
            || meta.kind === "computed_total") {
            classes.push("eh_dr_computed");
        }
        return classes.join(" ");
    }

    nameStyle(line) {
        const indentEm = (line.level || 0) * 1.5;
        return indentEm ? "padding-left: " + indentEm + "em;" : "";
    }

    isExpanded(line) {
        return this.state.expandedLines.includes(line.id);
    }

    onToggleLine(line) {
        const newlyExpanded = !this.isExpanded(line);
        if (newlyExpanded) {
            this.state.expandedLines = [
                ...this.state.expandedLines, line.id,
            ];
        } else {
            this.state.expandedLines = this.state.expandedLines.filter(
                (id) => id !== line.id,
            );
        }
        // Lazy leaf: fetch the first page of children on first expand only.
        // Collapse keeps the cached children, so a re-expand is instant and
        // does NOT refetch (the windowed builder simply stops splicing them
        // while collapsed). We fetch only when no page is cached yet.
        if (line.lazy && newlyExpanded && !this.state.childLines[line.id]) {
            this.loadChildren(line, 0);
        }
        // Fire-and-forget persistence: any failure leaves the local
        // state intact; the next reload simply falls back to the
        // saved-or-default behaviour. We deliberately do not await
        // so the click feels immediate. Lazy leaves are not persisted as
        // expanded (they always start collapsed on reload), but recording
        // the toggle is harmless because hydrate skips lazy leaves.
        const code = this.reportCode;
        if (code) {
            this.orm.call(
                "eh.account.report.fold.state",
                "set_for_user",
                [code, line.id, newlyExpanded],
            ).catch(() => {});
        }
    }

    async loadChildren(line, offset) {
        // Fetch one page of an account leaf's journal items via the
        // stateless expand_line RPC. Appends to any already-loaded page so
        // load-more accumulates. Defensive: a failed expand leaves the row
        // expanded-but-empty (or with its prior page), never throwing.
        if (!this.state.reportId || !line || !line.id) return;
        const existing = this.state.childLines[line.id] || {
            lines: [], hasMore: false, nextOffset: 0, totalCount: 0,
        };
        // Mark loading so the sentinel can show a spinner; reuse the same
        // object identity is unnecessary because state is reactive.
        this.state.childLines = {
            ...this.state.childLines,
            [line.id]: { ...existing, loading: true },
        };
        try {
            const res = await this.orm.call(
                "eh.account.dynamic.report",
                "expand_line",
                [
                    [this.state.reportId], this.state.options, line.id,
                    offset || 0, null,
                ],
            );
            const fetched = (res && res.child_lines) || [];
            const merged = offset
                ? [...existing.lines, ...fetched]
                : fetched;
            this.state.childLines = {
                ...this.state.childLines,
                [line.id]: {
                    lines: merged,
                    hasMore: !!(res && res.has_more),
                    nextOffset: (res && res.next_offset) || merged.length,
                    totalCount: (res && res.total_count) || merged.length,
                    loading: false,
                },
            };
        } catch (exc) {
            // Keep whatever we had; clear the loading flag so the spinner
            // stops and the user can retry by collapsing/re-expanding.
            this.state.childLines = {
                ...this.state.childLines,
                [line.id]: { ...existing, loading: false },
            };
            console.warn("eh_dynamic_report expand_line failed", exc);
        }
    }

    onLoadMore(line) {
        const entry = this.state.childLines[line.id];
        if (!entry || entry.loading || !entry.hasMore) return;
        this.loadChildren(line, entry.nextOffset);
    }

    hasHierarchy() {
        return !!(this.state.payload && this.state.payload.lines.some(
            (l) => l.unfoldable,
        ));
    }

    onExpandAll() {
        if (!this.state.payload) return;
        // Expand every group, but NEVER lazy leaves: expanding all lazy
        // leaves would fire one fetch per account and fan out to every
        // journal item (the §2 invariant). Lazy leaves stay collapsed and
        // the user expands the ones they care about individually.
        this.state.expandedLines = this.state.payload.lines
            .filter((l) => l.unfoldable && !l.lazy).map((l) => l.id);
    }

    onCollapseAll() {
        this.state.expandedLines = [];
    }

    visibleLines() {
        if (!this.state.payload) return [];
        // ---- WS5 compose order: filter -> fold-visibility ----
        // Step 1 (filter): when an in-table search is active, restrict the
        // payload to the matched set (matches + their ancestors + the
        // descendants of a matched group) BEFORE the fold walk, so a search
        // result's parents/children stay coherent and the fold walk sees the
        // smaller set. null = no active filter.
        const keepIds = this.tableFilteredIds;
        const sourceLines = keepIds
            ? this.state.payload.lines.filter((l) => keepIds.has(l.id))
            : this.state.payload.lines;
        // Step 2 (fold-visibility): a line is visible only when EVERY
        // ancestor up the parent chain is expanded, not just its direct
        // parent. Checking the direct parent alone let a deeply nested row
        // stay on screen after a grandparent section above its
        // (still-expanded) parent was collapsed: e.g. group -> subgroup ->
        // account, collapse the group and the account leaked through because
        // its parent subgroup was still in the expanded set.
        const expanded = new Set(this.state.expandedLines);
        // While a search is active, a matched row must actually surface even
        // if the user had collapsed its parent group. Temporarily treat every
        // kept (in-filter) line as expanded so the fold walk does not hide a
        // match behind a collapsed ancestor; the user's real expandedLines is
        // untouched, so clearing the search restores the prior fold shape.
        if (keepIds) {
            for (const id of keepIds) {
                expanded.add(id);
            }
        }
        const byId = new Map();
        for (const line of sourceLines) {
            byId.set(line.id, line);
        }
        const result = [];
        for (const line of sourceLines) {
            let parentId = line.parent_id;
            let visible = true;
            const seen = new Set(); // guard against a malformed parent cycle
            while (parentId) {
                if (seen.has(parentId)) break;
                seen.add(parentId);
                const parent = byId.get(parentId);
                // Only a FOLDABLE ancestor gates visibility. A structural
                // section header (unfoldable:false) is always open and must
                // never hide its own account rows - otherwise the accounts
                // under Income / Expenses are invisible with no caret to
                // reveal them. Gate only when a foldable ancestor is collapsed.
                if (parent && parent.unfoldable && !expanded.has(parentId)) {
                    visible = false;
                    break;
                }
                parentId = parent ? parent.parent_id : null;
            }
            if (visible) {
                result.push(line);
                // Splice fetched children directly under an expanded lazy
                // leaf, then a load_more sentinel when more pages remain.
                // (Virtual windowing itself is WS5; this keeps the ordered
                // visible array correct so windowing can slice it later.)
                if (line.lazy && expanded.has(line.id)) {
                    const entry = this.state.childLines[line.id];
                    if (entry && entry.lines && entry.lines.length) {
                        for (const child of entry.lines) {
                            result.push(child);
                        }
                    }
                    if (entry && (entry.hasMore || entry.loading)) {
                        result.push(this._loadMoreSentinel(line, entry));
                    } else if (!entry || entry.loading === undefined) {
                        // Expanded but no page yet resolved: show a spinner.
                        result.push(this._loadMoreSentinel(line, {
                            loading: true, hasMore: false,
                        }));
                    }
                }
            }
        }
        return result;
    }

    _loadMoreSentinel(line, entry) {
        // A synthetic one-row line the template renders as a "load more" /
        // loading sentinel. level = leaf.level + 1 so it indents under the
        // children. Carries the parent line id so onLoadMore can resolve it.
        return {
            id: "loadmore-" + line.id,
            name: "",
            level: (line.level || 0) + 1,
            parent_id: line.id,
            columns: [],
            unfoldable: false,
            meta: {
                kind: "load_more",
                parent_line_id: line.id,
                loading: !!entry.loading,
                has_more: !!entry.hasMore,
                total_count: entry.totalCount || 0,
            },
        };
    }

    onSentinelClick(line) {
        // Resolve the parent lazy leaf and page the next slice.
        const meta = line.meta || {};
        const parentId = meta.parent_line_id;
        if (!parentId || !this.state.payload) return;
        const parent = this.state.payload.lines.find((l) => l.id === parentId);
        if (parent) {
            this.onLoadMore(parent);
        }
    }

    // ---- WS5 virtual scroll ----
    //
    // Compose order (mandatory): filter -> fold-visibility -> window.
    // visibleLines() above already does filter (step 1) then fold-visibility
    // (step 2). windowedLines() is step 3: slice that ordered visible array
    // to the rows that intersect the viewport (plus overscan) so only ~40
    // rows are ever in the DOM regardless of payload size. The non-rendered
    // height is reproduced by top/bottom spacer rows so the scrollbar still
    // reflects the full list. The slice metadata (full count, start index,
    // spacer heights) is cached per call so the template's spacer getters
    // read the same window the rows came from.

    _computeWindow() {
        const visible = this.visibleLines();
        // Virtual windowing ONLY kicks in above a threshold. Below it, render
        // the full visible list with NO spacer rows, so the table is a single
        // plain <tbody> and the sticky <thead> pins reliably. The spacer-row +
        // async-rerender windowing was desyncing from the native scroll and
        // letting the header detach into the body mid-scroll. The lazy engine
        // keeps initial payloads small (O(accounts/partners), <= ~1500 rows),
        // so every real report renders in this stable, spacer-free path; the
        // window only engages for an exceptionally large expanded view.
        if (visible.length <= VIRTUAL_THRESHOLD) {
            return {
                lines: visible, total: visible.length,
                startIndex: 0, topPad: 0, bottomPad: 0,
            };
        }
        // visibleLines() = filter (step 1) + fold-visibility (step 2); the
        // slice (step 3) is the pure, hoot-tested sliceWindow(). It degrades
        // to the full list when the row-height math is degenerate, so the
        // table never blanks or divides by zero.
        return sliceWindow(visible, {
            rowHeight: ROW_HEIGHT,
            overscan: OVERSCAN,
            scrollTop: this.state.scrollTop || 0,
            viewportPx: this.state.viewportPx || DEFAULT_VIEWPORT_PX,
        });
    }

    windowedLines() {
        // Single source for the rendered slice; cache the whole window object
        // so windowTopPad / windowBottomPad read the matching spacers without
        // recomputing visibleLines() three times per render.
        this._window = this._computeWindow();
        return this._window.lines;
    }

    get windowTopPad() {
        return (this._window && this._window.topPad) || 0;
    }

    get windowBottomPad() {
        return (this._window && this._window.bottomPad) || 0;
    }

    get rowHeight() {
        return ROW_HEIGHT;
    }

    // Count of currently-visible (post-filter, post-fold) payload rows, for
    // the "showing X of Y" meta. Excludes the synthetic load-more sentinels
    // so the figure reflects real lines. Falls back to the window total.
    get visibleCount() {
        if (this._window && typeof this._window.total === "number") {
            return this._window.total;
        }
        return this.visibleLines().length;
    }

    // Total payload row count (the Y in "showing X of Y").
    get totalRowCount() {
        return (this.state.payload && this.state.payload.lines
            && this.state.payload.lines.length) || 0;
    }

    // True when the active search yields no rows, so the template shows an
    // empty-state instead of a blank, crashing table.
    get searchHasNoMatch() {
        const q = (this.state.tableQuery || "").trim();
        if (!q) return false;
        const ids = this.tableFilteredIds;
        return !!ids && ids.size === 0;
    }
}

registry.category("actions").add(
    "eh_account_dynamic_report", EhDynamicReportViewer,
);
