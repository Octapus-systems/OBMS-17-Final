# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Subsequent accounting for a recognised business combination.

* eh.bizcombo.adjustment: a measurement-period adjustment (IFRS 3.45-49).
  New information about facts and circumstances that existed at the
  acquisition date, obtained within 12 months of it, restates the
  provisional amounts retrospectively: the stepped asset or liability moves
  against goodwill (with any deferred-tax and proportionate-NCI knock-on),
  and the combination's recognised amounts are updated to the revised
  figures. Frozen once applied; blocked after 12 months or once the
  measurement period is closed.

* eh.bizcombo.contingent.remeasure: a subsequent fair-value remeasurement of
  liability-classified contingent consideration, recognised in profit or
  loss (IFRS 3.58a). Equity-classified contingent consideration is never
  remeasured (IFRS 3.58b), enforced by constraint.
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhBizcomboAdjustment(models.Model):
    _name = 'eh.bizcombo.adjustment'
    _description = "Measurement-period adjustment (IFRS 3.45-49)"
    _inherit = ['mail.thread', 'eh.workflow.guard']
    _order = 'date, id'
    _rec_name = 'name'

    # State advances only through this record's own actions (run under sudo),
    # never a direct RPC write past action_apply and its posted entry.
    _eh_guarded_fields = ('state',)

    combination_id = fields.Many2one(
        'eh.business.combination', required=True, ondelete='cascade',
        index=True)
    company_id = fields.Many2one(
        related='combination_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='combination_id.currency_id', store=True, readonly=True)
    name = fields.Char(
        required=True, string="New Information",
        help="The new information about facts and circumstances that "
             "existed at the acquisition date which drives this adjustment "
             "(IFRS 3.45); disclosed under IFRS 3.B67(a).")
    date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Date the new information was obtained. Must fall within the "
             "measurement period: at most 12 months after the acquisition "
             "date (IFRS 3.45).")
    state = fields.Selection(
        [('draft', "Draft"), ('applied', "Applied"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)
    line_ids = fields.One2many(
        'eh.bizcombo.adjustment.line', 'adjustment_id', string="Line Deltas",
        help="Provisional identifiable asset or liability lines restated to "
             "a revised fair value (IFRS 3.46).")
    consideration_delta = fields.Monetary(
        currency_field='currency_id',
        help="Change to the consideration transferred arising from the new "
             "information (for example a measurement-period true-up of the "
             "acquisition-date fair value of contingent consideration, "
             "IFRS 3.58a). Positive raises goodwill.")
    nci_delta = fields.Monetary(
        currency_field='currency_id',
        help="Change to the fair-value-basis non-controlling interest. Only "
             "for combinations measuring NCI at fair value; under the "
             "proportionate basis NCI re-derives from the restated net "
             "assets automatically.")
    goodwill_delta = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Net movement posted against goodwill by this adjustment; the "
             "retrospective restatement disclosed under IFRS 3.49/B67(a).")
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='set null')
    notes = fields.Text()

    @api.constrains('date', 'combination_id')
    def _check_measurement_period(self):
        """The measurement period cannot exceed one year (IFRS 3.45)."""
        for adj in self:
            acquisition = adj.combination_id.acquisition_date
            if not adj.date or not acquisition:
                continue
            if adj.date < acquisition:
                raise ValidationError(_(
                    "A measurement-period adjustment cannot be dated before "
                    "the acquisition date."))
            if adj.date > acquisition + relativedelta(months=12):
                raise ValidationError(_(
                    "The measurement period ends at most 12 months after "
                    "the acquisition date (IFRS 3.45); new information "
                    "obtained after %s is accounted for prospectively, not "
                    "as a measurement-period adjustment.",
                    acquisition + relativedelta(months=12)))

    def write(self, vals):
        for adj in self:
            if adj.state == 'applied':
                raise UserError(_(
                    "Measurement-period adjustment %s is applied and "
                    "posted; it cannot be changed.", adj.display_name))
        return super().write(vals)

    def unlink(self):
        for adj in self:
            if adj.state == 'applied':
                raise UserError(_(
                    "Measurement-period adjustment %s is applied and "
                    "posted; it cannot be deleted.", adj.display_name))
        return super().unlink()

    def action_cancel(self):
        self = self._eh_workflow_action()
        for adj in self:
            if adj.state != 'draft':
                raise UserError(_(
                    "Only a draft adjustment can be cancelled."))
            adj.state = 'cancelled'
        return True

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move', 'res_id': self.move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }

    def action_apply(self):
        """Post the retrospective restatement and update the combination.

        Recomputes each goodwill component (identifiable net assets from the
        revised line fair values, deferred tax on the revised step-up,
        proportionate NCI from the restated net assets, consideration and
        fair-value NCI from the scalar deltas), posts one balanced entry for
        the differences with goodwill absorbing the residual (IFRS 3.48),
        then writes the revised amounts onto the frozen combination under
        the restatement context. Balanced by construction because the
        goodwill delta is derived from the same rounded components as the
        other legs.
        """
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can apply a "
                "measurement-period adjustment."))
        # Run the state transition as su so the guarded 'state' write is
        # accepted; env.user (checked above) is preserved for audit stamps.
        self = self._eh_workflow_action()
        if self.state != 'draft':
            raise UserError(_("Only a draft adjustment can be applied."))
        combo = self.combination_id
        currency = combo.currency_id
        if combo.state != 'recognised':
            raise UserError(_(
                "Measurement-period adjustments restate a recognised "
                "combination; recognise %s first.", combo.display_name))
        if combo.measurement_period_closed:
            raise UserError(_(
                "The measurement period of %s is closed (IFRS 3.45); "
                "account for the new information prospectively instead.",
                combo.display_name))
        if self.date > combo.measurement_period_end:
            raise UserError(_(
                "The measurement period of %s ended on %s (IFRS 3.45); "
                "this adjustment is dated after it.",
                combo.display_name, combo.measurement_period_end))
        if not combo.journal_id:
            raise UserError(_("Configure the journal on the combination."))
        if not combo.goodwill_account_id:
            raise UserError(_(
                "Configure the goodwill account on the combination."))
        if not currency.is_zero(combo.bargain_purchase_gain):
            raise UserError(_(
                "%s was recognised as a bargain purchase; restating it "
                "through goodwill is not supported. Reverse and rebook the "
                "combination instead.", combo.display_name))
        if (not self.line_ids
                and currency.is_zero(self.consideration_delta)
                and currency.is_zero(self.nci_delta)):
            raise UserError(_(
                "Enter at least one revised line or a scalar delta; there "
                "is nothing to apply."))
        if combo.nci_measurement == 'proportionate' \
                and not currency.is_zero(self.nci_delta):
            raise UserError(_(
                "Under the proportionate basis the non-controlling interest "
                "re-derives from the restated net assets (IFRS 3.19); leave "
                "the NCI delta nil."))

        # ---- recompute every goodwill component with the revisions ----
        revisions = {
            line.asset_line_id.id: currency.round(line.revised_fair_value)
            for line in self.line_ids}
        if combo.asset_line_ids:
            fina_new = 0.0
            step_up_new = 0.0
            for asset in combo.asset_line_ids:
                fair_value = revisions.get(asset.id, asset.fair_value)
                fina_new += -fair_value if asset.is_liability else fair_value
                diff = fair_value - asset.tax_base
                step_up_new += -diff if asset.is_liability else diff
            fina_new = currency.round(fina_new)
            deferred_tax_new = currency.round(
                currency.round(step_up_new) * (combo.tax_rate / 100.0))
        else:
            # Plug-style combination: only scalar deltas can apply.
            fina_new = combo.fv_identifiable_net_assets
            deferred_tax_new = combo.deferred_tax
        deferred_tax_delta = currency.round(
            deferred_tax_new - combo.deferred_tax)
        if not currency.is_zero(deferred_tax_delta) \
                and not combo.deferred_tax_account_id:
            raise UserError(_(
                "Configure the deferred tax account on the combination; the "
                "revised step-up changes the deferred tax."))
        if combo.nci_measurement == 'proportionate':
            # IFRS 3.19: proportionate NCI is a share of the recognised net
            # assets, i.e. net of the IAS 12.19 deferred tax on the step-up.
            # Mirror the stored _compute_nci_amount so the restated figure and
            # the posted NCI leg agree (and goodwill_new stays balanced).
            nci_new = currency.round(
                (fina_new - deferred_tax_new) * (combo.nci_pct / 100.0))
        else:
            nci_new = currency.round(combo.nci_amount + self.nci_delta)
        nci_delta = currency.round(nci_new - combo.nci_amount)
        if not currency.is_zero(nci_delta) and not combo.nci_account_id:
            raise UserError(_(
                "Configure the non-controlling interest account on the "
                "combination."))
        consideration_new = currency.round(
            combo.consideration_transferred + self.consideration_delta)
        consideration_delta = currency.round(
            consideration_new - combo.consideration_transferred)
        if not currency.is_zero(consideration_delta) \
                and not combo.clearing_account_id:
            raise UserError(_(
                "Configure the acquisition clearing account on the "
                "combination."))
        goodwill_new = combo._goodwill_raw(
            consideration_new, nci_new, fina_new, deferred_tax_new)
        if currency.compare_amounts(goodwill_new, 0.0) < 0:
            raise UserError(_(
                "This adjustment would turn the remaining goodwill "
                "negative (a bargain purchase); restating goodwill below "
                "nil is not supported. Reverse and rebook the combination "
                "instead."))
        goodwill_delta = currency.round(goodwill_new - combo.goodwill)

        # ---- build the balanced restatement entry (IFRS 3.48) ----
        legs = []
        for line in self.line_ids:
            asset = line.asset_line_id
            delta = currency.round(
                revisions[asset.id] - asset.fair_value)
            if currency.is_zero(delta):
                continue
            label = _("Measurement-period restatement %s", asset.name)
            debit_side = (delta > 0) != asset.is_liability
            amount = abs(delta)
            if debit_side:
                legs.append((asset.account_id, amount, 0.0, label))
            else:
                legs.append((asset.account_id, 0.0, amount, label))
        if not currency.is_zero(deferred_tax_delta):
            label = _("Deferred tax on restated step-up %s", combo.name)
            if deferred_tax_delta > 0:
                legs.append((combo.deferred_tax_account_id, 0.0,
                             deferred_tax_delta, label))
            else:
                legs.append((combo.deferred_tax_account_id,
                             -deferred_tax_delta, 0.0, label))
        if not currency.is_zero(nci_delta):
            label = _("Non-controlling interest restated %s", combo.name)
            if nci_delta > 0:
                legs.append((combo.nci_account_id, 0.0, nci_delta, label))
            else:
                legs.append((combo.nci_account_id, -nci_delta, 0.0, label))
        if not currency.is_zero(consideration_delta):
            label = _("Consideration restated %s", combo.name)
            if consideration_delta > 0:
                legs.append((combo.clearing_account_id, 0.0,
                             consideration_delta, label))
            else:
                legs.append((combo.clearing_account_id,
                             -consideration_delta, 0.0, label))
        if not currency.is_zero(goodwill_delta):
            label = _("Goodwill restated %s", combo.name)
            if goodwill_delta > 0:
                legs.append((combo.goodwill_account_id, goodwill_delta, 0.0,
                             label))
            else:
                legs.append((combo.goodwill_account_id, 0.0,
                             -goodwill_delta, label))
        if not legs:
            raise UserError(_(
                "Every revised amount equals the recognised amount; there "
                "is nothing to apply."))
        move = self._post_move(legs)

        # ---- restate the recognised amounts on the frozen combination ----
        # Capture each line's pre-restatement fair value for the IFRS 3.B67
        # disclosure before the stored amount moves (the adjustment is still
        # draft here, so its own freeze does not bite yet).
        for line in self.line_ids:
            line.previous_fair_value = line.asset_line_id.fair_value
        for line in self.line_ids:
            line.asset_line_id.with_context(
                eh_mp_restate=True).fair_value = revisions[
                    line.asset_line_id.id]
        combo_vals = {}
        if not currency.is_zero(consideration_delta):
            combo_vals['consideration_transferred'] = consideration_new
        if combo.nci_measurement != 'proportionate' \
                and not currency.is_zero(nci_delta):
            combo_vals['nci_amount'] = nci_new
        if combo_vals:
            combo.with_context(eh_mp_restate=True).write(combo_vals)
        self.write({
            'state': 'applied',
            'move_id': move.id,
            'goodwill_delta': goodwill_delta,
        })
        return True

    def _post_move(self, legs):
        self.ensure_one()
        combo = self.combination_id
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': self.date,
            'journal_id': combo.journal_id.id,
            'ref': _("%s measurement-period adjustment", combo.name),
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class EhBizcomboAdjustmentLine(models.Model):
    _name = 'eh.bizcombo.adjustment.line'
    _description = "Measurement-period line delta (IFRS 3.46)"
    _order = 'id'

    adjustment_id = fields.Many2one(
        'eh.bizcombo.adjustment', required=True, ondelete='cascade',
        index=True)
    company_id = fields.Many2one(
        related='adjustment_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='adjustment_id.currency_id', store=True, readonly=True)
    asset_line_id = fields.Many2one(
        'eh.business.combination.asset', required=True, ondelete='restrict',
        string="Identifiable Line",
        help="The provisional identifiable asset or liability line being "
             "restated.")
    current_fair_value = fields.Monetary(
        related='asset_line_id.fair_value', string="Recognised Fair Value",
        readonly=True)
    previous_fair_value = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Fair value recognised before this adjustment was applied "
             "(captured on apply, for the IFRS 3.B67(a) disclosure).")
    revised_fair_value = fields.Monetary(
        currency_field='currency_id', required=True,
        help="Revised acquisition-date fair value per the new information "
             "(IFRS 3.46).")
    delta = fields.Monetary(
        compute='_compute_delta', currency_field='currency_id',
        help="Revised fair value less the fair value it replaces.")

    @api.depends('revised_fair_value', 'previous_fair_value',
                 'asset_line_id.fair_value', 'adjustment_id.state')
    def _compute_delta(self):
        for line in self:
            base = (line.previous_fair_value
                    if line.adjustment_id.state == 'applied'
                    else line.asset_line_id.fair_value)
            raw = line.revised_fair_value - base
            line.delta = (
                line.currency_id.round(raw) if line.currency_id else raw)

    @api.constrains('asset_line_id', 'adjustment_id')
    def _check_lines(self):
        for line in self:
            if (line.asset_line_id.combination_id
                    != line.adjustment_id.combination_id):
                raise ValidationError(_(
                    "The restated line must belong to the adjustment's "
                    "combination."))
        for adjustment in self.mapped('adjustment_id'):
            targets = adjustment.line_ids.mapped('asset_line_id')
            if len(targets) != len(adjustment.line_ids):
                raise ValidationError(_(
                    "An identifiable line can only be restated once per "
                    "adjustment."))

    def _check_not_applied(self, action):
        for line in self:
            if line.adjustment_id.state == 'applied':
                raise UserError(_(
                    "Measurement-period adjustment %s is applied and "
                    "posted; its lines cannot be %s.",
                    line.adjustment_id.display_name, action))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            adjustment = self.env['eh.bizcombo.adjustment'].browse(
                vals.get('adjustment_id'))
            if adjustment and adjustment.state == 'applied':
                raise UserError(_(
                    "Measurement-period adjustment %s is applied and "
                    "posted; lines cannot be added.",
                    adjustment.display_name))
        return super().create(vals_list)

    def write(self, vals):
        self._check_not_applied(_("changed"))
        return super().write(vals)

    def unlink(self):
        self._check_not_applied(_("deleted"))
        return super().unlink()


