# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Asset disposal wizard.

Posts the disposal entry (IAS 16.67-72 derecognition):

  Dr Cash / Receivable        proceeds
  Dr Accumulated depreciation total_depreciated
  Dr Accumulated impairment   net posted impairment (per contra account)
  Dr Loss (or Cr Gain)        balancing
  Cr Asset                    acquisition_cost

Both contra balances (accumulated depreciation AND accumulated impairment)
are removed so no impairment is stranded on the balance sheet after the
asset is gone, and the gain / loss is measured against the true carrying
amount (cost less depreciation less impairment), which is exactly the
figure shown on the wizard before posting.

On disposal, any remaining revaluation surplus (IAS 16.41) is recycled
directly to retained earnings, never through P&L, as an extra pair of
equity legs that net to zero within the same balanced disposal move:

  Dr Revaluation Reserve   revaluation_surplus
  Cr Retained Earnings     revaluation_surplus

The asset's revaluation_surplus is zeroed after posting.

Marks remaining (unposted) schedule lines as cancelled by removing them,
and moves the asset to the disposed state.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhAssetDisposeWizard(models.TransientModel):
    _name = 'eh.asset.dispose.wizard'
    _description = "Asset Disposal Wizard"

    asset_id = fields.Many2one(
        'eh.asset', required=True, ondelete='cascade',
    )
    disposal_date = fields.Date(
        required=True, default=fields.Date.context_today,
    )
    proceeds = fields.Monetary(default=0.0)
    partner_id = fields.Many2one('res.partner', string="Buyer / Counterparty")
    cash_account_id = fields.Many2one(
        'account.account', string="Proceeds Account",
        domain="[('account_type', 'in', "
               "['asset_cash', 'asset_receivable', 'asset_current'])]",
        help="Where to debit the proceeds. Required if proceeds > 0.",
    )
    revaluation_reserve_account_id = fields.Many2one(
        'account.account', string="Revaluation Reserve Account",
        help="Equity revaluation reserve to debit when recycling any "
             "remaining revaluation surplus on disposal (IAS 16.41). "
             "Required only when the asset still carries a surplus.",
    )
    retained_earnings_account_id = fields.Many2one(
        'account.account', string="Retained Earnings Account",
        help="Equity retained-earnings account to credit with the recycled "
             "revaluation surplus on disposal (IAS 16.41). The transfer is "
             "made directly within equity, never through P&L. Required only "
             "when the asset still carries a surplus.",
    )
    notes = fields.Text()

    revaluation_surplus = fields.Monetary(
        related='asset_id.revaluation_surplus', readonly=True,
        help="Remaining revaluation surplus that will be recycled to "
             "retained earnings on disposal.",
    )

    currency_id = fields.Many2one(
        related='asset_id.currency_id', readonly=True,
    )
    company_id = fields.Many2one(
        related='asset_id.company_id', readonly=True,
    )
    nbv = fields.Monetary(
        compute='_compute_nbv', readonly=True,
        help="Net book value of the asset right now.",
    )
    expected_gain_loss = fields.Monetary(
        compute='_compute_gain_loss', readonly=True,
    )

    @api.depends('asset_id')
    def _compute_nbv(self):
        for w in self:
            w.nbv = w._eh_carrying_amount() if w.asset_id else 0.0

    @api.depends('asset_id', 'proceeds')
    def _compute_gain_loss(self):
        for w in self:
            w.expected_gain_loss = (w.proceeds or 0.0) - (w.nbv or 0.0)

    def _eh_posted_impairment_by_account(self):
        """Net posted impairment (charges minus reversals) grouped by the
        contra account each event was booked to.

        Impairment is credited to accumulated_account_id when set, otherwise
        to the asset's accumulated_depreciation_account_id (the same fallback
        the impairment posting uses). Grouping by account lets disposal debit
        back exactly what is sitting in each contra account, so nothing is
        stranded regardless of how the charges were configured. Only posted
        events are considered: they are the ones actually in the ledger.
        """
        self.ensure_one()
        asset = self.asset_id
        by_account = {}
        for imp in asset.impairment_ids.filtered(lambda i: i.state == 'posted'):
            contra = (imp.accumulated_account_id
                      or asset.accumulated_depreciation_account_id)
            signed = -imp.amount if imp.is_reversal else imp.amount
            by_account[contra] = by_account.get(contra, 0.0) + signed
        return by_account

    def _eh_carrying_amount(self):
        """True carrying amount: cost less accumulated depreciation less
        net posted impairment. This is the basis for the disposal gain/loss
        and is the figure the wizard displays, so the posted result matches
        what the approving manager was shown."""
        self.ensure_one()
        asset = self.asset_id
        net_impairment = sum(self._eh_posted_impairment_by_account().values())
        carrying = (
            asset.acquisition_cost
            + asset.revaluation_adjustment
            - asset.total_depreciated
            - net_impairment
        )
        return asset.currency_id.round(carrying) if asset.currency_id else carrying

    def action_dispose(self):
        self.ensure_one()
        asset = self.asset_id
        if asset.state == 'disposed':
            raise UserError(_("Asset is already disposed."))
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_(
                "Only accounting managers can dispose of assets.",
            ))
        if (self.proceeds or 0.0) > 0 and not self.cash_account_id:
            raise UserError(_(
                "Provide a proceeds account when proceeds is greater than zero.",
            ))
        asset._validate_posting_setup()

        accumulated = asset.total_depreciated
        cost = asset.acquisition_cost
        proceeds = self.proceeds or 0.0
        impairment_by_account = self._eh_posted_impairment_by_account()
        nbv = self._eh_carrying_amount()
        currency = asset.currency_id
        gain_loss = currency.round(proceeds - nbv) if currency else proceeds - nbv

        lines = []
        if proceeds > 0:
            lines.append((0, 0, {
                'name': _("Disposal proceeds %s", asset.display_name),
                'account_id': self.cash_account_id.id,
                'partner_id': self.partner_id.id if self.partner_id else False,
                'debit': proceeds,
                'credit': 0.0,
            }))
        if accumulated > 0:
            lines.append((0, 0, {
                'name': _("Accumulated depreciation reversal %s", asset.display_name),
                'account_id': asset.accumulated_depreciation_account_id.id,
                'debit': accumulated,
                'credit': 0.0,
            }))
        # Derecognise accumulated impairment against the same contra
        # account(s) it was charged to, so no impairment lingers on the
        # balance sheet after the asset leaves. Normal case is a debit
        # (net charge); a net reversal balance flips to a credit.
        for contra_account, net in impairment_by_account.items():
            net = currency.round(net) if currency else net
            if not net:
                continue
            lines.append((0, 0, {
                'name': _("Accumulated impairment reversal %s", asset.display_name),
                'account_id': contra_account.id,
                'debit': net if net > 0 else 0.0,
                'credit': -net if net < 0 else 0.0,
            }))
        if gain_loss > 0:
            if not asset.disposal_gain_account_id:
                raise UserError(_(
                    "Asset gain on disposal: configure a Disposal Gain "
                    "account on the asset or its category.",
                ))
            lines.append((0, 0, {
                'name': _("Gain on disposal %s", asset.display_name),
                'account_id': asset.disposal_gain_account_id.id,
                'debit': 0.0,
                'credit': gain_loss,
            }))
        elif gain_loss < 0:
            if not asset.disposal_loss_account_id:
                raise UserError(_(
                    "Asset loss on disposal: configure a Disposal Loss "
                    "account on the asset or its category.",
                ))
            lines.append((0, 0, {
                'name': _("Loss on disposal %s", asset.display_name),
                'account_id': asset.disposal_loss_account_id.id,
                'debit': abs(gain_loss),
                'credit': 0.0,
            }))
        if asset.asset_account_id:
            # Derecognise the full gross carrying on the asset account: the
            # original cost plus any revaluation adjustment that a prior
            # uplift/downward revaluation posted to this account. Crediting
            # only cost would strand the revaluation on the balance sheet.
            gross_asset = currency.round(cost + asset.revaluation_adjustment) \
                if currency else cost + asset.revaluation_adjustment
            lines.append((0, 0, {
                'name': _("Asset cost reversal %s", asset.display_name),
                'account_id': asset.asset_account_id.id,
                'debit': 0.0 if gross_asset >= 0 else -gross_asset,
                'credit': gross_asset if gross_asset >= 0 else 0.0,
            }))
        else:
            raise UserError(_(
                "Asset is missing the Asset Account; cannot reverse the "
                "capitalised cost on disposal.",
            ))

        # IAS 16.41: recycle any remaining revaluation surplus directly to
        # retained earnings, NOT through P&L. These two legs net to zero
        # within equity, so they leave the disposal move balanced and do not
        # disturb the gain/loss measurement above. Assets that never carried
        # a surplus (revaluation_surplus == 0) get no extra legs and behave
        # exactly as before.
        surplus = currency.round(asset.revaluation_surplus) if currency \
            else asset.revaluation_surplus
        if surplus:
            if not self.revaluation_reserve_account_id \
                    or not self.retained_earnings_account_id:
                raise UserError(_(
                    "Asset %(asset)s carries a revaluation surplus of "
                    "%(amt).2f that must be recycled to retained earnings on "
                    "disposal (IAS 16.41). Provide both a Revaluation Reserve "
                    "account and a Retained Earnings account.",
                    asset=asset.display_name, amt=surplus,
                ))
            lines.append((0, 0, {
                'name': _("Revaluation surplus recycle %s", asset.display_name),
                'account_id': self.revaluation_reserve_account_id.id,
                'debit': surplus if surplus > 0 else 0.0,
                'credit': -surplus if surplus < 0 else 0.0,
            }))
            lines.append((0, 0, {
                'name': _(
                    "Retained earnings from surplus %s", asset.display_name,
                ),
                'account_id': self.retained_earnings_account_id.id,
                'debit': -surplus if surplus < 0 else 0.0,
                'credit': surplus if surplus > 0 else 0.0,
            }))

        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': self.disposal_date,
            'journal_id': asset.journal_id.id,
            'ref': _("Disposal %s", asset.display_name),
            'line_ids': lines,
        })
        move.action_post()

        # Drop unposted schedule lines.
        unposted = asset.depreciation_line_ids.filtered(lambda l: not l.is_posted)
        unposted.unlink()

        disposal_vals = {
            'state': 'disposed',
            'disposed_at': fields.Datetime.now(),
            'disposed_by_id': self.env.user.id,
            'disposal_date': self.disposal_date,
            'disposal_proceeds': proceeds,
            'disposal_partner_id': self.partner_id.id if self.partner_id else False,
            'disposal_move_id': move.id,
        }
        # Surplus has been recycled to retained earnings; zero it so no
        # revaluation reserve is stranded against a disposed asset.
        if surplus:
            disposal_vals['revaluation_surplus'] = 0.0
        asset._eh_workflow_write(disposal_vals)
        if self.notes:
            asset.message_post(body=_("Disposal notes: %s", self.notes))
        return {'type': 'ir.actions.act_window_close'}
