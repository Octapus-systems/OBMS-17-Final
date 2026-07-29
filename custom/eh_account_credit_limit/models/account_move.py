# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move integration: gate _post() on customer credit limit.

The gate fires for out_invoice and out_refund only. Vendor moves and
manual journal entries are out of scope; their credit considerations
belong to the AP and approval workflow modules respectively.

Override flow:

1. User attempts to post a customer invoice that breaches the partner's
   limit. _post raises a UserError with a clear message naming the
   exposure, limit, and excess amount.
2. A manager (member of the policy's override_group) writes
   eh_credit_override_reason on the move and posts again. The gate
   sees the reason, records an immutable audit log row, and allows the
   post.
3. The override is per-move; a manager who blesses one invoice does
   not implicitly bless the next one.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_credit_override_reason = fields.Char(
        copy=False,
        help=(
            "Reason for overriding the customer credit limit on this "
            "specific invoice. Setting this field is a positive act: "
            "the gate sees it as 'manager has explicitly accepted "
            "this excess' and records the override before posting."
        ),
    )
    eh_credit_override_log_id = fields.Many2one(
        'eh.credit.override.log',
        readonly=True, copy=False,
        help=(
            "Audit row recorded when this move's post bypassed the "
            "credit gate. Empty when no override was needed."
        ),
    )
    eh_credit_warning = fields.Char(
        compute='_compute_eh_credit_warning',
        help=(
            "Live, non-blocking warning shown on a draft customer "
            "invoice when posting it would take the partner over its "
            "credit limit. The hard gate still runs at post time."
        ),
    )

    @api.depends('partner_id', 'amount_total', 'move_type', 'state',
                 'company_id')
    def _compute_eh_credit_warning(self):
        for move in self:
            warning = False
            partner = move.partner_id.commercial_partner_id
            if (move.move_type in ('out_invoice', 'out_refund')
                    and partner and move.state == 'draft'):
                policy = partner._eh_resolve_credit_policy()
                limit = (partner.eh_credit_limit
                         or (policy.default_credit_limit if policy else 0.0))
                if policy and limit:
                    exposure = move._eh_partner_exposure_excluding_self(
                        partner, policy)
                    projected = exposure + move._eh_credit_relevant_amount()
                    if projected > limit:
                        warning = _(
                            "Posting brings %(partner)s to %(new).2f, over "
                            "the %(limit).2f credit limit (excess "
                            "%(excess).2f).",
                            partner=partner.display_name, new=projected,
                            limit=limit, excess=projected - limit)
            move.eh_credit_warning = warning

    def _post(self, soft=True):
        """Hook the post pipeline so the credit check runs first.

        For out_invoice / out_refund moves, computes the partner's
        exposure plus this move's amount and compares against the
        effective limit. Block-mode policies raise unless the move
        carries an override reason. Warn-mode policies log to chatter
        but do not block.

        The check runs in order alongside the move's own validation,
        so a malformed move still fails on its own validation; the
        credit gate only fires when validation would otherwise pass.
        """
        for move in self:
            if move.state == 'posted':
                continue
            if move.move_type not in ('out_invoice', 'out_refund'):
                continue
            move._eh_check_credit_gate()
        return super()._post(soft=soft)

    def _eh_check_credit_gate(self):
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id
        if not partner:
            return
        policy = partner._eh_resolve_credit_policy()
        if not policy:
            return  # no policy configured; gate is disabled.
        limit = partner.eh_credit_limit or policy.default_credit_limit
        if not limit:
            return  # zero limit means no enforcement.
        # Block mode is a hard check-then-act: read exposure, compare
        # against the limit, then post. Serialise concurrent block-mode
        # gates for the same customer on the partner row so two posts
        # cannot both read the pre-post exposure, both pass, and both
        # post over the limit with no override recorded. Warn mode is
        # advisory only, so it stays lock-free. The lock is taken before
        # reading exposure so the queued second post re-reads the
        # freshly-committed first invoice.
        if policy.enforcement_mode == 'block':
            self._eh_lock_partner_for_gate(partner)
        # We exclude this move from exposure to avoid double counting:
        # if drafts are included in the policy, the current move was
        # already counted as a draft. Compute exposure WITHOUT this
        # move, then add this move's amount once.
        exposure = self._eh_partner_exposure_excluding_self(partner, policy)
        move_amount = self._eh_credit_relevant_amount()
        new_exposure = exposure + move_amount
        if new_exposure <= limit:
            return  # within limit; no further action needed.

        excess = new_exposure - limit
        if policy.enforcement_mode == 'warn':
            self.message_post(body=_(
                "Credit warning: posting this invoice brings %(partner)s "
                "to %(new).2f, exceeding the %(limit).2f limit by "
                "%(excess).2f. Posting allowed (warn-only policy).",
                partner=partner.display_name,
                new=new_exposure, limit=limit, excess=excess,
            ))
            return

        # Block mode: require an override reason.
        if not self.eh_credit_override_reason:
            raise UserError(_(
                "Credit limit exceeded for %(partner)s. Posting this "
                "invoice would bring exposure to %(new).2f against a "
                "limit of %(limit).2f (excess %(excess).2f). To proceed, "
                "a manager must record the override reason on this "
                "invoice before posting again.",
                partner=partner.display_name,
                new=new_exposure, limit=limit, excess=excess,
            ))

        # Override path: verify the user is authorised. The override group
        # is configurable per policy and defaults to the suite manager
        # group. When a deployment explicitly clears it, fall back to that
        # manager group rather than letting any invoice writer override:
        # the previous "override_group and ..." short-circuit allowed an
        # unrestricted bypass whenever the group was empty.
        override_group = policy.override_group_id or self.env.ref(
            'eh_account_base.group_eh_manager', raise_if_not_found=False,
        )
        # all_group_ids includes implied/parent groups (group_ids holds only
        # explicitly assigned ones), matching how has_group resolves access.
        if (not override_group
                or override_group not in self.env.user.groups_id):
            raise UserError(_(
                "Posting %(partner)s over its credit limit requires an "
                "override by a member of %(group)s. Configure the override "
                "group on the credit policy, or have an authorised user "
                "post this invoice.",
                partner=partner.display_name,
                group=(
                    override_group.name if override_group
                    else _("the credit-override group")
                ),
            ))

        # Record the audit row, link it to the move.
        log = self.env['eh.credit.override.log'].create({
            'move_id': self.id,
            'partner_id': partner.id,
            'company_id': self.company_id.id,
            'exposure_at_override': exposure,
            'limit_at_override': limit,
            'move_amount': move_amount,
            'reason': self.eh_credit_override_reason,
        })
        self.eh_credit_override_log_id = log.id
        self.message_post(body=_(
            "Credit override recorded. Exposure %(exp).2f, limit "
            "%(lim).2f, excess %(exc).2f. Reason: %(reason)s.",
            exp=exposure, lim=limit, exc=excess,
            reason=self.eh_credit_override_reason,
        ))

    def _eh_lock_partner_for_gate(self, partner):
        """Serialise concurrent block-mode credit gates for one customer.

        The block guarantee is a check-then-act: read the partner's
        exposure, compare against the limit, then post. Without a lock
        two posts for the same commercial partner both read the pre-post
        exposure (each other's draft is invisible under READ COMMITTED),
        both pass, and their combined exposure silently breaches the
        limit with no override recorded in the audit log.

        A row lock on the commercial partner forces the second gate to
        queue behind the first; once the first commits, the second
        re-reads exposure (now including the freshly-posted invoice via a
        fresh read_group) and correctly blocks or forces the override
        path. flush + invalidate around the lock so the re-read sees
        committed state rather than a stale snapshot. Mirrors the FOR
        UPDATE discipline in eh.cheque.book and the run-model posting
        producers.
        """
        partner.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM res_partner WHERE id = %s FOR UPDATE",
            (partner.id,),
        )
        partner.invalidate_recordset()

    def _eh_partner_exposure_excluding_self(self, partner, policy):
        """Compute partner exposure excluding this move's contribution.

        Mirrors res.partner._eh_compute_exposure but adds an exclusion
        on this move's id so a draft customer invoice is not double-
        counted when the policy includes drafts and we then add the
        move amount on top of the exposure.
        """
        self.ensure_one()
        AML = self.env['account.move.line'].sudo()
        domain = [
            ('partner_id', '=', partner.id),
            ('account_id.account_type', '=', 'asset_receivable'),
            ('parent_state', '=', 'posted'),
            ('amount_residual', '!=', 0),
            ('company_id', '=', self.company_id.id),
            ('move_id', '!=', self.id),
        ]
        # One SQL pass instead of materialising every AR line per post.
        posted_rows = read_group_compat(AML, domain, [], ['amount_residual:sum'])
        total = posted_rows[0][0] if posted_rows else 0.0

        if policy.include_drafts:
            Move = self.env['account.move'].sudo()
            draft_rows = read_group_compat(Move, 
                [
                    ('partner_id', '=', partner.id),
                    ('move_type', 'in', ('out_invoice', 'out_refund')),
                    ('state', '=', 'draft'),
                    ('company_id', '=', self.company_id.id),
                    ('id', '!=', self.id),
                ],
                [],
                # amount_total_signed is in company currency and signs
                # refunds negative, matching the partner-form exposure
                # (res.partner._eh_compute_exposure). amount_total would
                # use each draft's own currency and inflate refunds.
                ['amount_total_signed:sum'],
            )
            total += draft_rows[0][0] if draft_rows else 0.0

        # Mirror the partner-side sale-order branch so the post-time
        # gate sees the same exposure as the partner form. Excluding
        # self does not apply here: an account.move is not a sale.order,
        # so there is no double-count between the move under post and
        # the order list.
        if policy.include_sale_orders and 'sale.order' in self.env:
            SaleOrder = self.env['sale.order'].sudo()
            company = self.company_id or self.env.company
            company_currency = company.currency_id
            so_domain = [
                ('partner_id', '=', partner.id),
                ('state', 'in', ('sale', 'done')),
                ('company_id', '=', self.company_id.id),
            ]
            # Convert each open order from its own currency to company
            # currency at the order's effective date, mirroring
            # res.partner._eh_compute_exposure. A raw sum of
            # amount_to_invoice would compare a foreign-currency order
            # against the limit at its unconverted figure and
            # mis-enforce the block.
            orders = SaleOrder.search(so_domain)
            for order in orders:
                if 'amount_to_invoice' in order._fields:
                    raw = order.amount_to_invoice or 0.0
                else:
                    invoiced = getattr(order, 'amount_invoiced', 0.0) or 0.0
                    raw = max((order.amount_total or 0.0) - invoiced, 0.0)
                if not raw:
                    continue
                order_currency = order.currency_id or company_currency
                if order_currency == company_currency:
                    total += raw
                else:
                    rate_date = (
                        getattr(order, 'date_order', False)
                        or fields.Date.context_today(self)
                    )
                    total += order_currency._convert(
                        raw, company_currency, company, rate_date,
                    )

        return total

    def _eh_credit_relevant_amount(self):
        """Signed amount this move adds to partner exposure.

        out_invoice adds to exposure (customer owes more), out_refund
        subtracts (customer owes less). The value must be in COMPANY
        currency, because both the stored exposure and the credit limit
        are held in company currency: amount_total lives in the move's
        own (possibly foreign) currency, so returning it raw would
        compare a foreign-currency invoice against the limit at its
        unconverted figure and mis-enforce the gate.

        amount_total_signed is already company-currency and signs
        out_refund negative / out_invoice positive, matching the
        partner-form exposure (res.partner._eh_compute_exposure, which
        sums amount_total_signed for drafts). Use it directly so the
        gate compares like-for-like.
        """
        self.ensure_one()
        return float(self.amount_total_signed or 0.0)
