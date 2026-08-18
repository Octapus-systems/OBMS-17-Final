# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Inter-company elimination pair engine (IFRS 10.B86).

eh.ic.elimination.batch matches the posted inter-company move pairs of one
company pair for one period (via the eh_intercompany_origin_id linkage the
mirroring engine maintains) and builds the elimination legs the group
accounts need:

* balance elimination: the receivable recognised on the selling side
  against the payable recognised on the buying side;
* flow elimination: the revenue recognised on the selling side against
  the expense recognised on the buying side, for the period.

Lifecycle: draft -> computed -> posted. Compute is idempotent: rerunning
it replaces the engine-built lines (a DB unique constraint on
pair + period forbids duplicate batches, including the reversed pair).
Posting books one balanced, sealed journal entry in the designated
elimination company (the parent / consolidating company) in a dedicated
elimination journal that is auto-created on first use and stored on that
company's inter-company configuration.

Pairs that do not reconcile are never silently eliminated:

* a posted source with no mirror, or with a mirror still in draft, is
  listed on the mismatch tab with the reason and produces no legs;
* a pair whose two totals diverge by more than one cent is listed on the
  mismatch tab AND eliminated only at the common (lower) amount, so the
  unreconciled difference stays visible in the group accounts;
* posting is refused while mismatches exist, unless the manager clears
  the batch's block_on_mismatch flag (the audited override path).

Unrealised profit in inventory (IFRS 10.B86(c)) is computed from the
source documents, never hand-typed: for every matched pair whose selling
side is a customer invoice, the margin per product line is derived from
the invoice price against the product's standard cost in the selling
company. The fraction of that margin still held in the buyer's inventory
comes from stock quants when the stock module is installed (guarded with
a registry check so account-only installs still work); otherwise the
remaining fraction is entered per line, but the margin itself always
stays engine-derived.

Consolidation hook
------------------
eh_ic_elimination_summary(period_from, period_to, company_ids=None) on
this model returns the structured totals (receivable / payable
eliminated, revenue / expense eliminated, unrealised profit, mismatch
count, per-batch rows) so eh_account_consolidation (or any caller) can
consume the elimination work without this module writing into the
consolidation models. The unrealised legs are deliberately NOT booked in
the elimination move here: the consolidation run owns the inventory /
COGS restatement and reads the figure from the hook, which prevents the
same margin being eliminated twice.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_is_zero

from .account_move import _DIRECTION_MIRROR

# Context key the elimination engine sets on its own writes so the
# engine-built child rows (lines / mismatches / unrealised) let the
# sanctioned rebuild paths through their own edit guards.
IC_ENGINE_CTX = 'eh_ic_elim_engine'

_INCOME_TYPES = ('income', 'income_other')
_EXPENSE_TYPES = ('expense', 'expense_depreciation', 'expense_direct_cost')

# Identity fields frozen once the batch has been computed (they define
# which pairs were matched); change them by resetting to draft.
_FROZEN_AFTER_COMPUTE = (
    'company_a_id', 'company_b_id', 'period_from', 'period_to',
    'elimination_company_id',
)

# Tolerance for pair totals, matching the 1c tolerance the mirroring
# engine's eh_intercompany_state compute already applies.
_PAIR_TOLERANCE = 0.01


def _acc_company_field(env):
    # account.account is multi-company (company_ids) from Odoo 18;
    # single company_id before that.
    Account = env['account.account']
    return 'company_ids' if 'company_ids' in Account._fields else 'company_id'


