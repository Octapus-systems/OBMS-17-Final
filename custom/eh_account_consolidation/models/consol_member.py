# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.consol.member: a subsidiary company in a consolidation entity.

Holds the parent's ownership percentage (drives NCI computation),
the consolidation method (full / equity / proportional), and the
member's functional currency (drives IAS 21 translation).

Method semantics enforced by the run engine (consol_run.py):

* full: 100% of translated balances roll up; NCI carved for
  ownership below 100%, measured per nci_basis (proportionate share
  of equity, or fair value at acquisition plus the minority share of
  post-acquisition equity movement, IFRS 3.19).
* proportional: translation happens FIRST at full value, then every
  translated balance is scaled by ownership_pct / 100 before rollup,
  so FX translation is never distorted by the scaling. NCI does not
  exist under the proportional method (there is no minority in a
  proportionately consolidated interest), so NCI configuration is
  blocked by constraint and the carve is skipped.
* equity: not rolled up; IAS 28 one-line pick-up of the parent's
  share of the period result. Configuration (investment account +
  share-of-profit account) is mandatory unless the IAS 28.1A fair
  value option (fv_option) is elected, in which case no pick-up is
  booked and the run carries a memo disclosure line instead.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_METHOD_CHOICES = [
    ('full', "Full consolidation"),
    ('proportional', "Proportional"),
    ('equity', "Equity method"),
]


