# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.benefit.plan: one defined benefit plan register (IAS 19).

Scope statement (also in the manifest): actuarial results are IMPORTED
INPUTS, never computed. The plan register holds the accounts and policy
choices the ledger mechanics need; the per-period actuarial figures live on
eh.benefit.valuation records.

Posting design decision (documented for the golden tests): the balance
sheet carries SEPARATE accounts for the defined benefit obligation (a
liability, credit balance equals the closing DBO) and for the plan assets
(an asset or contra-liability presentation account, debit balance equals
the closing plan assets net of the asset ceiling allowance). Each posted
valuation moves both accounts by the period movement, so the net defined
benefit liability/(asset) is the net of the two accounts at every date and
the IAS 19.140-141 movement schedules read straight off the ledger.

Contribution routing decision (contributions_posted_elsewhere):

* True (default): payroll normally pays the fund and books
  Dr contribution clearing / Cr bank when the cash leaves. The valuation
  entry then books Dr plan assets / Cr contribution clearing, closing the
  clearing loop without touching cash twice.
* False: no other system posts the payment, so the valuation entry credits
  the bank/clearing account directly (the "Cr cash/bank only if
  contributions posted here" case).

Funding status decision: a funded plan pays benefits and settlement
payments OUT OF PLAN ASSETS (no employer cash leg; the amounts cancel
inside the two movement deltas). An unfunded plan holds no assets at all
(opening assets, contributions and excess return are constrained to zero)
and the employer pays benefits and settlements directly, credited to the
benefit payment account.
"""

from odoo import _, api, fields, models  # noqa: F401
from odoo.exceptions import UserError


class EhBenefitPlan(models.Model):
    _name = 'eh.benefit.plan'
    _description = "Defined benefit plan (IAS 19)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'name, id'
    # Block a direct RPC write of state that would skip the activation /
    # closing gates (a closed plan cannot take new valuations; a plan must be
    # active before valuations post; closing refuses outstanding drafts). Only
    # the record's own actions, which run under sudo, may move state.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('active', "Active"), ('closed', "Closed")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    country_note = fields.Char(
        string="Country / Jurisdiction Note", tracking=True,
        help="Where the plan operates and under which law (free note; the "
             "module carries no regional logic).")
    funded = fields.Boolean(
        default=True, tracking=True, string="Funded Plan",
        help="Funded: the plan holds assets, and benefits and settlement "
             "payments are paid out of those assets. Unfunded: the plan "
             "holds no assets and the employer pays benefits directly "
             "(credited to the benefit payment account).")
    contributions_posted_elsewhere = fields.Boolean(
        default=True, tracking=True,
        string="Contributions Posted Elsewhere",
        help="Default True: payroll pays the fund and posts the cash leg "
             "(Dr contribution clearing / Cr bank); the valuation entry "
             "credits the contribution clearing account. Untick only when "
             "nothing else posts the payment, in which case the valuation "
             "entry credits the bank/clearing account directly.")

    # ---- accounts ----
    service_cost_account_id = fields.Many2one(
        'account.account', string="Service Cost Expense Account",
        tracking=True,
        domain="[('account_type', 'in', ['expense', 'expense_direct_cost'])]",
        help="P&L. Carries current service cost, past service cost "
             "(IAS 19.103) and settlement gains/losses (IAS 19.8 includes "
             "them in service cost).")
    net_interest_account_id = fields.Many2one(
        'account.account', string="Net Interest Expense Account",
        tracking=True,
        domain="[('account_type', 'in', ['expense', 'expense_direct_cost'])]",
        help="P&L. Net interest on the opening net defined benefit "
             "liability/(asset) at the discount rate (IAS 19.123). A net "
             "asset position produces net interest income (credit).")
    oci_account_id = fields.Many2one(
        'account.account', string="Remeasurement OCI Account", tracking=True,
        domain="[('account_type', '=', 'equity')]",
        help="Equity/OCI. Remeasurements (actuarial gains and losses on the "
             "obligation, excess return on plan assets, asset ceiling "
             "changes) go here and are NEVER reclassified to profit or "
             "loss (IAS 19.122).")
    dbo_account_id = fields.Many2one(
        'account.account', string="DBO Liability Account", tracking=True,
        domain="[('account_type', 'in', "
               "['liability_current', 'liability_non_current'])]",
        help="Balance sheet. Credit balance equals the closing defined "
             "benefit obligation.")
    plan_asset_account_id = fields.Many2one(
        'account.account', string="Plan Asset Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_current', 'asset_non_current', "
               "'liability_non_current'])]",
        help="Balance sheet, contra to the DBO account. Debit balance "
             "equals the closing plan assets net of the asset ceiling "
             "allowance (the ceiling effect is credited here so the "
             "recognised net position never exceeds the ceiling).")
    contribution_account_id = fields.Many2one(
        'account.account', string="Contribution Clearing / Bank Account",
        tracking=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'asset_current', 'liability_current'])]",
        help="Credited for employer contributions: the clearing account "
             "payroll settles when Contributions Posted Elsewhere is set, "
             "otherwise the bank/cash account paid from here.")
    benefit_payment_account_id = fields.Many2one(
        'account.account', string="Benefit Payment Account", tracking=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'asset_current', 'liability_current'])]",
        help="Unfunded plans only: credited when the employer pays benefits "
             "and settlement amounts directly (cash or a payment clearing "
             "account).")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general')]")

    valuation_ids = fields.One2many(
        'eh.benefit.valuation', 'plan_id', string="Valuations")
    valuation_count = fields.Integer(compute='_compute_valuation_count')

    # Latest posted position, for the register list and the disclosure feed.
    latest_closing_dbo = fields.Monetary(
        compute='_compute_latest_position', currency_field='currency_id',
        string="Closing DBO")
    latest_closing_assets = fields.Monetary(
        compute='_compute_latest_position', currency_field='currency_id',
        string="Closing Plan Assets")
    latest_net_liability = fields.Monetary(
        compute='_compute_latest_position', currency_field='currency_id',
        string="Net Liability / (Asset)")
    latest_recognised_position = fields.Monetary(
        compute='_compute_latest_position', currency_field='currency_id',
        string="Recognised Net Position",
        help="Net liability, or the recognised asset after the asset "
             "ceiling (negative = asset).")

    notes = fields.Text()

    # Accounts and policy flags freeze once a valuation has posted: changing
    # them mid-stream would break the account-level rollforward the
    # disclosures read from the ledger.
    _PLAN_FROZEN_FIELDS = (
        'funded', 'contributions_posted_elsewhere', 'company_id',
        'service_cost_account_id', 'net_interest_account_id',
        'oci_account_id', 'dbo_account_id', 'plan_asset_account_id',
        'contribution_account_id', 'benefit_payment_account_id',
        'journal_id',
    )

    def _compute_valuation_count(self):
        for plan in self:
            plan.valuation_count = len(plan.valuation_ids)

    def _posted_valuations(self):
        self.ensure_one()
        return self.valuation_ids.filtered(
            lambda v: v.state == 'posted').sorted(
            key=lambda v: (v.period_end, v.id))

    def _compute_latest_position(self):
        for plan in self:
            posted = plan._posted_valuations()
            latest = posted[-1] if posted else None
            plan.latest_closing_dbo = latest.closing_dbo if latest else 0.0
            plan.latest_closing_assets = (
                latest.closing_assets if latest else 0.0)
            plan.latest_net_liability = (
                latest.net_liability if latest else 0.0)
            plan.latest_recognised_position = (
                latest.recognised_net_position if latest else 0.0)

    def write(self, vals):
        frozen = [f for f in self._PLAN_FROZEN_FIELDS if f in vals]
        if frozen:
            locked = self.filtered(
                lambda p: any(v.state in ('posted', 'reversed')
                              for v in p.valuation_ids))
            if locked:
                raise UserError(_(
                    "Plan settings (%(fields)s) are frozen once a valuation "
                    "has posted on %(plans)s: the disclosure rollforward "
                    "reads these accounts off the ledger. Close the plan "
                    "and open a new one to change its structure.",
                    fields=', '.join(frozen),
                    plans=', '.join(locked.mapped('display_name'))))
        return super().write(vals)

    def unlink(self):
        with_valuations = self.filtered(lambda p: p.valuation_ids)
        if with_valuations:
            raise UserError(_(
                "A plan with valuations cannot be deleted (%s); its ledger "
                "chain would be orphaned. Close it instead.",
                ', '.join(with_valuations.mapped('display_name'))))
        return super().unlink()

    # ---- state machine ----

    def action_activate(self):
        self = self._eh_workflow_action()
        for plan in self:
            if plan.state != 'draft':
                raise UserError(_(
                    "Only a draft plan can be activated."))
            plan.state = 'active'
        return True

    def action_close(self):
        self = self._eh_workflow_action()
        for plan in self:
            if plan.state != 'active':
                raise UserError(_("Only an active plan can be closed."))
            open_valuations = plan.valuation_ids.filtered(
                lambda v: v.state == 'draft')
            if open_valuations:
                raise UserError(_(
                    "Post or delete the draft valuations of %s before "
                    "closing it.", plan.display_name))
            plan.state = 'closed'
        return True

    def action_reopen(self):
        self = self._eh_workflow_action()
        for plan in self:
            if plan.state != 'closed':
                raise UserError(_("Only a closed plan can be reopened."))
            plan.state = 'active'
        return True

    def action_view_valuations(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Valuations"),
            'res_model': 'eh.benefit.valuation',
            'view_mode': 'list,form',
            'domain': [('plan_id', '=', self.id)],
            'context': {'default_plan_id': self.id},
        }

    # ---- disclosure feed (IAS 19.140-141) ----

    def get_rollforward(self):
        """Movement schedules built straight from the posted valuations.

        Returns {'dbo': [...], 'assets': [...], 'ceiling': [...]} where each
        row is one posted period in chronological order. Because every
        posted valuation is (a) tie-checked against its own movement
        analysis and (b) opening-chained to the prior posted valuation, the
        schedule reconciles opening to closing per IAS 19.140-141 by
        construction; nothing here is typed in.
        """
        self.ensure_one()
        dbo_rows, asset_rows, ceiling_rows = [], [], []
        for v in self._posted_valuations():
            dbo_rows.append({
                'period_end': v.period_end,
                'opening': v.opening_dbo,
                'current_service_cost': v.current_service_cost,
                'past_service_cost': v.past_service_cost,
                'interest_cost': v.interest_cost,
                'benefits_paid': v.benefits_paid,
                'actuarial_loss_gain': v.actuarial_gain_loss_dbo,
                'settlements': v.settlement_dbo_released,
                'closing': v.closing_dbo,
            })
            asset_rows.append({
                'period_end': v.period_end,
                'opening': v.opening_assets,
                'interest_income': v.interest_income,
                'contributions_employer': v.contributions_employer,
                'benefits_paid': v.benefits_paid if v.plan_id.funded else 0.0,
                'return_excess': v.return_on_assets_excess,
                'settlements_paid': (
                    v.settlement_payment if v.plan_id.funded else 0.0),
                'closing': v.closing_assets,
            })
            ceiling_rows.append({
                'period_end': v.period_end,
                'opening_effect': v.opening_ceiling_effect,
                'change_in_effect': v.ceiling_effect_delta,
                'closing_effect': v.ceiling_effect,
                'recognised_asset': v.recognised_asset,
                'net_liability': v.net_liability,
                'recognised_net_position': v.recognised_net_position,
            })
        return {'dbo': dbo_rows, 'assets': asset_rows,
                'ceiling': ceiling_rows}