class EhIcEliminationBatch(models.Model):
    _name = 'eh.ic.elimination.batch'
    _description = "Inter-company elimination batch"
    _order = 'period_to desc, id desc'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.gl.reversal',
                'eh.workflow.guard']

    # The state machine is enforced by eh.workflow.guard: 'state' may only
    # change through this model's own actions (which run as su), never a
    # direct RPC/ORM write, closing the "write({'state': 'posted'}) skips
    # action_post and its sealed journal entry" bypass.
    _eh_guarded_fields = ('state',)

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    company_a_id = fields.Many2one(
        'res.company', required=True, index=True, tracking=True,
        string="Company A",
        help=(
            "First company of the pair whose inter-company move pairs "
            "this batch matches and eliminates. The pair is unordered: "
            "a batch for (A, B) also covers documents that originate "
            "in B towards A."
        ),
    )
    company_b_id = fields.Many2one(
        'res.company', required=True, index=True, tracking=True,
        string="Company B",
        help="Second company of the pair.",
    )
    period_from = fields.Date(
        required=True, tracking=True,
        help="Inclusive start of the elimination period.",
    )
    period_to = fields.Date(
        required=True, tracking=True,
        help=(
            "Inclusive end of the elimination period. The elimination "
            "move is dated on this day."
        ),
    )
    elimination_company_id = fields.Many2one(
        'res.company', required=True, tracking=True,
        default=lambda self: self._default_elimination_company(),
        string="Elimination Company",
        help=(
            "Parent / consolidating company the elimination journal "
            "entry is booked in. Defaults from the Elimination Company "
            "configured on an inter-company configuration. Every "
            "account touched by the elimination must exist (by code) "
            "in this company's chart of accounts."
        ),
    )
    currency_id = fields.Many2one(
        related='elimination_company_id.currency_id',
        store=True, readonly=True,
    )
    block_on_mismatch = fields.Boolean(
        default=True, tracking=True,
        help=(
            "When set (the default), posting is refused while the batch "
            "carries mismatched or unmatched pairs. A manager can clear "
            "this flag to post the matched eliminations anyway; the "
            "override is tracked in the chatter and the mismatches stay "
            "listed for follow-up."
        ),
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('computed', "Computed"),
            ('posted', "Posted"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )

    line_ids = fields.One2many(
        'eh.ic.elimination.batch.line', 'batch_id',
        help=(
            "Engine-built elimination legs, one per account per matched "
            "pair. Rebuilt on every compute; not hand-editable."
        ),
    )
    mismatch_ids = fields.One2many(
        'eh.ic.elimination.mismatch', 'batch_id',
        help=(
            "Pairs that did not reconcile: no mirror, mirror still in "
            "draft, or totals diverging beyond one cent. Each row "
            "carries the reason; posting is blocked while rows exist "
            "unless block_on_mismatch is cleared."
        ),
    )
    unrealised_line_ids = fields.One2many(
        'eh.ic.unrealised.line', 'batch_id',
        help=(
            "Unrealised profit in inventory per product line of the "
            "matched inter-company sale invoices. The margin is always "
            "derived from the invoice against the product standard "
            "cost; only the remaining fraction is manual when the "
            "stock module is not installed."
        ),
    )

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, tracking=True,
        ondelete='restrict',
        help=(
            "The sealed elimination journal entry posted in the "
            "elimination company. Reset to draft reverses and removes "
            "it, preserving the audit trail."
        ),
    )
    move_state = fields.Selection(
        related='move_id.state', string="Move status", readonly=True,
    )

    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)

    line_count = fields.Integer(compute='_compute_counts')
    mismatch_count = fields.Integer(compute='_compute_counts')
    unrealised_count = fields.Integer(compute='_compute_counts')

    receivable_total = fields.Monetary(
        compute='_compute_totals', currency_field='currency_id',
        help="Gross receivable balance eliminated by this batch.",
    )
    payable_total = fields.Monetary(
        compute='_compute_totals', currency_field='currency_id',
        help="Gross payable balance eliminated by this batch.",
    )
    revenue_total = fields.Monetary(
        compute='_compute_totals', currency_field='currency_id',
        help="Gross revenue flow eliminated by this batch.",
    )
    expense_total = fields.Monetary(
        compute='_compute_totals', currency_field='currency_id',
        help="Gross expense flow eliminated by this batch.",
    )
    unrealised_total = fields.Monetary(
        compute='_compute_totals', currency_field='currency_id',
        help=(
            "Unrealised profit in ending inventory computed from the "
            "matched inter-company sale invoices. Exposed to the "
            "consolidation run via eh_ic_elimination_summary; not "
            "booked in the elimination move here."
        ),
    )

    notes = fields.Text()

    _sql_constraints = [
        ('unique_pair_period', 'unique(company_a_id, company_b_id, period_from, period_to)', "An elimination batch already exists for this company pair and "  # noqa: E501
        "period."),  # noqa: E128
    ]

    # ------------------------------------------------------------------
    # defaults / constraints / CRUD guards
    # ------------------------------------------------------------------

    @api.model
    def _default_elimination_company(self):
        config = self.env['eh.intercompany.config'].sudo().search(
            [('enabled', '=', True),
             ('elimination_company_id', '!=', False)],
            limit=1,
        )
        return config.elimination_company_id or self.env.company

    @api.constrains('company_a_id', 'company_b_id',
                    'period_from', 'period_to')
    def _check_pair(self):
        for batch in self:
            if batch.company_a_id == batch.company_b_id:
                raise ValidationError(_(
                    "An elimination batch pairs two different companies.",
                ))
            if batch.period_from and batch.period_to \
                    and batch.period_from > batch.period_to:
                raise ValidationError(_(
                    "The period start must not be after the period end.",
                ))
            # The DB unique constraint cannot see the pair as unordered,
            # so the reversed duplicate is refused here.
            swapped = self.search_count([
                ('id', '!=', batch.id),
                ('company_a_id', '=', batch.company_b_id.id),
                ('company_b_id', '=', batch.company_a_id.id),
                ('period_from', '=', batch.period_from),
                ('period_to', '=', batch.period_to),
            ])
            if swapped:
                raise ValidationError(_(
                    "An elimination batch already exists for this company "
                    "pair and period (with the companies in the other "
                    "order).",
                ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                a = self.env['res.company'].browse(
                    vals.get('company_a_id')).name or '?'
                b = self.env['res.company'].browse(
                    vals.get('company_b_id')).name or '?'
                vals['name'] = "IC ELIM %s / %s %s" % (
                    a, b, vals.get('period_to') or '')
        return super().create(vals_list)

    def write(self, vals):
        """Freeze the pair / period identity once the batch leaves draft
        (the matched pairs are defined by them); restate via a reset to
        draft, not a direct edit. This freeze is always on for everyone -
        a settled figure is immutable regardless of privilege. The state
        machine itself is owned by eh.workflow.guard (state changes only
        through this model's own su-run actions). block_on_mismatch stays
        writable: clearing it is the sanctioned, tracked override for
        posting past mismatches.
        """
        frozen = [f for f in _FROZEN_AFTER_COMPUTE if f in vals]
        settled = self.filtered(lambda b: b.state != 'draft')
        if frozen and settled:
            raise UserError(_(
                "The pair and period of a computed or posted elimination "
                "batch are frozen (%(fields)s); they define the matched "
                "pairs. Reset the batch to draft to change them.",
                fields=', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        posted = self.filtered(lambda b: b.state == 'posted')
        if posted:
            raise UserError(_(
                "A posted elimination batch cannot be deleted; it backs a "
                "posted journal entry. Reset it to draft first.",
            ))
        return super().unlink()

    @api.depends('line_ids', 'mismatch_ids', 'unrealised_line_ids')
    def _compute_counts(self):
        for batch in self:
            batch.line_count = len(batch.line_ids)
            batch.mismatch_count = len(batch.mismatch_ids)
            batch.unrealised_count = len(batch.unrealised_line_ids)

    @api.depends('line_ids.kind', 'line_ids.debit', 'line_ids.credit',
                 'unrealised_line_ids.unrealised_amount')
    def _compute_totals(self):
        for batch in self:
            totals = {'receivable': 0.0, 'payable': 0.0,
                      'revenue': 0.0, 'expense': 0.0}
            for line in batch.line_ids:
                # Each leg is single-sided (debit XOR credit), so the
                # gross eliminated magnitude per kind is the sum of both
                # columns.
                totals[line.kind] += line.debit + line.credit
            batch.receivable_total = totals['receivable']
            batch.payable_total = totals['payable']
            batch.revenue_total = totals['revenue']
            batch.expense_total = totals['expense']
            batch.unrealised_total = sum(
                batch.unrealised_line_ids.mapped('unrealised_amount'))

    # ------------------------------------------------------------------
    # compute: pair matching engine
    # ------------------------------------------------------------------

    def action_compute(self):
        """Match the posted IC move pairs of this company pair for the
        period and rebuild the elimination legs, mismatch rows and
        unrealised-profit lines. Idempotent: rerunning replaces every
        engine-built record, so two computes leave exactly one set.
        """
        for batch in self:
            if batch.state not in ('draft', 'computed'):
                raise UserError(_(
                    "Compute is only available on a draft or computed "
                    "batch. Reset the batch to draft first.",
                ))
            engine_ctx = {IC_ENGINE_CTX: True}
            batch.line_ids.sudo().with_context(**engine_ctx).unlink()
            batch.mismatch_ids.sudo().with_context(**engine_ctx).unlink()
            batch.unrealised_line_ids.sudo().with_context(
                **engine_ctx).unlink()
            pairs, mismatch_vals = batch._eh_match_pairs()
            line_vals = []
            for pair in pairs:
                line_vals.extend(batch._eh_build_pair_line_vals(pair))
            up_vals = batch._eh_build_unrealised_vals(pairs)
            if line_vals:
                self.env['eh.ic.elimination.batch.line'].sudo(
                ).with_context(**engine_ctx).create(line_vals)
            if mismatch_vals:
                self.env['eh.ic.elimination.mismatch'].sudo(
                ).with_context(**engine_ctx).create(mismatch_vals)
            if up_vals:
                self.env['eh.ic.unrealised.line'].sudo(
                ).with_context(**engine_ctx).create(up_vals)
            batch.sudo().write({
                'state': 'computed',
                'computed_at': fields.Datetime.now(),
                'computed_by_id': self.env.user.id,
            })
            batch.message_post(body=_(
                "Elimination computed: %(pairs)s pair(s) matched, "
                "%(legs)s leg(s) built, %(mismatch)s mismatch(es) "
                "flagged.",
                pairs=len(pairs), legs=len(line_vals),
                mismatch=len(mismatch_vals),
            ))
        return True

    @api.model
    def _eh_move_dest_company(self, move):
        """Destination company a source move mirrors into, resolved the
        same way the mirroring trigger resolves it (represented company
        first, legacy commercial company fallback)."""
        commercial = move.partner_id.commercial_partner_id
        if not commercial:
            return self.env['res.company']
        return (commercial.eh_represented_company_id
                or commercial.company_id)

    def _eh_match_pairs(self):
        """Return (pairs, mismatch_vals) for this batch.

        pairs: list of dicts {source, mirror, matched, source_total,
        mirror_total} where both sides are posted; matched is False when
        the totals diverge beyond one cent (the pair is then ALSO listed
        in mismatch_vals and eliminated only at the common amount).

        mismatch_vals: create-vals for the mismatch tab rows (no mirror,
        mirror in draft, amount mismatch), each with a reason.

        Pair selection: posted source moves of either pair company whose
        date falls in the period. Balances opened in earlier periods are
        out of scope for this batch (run a batch per period).
        """
        self.ensure_one()
        Move = self.env['account.move'].sudo()
        pair_companies = self.company_a_id | self.company_b_id
        # Window on the invoice date, not the accounting date: a vendor
        # bill's accounting date can land at month end while both books
        # share the document's invoice date, and a pair must never fall
        # out of the batch because the two legs post to different
        # accounting dates. Moves without an invoice date fall back to
        # the accounting date.
        sources = Move.search([
            ('state', '=', 'posted'),
            ('company_id', 'in', pair_companies.ids),
            ('move_type', 'in', list(_DIRECTION_MIRROR)),
            ('eh_intercompany_origin_id', '=', False),
            '|',
            '&', ('invoice_date', '!=', False),
            '&', ('invoice_date', '>=', self.period_from),
            ('invoice_date', '<=', self.period_to),
            '&', ('invoice_date', '=', False),
            '&', ('date', '>=', self.period_from),
            ('date', '<=', self.period_to),
        ], order='date, id')
        pairs, mismatch_vals = [], []
        currency = self.currency_id
        for src in sources:
            other = pair_companies - src.company_id
            mirror = src.eh_intercompany_mirror_id
            if mirror:
                if mirror.company_id != other:
                    continue  # pair belongs to a different company pair
            else:
                dest = self._eh_move_dest_company(src)
                if dest != other:
                    continue  # not an IC document towards the pair company
                mismatch_vals.append({
                    'batch_id': self.id,
                    'source_move_id': src.id,
                    'kind': 'no_mirror',
                    'source_amount': src.amount_total,
                    'reason': _(
                        "No mirror exists in %(company)s for posted "
                        "source %(name)s.",
                        company=other.display_name,
                        name=src.display_name),
                })
                continue
            if mirror.state != 'posted':
                mismatch_vals.append({
                    'batch_id': self.id,
                    'source_move_id': src.id,
                    'mirror_move_id': mirror.id,
                    'kind': 'mirror_draft',
                    'source_amount': src.amount_total,
                    'mirror_amount': mirror.amount_total,
                    'reason': _(
                        "Mirror %(name)s is not posted yet.",
                        name=mirror.display_name),
                })
                continue
            src_total = src.amount_total or 0.0
            mir_total = mirror.amount_total or 0.0
            matched = abs(src_total - mir_total) <= _PAIR_TOLERANCE
            if not matched:
                mismatch_vals.append({
                    'batch_id': self.id,
                    'source_move_id': src.id,
                    'mirror_move_id': mirror.id,
                    'kind': 'amount',
                    'source_amount': src_total,
                    'mirror_amount': mir_total,
                    'reason': _(
                        "Amount mismatch: source total %(src).2f vs "
                        "mirror total %(mir).2f (difference %(diff).2f). "
                        "Eliminated at the common amount only.",
                        src=src_total, mir=mir_total,
                        diff=currency.round(abs(src_total - mir_total))),
                })
            pairs.append({
                'source': src,
                'mirror': mirror,
                'matched': matched,
                'source_total': src_total,
                'mirror_total': mir_total,
            })
        return pairs, mismatch_vals

    @staticmethod
    def _eh_bucket_balances(move, account_types):
        """Sum debit-minus-credit per account for the move's lines whose
        account type is in account_types."""
        out = {}
        for line in move.line_ids:
            if line.account_id.account_type in account_types:
                out[line.account_id] = (
                    out.get(line.account_id, 0.0)
                    + (line.debit - line.credit))
        return out

    def _eh_translate_to_elim(self, amount, from_company, rate_type):
        """Translate a member's functional-currency amount into the
        elimination company's presentation currency (IAS 21.39).

        rate_type 'closing' uses the spot rate at period_to (balance-sheet
        items); 'average' approximates the period-average rate as the mean of
        the opening and closing conversions (IAS 21.40 permits an average
        that approximates the actual rates). Identity when the member already
        reports in the elimination currency, so a single-currency group
        produces byte-identical legs to the pre-FX behaviour.
        """
        self.ensure_one()
        elim = self.elimination_company_id
        elim_currency = elim.currency_id
        src_currency = from_company.currency_id
        if (not amount or not src_currency or not elim_currency
                or src_currency == elim_currency):
            return amount
        if rate_type == 'average':
            at_open = src_currency._convert(
                amount, elim_currency, elim, self.period_from)
            at_close = src_currency._convert(
                amount, elim_currency, elim, self.period_to)
            return (at_open + at_close) / 2.0
        return src_currency._convert(
            amount, elim_currency, elim, self.period_to)

    def _eh_resolve_cta_account(self):
        """Account for the cross-currency translation residual (CTA / FX).

        Uses the elimination company's configured currency-exchange accounts,
        which every multi-currency company already sets for reconciliation.
        """
        self.ensure_one()
        company = self.elimination_company_id.sudo()
        account = (company.income_currency_exchange_account_id
                   or company.expense_currency_exchange_account_id)
        if not account:
            raise UserError(_(
                "Configure a currency exchange account on company %(company)s "
                "to post cross-currency intercompany eliminations (the "
                "translation difference is booked there per IAS 21).",
                company=company.display_name))
        return account

    def _eh_build_pair_line_vals(self, pair):
        """Build the elimination-leg vals for one pair.

        The OUT side (customer invoice / refund) carries the receivable
        and the revenue; the IN side carries the payable and the
        expense. Each leg negates the recognised balance, so a normal
        invoice pair yields Dr payable / Cr receivable and Dr revenue /
        Cr expense; refund pairs flip through the signs naturally.

        Matched pairs get one exact leg per account. Mismatched pairs
        are eliminated at the COMMON amount only (the lower of the two
        sides per bucket), booked on the dominant account of each side,
        so the group never eliminates more than both sides recognised;
        the residual difference stays on the mismatch tab.
        """
        self.ensure_one()
        src, mirror = pair['source'], pair['mirror']
        out_move = (
            src if src.move_type in ('out_invoice', 'out_refund')
            else mirror)
        in_move = mirror if out_move is src else src
        currency = self.currency_id
        rounding = currency.rounding or 0.01
        # Translate each member's functional-currency balances into the
        # elimination presentation currency (IAS 21.39): closing rate at
        # period_to for balance-sheet items, period-average for P&L. Identity
        # when the currencies match, so single-currency groups are unchanged.
        bucket_spec = (
            ('receivable', out_move, ('asset_receivable',), 'closing'),
            ('payable', in_move, ('liability_payable',), 'closing'),
            ('revenue', out_move, _INCOME_TYPES, 'average'),
            ('expense', in_move, _EXPENSE_TYPES, 'average'),
        )
        buckets = {}
        for kind, mv, types, rate_type in bucket_spec:
            raw = self._eh_bucket_balances(mv, types)
            buckets[kind] = {
                acc: self._eh_translate_to_elim(bal, mv.company_id, rate_type)
                for acc, bal in raw.items()
            }
        base = {
            'batch_id': self.id,
            'source_move_id': src.id,
            'mirror_move_id': mirror.id,
        }
        vals = []
        if pair['matched']:
            for kind, accounts in buckets.items():
                for account, balance in accounts.items():
                    elim = currency.round(-balance)
                    if float_is_zero(elim, precision_rounding=rounding):
                        continue
                    vals.append(dict(
                        base, kind=kind, account_id=account.id,
                        debit=elim if elim > 0.0 else 0.0,
                        credit=-elim if elim < 0.0 else 0.0,
                    ))
            return vals
        totals = {k: sum(v.values()) for k, v in buckets.items()}
        common_balance = min(
            abs(totals['receivable']), abs(totals['payable']))
        common_flow = min(abs(totals['revenue']), abs(totals['expense']))
        for kind, common in (
            ('receivable', common_balance), ('payable', common_balance),
            ('revenue', common_flow), ('expense', common_flow),
        ):
            accounts = buckets[kind]
            common = currency.round(common)
            if not accounts or float_is_zero(
                    common, precision_rounding=rounding):
                continue
            account = max(accounts, key=lambda a: abs(accounts[a]))
            sign = 1.0 if totals[kind] > 0.0 else -1.0
            elim = currency.round(-sign * common)
            vals.append(dict(
                base, kind=kind, account_id=account.id,
                debit=elim if elim > 0.0 else 0.0,
                credit=-elim if elim < 0.0 else 0.0,
            ))
        return vals

    # ------------------------------------------------------------------
    # unrealised profit from source documents
    # ------------------------------------------------------------------

    @api.model
    def _eh_product_storable(self, product):
        """Cross-series storable test: is_storable from Odoo 17.2+,
        type == 'product' before."""
        if 'is_storable' in product._fields:
            return bool(product.is_storable)
        return product.type == 'product'

    def _eh_build_unrealised_vals(self, pairs):
        """Build the unrealised-profit line vals from the matched pairs.

        For every pair whose OUT side is a customer invoice, one line
        per product invoice line: the margin is the invoice line
        subtotal less the product standard cost (in the selling
        company) times the quantity, so it is always engine-derived.
        When the stock module is installed the fraction of the sold
        quantity still in the buyer's internal locations is read from
        stock quants (capped at the invoiced quantity); otherwise the
        fraction starts at zero and is entered per line. Refund pairs
        are out of scope for unrealised profit.
        """
        self.ensure_one()
        currency = self.currency_id
        has_stock = 'stock.quant' in self.env.registry
        pair_companies = self.company_a_id | self.company_b_id
        vals = []
        for pair in pairs:
            src, mirror = pair['source'], pair['mirror']
            out_move = (
                src if src.move_type in ('out_invoice', 'out_refund')
                else mirror)
            if out_move.move_type != 'out_invoice':
                continue
            seller = out_move.company_id
            buyer = pair_companies - seller
            for line in out_move.invoice_line_ids:
                product = line.product_id
                if not product or not line.quantity:
                    continue
                unit_cost = product.with_company(seller).standard_price
                margin = currency.round(
                    line.price_subtotal - unit_cost * line.quantity)
                fraction, remaining_qty, source = 0.0, 0.0, 'manual'
                if has_stock and self._eh_product_storable(product):
                    quants = self.env['stock.quant'].sudo().search([
                        ('product_id', '=', product.id),
                        ('company_id', '=', buyer.id),
                        ('location_id.usage', '=', 'internal'),
                    ])
                    on_hand = sum(quants.mapped('quantity'))
                    remaining_qty = max(0.0, min(on_hand, line.quantity))
                    fraction = (
                        remaining_qty / line.quantity
                        if line.quantity else 0.0)
                    source = 'stock'
                vals.append({
                    'batch_id': self.id,
                    'source_move_id': src.id,
                    'mirror_move_id': mirror.id,
                    'product_id': product.id,
                    'quantity': line.quantity,
                    'unit_cost': unit_cost,
                    'price_subtotal': line.price_subtotal,
                    'margin': margin,
                    'remaining_qty': remaining_qty,
                    'remaining_fraction': fraction,
                    'fraction_source': source,
                })
        return vals

    # ------------------------------------------------------------------
    # post / reset
    # ------------------------------------------------------------------

    def _eh_get_elimination_journal(self):
        """Return the dedicated elimination journal in the elimination
        company, auto-creating it on first use and storing it on that
        company's inter-company configuration (when one exists), so
        every batch books into the same journal from then on.
        """
        self.ensure_one()
        company = self.elimination_company_id
        config = self.env['eh.intercompany.config'].sudo().search(
            [('company_id', '=', company.id)], limit=1)
        if config and config.elimination_journal_id \
                and config.elimination_journal_id.company_id == company:
            return config.elimination_journal_id
        Journal = self.env['account.journal'].sudo()
        journal = Journal.search([
            ('company_id', '=', company.id),
            ('code', '=', 'ICEL'),
        ], limit=1)
        if not journal:
            journal = Journal.create({
                'name': "IC Eliminations",
                'code': 'ICEL',
                'type': 'general',
                'company_id': company.id,
            })
        if config and not config.elimination_journal_id:
            config.elimination_journal_id = journal.id
        return journal

    def _eh_resolve_elim_account(self, account, cache):
        """Map a source-company account into the elimination company's
        chart by code (the consolidation module's mapping convention).
        Raises a clear error naming the account when no counterpart
        exists, so a missing mapping fails loudly rather than dropping
        a leg.
        """
        self.ensure_one()
        company = self.elimination_company_id
        if account.id in cache:
            return cache[account.id]
        company_field = _acc_company_field(self.env)
        if company_field == 'company_ids':
            owns = company in account.company_ids
        else:
            owns = account.company_id == company
        if owns:
            cache[account.id] = account
            return account
        # account.account.code is company-dependent on Odoo 19, so read
        # it in the source account's own company context, then search in
        # the elimination company's context.
        if company_field == 'company_ids':
            source_company = account.company_ids[:1] or company
        else:
            source_company = account.company_id or company
        code = account.with_company(source_company).code
        Account = self.env['account.account'].sudo().with_company(company)
        resolved = Account.search([
            ('code', '=', code),
            (company_field, 'in', company.ids),
        ], limit=1)
        if not resolved:
            raise UserError(_(
                "Account %(code)s %(name)s is not present in the "
                "elimination company %(company)s chart of accounts. Add "
                "it (by code) before posting the elimination batch.",
                code=code or '?',
                name=account.name or '',
                company=company.display_name,
            ))
        cache[account.id] = resolved
        return resolved

    def action_post(self):
        """Book the elimination journal entry (manager-gated).

        Refused while mismatches exist and block_on_mismatch is set. The
        move is booked in the elimination company's dedicated journal,
        dated on period_to, sealed (eh_sealed) so its figures cannot be
        edited or unposted outside the sanctioned reset path. Each batch
        leg becomes one journal line; a residual of at most one rounding
        unit (possible when a matched pair's two sides differ by the
        tolerated cent) is absorbed into the largest leg so the move
        always balances.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can post an elimination "
                "batch.",
            ))
        for batch in self:
            if batch.state != 'computed':
                raise UserError(_(
                    "Only a computed elimination batch can be posted.",
                ))
            if batch.mismatch_ids and batch.block_on_mismatch:
                raise UserError(_(
                    "Batch %(name)s carries %(count)s mismatched or "
                    "unmatched pair(s); resolve them (or clear Block on "
                    "Mismatch to post the matched eliminations anyway).",
                    name=batch.display_name,
                    count=len(batch.mismatch_ids),
                ))
            if not batch.line_ids:
                raise UserError(_(
                    "Batch %(name)s has no elimination lines; nothing "
                    "to post.",
                    name=batch.display_name,
                ))
            move = batch._eh_build_elimination_move()
            move.action_post()
            batch.sudo().write({
                'state': 'posted',
                'move_id': move.id,
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
            })
            batch.message_post(body=_(
                "Elimination move %(move)s posted in %(company)s by "
                "%(user)s.",
                move=move.name,
                company=batch.elimination_company_id.display_name,
                user=self.env.user.display_name,
            ))
        return True

    def _eh_build_elimination_move(self):
        self.ensure_one()
        company = self.elimination_company_id
        currency = company.currency_id
        rounding = currency.rounding or 0.01
        journal = self._eh_get_elimination_journal()
        cache = {}
        line_vals = []
        for line in self.line_ids:
            account = self._eh_resolve_elim_account(line.account_id, cache)
            amount = currency.round(line.debit - line.credit)
            if float_is_zero(amount, precision_rounding=rounding):
                continue
            line_vals.append({
                'account_id': account.id,
                'name': "%s: %s" % (
                    dict(line._fields['kind'].selection).get(
                        line.kind, line.kind),
                    line.source_move_id.name or '/'),
                'debit': amount if amount > 0.0 else 0.0,
                'credit': -amount if amount < 0.0 else 0.0,
            })
        if not line_vals:
            raise UserError(_(
                "Batch %(name)s produced no non-zero journal lines.",
                name=self.display_name,
            ))
        # Balance the entry. A residual of at most a couple of rounding units
        # is the tolerated cent from a matched pair diverging within
        # _PAIR_TOLERANCE; absorb it into the largest leg. A LARGER residual
        # is a genuine translation difference from members reporting in
        # different functional currencies (IAS 21.39/48); book it to the
        # currency-translation (FX) account instead of silently distorting an
        # elimination leg, which was the pre-fix behaviour.
        net = currency.round(
            sum(v['debit'] - v['credit'] for v in line_vals))
        if not float_is_zero(net, precision_rounding=rounding):
            if abs(net) <= rounding * 2.0:
                biggest = max(
                    line_vals, key=lambda v: abs(v['debit'] - v['credit']))
                adjusted = currency.round(
                    (biggest['debit'] - biggest['credit']) - net)
                biggest['debit'] = adjusted if adjusted > 0.0 else 0.0
                biggest['credit'] = -adjusted if adjusted < 0.0 else 0.0
            else:
                fx_account = self._eh_resolve_cta_account()
                line_vals.append({
                    'account_id': fx_account.id,
                    'name': _("Cumulative translation adjustment (IAS 21)"),
                    'debit': -net if net < 0.0 else 0.0,
                    'credit': net if net > 0.0 else 0.0,
                })
        return self.env['account.move'].sudo().create({
            'company_id': company.id,
            'move_type': 'entry',
            'date': self.period_to,
            'journal_id': journal.id,
            'ref': _("IC elimination %(name)s", name=self.name),
            'eh_sealed': True,
            'line_ids': [(0, 0, v) for v in line_vals],
        })

    def action_reset_to_draft(self):
        """Manager-gated reset. A posted batch's sealed move is reversed
        and removed (the sanctioned unwind), leaving the elimination
        company's ledger flat, then the batch returns to draft for
        recompute."""
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can reset an elimination "
                "batch.",
            ))
        for batch in self:
            batch._eh_reverse_elimination_move()
            batch.sudo().write({
                'state': 'draft',
                'posted_at': False,
                'posted_by_id': False,
            })
        return True

    def _eh_reverse_elimination_move(self):
        self.ensure_one()
        move = self.move_id
        if not move:
            return
        if move.state == 'posted':
            reversal = move._reverse_moves(
                default_values_list=[{
                    'date': self.period_to,
                    'ref': _("Reversal of IC elimination %(name)s",
                             name=self.name),
                }],
                cancel=True,
            )
            self._eh_seal_reversal(reversal)
            self.message_post(body=_(
                "Elimination move %(move)s reversed (%(rev)s) on batch "
                "reset by %(user)s.",
                move=move.name,
                rev=reversal.name if reversal else '',
                user=self.env.user.display_name,
            ))
            to_remove = (move | reversal).sudo()
            # The elimination move is sealed; this reset is the
            # sanctioned unwind, so it carries the allow-unpost flag.
            to_remove.sudo().with_context(eh_allow_unpost=True).button_draft()
            self.move_id = False
            to_remove.unlink()
        else:
            self.move_id = False
            move.sudo().unlink()

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No elimination move has been posted."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Elimination move"),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
            'context': {
                'allowed_company_ids': [self.elimination_company_id.id],
            },
        }

    # ------------------------------------------------------------------
    # consolidation hook
    # ------------------------------------------------------------------

    @api.model
    def eh_ic_elimination_summary(self, period_from, period_to,
                                  company_ids=None):
        """Structured elimination totals for a period, for consumption
        by the consolidation run (or any caller) without that module
        touching this module's models.

        :param period_from: date or ISO string, inclusive.
        :param period_to: date or ISO string, inclusive.
        :param company_ids: optional list of res.company ids; when
            given, only batches whose BOTH pair companies are in the
            list are included.
        :return: dict with keys:
            receivable_eliminated, payable_eliminated,
            revenue_eliminated, expense_eliminated (gross magnitudes),
            unrealised_profit (sum of the engine-derived unrealised
            amounts), mismatch_count, and batches (one row per batch:
            id, name, state, company_a_id, company_b_id, and the same
            per-batch totals).

        Only computed and posted batches count. A batch is included
        when its period overlaps [period_from, period_to].
        """
        period_from = fields.Date.to_date(period_from)
        period_to = fields.Date.to_date(period_to)
        batches = self.sudo().search([
            ('state', 'in', ('computed', 'posted')),
            ('period_from', '<=', period_to),
            ('period_to', '>=', period_from),
        ])
        if company_ids:
            wanted = set(company_ids)
            batches = batches.filtered(
                lambda b: b.company_a_id.id in wanted
                and b.company_b_id.id in wanted)
        totals = {
            'receivable_eliminated': 0.0,
            'payable_eliminated': 0.0,
            'revenue_eliminated': 0.0,
            'expense_eliminated': 0.0,
            'unrealised_profit': 0.0,
        }
        rows = []
        mismatch_count = 0
        for batch in batches:
            row = {
                'id': batch.id,
                'name': batch.name,
                'state': batch.state,
                'company_a_id': batch.company_a_id.id,
                'company_b_id': batch.company_b_id.id,
                'receivable_eliminated': batch.receivable_total,
                'payable_eliminated': batch.payable_total,
                'revenue_eliminated': batch.revenue_total,
                'expense_eliminated': batch.expense_total,
                'unrealised_profit': batch.unrealised_total,
                'mismatch_count': len(batch.mismatch_ids),
            }
            for key in totals:
                totals[key] += row[key]
            mismatch_count += row['mismatch_count']
            rows.append(row)
        totals['mismatch_count'] = mismatch_count
        totals['batches'] = rows
        return totals


class EhIcEliminationBatchLine(models.Model):
    _name = 'eh.ic.elimination.batch.line'
    _description = "Inter-company elimination leg"
    _order = 'batch_id, id'

    batch_id = fields.Many2one(
        'eh.ic.elimination.batch', required=True,
        ondelete='cascade', index=True,
    )
    currency_id = fields.Many2one(
        related='batch_id.currency_id', store=True, readonly=True,
    )
    source_move_id = fields.Many2one(
        'account.move', string="Source move", index=True,
        ondelete='set null',
    )
    mirror_move_id = fields.Many2one(
        'account.move', string="Mirror move", ondelete='set null',
    )
    kind = fields.Selection(
        [
            ('receivable', "Receivable"),
            ('payable', "Payable"),
            ('revenue', "Revenue"),
            ('expense', "Expense"),
        ],
        required=True, index=True,
    )
    account_id = fields.Many2one(
        'account.account', required=True,
        help=(
            "Source-company account the leg eliminates. Resolved by "
            "code into the elimination company's chart at post time."
        ),
    )
    debit = fields.Monetary(currency_field='currency_id')
    credit = fields.Monetary(currency_field='currency_id')

    def _eh_guard_engine_only(self):
        if self.env.context.get(IC_ENGINE_CTX):
            return
        raise UserError(_(
            "Elimination legs are engine-built from the matched pairs "
            "and cannot be edited by hand. Recompute the batch instead.",
        ))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(IC_ENGINE_CTX):
            self._eh_guard_engine_only()
        return super().create(vals_list)

    def write(self, vals):
        self._eh_guard_engine_only()
        return super().write(vals)

    def unlink(self):
        self._eh_guard_engine_only()
        return super().unlink()


class EhIcEliminationMismatch(models.Model):
    _name = 'eh.ic.elimination.mismatch'
    _description = "Inter-company elimination mismatch"
    _order = 'batch_id, id'

    batch_id = fields.Many2one(
        'eh.ic.elimination.batch', required=True,
        ondelete='cascade', index=True,
    )
    currency_id = fields.Many2one(
        related='batch_id.currency_id', store=True, readonly=True,
    )
    source_move_id = fields.Many2one(
        'account.move', string="Source move", ondelete='set null',
    )
    mirror_move_id = fields.Many2one(
        'account.move', string="Mirror move", ondelete='set null',
    )
    kind = fields.Selection(
        [
            ('no_mirror', "No mirror"),
            ('mirror_draft', "Mirror not posted"),
            ('amount', "Amount mismatch"),
        ],
        required=True, index=True,
    )
    source_amount = fields.Monetary(currency_field='currency_id')
    mirror_amount = fields.Monetary(currency_field='currency_id')
    difference = fields.Monetary(
        compute='_compute_difference', store=True,
        currency_field='currency_id',
    )
    reason = fields.Char()

    @api.depends('source_amount', 'mirror_amount')
    def _compute_difference(self):
        for rec in self:
            rec.difference = abs(
                (rec.source_amount or 0.0) - (rec.mirror_amount or 0.0))

    def _eh_guard_engine_only(self):
        if self.env.context.get(IC_ENGINE_CTX):
            return
        raise UserError(_(
            "Mismatch rows are engine-built from the matched pairs and "
            "cannot be edited by hand. Recompute the batch instead.",
        ))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(IC_ENGINE_CTX):
            self._eh_guard_engine_only()
        return super().create(vals_list)

    def write(self, vals):
        self._eh_guard_engine_only()
        return super().write(vals)

    def unlink(self):
        self._eh_guard_engine_only()
        return super().unlink()


class EhIcUnrealisedLine(models.Model):
    _name = 'eh.ic.unrealised.line'
    _description = "Inter-company unrealised profit line"
    _order = 'batch_id, id'

    # Fields a user may edit while the batch is not yet posted. The
    # margin and its inputs are engine-derived from the source invoice
    # and the product standard cost, never hand-typed.
    _EH_USER_FIELDS = ('remaining_fraction', 'remaining_qty',
                       'fraction_source', 'notes')

    batch_id = fields.Many2one(
        'eh.ic.elimination.batch', required=True,
        ondelete='cascade', index=True,
    )
    currency_id = fields.Many2one(
        related='batch_id.currency_id', store=True, readonly=True,
    )
    source_move_id = fields.Many2one(
        'account.move', string="Source move", ondelete='set null',
    )
    mirror_move_id = fields.Many2one(
        'account.move', string="Mirror move", ondelete='set null',
    )
    product_id = fields.Many2one('product.product', index=True)
    quantity = fields.Float(digits=(16, 4))
    unit_cost = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Product standard cost in the selling company at compute "
            "time. Engine-derived."
        ),
    )
    price_subtotal = fields.Monetary(
        currency_field='currency_id',
        help="Untaxed amount of the source invoice line. Engine-derived.",
    )
    margin = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Invoice line subtotal less standard cost times quantity. "
            "Always derived from the source documents, never typed."
        ),
    )
    remaining_qty = fields.Float(
        digits=(16, 4),
        help=(
            "Quantity of the sold product still in the buying company's "
            "internal locations at compute time (stock installed), "
            "capped at the invoiced quantity."
        ),
    )
    remaining_fraction = fields.Float(
        digits=(12, 4),
        help=(
            "Fraction of the invoiced quantity still held by the buyer "
            "at period end (0 to 1). Read from stock quants when the "
            "stock module is installed; otherwise entered per line."
        ),
    )
    fraction_source = fields.Selection(
        [
            ('stock', "Stock quants"),
            ('manual', "Manual"),
        ],
        default='manual', required=True,
    )
    unrealised_amount = fields.Monetary(
        compute='_compute_unrealised_amount', store=True,
        currency_field='currency_id',
        help=(
            "Margin still unrealised at period end: margin times the "
            "remaining fraction, rounded to the elimination currency."
        ),
    )
    notes = fields.Char()

    @api.depends('margin', 'remaining_fraction')
    def _compute_unrealised_amount(self):
        for line in self:
            currency = line.currency_id
            amount = (line.margin or 0.0) * (line.remaining_fraction or 0.0)
            line.unrealised_amount = (
                currency.round(amount) if currency else round(amount, 2))

    @api.constrains('remaining_fraction')
    def _check_fraction(self):
        for line in self:
            if not 0.0 <= (line.remaining_fraction or 0.0) <= 1.0:
                raise ValidationError(_(
                    "The remaining fraction must be between 0 and 1.",
                ))

    def _eh_guard_engine_only(self, vals=None):
        if self.env.context.get(IC_ENGINE_CTX):
            return
        if vals is not None:
            touched = set(vals) - set(self._EH_USER_FIELDS)
            if touched:
                raise UserError(_(
                    "The margin figures on an unrealised-profit line are "
                    "engine-derived from the source invoice and product "
                    "standard cost (%(fields)s cannot be edited). Only "
                    "the remaining fraction is manual.",
                    fields=', '.join(sorted(touched))))
            posted = self.filtered(lambda line_item: line_item.batch_id.state == 'posted')
            if posted:
                raise UserError(_(
                    "The unrealised-profit lines of a posted elimination "
                    "batch are frozen. Reset the batch to draft to "
                    "change them.",
                ))
            return
        raise UserError(_(
            "Unrealised-profit lines are engine-built from the matched "
            "pairs. Recompute the batch instead.",
        ))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(IC_ENGINE_CTX):
            self._eh_guard_engine_only()
        return super().create(vals_list)

    def write(self, vals):
        self._eh_guard_engine_only(vals=vals)
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get(IC_ENGINE_CTX):
            self._eh_guard_engine_only()
        return super().unlink()
