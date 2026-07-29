# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move integration: gate _post() on approval.

When a policy applies to a draft move and no approved approval request
exists, _post raises a UserError naming the policy. The approver
clicks Submit -> approve -> approve... -> approved on the request,
the move can then post.

Material amount changes after submission reset the request via the
detect_material_change/reset_for_re_approval pair on the request
model. The reset flips the state back to in_review at step 0 and
schedules fresh approver activities.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_approval_request_ids = fields.One2many(
        'eh.approval.request', 'move_id',
        string="Approval requests",
        copy=False,
    )
    eh_active_approval_request_id = fields.Many2one(
        'eh.approval.request',
        compute='_compute_active_request', store=True,
        help=(
            "The currently-effective approval request for this move. "
            "Empty when no policy applies, no request exists, or the "
            "request was withdrawn / rejected."
        ),
    )
    eh_approval_state = fields.Selection(
        related='eh_active_approval_request_id.state',
        string="Approval state",
        readonly=True, store=False,
    )
    eh_requires_approval = fields.Boolean(
        compute='_compute_requires_approval',
        help=(
            "True when a policy applies to this move and the approval "
            "is not yet complete. Used by the form to surface the "
            "Submit for approval button."
        ),
    )

    @api.depends(
        'eh_approval_request_ids',
        'eh_approval_request_ids.state',
    )
    def _compute_active_request(self):
        for move in self:
            # create_date is False on in-memory NewId records during an
            # onchange; sorting a raw False against datetimes raises
            # TypeError, so fall back to "now" for unsaved rows.
            active = move.eh_approval_request_ids.filtered(
                lambda r: r.state in ('pending', 'in_review', 'approved'),
            ).sorted(
                lambda r: r.create_date or fields.Datetime.now(),
                reverse=True,
            )
            move.eh_active_approval_request_id = active[:1].id

    @api.depends('eh_active_approval_request_id', 'eh_approval_state', 'state')
    def _compute_requires_approval(self):
        for move in self:
            if move.state != 'draft':
                move.eh_requires_approval = False
                continue
            policy = self.env['eh.approval.policy'].find_for_move(move)
            if not policy:
                move.eh_requires_approval = False
                continue
            rule = policy.find_matching_rule(move._eh_amount_for_approval())
            if not rule:
                move.eh_requires_approval = False
                continue
            req = move.eh_active_approval_request_id
            move.eh_requires_approval = not (req and req.state == 'approved')

    def _eh_amount_for_approval(self):
        """Return the absolute amount the policy compares against.

        Vendor bills / customer invoices use amount_total; journal
        entries fall back to the sum of debits, which is a sensible
        scalar even for non-invoice moves.
        """
        self.ensure_one()
        if self.move_type in ('in_invoice', 'in_refund', 'out_invoice', 'out_refund'):
            amount = self.amount_total or 0.0
            # Policy thresholds are stated in company currency, but a foreign-
            # currency bill's amount_total is in the document currency. Convert
            # so a 10,000 USD bill on a EUR company routes on its EUR value,
            # not a raw 10,000 compared against EUR bands.
            move_currency = self.currency_id
            company = self.company_id or self.env.company
            company_currency = company.currency_id
            if move_currency and company_currency \
                    and move_currency != company_currency:
                amount = move_currency._convert(
                    amount, company_currency, company,
                    self.invoice_date or self.date
                    or fields.Date.context_today(self),
                )
            return amount
        # Journal entries: debits are already in company currency.
        return sum(self.line_ids.mapped('debit')) or 0.0

    # ---- public actions ----

    def action_eh_view_approval_request(self):
        """Open the active approval request from a stat button on the
        move form. Returns an act_window that lands on the request
        directly so the user doesn't need to navigate via the menu.
        """
        self.ensure_one()
        if not self.eh_active_approval_request_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': 'Approval request',
            'res_model': 'eh.approval.request',
            'res_id': self.eh_active_approval_request_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    def action_eh_request_approval(self):
        """Create an approval request for this move and submit it.

        If a request already exists in pending or in_review, navigate
        to it. If a prior request was rejected or withdrawn, restart
        it. Otherwise create a fresh request.
        """
        for move in self:
            policy = self.env['eh.approval.policy'].find_for_move(move)
            if not policy:
                raise UserError(_(
                    "No approval policy applies to %s. Configure one "
                    "or post directly.",
                    move.display_name,
                ))
            amount = move._eh_amount_for_approval()
            rule = policy.find_matching_rule(amount)
            if not rule:
                raise UserError(_(
                    "Policy %(policy)s has no rule covering amount "
                    "%(amt).2f. Adjust the policy's amount bands.",
                    policy=policy.name, amt=amount,
                ))
            existing = move.eh_active_approval_request_id
            if existing and existing.state == 'in_review':
                return existing._eh_open_form_action()
            if existing and existing.state == 'pending':
                existing.action_submit()
                return existing._eh_open_form_action()
            # Create a fresh request. sudo() because this is a system-
            # orchestrated creation that sets the initial in_review state and
            # the identity fields (move/policy/rule/amount), which the
            # eh.workflow.guard create-strip would otherwise drop for a
            # non-superuser caller.
            request = self.env['eh.approval.request'].sudo().create({
                'move_id': move.id,
                'policy_id': policy.id,
                'rule_id': rule.id,
                'submitted_amount': amount,
                'state': 'in_review',
            })
            request._eh_log(
                'submitted',
                _("Approval requested for amount %.2f") % amount,
            )
            return request._eh_open_form_action()

    # ---- post gate ----

    def _post(self, soft=True):
        """Block posting until the approval request is approved.

        Runs BEFORE the upstream _post so the upstream side effects
        (sequence numbering, reconciliation, journal entry validation)
        only fire after approval.
        """
        for move in self:
            if move.state == 'posted':
                continue
            move._eh_check_approval_gate()
            move._eh_check_re_approval_after_edit()
        result = super()._post(soft=soft)
        # Mark the approval request archived once the move actually
        # posts so the active list view stops showing it.
        for move in self:
            if move.state == 'posted' and move.eh_active_approval_request_id:
                move.eh_active_approval_request_id.action_archive()
        return result

    def _eh_check_approval_gate(self):
        self.ensure_one()
        policy = self.env['eh.approval.policy'].find_for_move(self)
        if not policy:
            return
        amount = self._eh_amount_for_approval()
        rule = policy.find_matching_rule(amount)
        if not rule:
            return
        request = self.eh_active_approval_request_id
        if not request:
            raise UserError(_(
                "%(move)s requires approval per policy %(policy)s "
                "before posting. Click 'Request Approval' to start.",
                move=self.display_name, policy=policy.name,
            ))
        if request.state != 'approved':
            raise UserError(_(
                "%(move)s has approval request %(req)s in state "
                "%(state)s. Posting is blocked until the request is "
                "approved.",
                move=self.display_name, req=request.name,
                state=request.state,
            ))
        # Defense in depth: the approved request must have been signed
        # under the rule that matches the CURRENT amount. If the amount
        # was raised into a higher band after approval and the request
        # still points at the weaker (stale) band's rule, the approval
        # does not satisfy the stronger chain the new amount requires.
        # detect_material_change/reset covers most edits, but a band
        # crossing that stays below the re-approval threshold (e.g. a
        # 9,500 bill approved under the one-step rule bumped to 10,400,
        # under the percentage floor yet across the two-step boundary)
        # slips past it - this equality check blocks the mismatch outright.
        if request.rule_id != rule:
            raise UserError(_(
                "%(move)s was approved under rule %(approved)s but its "
                "current amount %(amt).2f now matches rule %(current)s. "
                "The amount changed approval bands after sign-off; "
                "re-approval under the correct rule is required before "
                "posting.",
                move=self.display_name,
                approved=request.rule_id.display_name,
                amt=amount,
                current=rule.display_name,
            ))

    def _eh_check_re_approval_after_edit(self, raise_on_reset=True):
        """If the move's amount changed materially since submission,
        reset the approval and (optionally) block the post until
        re-approval.

        Called from `_post` (with raise_on_reset=True) so an edit-
        and-post flow blocks. Also called from `write` (with
        raise_on_reset=False) so an edit alone resets the approval
        but does not raise mid-write; the next post attempt then sees
        the reset request and blocks via `_eh_check_approval_gate`.
        """
        self.ensure_one()
        request = self.eh_active_approval_request_id
        if not request:
            return False
        if request.state != 'approved':
            return False
        current = self._eh_amount_for_approval()
        if not request.detect_material_change(current):
            return False
        request.reset_for_re_approval(
            new_amount=current,
            reason=_(
                "Move amount changed from %(old).2f to %(new).2f after "
                "approval; re-approval required.",
                old=request.submitted_amount,
                new=current,
            ),
        )
        if raise_on_reset:
            raise UserError(_(
                "Move amount changed materially after approval. The "
                "request has been reset; every step must re-sign before "
                "posting.",
            ))
        return True

    def _eh_line_account_signature(self):
        """Return the multiset of line-account ids on this move.

        Used to detect an in-place account redirection: rewriting a
        posting line to a different account at the same debit/credit
        total leaves amount_total untouched, so the amount-based
        material-change check never fires. Comparing this signature
        before and after a write surfaces that redirection so it can be
        treated as a payment-routing change. Sorted so ordering of the
        line commands is irrelevant.
        """
        self.ensure_one()
        return tuple(sorted(self.line_ids.mapped('account_id').ids))

    def _eh_reset_routing_change(self, changed_fields, raise_on_reset=True,
                                 account_redirected=False):
        """Force re-approval when a payment-routing field changed.

        Unlike the amount check, any change to the payee bank account,
        the payment due date, or the destination account of a posting
        line resets the approved request regardless of the amount
        threshold: redirecting a paid bill to another account or
        shifting its due date is a segregation-of-duties break on its
        own. Returns True when a reset was performed.

        `account_redirected` is passed by write() when the set of line
        accounts changed at an unchanged total; it forces the reset even
        though no literal routing field name appears in changed_fields.
        """
        self.ensure_one()
        request = self.eh_active_approval_request_id
        if not request:
            return False
        if request.state != 'approved':
            return False
        touched = sorted(
            set(changed_fields) & set(self._EH_ROUTING_TRIGGER_FIELDS),
        )
        if account_redirected and 'line account' not in touched:
            touched.append('line account')
        if not touched:
            return False
        request.reset_for_re_approval(
            new_amount=request.submitted_amount,
            reason=_(
                "Payment-routing field(s) %(fields)s changed after "
                "approval; re-approval required.",
                fields=", ".join(touched),
            ),
        )
        if raise_on_reset:
            raise UserError(_(
                "A payment-routing field (payee bank account or due "
                "date) changed after approval. The request has been "
                "reset; every step must re-sign before posting.",
            ))
        return True

    # Fields that, when changed on a draft move with an approved
    # request attached, should trigger the re-approval check.
    _EH_REAPPROVAL_TRIGGER_FIELDS = (
        'invoice_line_ids', 'line_ids',
        'amount_total', 'amount_untaxed', 'amount_tax',
        'currency_id', 'partner_id',
        'invoice_date_due', 'partner_bank_id',
    )

    # Payment-routing / due-date fields where ANY change after approval
    # must force re-approval regardless of the amount threshold. An
    # approved bill redirected to a different payee bank account, or with
    # its payment due date shifted, is a segregation-of-duties break even
    # when the total is unchanged, so these bypass detect_material_change.
    # Redirecting a posting line to a DIFFERENT account at the same total
    # is the same class of break, but it surfaces only as a change to
    # line_ids/invoice_line_ids (not a field name here), so write()
    # detects it by comparing the line-account signature across the write
    # and passing account_redirected=True into _eh_reset_routing_change.
    _EH_ROUTING_TRIGGER_FIELDS = (
        'invoice_date_due', 'partner_bank_id',
    )

    # Field names on a write() vals that can carry a line-account
    # redirection (rewriting an existing line to a different account at
    # the same total).
    _EH_LINE_CONTAINER_FIELDS = ('line_ids', 'invoice_line_ids')

    def write(self, vals):
        """Re-approval guard on write.

        If any of the trigger fields change on a move that already
        carries an approved request, reset the request silently and
        post a chatter note. The next post attempt will block via
        the standard gate.
        """
        # Snapshot the line-account signature per move BEFORE the write
        # so an in-place account redirection (same total, different
        # destination account) can be detected afterwards. Only bother
        # when the write actually touches a line container and a move in
        # the set carries an approved request.
        pre_signatures = {}
        if vals and set(vals) & set(self._EH_LINE_CONTAINER_FIELDS):
            for move in self:
                if move.state != 'draft':
                    continue
                request = move.eh_active_approval_request_id
                if request and request.state == 'approved':
                    pre_signatures[move.id] = move._eh_line_account_signature()
        result = super().write(vals)
        if not vals:
            return result
        triggered = bool(
            set(vals) & set(self._EH_REAPPROVAL_TRIGGER_FIELDS),
        )
        if not triggered:
            return result
        for move in self:
            if move.state != 'draft':
                continue
            if not move.eh_active_approval_request_id:
                continue
            if move.eh_active_approval_request_id.state != 'approved':
                continue
            # A line-account redirection at an unchanged total shows up
            # only as a changed line-account signature, not a routing
            # field name, so detect it here and feed it into the routing
            # reset as a first-class SoD break.
            account_redirected = (
                move.id in pre_signatures
                and move._eh_line_account_signature()
                != pre_signatures[move.id]
            )
            # Routing changes reset unconditionally; the amount check
            # then handles material value changes. Skip the amount check
            # when a routing reset already fired to avoid a double reset.
            if move._eh_reset_routing_change(
                set(vals), raise_on_reset=False,
                account_redirected=account_redirected,
            ):
                continue
            move._eh_check_re_approval_after_edit(raise_on_reset=False)
        return result
