# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Customer portal controllers.

Three routes:

* /my/eh/statement (GET): renders the statement PDF for the logged-in
  partner over the period range supplied as query parameters, attaches
  it to the partner record, and serves the bytes inline. Rate-limited
  by the Odoo portal layer (per-IP throttling on /my routes).
* /my/eh/balance (GET, JSON): returns the partner's open balance and
  days-overdue figure as a small JSON dict. Used by the portal home
  card without round-tripping a full HTML page.
* CustomerPortal._prepare_home_portal_values is overridden to surface
  the open-balance summary on the portal home so the user lands
  somewhere meaningful.

Security: every route resolves the partner via request.env.user
.partner_id.commercial_partner_id and refuses any partner_id parameter
that does not match. There is no path that lets a portal user fetch
another partner's statement.
"""

import base64
import logging
from datetime import date, timedelta  # noqa: F401
from urllib.parse import quote

from odoo import _, fields, http
from odoo.exceptions import AccessError, MissingError, UserError  # noqa: F401
from odoo.http import request

from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat

_logger = logging.getLogger(__name__)


class EhAccountPortal(CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        """Add open invoice count and balance to the portal home.

        The standard Odoo portal home shows counters per related model
        (invoices, sales, etc). We layer in:

        * eh_open_invoice_count: partner's posted not-paid / partial
          invoices.
        * eh_total_overdue: sum of amount_residual on lines past due.
        * eh_oldest_due_date: earliest unpaid due date.

        Returns the values dict consumed by the portal's QWeb home
        template via the inherited template patch.
        """
        values = super()._prepare_home_portal_values(counters)
        partner = request.env.user.partner_id.commercial_partner_id
        if not partner:
            return values
        Move = request.env['account.move'].sudo()
        AML = request.env['account.move.line'].sudo()

        domain = [
            ('partner_id', '=', partner.id),
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('state', '=', 'posted'),
            ('payment_state', 'in', ('not_paid', 'partial')),
        ]
        values['eh_open_invoice_count'] = Move.search_count(domain)

        today = fields.Date.context_today(request.env.user)
        # One SQL pass for the overdue total + oldest due date. Routed through
        # read_group_compat: the raw _read_group signature here is 17+, so a
        # direct call crashes /my for every portal customer on Odoo 16 (whose
        # _read_group is a different, private signature). The compat helper
        # normalises both series to the 17+ tuple shape.
        overdue_rows = read_group_compat(
            AML,
            [
                ('partner_id', '=', partner.id),
                ('account_id.account_type', '=', 'asset_receivable'),
                ('parent_state', '=', 'posted'),
                ('amount_residual', '!=', 0),
                ('date_maturity', '<', today),
                ('date_maturity', '!=', False),
            ],
            [],
            ['amount_residual:sum', 'date_maturity:min'],
        )
        if overdue_rows and overdue_rows[0][0]:
            values['eh_total_overdue'] = overdue_rows[0][0]
            values['eh_oldest_due_date'] = overdue_rows[0][1]
            values['eh_days_overdue_max'] = (
                today - values['eh_oldest_due_date']
            ).days if values['eh_oldest_due_date'] else 0
        else:
            values['eh_total_overdue'] = 0.0
            values['eh_oldest_due_date'] = False
            values['eh_days_overdue_max'] = 0
        values['eh_partner_id'] = partner.id
        values['eh_partner_currency'] = partner.currency_id or partner.company_id.currency_id
        return values

    @http.route(
        ['/my/eh/statement'],
        type='http', auth='user', website=True,
    )
    def portal_eh_statement(self, date_from=None, date_to=None,
                            statement_type='open_item', **kw):
        """Render the customer statement PDF and serve it inline.

        Query parameters:
        * date_from (YYYY-MM-DD) optional, defaults to first of current month.
        * date_to (YYYY-MM-DD) optional, defaults to today.
        * statement_type ('open_item' default | 'activity').

        The route refuses if the user has no partner or if the partner
        is not a commercial entity (no IBAN-style identity); in that
        case we return a clear error page rather than an opaque 500.
        """
        partner = request.env.user.partner_id.commercial_partner_id
        if not partner:
            return request.render(
                'eh_account_portal_extra.portal_no_partner',
                {},
            )

        today = fields.Date.context_today(request.env.user)
        try:
            df = (
                fields.Date.from_string(date_from)
                if date_from else today.replace(day=1)
            )
            dt = (
                fields.Date.from_string(date_to)
                if date_to else today
            )
        except (ValueError, TypeError):
            return request.render(
                'eh_account_portal_extra.portal_invalid_dates',
                {},
            )
        if df > dt:
            return request.render(
                'eh_account_portal_extra.portal_invalid_dates',
                {},
            )
        if statement_type not in ('open_item', 'activity'):
            statement_type = 'open_item'

        report = request.env.ref(
            'eh_account_dynamic_reports.report_customer_statement',
            raise_if_not_found=False,
        )
        if not report:
            return request.render(
                'eh_account_portal_extra.portal_report_missing',
                {},
            )
        options = report.sudo().get_default_options()
        options['partner_id'] = partner.id
        options['date'] = {
            'mode': 'range',
            'date_from': df.isoformat(),
            'date_to': dt.isoformat(),
        }
        options['statement_type'] = statement_type
        try:
            pdf_bytes = report.sudo().render_pdf(options)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "Customer portal statement render failed for "
                "partner %s: %s",
                partner.display_name, exc,
            )
            raise UserError(_(
                "Could not generate your statement at this time. "
                "Please contact support if the issue persists.",
            )) from exc

        # Persist the PDF as an attachment so the portal user sees it under My
        # Documents. Reuse an existing statement for the same partner + date
        # range instead of creating a new row on every GET: an unbounded
        # create-per-request lets a portal user (or a crawler) exhaust the
        # filestore. Dedup by the deterministic name (which encodes partner +
        # range) caps it to one attachment per statement.
        att_name = "Statement_%s_%s_to_%s.pdf" % (
            partner.name.replace(' ', '_'),
            df.isoformat(),
            dt.isoformat(),
        )
        Attachment = request.env['ir.attachment'].sudo()
        attachment = Attachment.search([
            ('res_model', '=', 'res.partner'),
            ('res_id', '=', partner.id),
            ('name', '=', att_name),
        ], limit=1)
        att_vals = {
            'name': att_name,
            'type': 'binary',
            'datas': base64.b64encode(pdf_bytes),
            'mimetype': 'application/pdf',
            'res_model': 'res.partner',
            'res_id': partner.id,
        }
        if attachment:
            attachment.write({'datas': att_vals['datas']})
        else:
            attachment = Attachment.create(att_vals)
        # RFC 5987 encode the filename: partner.name is embedded in
        # attachment.name and may contain non-Latin-1, quote, newline or
        # control characters that would produce an invalid or injectable
        # Content-Disposition header (werkzeug rejects non-Latin-1 header
        # values outright). filename* carries the full UTF-8 name safely.
        disposition = "inline; filename*=UTF-8''%s" % quote(
            attachment.name, safe='',
        )
        return request.make_response(
            pdf_bytes,
            headers=[
                ('Content-Type', 'application/pdf'),
                ('Content-Disposition', disposition),
                ('Content-Length', str(len(pdf_bytes))),
            ],
        )

    @http.route(
        ['/my/eh/balance'],
        type='json', auth='user',
    )
    def portal_eh_balance(self):
        """Return JSON balance summary for the logged-in partner.

        Used by the portal home card to refresh the figure without a
        full page reload. Returns a flat dict with amount, days
        overdue, and the partner display name; no PII beyond what the
        partner already has access to about themselves.
        """
        partner = request.env.user.partner_id.commercial_partner_id
        if not partner:
            return {'error': 'no_partner'}
        today = fields.Date.context_today(request.env.user)
        AML = request.env['account.move.line'].sudo()
        overdue = AML.search(
            [
                ('partner_id', '=', partner.id),
                ('account_id.account_type', '=', 'asset_receivable'),
                ('parent_state', '=', 'posted'),
                ('amount_residual', '!=', 0),
                ('date_maturity', '<', today),
            ],
        )
        total = sum(overdue.mapped('amount_residual'))
        oldest = (
            min(overdue.mapped('date_maturity'))
            if overdue else False
        )
        return {
            'partner_name': partner.display_name,
            'total_overdue': float(total),
            'days_overdue_max': (today - oldest).days if oldest else 0,
            'oldest_due_date': oldest.isoformat() if oldest else None,
            'currency': (
                partner.currency_id.name
                if partner.currency_id
                else (
                    partner.company_id.currency_id.name
                    if partner.company_id else 'AUD'
                )
            ),
        }
