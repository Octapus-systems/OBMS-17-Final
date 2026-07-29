/** @odoo-module **/
// ============================================================================
// ERP Heritage
// Copyright (C) 2026 (https://www.erpheritage.com.au/)
// ============================================================================
//
// Pure presentation helpers for the dynamic report viewer.
//
// These are stateless functions and constants: date-preset maths, the
// account-type label table, and the currency/figure formatter. They are
// split out of the viewer component so the component carries only stateful
// behaviour, and so this logic can be reused and reasoned about on its own.

function isoDate(d) {
    return d.toISOString().slice(0, 10);
}

function endOfMonth(date) {
    return new Date(date.getFullYear(), date.getMonth() + 1, 0);
}

function shiftMonths(date, count) {
    return new Date(
        date.getFullYear(), date.getMonth() + count, date.getDate(),
    );
}

function startOfQuarter(date) {
    const q = Math.floor(date.getMonth() / 3);
    return new Date(date.getFullYear(), q * 3, 1);
}

function endOfQuarter(date) {
    const q = Math.floor(date.getMonth() / 3);
    return new Date(date.getFullYear(), q * 3 + 3, 0);
}

export function todayStr() {
    return new Date().toISOString().slice(0, 10);
}

export function firstOfMonthStr() {
    const d = new Date();
    return isoDate(new Date(d.getFullYear(), d.getMonth(), 1));
}

export const PRESET_RANGES = {
    this_month: () => {
        const today = new Date();
        return [isoDate(new Date(today.getFullYear(), today.getMonth(), 1)),
                isoDate(endOfMonth(today))];
    },
    last_month: () => {
        const today = new Date();
        const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
        return [isoDate(start), isoDate(endOfMonth(start))];
    },
    this_quarter: () => {
        const today = new Date();
        return [isoDate(startOfQuarter(today)), isoDate(endOfQuarter(today))];
    },
    last_quarter: () => {
        const today = new Date();
        const last = shiftMonths(today, -3);
        return [isoDate(startOfQuarter(last)), isoDate(endOfQuarter(last))];
    },
    this_year: () => {
        const today = new Date();
        return [`${today.getFullYear()}-01-01`, `${today.getFullYear()}-12-31`];
    },
    last_year: () => {
        const today = new Date();
        const y = today.getFullYear() - 1;
        return [`${y}-01-01`, `${y}-12-31`];
    },
};

export const ACCOUNT_TYPE_CHOICES = [
    { code: "asset_receivable", label: "Receivable" },
    { code: "asset_cash", label: "Cash" },
    { code: "asset_current", label: "Current Asset" },
    { code: "asset_non_current", label: "Non-current Asset" },
    { code: "asset_prepayments", label: "Prepayments" },
    { code: "asset_fixed", label: "Fixed Asset" },
    { code: "liability_payable", label: "Payable" },
    { code: "liability_credit_card", label: "Credit Card" },
    { code: "liability_current", label: "Current Liability" },
    { code: "liability_non_current", label: "Non-current Liability" },
    { code: "equity", label: "Equity" },
    { code: "equity_unaffected", label: "Current Year Earnings" },
    { code: "income", label: "Income" },
    { code: "income_other", label: "Other Income" },
    { code: "expense", label: "Expense" },
    { code: "expense_depreciation", label: "Depreciation" },
    { code: "expense_direct_cost", label: "Cost of Revenue" },
    { code: "off_balance", label: "Off-balance" },
];

export function formatCurrency(value, currency, figureType) {
    if (value === null || value === undefined || value === "") {
        return "";
    }
    if (typeof value !== "number") {
        return String(value);
    }
    if (figureType === "integer") {
        return Math.trunc(value).toLocaleString();
    }
    if (figureType === "percentage") {
        return (value * 100).toFixed(2) + "%";
    }
    if (figureType === "float") {
        const fixed = Math.abs(value).toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
        return value < 0 ? "(" + fixed + ")" : fixed;
    }
    if (figureType !== "monetary") {
        return String(value);
    }
    const decimals = (currency && currency.decimal_places !== undefined)
        ? currency.decimal_places : 2;
    const fixed = Math.abs(value).toLocaleString(undefined, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
    });
    let body;
    if (currency && currency.symbol && !currency.multi_currency) {
        if (currency.position === "before") {
            body = currency.symbol + " " + fixed;
        } else {
            body = fixed + " " + currency.symbol;
        }
    } else {
        body = fixed;
    }
    return value < 0 ? "(" + body + ")" : body;
}
