# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.consol.entity: a consolidated reporting entity.

A consolidation entity defines a parent perspective and the set of
subsidiary companies that roll up into it. The entity owns the
presentation currency (the currency consolidated balances are
reported in) and the list of member companies via the
eh.consol.member intermediate model.

Per IFRS 10, an entity controls another when it has power over the
relevant activities, exposure to variable returns, and the ability
to use its power to affect those returns. This module assumes
control determination is made off-platform; the entity definition
captures the result (which subsidiaries are in scope) plus the
ownership percentage that drives NCI computation.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class EhConsolEntity(models.Model):
    _name = 'eh.consol.entity'
    _description = "Consolidated reporting entity"
    _order = 'name'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(
        required=True, tracking=True,
        help=(
            "Display name of the consolidated entity. Convention: "
            "'<Group> Consolidated' or '<Parent> Group'."
        ),
    )
    code = fields.Char(
        required=True, copy=False,
        help=(
            "Stable programmatic identifier (alphanumeric + "
            "underscore). Used for cross-record references so a "
            "renamed name does not break external links."
        ),
    )

    presentation_currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
        tracking=True,
        help=(
            "Currency the consolidated balances are reported in. "
            "Member companies whose functional currency differs are "
            "translated per IAS 21 at run time."
        ),
    )
    parent_company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        tracking=True,
        help=(
            "Root company of the consolidated entity. Used for "
            "default-currency resolution and ownership of the "
            "consolidated reports themselves."
        ),
    )

    consolidation_company_id = fields.Many2one(
        'res.company',
        string="Consolidation Ledger Company",
        tracking=True,
        help=(
            "Optional dedicated company reserved for the consolidated "
            "ledger. When set, a computed run can post a single balanced, "
            "immutable account.move into this company's books (IFRS 10 "
            "auditability), and closing the run then requires that move to be "
            "posted. Leave empty to keep the run a memo-only set of run "
            "lines, which is the default behaviour. This must NOT be an "
            "operating company whose transactions are already pulled into the "
            "consolidation, or the subsidiaries would be double-counted; use "
            "a company created solely to hold the consolidated result. Its "
            "chart of accounts must carry, by code, every account referenced "
            "by the run (share the parent chart, or map codes 1:1)."
        ),
    )
    consolidation_journal_id = fields.Many2one(
        'account.journal',
        string="Consolidation Journal",
        tracking=True,
        domain="[('type', '=', 'general'),"
               " ('company_id', '=', consolidation_company_id)]",
        help=(
            "General journal in the consolidation ledger company that the "
            "posted consolidation move is booked to. When left empty, the "
            "first general journal in the consolidation company is used."
        ),
    )
    cta_account_id = fields.Many2one(
        'account.account',
        string="CTA / Translation Reserve Account",
        tracking=True,
        help=(
            "Equity account on the parent chart the IAS 21 currency "
            "translation adjustment (CTA) is booked to. When set, the run "
            "books the CTA plug to this account explicitly. When left empty "
            "the run falls back to a name heuristic (an equity account whose "
            "name contains 'translation' or 'CTA'); if that also resolves "
            "nothing and the CTA is non-zero the compute is refused rather "
            "than silently posting a CTA line with no account."
        ),
    )
    nci_account_id = fields.Many2one(
        'account.account',
        string="Default NCI Account",
        tracking=True,
        help=(
            "Equity account on the parent chart the non-controlling interest "
            "carve-out is booked to for members that do not set their own NCI "
            "account. When left empty the run falls back to a name heuristic "
            "(an equity account whose name contains 'non-controlling' or "
            "'minority'); if that also resolves nothing and an NCI carve-out "
            "is non-zero the compute is refused rather than silently posting "
            "an NCI line with no account."
        ),
    )

    auto_eliminate_investment = fields.Boolean(
        string="Auto-Eliminate Investment (IFRS 3)",
        default=True,
        tracking=True,
        help=(
            "When enabled (default, the shipped behaviour), each compute "
            "auto-generates the IFRS 3 acquisition elimination for every "
            "fully-configured full-method member: the parent's investment "
            "is removed against the subsidiary's acquisition-date equity, "
            "acquisition-date NCI is recognised on the member's NCI basis, "
            "and the residual is carried as goodwill (or a bargain-purchase "
            "credit). When disabled, no elimination is auto-generated and "
            "the run falls back to the diagnostic warning that the "
            "investment is not eliminated, leaving the entry to a manual "
            "elimination."
        ),
    )
    cta_gain_account_id = fields.Many2one(
        'account.account',
        string="CTA Recycling Gain Account",
        tracking=True,
        help=(
            "Income account credited when a member's accumulated "
            "translation GAIN is recycled from the CTA reserve to profit "
            "or loss on disposal of the member (IAS 21.48). A member whose "
            "CTA position link carries its own reclass gain account uses "
            "that instead."
        ),
    )
    cta_loss_account_id = fields.Many2one(
        'account.account',
        string="CTA Recycling Loss Account",
        tracking=True,
        help=(
            "Expense account debited when a member's accumulated "
            "translation LOSS is recycled from the CTA reserve to profit "
            "or loss on disposal of the member (IAS 21.48). A member whose "
            "CTA position link carries its own reclass loss account uses "
            "that instead."
        ),
    )

    auto_eliminate_intragroup = fields.Boolean(
        string="Auto-Eliminate Intragroup Balances",
        default=False,
        tracking=True,
        help=(
            "When enabled, each compute also generates automatic elimination "
            "run lines (IFRS 10.B86) for intragroup balances BETWEEN member "
            "companies, in addition to any manual elimination entries: "
            "reciprocal receivables versus payables, and sales income against "
            "the counterparty's purchases / cost of sales. Each auto "
            "elimination is a balanced pair that nets to zero, so the run "
            "still balances and the CTA is unaffected. When a reciprocal "
            "intragroup balance does not agree between the two companies, a "
            "diagnostic is surfaced rather than the difference being silently "
            "plugged. Default off: leaving it unchecked reproduces the prior "
            "behaviour exactly."
        ),
    )

    member_ids = fields.One2many(
        'eh.consol.member', 'entity_id', copy=True,
        help=(
            "Subsidiary companies in scope for the consolidation, "
            "with ownership percentage and consolidation method."
        ),
    )
    member_count = fields.Integer(
        compute='_compute_counts', store=False,
    )
    run_ids = fields.One2many(
        'eh.consol.run', 'entity_id', readonly=True,
    )
    run_count = fields.Integer(
        compute='_compute_counts', store=False,
    )

    active = fields.Boolean(
        default=True,
        help=(
            "Soft-archive flag. Inactive entities are hidden from "
            "the run-creation picker but existing runs stay readable."
        ),
    )
    notes = fields.Html()

    _sql_constraints = [
        ('unique_code', 'unique(code)', 'Consolidation entity code must be unique.'),
    ]

    @api.depends('member_ids', 'run_ids')
    def _compute_counts(self):
        for entity in self:
            entity.member_count = len(entity.member_ids)
            entity.run_count = len(entity.run_ids)

    @api.constrains('code')
    def _check_code_format(self):
        import re
        pattern = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]*$')
        for rec in self:
            if not pattern.match(rec.code or ''):
                raise ValidationError(_(
                    "Consolidation entity code must match "
                    "[a-zA-Z][a-zA-Z0-9_]* (got %r).",
                ) % rec.code)

    @api.constrains('consolidation_company_id', 'presentation_currency_id')
    def _check_consolidation_company_currency(self):
        """The consolidation ledger company currency must equal the
        presentation currency.

        The posted consolidation move books run-line amounts (which are in the
        presentation currency, per IAS 21) directly as debit/credit in the
        ledger company's own currency, without conversion. If the two differ
        the posted move would be in the wrong currency scale, so equality is
        required at configuration time rather than silently mis-scaling the
        books.
        """
        for rec in self:
            ledger = rec.consolidation_company_id
            if not ledger:
                continue
            if ledger.currency_id != rec.presentation_currency_id:
                raise ValidationError(_(
                    "The consolidation ledger company (%(company)s) uses "
                    "currency %(company_ccy)s, but the entity's presentation "
                    "currency is %(pres_ccy)s. The consolidation move is "
                    "posted directly in the presentation currency, so the "
                    "ledger company must use that same currency. Set the "
                    "ledger company currency to %(pres_ccy)s (or pick a "
                    "ledger company that already uses it).",
                    company=ledger.display_name,
                    company_ccy=ledger.currency_id.name,
                    pres_ccy=rec.presentation_currency_id.name,
                ))

    @api.constrains('consolidation_company_id', 'parent_company_id',
                    'member_ids')
    def _check_consolidation_company_dedicated(self):
        """The consolidation ledger company must be dedicated.

        Posting the consolidated move into the parent's or a member's
        operating company would double-count that company's own balances
        into the consolidated ledger, so the target must be a separate
        company reserved for consolidation.
        """
        for rec in self:
            ledger = rec.consolidation_company_id
            if not ledger:
                continue
            operating = rec.parent_company_id | rec.member_ids.mapped(
                'company_id')
            if ledger in operating:
                raise ValidationError(_(
                    "The consolidation ledger company (%s) must be a "
                    "dedicated company, not the parent company or any "
                    "member's operating company.",
                ) % ledger.display_name)

    def copy_data(self, default=None):
        # code is copy=False, required and unique, so a plain Duplicate
        # would leave it blank and fail both the required and the format
        # constraint. Generate a unique copy code instead.
        default = dict(default or {})
        vals_list = super().copy_data(default=default)
        if 'code' not in default:
            for entity, vals in zip(self, vals_list):
                vals['code'] = entity._eh_next_copy_code()
        return vals_list

    def _eh_next_copy_code(self):
        self.ensure_one()
        base = "%s_copy" % (self.code or 'entity')
        candidate, n = base, 1
        while self.sudo().with_context(active_test=False).search_count(
            [('code', '=', candidate)],
        ):
            n += 1
            candidate = "%s%d" % (base, n)
        return candidate

    def action_view_members(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Members"),
            'res_model': 'eh.consol.member',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('entity_id', '=', self.id)],
            'context': {'default_entity_id': self.id},
        }

    def action_view_runs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Runs"),
            'res_model': 'eh.consol.run',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('entity_id', '=', self.id)],
            'context': {'default_entity_id': self.id},
        }
