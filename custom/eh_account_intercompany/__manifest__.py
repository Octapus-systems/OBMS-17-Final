# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Inter-Company Rules',
    'summary': 'Automatic inter-company invoice mirroring for Odoo 17 Community: a posted sale invoice in company A builds the matching purchase bill in company B (and a vendor bill mirrors back to a customer invoice), with duplicate-proof linkage, a review queue, amount-mismatch detection, and an IFRS 10.B86 elimination pair engine that matches posted pairs per period, books receivable/payable and revenue/expense elimination entries in the parent company, and derives unrealised profit in inventory from the source invoices. Odoo intercompany invoicing, automatic intercompany bill, multi company sale to purchase mirror, sister company invoice automation, group accounting multi entity, intercompany elimination journal, consolidation elimination automation, unrealised profit elimination, cross company invoice automation, Odoo 17 intercompany Community.',
    'description': """Inter-Company Rules automates the mirror leg of trade between related entities on Odoo 17 Community. When a sale invoice or vendor bill posts and its partner is linked to another company (through the partner's Represented Company field, or through its commercial partner's company) and that destination company has an enabled inter-company config, the module builds the matching mirror in the destination company. A posted customer invoice in company A becomes a purchase bill in company B; a vendor bill becomes a customer invoice; refunds mirror the same way. The mirror lands as draft for the receiving accountant to review, or auto-posts, per the destination company config.

The linkage is duplicate-proof. Each mirror stores an origin pointer back to its source, and a database-level unique constraint on (origin, company) makes re-posting and concurrent posts idempotent, so two simultaneous posts cannot both slip past the search-before-create guard and create twin bills. A stat button jumps from either side to its counterpart, switching company context automatically.

Mirroring is non-blocking by design. The source posts first; the mirror is built immediately after inside a guarded step. If any part of the mirror build fails (a missing destination journal, an unresolvable account), the source still posts, the explicit failure reason is written to the source invoice chatter, and the move's inter-company state flips to No mirror so the unmirrored pair shows up in the review-queue filter instead of being silently lost. A missing or disabled inter-company config is simply treated as mirroring-off and skipped.

A stored lifecycle state (Not applicable, No mirror, Mirror draft, Amount mismatch, Matched) drives list badges, search filters, and a dedicated Inter-company review action, so a supervisor sees only the pairs that need attention. Once both legs are posted, a divergence above one cent (from a manual edit, a missing line, or FX drift) flips the pair to Amount mismatch for a human to resolve.

The mirror line build is deliberate. The destination account resolves in priority order: the destination company's product account first, then the configured fallback account, and if neither exists it raises a clear error naming the exact field to fix, so no NULL account ever reaches a move line. Tax is mapped by rate and direction (not by name), and an unresolved source tax is dropped with a warning rather than substituting a wrong-rate tax that would distort GST or VAT. Analytic distribution is carried across but company-scoped, so group-wide analytic accounts survive on the mirror while source-company-only analytic accounts are stripped and never leak into the destination company.

The mirror is created with sudo() and the destination company context, the standard Odoo pattern for cross-company writes, without broadening the user's own permissions. A Represented Company field on the partner keeps the counterparty partner globally usable on invoices in every company.

The elimination pair engine (IFRS 10.B86) closes the loop from mirroring to group accounting. An elimination batch takes a company pair and a period, matches the posted inter-company move pairs through the origin linkage the mirror maintains, and builds the elimination legs: the receivable recognised on the selling side against the payable on the buying side, and the revenue against the expense for the period. Pairs that do not reconcile are never silently eliminated: a missing or draft mirror, or totals diverging beyond one cent, land on a mismatch tab with the reason, posting is blocked while mismatches exist, and a manager can clear the tracked Block on Mismatch flag to post the matched eliminations anyway while the differences stay listed. Amount-mismatched pairs are eliminated at the common (lower) amount only, so the group never eliminates more than both sides recognised. Posting books one balanced, sealed journal entry in the designated elimination company (the parent) in a dedicated elimination journal that is auto-created on first use; accounts are mapped into the parent chart by code and a missing mapping fails loudly. Reset reverses the sealed entry, and a database unique constraint per pair and period (including the reversed pair) plus compute-time replacement of engine lines make the whole cycle idempotent.

Unrealised profit in ending inventory is computed from the source documents, never hand-typed. For every matched pair whose selling side is a customer invoice, the margin per product line is derived from the invoice against the product standard cost in the selling company. When the stock module is installed, the fraction of the sold quantity still held in the buyer's internal locations is read from stock quants; on account-only installs the remaining fraction is entered per line but the margin itself always stays engine-derived.

Consolidation hook: eh_ic_elimination_summary(period_from, period_to, company_ids=None) on eh.ic.elimination.batch returns the structured totals (receivable and payable eliminated, revenue and expense eliminated, unrealised profit, mismatch count, and one row per batch) so a consolidation run can consume the elimination work programmatically. The unrealised legs are deliberately not booked in the elimination move here: the consolidation run owns the inventory and COGS restatement and reads the figure from the hook, which prevents the same margin being eliminated twice.

A posting-time guard (Restrict group-company partners, off by default) catches mis-keyed inter-company transactions: with the flag on, posting an invoice or bill towards the raw partner record of another group company that is not flagged with a Represented Company is refused before anything books, because such a document would bypass the mirror and the elimination engine.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.2.0',
    'depends': ['eh_account_base', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'views/intercompany_config_views.xml',
        'views/account_move_views.xml',
        'views/ic_elimination_batch_views.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