class EhConsolMember(models.Model):
    _name = 'eh.consol.member'
    _description = "Consolidation member company"
    _order = 'entity_id, sequence, id'
    _rec_name = 'company_id'

    entity_id = fields.Many2one(
        'eh.consol.entity', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)

    company_id = fields.Many2one(
        'res.company', required=True,
        help=(
            "Subsidiary company. Must be different from the parent "
            "company on the entity (the parent is consolidated by "
            "definition; a self-reference would double-count)."
        ),
    )
    ownership_pct = fields.Float(
        default=100.0, digits=(7, 4), required=True,
        help=(
            "Parent's ownership of this subsidiary, as a percentage "
            "from 0.0 to 100.0. Drives the non-controlling interest "
            "(NCI) split: NCI = subsidiary equity x (1 - "
            "ownership_pct / 100). 100% ownership produces zero "
            "NCI."
        ),
    )
    method = fields.Selection(
        _METHOD_CHOICES, default='full', required=True,
        help=(
            "Consolidation method. Full: 100% of subsidiary balances "
            "roll up, NCI carved out. Proportional: ownership% of "
            "balances roll up. Equity: subsidiary not rolled up; "
            "instead, parent's share of subsidiary equity carried "
            "as a single investment line."
        ),
    )

    functional_currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_functional_currency', store=True, readonly=False,
        help=(
            "Functional currency of the subsidiary. Defaults to the "
            "company's currency_id on the company record. Override "
            "when the functional currency under IAS 21 differs from "
            "the booking currency (rare)."
        ),
    )

    investment_account_id = fields.Many2one(
        'account.account',
        string="Investment Account",
        help=(
            "Account in the PARENT's books that carries the investment in "
            "this subsidiary. Used to detect whether the investment has "
            "been eliminated against the subsidiary's equity: an unposted "
            "elimination against this account means consolidated equity is "
            "still double-counted. Automatic IFRS 3 elimination is on the "
            "roadmap; for now the run warns when this is left unresolved."
        ),
    )
    investment_amount = fields.Monetary(
        currency_field='investment_currency_id',
        help=(
            "Carrying amount (cost) of the parent's investment in this "
            "subsidiary, in the presentation currency. Used by the "
            "investment-elimination entry."
        ),
    )
    investment_currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_investment_currency', store=True, readonly=True,
    )

    acquisition_equity = fields.Monetary(
        currency_field='investment_currency_id',
        help=(
            "Subsidiary's total book equity at the acquisition date, as a "
            "positive number. Stated in the presentation currency when no "
            "historical rate is set; when a historical rate is set it is "
            "stated in the subsidiary's functional currency and translated "
            "at that rate (IAS 21.23(b): non-monetary equity at the "
            "historical rate). This is the pre-acquisition equity removed by "
            "the IFRS 3 investment elimination (debited to the "
            "equity-elimination account). Set this together with the "
            "investment account and amount to have the run auto-eliminate "
            "the parent's investment against the sub's acquisition-date "
            "equity."
        ),
    )
    acquisition_date = fields.Date(
        help=(
            "Date control was obtained (IFRS 3 acquisition date). "
            "Documentation anchor for the acquisition-date equity and the "
            "historical rate used by the investment elimination."
        ),
    )
    historical_rate = fields.Float(
        digits=(16, 6), default=0.0,
        help=(
            "Optional acquisition-date (historical) exchange rate, quoted as "
            "presentation-currency units per one unit of the subsidiary's "
            "functional currency. When set above zero, acquisition_equity is "
            "read as a functional-currency figure and multiplied by this "
            "rate in the IFRS 3 investment elimination. Leave at zero when "
            "acquisition_equity is already stated in the presentation "
            "currency (previous behaviour, unchanged)."
        ),
    )
    nci_basis = fields.Selection(
        [
            ('proportionate', "Proportionate share of net assets"),
            ('fair_value', "Fair value (full goodwill)"),
        ],
        default='proportionate', required=True,
        help=(
            "IFRS 3.19 measurement basis for non-controlling interest. "
            "Proportionate: NCI = minority share of the subsidiary's "
            "identifiable net assets (partial goodwill). Fair value: NCI is "
            "recognised at its acquisition-date fair value (nci_fair_value), "
            "and goodwill includes the goodwill attributable to the minority "
            "(full goodwill). Post-acquisition, both bases attribute the "
            "minority share of the equity movement to NCI: NCI carrying "
            "amount = acquisition-date NCI (proportionate share or fair "
            "value) + (1 - ownership) x post-acquisition equity movement."
        ),
    )
    nci_fair_value = fields.Monetary(
        currency_field='investment_currency_id',
        help=(
            "Acquisition-date fair value of the non-controlling interest, in "
            "the presentation currency, as a positive number. Required when "
            "the NCI basis is fair value; ignored on the proportionate "
            "basis."
        ),
    )
    fv_option = fields.Boolean(
        string="IAS 28.1A Fair Value Option",
        default=False,
        help=(
            "Fair value election for an equity-method interest held by a "
            "venture capital organisation, mutual fund or similar entity "
            "(IAS 28.18-19 via IFRS 9). When set, the run books NO equity "
            "pick-up for this member: the investment is expected to be "
            "remeasured at fair value through profit or loss by the "
            "fair-value engine, and the run carries a memo disclosure line "
            "recording the election instead."
        ),
    )
    reporting_date_offset_months = fields.Integer(
        default=0,
        help=(
            "Gap in whole months between this member's own reporting date "
            "and the group's reporting date. IFRS 10.B92-B93 caps the "
            "difference at three months (with adjustment for significant "
            "transactions); a run blocks when any member exceeds three "
            "months unless the run-level override is set with a reason."
        ),
    )
    policy_aligned = fields.Boolean(
        default=True,
        help=(
            "Confirms this member's financial statements are prepared under "
            "uniform group accounting policies (IFRS 10.B87) or that "
            "conforming adjustments have been made. A run blocks while this "
            "is unchecked unless the run-level override is set with a "
            "reason."
        ),
    )
    cta_position_id = fields.Many2one(
        'eh.fx.cta.position', string="CTA Position",
        ondelete='set null',
        help=(
            "Optional link to the IAS 21 CTA reserve position "
            "(eh_account_fx_revaluation) tracking this foreign operation's "
            "cumulative translation reserve on the parent's books. The "
            "run's CTA lines for this member carry the link, and the "
            "member-disposal recycling logs against it. The position's own "
            "Dispose action remains the ledger-side reclassification; the "
            "run-side recycling here keeps the consolidated memo set "
            "aligned (IAS 21.48)."
        ),
    )
    disposal_pct = fields.Float(
        string="Disposal %", default=100.0, digits=(5, 2),
        help=(
            "Share of this member's accumulated CTA to recycle to profit or "
            "loss on the next Dispose action. 100 reclassifies the full "
            "remaining balance (IAS 21.48, loss of control); below 100 "
            "reclassifies the proportionate share (IAS 21.48A-C, partial "
            "disposal)."
        ),
    )
    goodwill_account_id = fields.Many2one(
        'account.account',
        string="Goodwill Account",
        help=(
            "Account (asset) on the consolidated chart carrying goodwill "
            "recognised on acquisition, being the excess of the investment "
            "cost over the parent's share of acquisition-date equity. A "
            "bargain-purchase gain (negative goodwill) is booked as a credit "
            "to this account. Used by the auto-elimination."
        ),
    )
    nci_account_id = fields.Many2one(
        'account.account',
        string="NCI Account",
        help=(
            "Equity account on the consolidated chart carrying the "
            "non-controlling interest recognised on acquisition (the "
            "minority's share of acquisition-date equity). Used by the "
            "auto-elimination."
        ),
    )
    equity_elimination_account_id = fields.Many2one(
        'account.account',
        string="Equity Elimination Account",
        help=(
            "Account to debit when the subsidiary's pre-acquisition equity is "
            "removed in the IFRS 3 investment elimination. Typically the "
            "subsidiary's share-capital / retained-earnings equity account on "
            "the consolidated chart."
        ),
    )
    share_of_profit_account_id = fields.Many2one(
        'account.account',
        string="Share of Profit Account",
        help=(
            "Income account on the consolidated chart credited with the "
            "parent's share of an equity-method associate's period profit "
            "(IAS 28). Paired with the investment account, which is debited "
            "by the same share so the equity pick-up balances."
        ),
    )

    consolidation_account_map = fields.Text(
        help=(
            "Optional mapping of subsidiary chart-of-account codes "
            "to consolidated chart-of-account codes, one per line, "
            "format 'sub_code -> consol_code'. Lines not listed map "
            "1:1 by code. Used when subsidiaries follow different "
            "local CoA conventions and need to be remapped to the "
            "group standard."
        ),
    )

    notes = fields.Char()

    _sql_constraints = [
        ('check_ownership', 'CHECK (ownership_pct >= 0 AND ownership_pct <= 100)', 'Ownership percentage must be between 0 and 100.'),  # noqa: E501
        ('unique_member', 'unique(entity_id, company_id)', 'Each company can appear at most once per consolidation entity.'),  # noqa: E501
    ]

    @api.depends('company_id')
    def _compute_functional_currency(self):
        for member in self:
            if not member.functional_currency_id and member.company_id:
                member.functional_currency_id = (
                    member.company_id.currency_id
                )

    @api.depends('entity_id.presentation_currency_id')
    def _compute_investment_currency(self):
        for member in self:
            member.investment_currency_id = (
                member.entity_id.presentation_currency_id
            )

    @api.constrains('company_id', 'entity_id')
    def _check_not_parent(self):
        for rec in self:
            if (
                rec.entity_id
                and rec.entity_id.parent_company_id == rec.company_id
            ):
                raise ValidationError(_(
                    "Member %s is the parent company of entity %s. "
                    "The parent is consolidated by definition; "
                    "remove it from the member list.",
                    rec.company_id.display_name,
                    rec.entity_id.display_name,
                ))

    @api.constrains('method', 'nci_basis', 'nci_account_id')
    def _check_proportional_no_nci(self):
        """NCI does not exist under the proportional method: only the
        parent's share of each balance rolls up, so there is no minority
        interest left in the consolidated set to carve. Configuring an NCI
        account or a fair-value NCI basis on a proportional member is
        therefore a contradiction and is blocked here (the run engine also
        never books NCI for proportional members)."""
        for rec in self:
            if rec.method != 'proportional':
                continue
            if rec.nci_account_id or rec.nci_basis == 'fair_value':
                raise ValidationError(_(
                    "Member %s uses the proportional method: only the "
                    "parent's share of each balance rolls up, so no "
                    "non-controlling interest exists. Remove the NCI "
                    "account / fair-value NCI basis, or switch the member "
                    "to full consolidation.",
                    rec.company_id.display_name,
                ))

    @api.constrains('nci_basis', 'nci_fair_value', 'method',
                    'ownership_pct', 'acquisition_equity')
    def _check_fair_value_nci_config(self):
        """The fair-value NCI basis (IFRS 3.19 full goodwill) needs an
        acquisition-date anchor: the FV of the minority at acquisition and
        the acquisition-date equity the post-acquisition movement is
        measured against. It is only meaningful on a full-method member
        with a genuine minority (ownership below 100%)."""
        for rec in self:
            if rec.nci_basis != 'fair_value':
                continue
            if rec.method != 'full':
                raise ValidationError(_(
                    "Member %s: the fair-value NCI basis applies to full "
                    "consolidation only.",
                    rec.company_id.display_name,
                ))
            if rec.ownership_pct >= 100.0:
                raise ValidationError(_(
                    "Member %s: a wholly-owned subsidiary has no "
                    "non-controlling interest, so the fair-value NCI basis "
                    "does not apply at 100%% ownership.",
                    rec.company_id.display_name,
                ))
            if not rec.nci_fair_value or rec.nci_fair_value <= 0.0:
                raise ValidationError(_(
                    "Member %s: the fair-value NCI basis requires a "
                    "positive acquisition-date NCI fair value.",
                    rec.company_id.display_name,
                ))
            if not rec.acquisition_equity or rec.acquisition_equity <= 0.0:
                raise ValidationError(_(
                    "Member %s: the fair-value NCI basis requires the "
                    "acquisition-date equity, so the post-acquisition "
                    "movement attributed to the minority can be measured.",
                    rec.company_id.display_name,
                ))

    @api.constrains('fv_option', 'method')
    def _check_fv_option_equity_only(self):
        for rec in self:
            if rec.fv_option and rec.method != 'equity':
                raise ValidationError(_(
                    "Member %s: the IAS 28.1A fair value option applies to "
                    "equity-method interests only.",
                    rec.company_id.display_name,
                ))

    @api.constrains('reporting_date_offset_months')
    def _check_offset_non_negative(self):
        for rec in self:
            if rec.reporting_date_offset_months < 0:
                raise ValidationError(_(
                    "The reporting date offset cannot be negative.",
                ))

    @api.constrains('historical_rate')
    def _check_historical_rate(self):
        for rec in self:
            if rec.historical_rate < 0.0:
                raise ValidationError(_(
                    "The historical rate cannot be negative.",
                ))

    # ---- IAS 21.48 disposal recycling ----

    def action_dispose_member(self):
        """Recycle this member's accumulated CTA to profit or loss on
        disposal (IAS 21.48; partial disposal IAS 21.48A-C).

        Manager-gated. Targets the entity's most recent computed / reviewed
        run and books a balanced pair of kind='cta_recycle' run lines:

          reclass = remaining member CTA balance x disposal_pct / 100

          Dr CTA equity account   (removes the reserve slice)
          Cr FX gain account      (accumulated gain, credit-negative CTA)

        or the mirrored pair through the FX loss account when the
        accumulated CTA is a loss (debit-positive). The remaining balance
        is the member's kind='cta' lines net of any prior recycle legs on
        the CTA account, so repeated partial disposals never recycle more
        than the accumulated reserve. Delegated to the run engine so the
        lines carry the engine context flag.
        """
        for member in self:
            member._eh_dispose_cta(member.disposal_pct)
        return True

    def _eh_dispose_cta(self, pct):
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only a consolidation manager can dispose a member and "
                "recycle its translation reserve.",
            ))
        if pct is None or pct <= 0.0 or pct > 100.0:
            raise UserError(_(
                "Disposal %% must be greater than 0 and at most 100. "
                "Got %.2f.",
            ) % (pct or 0.0))
        run = self.env['eh.consol.run'].search([
            ('entity_id', '=', self.entity_id.id),
            ('state', 'in', ('computed', 'reviewed')),
        ], order='period_to desc, id desc', limit=1)
        if not run:
            raise UserError(_(
                "No computed or reviewed consolidation run exists for "
                "entity %s; compute a run before recycling the member's "
                "CTA on disposal.",
                self.entity_id.display_name,
            ))
        return run._eh_recycle_member_cta(self, pct)
