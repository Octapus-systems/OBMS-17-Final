# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Automatic asset creation from posted vendor bills.

When a vendor bill is posted, every invoice line whose account carries an
asset category (eh_asset_category_id) with a creation mode other than 'no'
spawns a fixed asset from the category defaults. The created asset is
linked back on the line (eh_asset_id) so a reset-and-repost never
duplicates it.
"""

from odoo import _, fields, models
from odoo.exceptions import UserError


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    eh_asset_id = fields.Many2one(
        'eh.asset', string="Generated Asset", copy=False, readonly=True,
        help="Asset auto-created from this bill line, if any.",
    )


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        posted._eh_autocreate_assets()
        return posted

    def _eh_autocreate_assets(self):
        """Create assets for posted vendor-bill lines tagged for it."""
        Asset = self.env['eh.asset']
        for move in self:
            if move.move_type != 'in_invoice' or move.state != 'posted':
                continue
            for line in move.invoice_line_ids:
                account = line.account_id
                category = account.eh_asset_category_id
                if not category or account.eh_asset_auto == 'no':
                    continue
                if line.eh_asset_id:
                    continue
                cost = line.price_subtotal
                if cost <= 0:
                    continue
                in_service = move.invoice_date or move.date
                asset = Asset.create(self._eh_asset_vals_from_line(
                    move, line, category, cost, in_service))
                line.eh_asset_id = asset.id
                if account.eh_asset_auto == 'validate':
                    self._eh_try_validate_asset(asset)

    @staticmethod
    def _eh_asset_vals_from_line(move, line, category, cost, in_service):
        return {
            'name': '/',
            'category_id': category.id,
            'partner_id': move.partner_id.id,
            'invoice_id': move.id,
            'acquisition_date': in_service,
            'in_service_date': in_service,
            'acquisition_cost': cost,
            'method': category.method,
            'useful_life_months': category.useful_life_months,
            'salvage_value': category.salvage_rate * cost,
            'declining_factor': category.declining_factor,
            'prorate_first_period': category.prorate_first_period,
            'asset_account_id': category.asset_account_id.id,
            'depreciation_account_id': category.depreciation_account_id.id,
            'accumulated_depreciation_account_id': (
                category.accumulated_depreciation_account_id.id),
            'disposal_gain_account_id': category.disposal_gain_account_id.id,
            'disposal_loss_account_id': category.disposal_loss_account_id.id,
            'journal_id': category.journal_id.id,
            'company_id': move.company_id.id,
        }

    def _eh_try_validate_asset(self, asset):
        """Generate the schedule and start the asset, but never let an
        incomplete asset setup roll back the bill posting: on failure the
        asset is left in draft for the user to finish."""
        try:
            with self.env.cr.savepoint():
                asset.action_compute_schedule()
                asset.action_activate()
        except UserError as exc:
            asset.message_post(body=_(
                "Auto-validation skipped; asset left in draft: %s", exc))