class EhBizcomboContingentRemeasure(models.Model):
    _name = 'eh.bizcombo.contingent.remeasure'
    _description = "Contingent consideration remeasurement (IFRS 3.58)"
    _inherit = ['mail.thread', 'eh.workflow.guard']
    _order = 'date, id'
    _rec_name = 'date'

    # State advances only through this record's own actions (run under sudo),
    # never a direct RPC write past action_apply and its posted entry.
    _eh_guarded_fields = ('state',)

    combination_id = fields.Many2one(
        'eh.business.combination', required=True, ondelete='cascade',
        index=True)
    company_id = fields.Many2one(
        related='combination_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='combination_id.currency_id', store=True, readonly=True)
    date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('applied', "Applied"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)
    new_fair_value = fields.Monetary(
        currency_field='currency_id', required=True,
        help="Fair value of the contingent consideration liability at the "
             "remeasurement date (IFRS 3.58a).")
    previous_fair_value = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Fair value carried before this remeasurement (captured on "
             "apply).")
    delta = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="New fair value less the fair value carried when applied; "
             "recognised in profit or loss (IFRS 3.58a).")
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='set null')
    notes = fields.Text()

    @api.constrains('combination_id')
    def _check_liability_classified(self):
        """IFRS 3.58b: equity-classified contingent consideration is not
        remeasured; its later settlement is accounted for within equity."""
        for rec in self:
            if rec.combination_id.contingent_classification != 'liability':
                raise ValidationError(_(
                    "Equity-classified contingent consideration is not "
                    "remeasured (IFRS 3.58b); its settlement is accounted "
                    "for within equity."))

    def write(self, vals):
        for rec in self:
            if rec.state == 'applied':
                raise UserError(_(
                    "Contingent consideration remeasurement of %s is "
                    "applied and posted; it cannot be changed.",
                    rec.combination_id.display_name))
        return super().write(vals)

    def unlink(self):
        for rec in self:
            if rec.state == 'applied':
                raise UserError(_(
                    "Contingent consideration remeasurement of %s is "
                    "applied and posted; it cannot be deleted.",
                    rec.combination_id.display_name))
        return super().unlink()

    def action_cancel(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_(
                    "Only a draft remeasurement can be cancelled."))
            rec.state = 'cancelled'
        return True

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move', 'res_id': self.move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }

    def action_apply(self):
        """Remeasure the liability to fair value through profit or loss.

        An increase in the liability is a loss (Dr profit or loss, Cr
        contingent consideration liability); a decrease is a gain the other
        way (IFRS 3.58a). The combination's current fair value rolls
        forward through the stored delta.
        """
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can remeasure contingent "
                "consideration."))
        # Run the state transition as su so the guarded 'state' write is
        # accepted; env.user (checked above) is preserved for audit stamps.
        self = self._eh_workflow_action()
        if self.state != 'draft':
            raise UserError(_("Only a draft remeasurement can be applied."))
        combo = self.combination_id
        currency = combo.currency_id
        if combo.state != 'recognised':
            raise UserError(_(
                "Recognise %s before remeasuring its contingent "
                "consideration.", combo.display_name))
        if combo.contingent_classification != 'liability':
            raise UserError(_(
                "Equity-classified contingent consideration is not "
                "remeasured (IFRS 3.58b)."))
        if not combo.journal_id:
            raise UserError(_("Configure the journal on the combination."))
        if not combo.contingent_account_id:
            raise UserError(_(
                "Configure the contingent consideration account on the "
                "combination."))
        pnl_account = (combo.contingent_pnl_account_id
                       or combo.gain_account_id)
        if not pnl_account:
            raise UserError(_(
                "Configure the contingent remeasurement account (or the "
                "bargain purchase gain account it falls back to) on the "
                "combination."))
        new_fair_value = currency.round(self.new_fair_value)
        if currency.compare_amounts(new_fair_value, 0.0) < 0:
            raise UserError(_(
                "The remeasured fair value cannot be negative."))
        previous = currency.round(
            combo.contingent_consideration_current_fv)
        delta = currency.round(new_fair_value - previous)
        if currency.is_zero(delta):
            raise UserError(_(
                "The fair value is unchanged; nothing to remeasure."))
        if delta > 0:
            legs = [
                (pnl_account, delta, 0.0,
                 _("Contingent consideration remeasurement loss %s",
                   combo.name)),
                (combo.contingent_account_id, 0.0, delta,
                 _("Contingent consideration to fair value %s", combo.name)),
            ]
        else:
            legs = [
                (combo.contingent_account_id, -delta, 0.0,
                 _("Contingent consideration to fair value %s", combo.name)),
                (pnl_account, 0.0, -delta,
                 _("Contingent consideration remeasurement gain %s",
                   combo.name)),
            ]
        move = self._post_move(legs)
        self.write({
            'state': 'applied',
            'move_id': move.id,
            'previous_fair_value': previous,
            'delta': delta,
        })
        return True

    def _post_move(self, legs):
        self.ensure_one()
        combo = self.combination_id
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': self.date,
            'journal_id': combo.journal_id.id,
            'ref': _("%s contingent consideration remeasurement",
                     combo.name),
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move
