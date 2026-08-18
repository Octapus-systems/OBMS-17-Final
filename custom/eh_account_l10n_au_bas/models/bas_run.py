# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.bas.run -- one BAS computation per (company, period).

Lifecycle: draft -> computed -> lodged. A user creates a run for a
quarter, runs Compute, reviews the figures, then marks Lodged when the
agent has lodged the form with the ATO. Lodged runs are read-only:
re-running compute on a lodged run raises an explicit error rather
than silently overwriting figures the ATO already received.

Audit: every compute appends a row to `eh.account.report.execution`
through the same audit pipeline as the standard reports, so an
auditor can trace which run produced which figure on which date.
"""

import json  # noqa: F401
import re
from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import SQL

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat


_QUARTERS = [
    ('q1', "Q1 (Jul-Sep)"),
    ('q2', "Q2 (Oct-Dec)"),
    ('q3', "Q3 (Jan-Mar)"),
    ('q4', "Q4 (Apr-Jun)"),
]


class EhBasRun(models.Model):
    _name = 'eh.bas.run'
    _description = "BAS run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'fy_label desc, quarter desc'

    # State and the lodgement stamps may only change through this model's own
    # actions (which run under sudo), never a direct RPC/ORM write. Without
    # this a plain user could write({'state': 'lodged'}) to skip action_compute
    # and its audit trail, or repoint lodged_at/lodged_by. See
    # eh.workflow.guard.
    _eh_guarded_fields = ('state', 'lodged_at', 'lodged_by')

    name = fields.Char(
        compute='_compute_name', store=True,
        help="Display label '<company> <FY> <quarter>'.",
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
        help="Reporting entity. One BAS run per company per quarter.",
    )
    fy_label = fields.Char(
        required=True, default=lambda self: self._default_fy_label(),
        help=(
            "Australian financial year label in YYYY-YY form (the "
            "Australian FY runs 1 July to 30 June). Example: '2025-26' "
            "covers 1 Jul 2025 to 30 Jun 2026."
        ),
    )
    quarter = fields.Selection(
        _QUARTERS, required=True, default='q1', tracking=True,
        help=(
            "BAS quarter within the financial year. Q1 covers Jul-Sep, "
            "Q2 Oct-Dec, Q3 Jan-Mar, Q4 Apr-Jun."
        ),
    )
    date_from = fields.Date(
        compute='_compute_period_dates', store=True,
        help="Inclusive start date derived from fy_label + quarter.",
    )
    date_to = fields.Date(
        compute='_compute_period_dates', store=True,
        help="Inclusive end date derived from fy_label + quarter.",
    )
    state = fields.Selection([
        ('draft', "Draft"),
        ('computed', "Computed"),
        ('lodged', "Lodged"),
    ], default='draft', tracking=True, required=True,
        help=(
            "draft: just instantiated, no figures yet. computed: the "
            "Compute action populated label totals. lodged: the agent "
            "has filed the BAS with the ATO; the run is read-only "
            "from this point so re-running compute cannot overwrite "
            "lodged figures."
        ),
    )
    lodged_at = fields.Datetime(
        readonly=True, copy=False,
        help="Timestamp recorded when the run was marked lodged.",
    )
    lodged_by = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help="User who marked the run lodged.",
    )
    line_ids = fields.One2many(
        'eh.bas.run.line', 'run_id', copy=False,
        help=(
            "One line per BAS label (G1-G20, 1A, 1B, W1, W2). "
            "Populated by the Compute action."
        ),
    )
    line_count = fields.Integer(
        compute='_compute_line_count',
        help="Number of populated label rows on this run.",
    )
    notes = fields.Text(
        help=(
            "Free-form notes for the agent or auditor: assumptions, "
            "manual adjustments, reconciliation steps."
        ),
    )

    reporting_basis = fields.Selection(
        [
            ('accruals', "Accruals"),
            ('cash', "Cash"),
        ],
        default='accruals',
        required=True,
        help=(
            "ATO reporting basis. Accruals (the default) recognises "
            "GST on the date the invoice is posted. Cash recognises "
            "GST on the date the invoice is paid, apportioned by the "
            "paid fraction of each invoice, using the payment date for "
            "period membership; small businesses with turnover under "
            "the cash-basis threshold can elect this to defer GST until "
            "receipt."
        ),
    )
    is_simpler_bas = fields.Boolean(
        string="Simpler BAS",
        default=False,
        help=(
            "Simpler BAS available for businesses with GST turnover "
            "under the ATO threshold (currently AUD 10 million). "
            "Reports only G1, 1A, 1B and PAYG-W; the additional G "
            "labels are still computed for internal review but not "
            "lodged."
        ),
    )

    _sql_constraints = [
        ('unique_period', 'unique(company_id, fy_label, quarter)', 'A BAS run already exists for this company / financial year / quarter.'),  # noqa: E501
    ]

    @staticmethod
    def _default_fy_label():
        today = date.today()
        # Australian FY runs Jul to Jun; the FY label is YYYY-YY.
        if today.month >= 7:
            return "%d-%02d" % (today.year, (today.year + 1) % 100)
        return "%d-%02d" % (today.year - 1, today.year % 100)

    @api.depends('fy_label', 'quarter')
    def _compute_period_dates(self):
        # Map the quarter to month ranges; fy_label format is YYYY-YY.
        for rec in self:
            try:
                start_year = int((rec.fy_label or '').split('-')[0])
            except (ValueError, IndexError):
                rec.date_from = False
                rec.date_to = False
                continue
            mapping = {
                'q1': (date(start_year, 7, 1), date(start_year, 9, 30)),
                'q2': (date(start_year, 10, 1), date(start_year, 12, 31)),
                'q3': (date(start_year + 1, 1, 1), date(start_year + 1, 3, 31)),
                'q4': (date(start_year + 1, 4, 1), date(start_year + 1, 6, 30)),
            }
            df, dt = mapping.get(rec.quarter, (False, False))
            rec.date_from = df
            rec.date_to = dt

    @api.constrains('fy_label')
    def _check_fy_label_format(self):
        # fy_label drives the period dates; a malformed value leaves
        # date_from/date_to unset and would crash the run. Enforce the
        # YYYY-YY shape (e.g. 2025-26) at write time.
        for rec in self:
            if not re.match(r'^\d{4}-\d{2}$', rec.fy_label or ''):
                raise ValidationError(_(
                    "Financial-year label must be in YYYY-YY form "
                    "(for example 2025-26); got %r.",
                ) % rec.fy_label)

    @api.depends('company_id', 'fy_label', 'quarter', 'state')
    def _compute_name(self):
        for rec in self:
            rec.name = "BAS %s %s [%s] %s" % (
                rec.fy_label or '',
                dict(_QUARTERS).get(rec.quarter, ''),
                rec.company_id.name or '',
                rec.state,
            )

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    # ---- actions ----

    def action_compute(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'lodged':
                raise UserError(_(
                    "BAS %s is lodged; recompute is refused. Create a "
                    "new run if you need to amend.",
                ) % rec.display_name)
            rec._compute_lines()
            rec.state = 'computed'
            rec.message_post(body=_(
                "BAS computed for %(start)s to %(end)s; %(count)s "
                "labels populated.",
                start=rec.date_from, end=rec.date_to,
                count=len(rec.line_ids),
            ))
        return True

    def action_mark_lodged(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'computed':
                raise UserError(_(
                    "BAS run must be computed before it is lodged "
                    "(state is %s).",
                ) % rec.state)
            rec.write({
                'state': 'lodged',
                'lodged_at': fields.Datetime.now(),
                'lodged_by': self.env.user.id,
            })
            rec.message_post(body=_(
                "BAS marked lodged by %s.",
            ) % self.env.user.display_name)
        return True

    def action_reset_draft(self):
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'lodged':
                raise UserError(_(
                    "Lodged BAS runs cannot be reset; create a new run "
                    "for amendment instead."
                ))
            rec.line_ids.unlink()
            rec.state = 'draft'
        return True

    def action_reconcile_gst_control(self):
        """Open the GST control account reconciliation view.

        Compares the 1A and 1B label totals against the period balance
        movement on each company's GST collected and GST paid control
        accounts. A non-zero difference signals either a missing tag
        on a journal item or a manual entry on the control account
        that bypassed the tax engine. Common bookkeeping error;
        catching it pre-lodge avoids an ATO query.
        """
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_(
                "Compute the BAS first; reconciliation needs label "
                "totals to compare against.",
            ))
        report = self.compute_gst_control_reconciliation()
        # Render result as a transient report record the user can inspect
        # through a transient form.
        Recon = self.env['eh.bas.gst.recon.result']
        result = Recon.create({
            'run_id': self.id,
            'label_1a': report['label_1a'],
            'label_1b': report['label_1b'],
            'gst_collected_movement': report['gst_collected_movement'],
            'gst_paid_movement': report['gst_paid_movement'],
            'collected_diff': report['collected_diff'],
            'paid_diff': report['paid_diff'],
            'detail_lines': report['detail_text'],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _("GST control reconciliation"),
            'res_model': 'eh.bas.gst.recon.result',
            'res_id': result.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def compute_gst_control_reconciliation(self):
        """Compute the 1A/1B vs GL control account variance.

        Walks the company's tax control accounts (the accounts marked
        as the GST clearing accounts on account.tax records) and
        sums the period balance movement. Compares to the BAS line
        for 1A and 1B. Returns a dict suitable for either the wizard
        view or programmatic callers.
        """
        self.ensure_one()
        Line = self.env['eh.bas.run.line']
        line_1a = Line.search([
            ('run_id', '=', self.id),
            ('label_id.code', '=', '1A'),
        ], limit=1)
        line_1b = Line.search([
            ('run_id', '=', self.id),
            ('label_id.code', '=', '1B'),
        ], limit=1)
        # Resolve every account that appears as a tax repartition
        # destination for taxes on this company. The tax clearing
        # accounts are where the GST tax journal items land; their
        # balance movement during the BAS period equals the GST
        # collected (credit movement) and GST paid (debit movement).
        Tax = self.env['account.tax'].sudo()
        taxes = Tax.search([('company_id', '=', self.company_id.id)])
        repartition = taxes.mapped('invoice_repartition_line_ids') \
            + taxes.mapped('refund_repartition_line_ids')
        tax_accounts = repartition.filtered(
            lambda r: r.repartition_type == 'tax',
        ).mapped('account_id')
        if not tax_accounts:
            collected_movement = 0.0
            paid_movement = 0.0
        else:
            AML = self.env['account.move.line'].sudo()
            collected_rows = read_group_compat(AML,
                [  # noqa: E128
                    ('account_id', 'in', tax_accounts.ids),
                    ('parent_state', '=', 'posted'),
                    ('company_id', '=', self.company_id.id),
                    ('date', '>=', self.date_from),
                    ('date', '<=', self.date_to),
                ],
                [],
                ['credit:sum', 'debit:sum'],
            )
            credit_total = collected_rows[0][0] if collected_rows else 0.0
            debit_total = collected_rows[0][1] if collected_rows else 0.0
            # GST collected appears as a credit on the tax control
            # account; GST paid appears as a debit.
            collected_movement = round(credit_total - debit_total, 2)
            # GST paid (input credits) is the debit-side; isolate via a
            # second group constrained to accounts whose tax repartition
            # is for purchases. For v1 we keep this simple and report
            # the gross debit total as paid_movement; sites with mixed
            # in/out tax on the same account can override.
            paid_movement = round(debit_total, 2)
        amount_1a = float(line_1a.amount or 0.0)
        amount_1b = float(line_1b.amount or 0.0)
        collected_diff = round(collected_movement - amount_1a, 2)
        paid_diff = round(paid_movement - amount_1b, 2)
        detail_lines = [
            "Tax control accounts considered: %d" % len(tax_accounts),
        ]
        if tax_accounts:
            detail_lines.append("Accounts:")
            for acc in tax_accounts:
                detail_lines.append(
                    "  %s %s" % (acc.code or '', acc.name or ''),
                )
        return {
            'label_1a': amount_1a,
            'label_1b': amount_1b,
            'gst_collected_movement': collected_movement,
            'gst_paid_movement': paid_movement,
            'collected_diff': collected_diff,
            'paid_diff': paid_diff,
            'detail_text': "\n".join(detail_lines),
        }

    # ---- compute ----

    def _compute_lines(self):
        self.ensure_one()
        if not (self.date_from and self.date_to):
            raise UserError(_(
                "This BAS run has no reporting period. Set a valid "
                "financial-year label (YYYY-YY) and quarter before "
                "computing the lines.",
            ))
        Label = self.env['eh.bas.label']
        labels = Label.search([('active', '=', True)])
        # Drop existing lines so the recompute is deterministic.
        self.line_ids.unlink()

        executor = self.env['eh.account.dynamic.report'].search(
            [('code', '=', 'au_bas')], limit=1,
        )
        # The audit trail is a soft dependency on dynamic_reports; if
        # the report record is not registered we still emit the lines
        # (the user has installed l10n_au_bas without dynamic_reports
        # for some reason). We log a chatter note so the absence is
        # visible.
        Execution = self.env['eh.account.report.execution']
        audit = False
        if executor:
            audit = Execution.start_execution(
                report_code='au_bas', name='BAS Run',
                options={
                    'date_from': self.date_from.isoformat(),
                    'date_to': self.date_to.isoformat(),
                    'company_ids': [self.company_id.id],
                    'fy_label': self.fy_label,
                    'quarter': self.quarter,
                },
                company_ids=[self.company_id.id],
                result_format='json',
            )
        try:
            new_vals = []
            for label in labels:
                amount = self._aggregate(label)
                new_vals.append({
                    'run_id': self.id,
                    'label_id': label.id,
                    'amount': amount,
                })
            self.env['eh.bas.run.line'].create(new_vals)
            if audit:
                audit.complete_execution(
                    row_count=len(new_vals),
                )
        except Exception as exc:
            if audit:
                audit.fail_execution(str(exc))
            raise

    def _aggregate(self, label):
        if label.aggregation == 'manual':
            return 0.0
        # Tax-type driven modes resolve the relevant account.tax records at
        # compute time from their type_tax_use, so a fresh install produces
        # non-zero figures for any company that carries standard GST taxes,
        # with no pre-seeded account tags. The tag-driven modes below remain
        # available for bespoke labels that map to a specific chart tag.
        if label.aggregation in (
            'gst_on_sales', 'gst_on_purchases', 'total_sales_incl',
        ):
            if self.reporting_basis == 'cash':
                return self._aggregate_cash_by_tax_type(label.aggregation)
            return self._aggregate_by_tax_type(label.aggregation)
        if not label.tag_ids:
            return 0.0
        sign_filter, balance_expr = self._aggregation_to_sql(label.aggregation)
        # Cross join with the m2m of tax tags via the upstream
        # account_account_tag_account_move_line_rel relation. The
        # MoveLineQuery composer does not yet expose tag joins so we
        # use where_raw with an EXISTS clause.
        query = MoveLineQuery(
            self.env, company_ids=[self.company_id.id],
        )
        query.where_date_range(
            date_from=self.date_from, date_to=self.date_to,
        )
        query.where_posted_only()
        query.where_raw(SQL(
            "EXISTS (SELECT 1 FROM account_account_tag_account_move_line_rel "
            "rel WHERE rel.account_move_line_id = aml.id AND "
            "rel.account_account_tag_id IN %s)",
            tuple(label.tag_ids.ids),
        ))
        if sign_filter is not None:
            query.where_raw(SQL(sign_filter))
        query.select(SQL(balance_expr), 'agg_amount')
        rows = query.execute()
        return float(rows[0]['agg_amount'] or 0.0) if rows else 0.0

    def _aggregate_by_tax_type(self, mode):
        """Sum posted movement for the GST labels without pre-seeded tags.

        The standard GST labels resolve directly from the company's
        account.tax records by their type_tax_use, so a freshly installed
        module produces correct figures for any AU company with standard
        GST taxes:

        * gst_on_sales (1A)     -- NET of tax journal items (credit minus
          debit) whose tax_line_id is a sale-type tax. This is the GST
          collected. Netting both sides subtracts the debit-side reversals
          a credit note posts, so a refunded sale reduces 1A rather than
          leaving it overstated.
        * gst_on_purchases (1B) -- NET of tax journal items (debit minus
          credit) whose tax_line_id is a purchase-type tax. This is the
          GST paid (input credits), net of supplier refunds/credit notes.
        * total_sales_incl (G1) -- tax-inclusive sales: the net (credit
          minus debit) base of lines carrying a sale-type tax, plus the
          net sale-type GST collected. Base + GST equals the GST-inclusive
          sale total, net of credit-note reversals.

        Netting both ledger sides is what compute_gst_control_reconciliation
        does (credit_total - debit_total); the labels must match it or the
        GST control reconciliation raises a false variance on clean books
        the moment a credit note or refund is posted in the period.

        All sums are over posted, non-cancelled lines in the run period for
        the run company, computed with the ORM (cross-version safe, no
        implicit m2m table names).
        """
        AML = self.env['account.move.line'].sudo()
        Tax = self.env['account.tax'].sudo()
        sale_taxes = Tax.search([
            ('company_id', '=', self.company_id.id),
            ('type_tax_use', '=', 'sale'),
        ])
        purchase_taxes = Tax.search([
            ('company_id', '=', self.company_id.id),
            ('type_tax_use', '=', 'purchase'),
        ])
        base_domain = [
            ('parent_state', '=', 'posted'),
            ('company_id', '=', self.company_id.id),
            ('date', '>=', self.date_from),
            ('date', '<=', self.date_to),
        ]

        def _net(domain, positive_column):
            # Net the two ledger sides so credit-note / refund reversals,
            # which post on the opposite side, are subtracted rather than
            # ignored. positive_column names the side the label accrues on
            # (credit for GST collected / sales; debit for GST paid); the
            # other side is the reversal. Matches the credit_total -
            # debit_total netting in compute_gst_control_reconciliation.
            rows = read_group_compat(
                AML, domain, [], ['credit:sum', 'debit:sum'],
            )
            if not (rows and rows[0]):
                return 0.0
            credit_total = rows[0][0] or 0.0
            debit_total = rows[0][1] or 0.0
            if positive_column == 'credit':
                return round(credit_total - debit_total, 2)
            return round(debit_total - credit_total, 2)

        if mode == 'gst_on_sales':
            if not sale_taxes:
                return 0.0
            return _net(
                base_domain + [('tax_line_id', 'in', sale_taxes.ids)],
                'credit',
            )
        if mode == 'gst_on_purchases':
            if not purchase_taxes:
                return 0.0
            return _net(
                base_domain + [('tax_line_id', 'in', purchase_taxes.ids)],
                'debit',
            )
        if mode == 'total_sales_incl':
            if not sale_taxes:
                return 0.0
            # Base of the sale (credit on the income line carrying the sale
            # tax) plus the GST collected on the same sales, each netted
            # against its credit-note reversal.
            base_credit = _net(
                base_domain + [
                    ('tax_ids', 'in', sale_taxes.ids),
                    ('tax_line_id', '=', False),
                ],
                'credit',
            )
            gst_credit = _net(
                base_domain + [('tax_line_id', 'in', sale_taxes.ids)],
                'credit',
            )
            return round(base_credit + gst_credit, 2)
        return 0.0

    def _aggregate_cash_by_tax_type(self, mode):
        """Cash-basis counterpart of :meth:`_aggregate_by_tax_type`.

        Accruals recognise GST on the invoice posting date. Cash basis
        recognises GST on the date the invoice is PAID, and only on the
        paid portion. This method aggregates the same GST tax journal
        items but apportions each by the fraction of its invoice that was
        settled within the run period, using the payment/reconciliation
        date (account.partial.reconcile.max_date) for period membership
        rather than the invoice accrual date (aml.date).

        Mechanics, per posted invoice/bill carrying the relevant tax:

        * The paid fraction attributable to the period is the sum of the
          partial-reconcile amounts (company currency) landing on the
          move's receivable/payable lines whose max_date falls within
          [date_from, date_to], divided by the move total.
        * The recognised GST for that move is its NET accrual GST (credit
          minus debit on the tax lines of the requested type, exactly as
          the accrual path nets it) multiplied by that period paid
          fraction, rounded with the company currency.
        * Summing over moves nets a credit note against the sale it
          offsets: the credit note is its own move with opposite-sign
          tax and its own reconciliation, so once its allocation settles
          in the period it subtracts from the total, mirroring the
          accrual netting and the GST control reconciliation.

        A move with no reconciliation dated in the period contributes
        nothing: an invoice posted this quarter but paid next quarter is
        excluded here and picked up in the period it is paid. All reads
        go through the ORM so the join is cross-version safe (16-19).
        """
        Tax = self.env['account.tax'].sudo()
        if mode == 'gst_on_purchases':
            type_tax_use = 'purchase'
            positive_column = 'debit'
        else:
            type_tax_use = 'sale'
            positive_column = 'credit'
        taxes = Tax.search([
            ('company_id', '=', self.company_id.id),
            ('type_tax_use', '=', type_tax_use),
        ])
        if not taxes:
            return 0.0

        currency = self.company_id.currency_id
        AML = self.env['account.move.line'].sudo()
        # Every posted GST tax journal item of the requested type, company
        # and (crucially) any date: the period filter for cash basis is on
        # the payment date, not the accrual date, so an invoice posted
        # before the period but paid within it must still be considered.
        tax_lines = AML.search([
            ('parent_state', '=', 'posted'),
            ('company_id', '=', self.company_id.id),
            ('tax_line_id', 'in', taxes.ids),
        ])
        # Group net accrual GST and the base (for G1) by their invoice move.
        moves = tax_lines.mapped('move_id')
        total = 0.0
        for move in moves:
            move_tax_lines = tax_lines.filtered(
                lambda line, m=move: line.move_id == m,
            )
            # Net GST on this move: credit minus debit (sales) or debit
            # minus credit (purchases), matching the accrual netting.
            credit_total = sum(move_tax_lines.mapped('credit'))
            debit_total = sum(move_tax_lines.mapped('debit'))
            if positive_column == 'credit':
                move_gst = credit_total - debit_total
            else:
                move_gst = debit_total - credit_total
            recognised = move_gst
            if mode == 'total_sales_incl':
                # G1 is the tax-inclusive sale: net base of the sale lines
                # carrying the tax plus the net GST, apportioned together.
                base_lines = move.line_ids.filtered(
                    lambda line: not line.tax_line_id
                    and (taxes & line.tax_ids),
                )
                base_credit = sum(base_lines.mapped('credit'))
                base_debit = sum(base_lines.mapped('debit'))
                recognised = (base_credit - base_debit) + move_gst
            if currency.is_zero(recognised):
                continue
            fraction = self._period_paid_fraction(move)
            if not fraction:
                continue
            total += currency.round(recognised * fraction)
        return currency.round(total)

    def _period_paid_fraction(self, move):
        """Fraction of ``move`` settled within the run period.

        Sums the partial-reconcile amounts (company currency) on the
        move's receivable/payable lines whose reconciliation date
        (max_date) falls within [date_from, date_to], divided by the
        move total. Returns a float in [0, 1]; 0 when nothing on the move
        settled in the period. Both matched sides are walked so the
        method is agnostic to whether the counterpart is a payment, a
        bank statement line, or an offsetting credit note.
        """
        # The receivable/payable lines are the reconcilable, non-tax lines
        # whose account is the AR/AP control; they carry the settlement.
        term_lines = move.line_ids.filtered(
            lambda line: line.account_id.reconcile and not line.tax_line_id,
        )
        if not term_lines:
            return 0.0
        move_total = sum(abs(bal) for bal in term_lines.mapped('balance'))
        if not move_total:
            return 0.0
        partials = (
            term_lines.mapped('matched_debit_ids')
            + term_lines.mapped('matched_credit_ids')
        )
        paid_in_period = 0.0
        for partial in partials:
            pay_date = partial.max_date
            if not pay_date:
                continue
            if self.date_from <= pay_date <= self.date_to:
                paid_in_period += partial.amount
        if paid_in_period <= 0.0:
            return 0.0
        fraction = paid_in_period / move_total
        # Guard against rounding pushing a fully-paid move slightly over 1.
        return min(fraction, 1.0)

    @staticmethod
    def _aggregation_to_sql(mode):
        # For BAS purposes, GST tags on credit lines = sales/income;
        # GST tags on debit lines = acquisitions/expenses. Tax tags can
        # be applied to either the base or the tax journal item; the
        # mode determines which SUM expression is correct.
        if mode == 'base_credit':
            return ("aml.credit > 0", "SUM(aml.credit)")
        if mode == 'base_debit':
            return ("aml.debit > 0", "SUM(aml.debit)")
        if mode == 'tax_credit':
            return ("aml.credit > 0", "SUM(aml.credit)")
        if mode == 'tax_debit':
            return ("aml.debit > 0", "SUM(aml.debit)")
        return (None, "0")


class EhBasRunLine(models.Model):
    _name = 'eh.bas.run.line'
    _description = "BAS run line"
    _order = 'label_sequence, label_code'

    run_id = fields.Many2one(
        'eh.bas.run', required=True, ondelete='cascade', index=True,
    )
    label_id = fields.Many2one(
        'eh.bas.label', required=True, ondelete='restrict',
    )
    label_code = fields.Char(
        related='label_id.code', store=True, index=True,
    )
    # Read-through to the translated label name. Not stored: a stored
    # related copy of a translated field cannot keep all language
    # variants in sync with the parent and produced an Odoo startup
    # warning (computed correctly only in the language at write
    # time). The view that displays this column lives on a small
    # record set so the read-time JOIN is negligible.
    label_name = fields.Char(related='label_id.name')
    label_sequence = fields.Integer(
        related='label_id.sequence', store=True,
    )
    amount = fields.Monetary(currency_field='currency_id')
    currency_id = fields.Many2one(
        related='run_id.company_id.currency_id', readonly=True,
    )
    notes = fields.Char()
