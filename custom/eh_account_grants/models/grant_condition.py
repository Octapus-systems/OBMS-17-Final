# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.gov.grant.condition: one condition attached to a government grant.

IAS 20.7/8: a grant is recognised only when there is reasonable assurance
that the entity will comply with the conditions attaching to it. The
register tracks each condition open / fulfilled / breached. When the grant
defers income until its conditions are met, an open or breached condition
blocks the release to income. Breaching a condition on a received
deferred-income grant accrues the clawback per IAS 20.32 (reverse
unamortised deferred income first, excess to profit or loss, liability
until the cash is repaid).
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhGovGrantCondition(models.Model):
    _name = 'eh.gov.grant.condition'
    _description = "Government grant condition (IAS 20)"
    _inherit = ['eh.workflow.guard']
    _order = 'grant_id, due_date, id'

    # The condition state (open / fulfilled / breached) gates grant income
    # release and triggers the breach clawback, so it may change only through
    # the record's own actions, never a direct RPC/ORM write.
    _eh_guarded_fields = ('state',)

    grant_id = fields.Many2one(
        'eh.gov.grant', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='grant_id.company_id', store=True, readonly=True)
    name = fields.Char(
        string="Condition", required=True,
        help="Condition attaching to the grant (IAS 20.7), e.g. 'employ 20 "
             "apprentices for 3 years' or 'complete the facility by "
             "2027-06-30'.")
    due_date = fields.Date(
        help="Date by which the condition must be met.")
    state = fields.Selection(
        [('open', "Open"), ('fulfilled', "Fulfilled"),
         ('breached', "Breached")],
        default='open', required=True, index=True)
    fulfilled_date = fields.Date(readonly=True, copy=False)

    # ---- actions ----

    def action_fulfil(self):
        self = self._eh_workflow_action()
        for cond in self:
            if cond.state == 'breached':
                raise UserError(_(
                    "Condition '%(name)s' is breached; a breached condition "
                    "cannot be marked fulfilled. The clawback flow governs "
                    "it now (IAS 20.32).", name=cond.name))
            cond.write({
                'state': 'fulfilled',
                'fulfilled_date': fields.Date.context_today(cond),
            })
            cond.grant_id.message_post(body=_(
                "Grant condition fulfilled: %(name)s.", name=cond.name))
        return True

    def action_reopen(self):
        self = self._eh_workflow_action()
        for cond in self:
            if cond.state != 'fulfilled':
                raise UserError(_(
                    "Only a fulfilled condition can be reopened."))
            cond.write({'state': 'open', 'fulfilled_date': False})
            cond.grant_id.message_post(body=_(
                "Grant condition reopened: %(name)s.", name=cond.name))
        return True

    def action_breach(self):
        """Mark the condition breached and accrue the clawback (IAS 20.32).

        On a received deferred-income grant the breach immediately accrues
        the repayment obligation through eh.gov.grant.action_accrue_clawback
        (manager-gated, loud on missing inputs, atomic with the breach
        mark). On a grant that holds no deferred income (draft, netting,
        closed, repaid) the breach is only recorded in the register and
        chatter.
        """
        self = self._eh_workflow_action()
        for cond in self:
            if cond.state == 'breached':
                raise UserError(_(
                    "Condition '%(name)s' is already breached.",
                    name=cond.name))
            cond.write({'state': 'breached', 'fulfilled_date': False})
            grant = cond.grant_id
            grant.message_post(body=_(
                "Grant condition breached: %(name)s.", name=cond.name))
            if (grant.state == 'received' and not grant._is_netting
                    and not grant.clawback_accrued):
                grant.action_accrue_clawback()
        return True
