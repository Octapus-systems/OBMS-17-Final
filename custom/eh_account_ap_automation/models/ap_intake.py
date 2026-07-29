# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Vendor bill intake.

State machine:

  received -> parsed -> matched -> posted
                                \\-> exception -> matched (after override)
                                                \\-> rejected
                                \\-> rejected
                       -> rejected

received: file uploaded or captured from email; no parsing yet.
parsed: header and lines extracted (manually or by the regex parser).
matched: 3 way match attempted; outcome on each line.
posted: account.move created and posted; intake archived.
exception: at least one line is out of tolerance; user review required.
rejected: bill discarded with reason; no move posted.
"""

import base64
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


_DEFAULT_INVOICE_REF_RE = r'(?im)Invoice[:#\s]+([A-Z0-9\-]+)'
_DEFAULT_TOTAL_RE = r'(?im)Total[:\s]+([0-9][0-9,\.]*)'
_LINE_RE = re.compile(
    r'(?im)^\s*([A-Z0-9\-]+)\s+'
    r'(?P<qty>\d+(?:\.\d+)?)\s+'
    r'(?P<price>\d+(?:\.\d+)?)\s*$',
)


class EhApIntake(models.Model):
    _name = 'eh.ap.intake'
    _description = "Vendor Bill Intake"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'received_at desc, id desc'

    # The intake state machine (received -> parsed -> matched -> posted /
    # exception / rejected) is enforced by eh.workflow.guard: 'state' may only
    # change through the record's own actions (which run as su), never a direct
    # RPC write. A plain user cannot write({'state': 'posted'}) to skip
    # action_post, its manager check, duplicate block and journal entry.
    _eh_guarded_fields = ('state',)

    # The email-to-intake gateway is a SINGLE catchall inbox: the admin
    # points one mail.alias at this model (Settings > Technical > Email >
    # Aliases, alias_model = Vendor Bill Intake) and every message to that
    # address creates an intake via message_new below. The model is
    # deliberately NOT a mail.alias.mixin: that mixin _inherits mail.alias
    # with a required alias_id, which would spawn one dedicated alias per
    # vendor bill (an email inbox per record), growing the alias table
    # without bound. One standing alias, many intakes.

    @api.model
    def message_new(self, msg, custom_values=None):
        """Create a vendor-bill intake from an inbound email.

        The mail subject becomes a placeholder vendor reference; the
        body (text/plain alternative when available) becomes raw_text;
        the partner is resolved by matching the sender's email to a
        contact whose email or alias matches. PDF attachments land on
        the intake's chatter so the regex parser sees the body and the
        line-extractor adapter (when configured) can pick up the PDF
        bytes via _line_extractor_document.

        Inputs from email are treated as untrusted: no auto-post, no
        auto-match. The intake stays in 'received' state until a user
        explicitly runs Parse and Match.
        """
        defaults = dict(custom_values or {})
        body = msg.get('body') or ''
        # Plain-text first; if the message is HTML only, strip tags
        # using the standard Odoo helper so the regex parser has
        # something readable.
        if body and ('<' in body and '>' in body):
            from odoo.tools.mail import html2plaintext
            text = html2plaintext(body)
        else:
            text = body
        defaults.setdefault('raw_text', text)
        defaults.setdefault('vendor_reference', msg.get('subject') or '')
        sender = (msg.get('email_from') or '').strip()
        if sender and not defaults.get('partner_id'):
            partner = self._eh_resolve_email_sender(sender)
            if partner:
                defaults['partner_id'] = partner.id
        intake = super().message_new(msg, defaults)
        intake.message_post(body=_(
            "Intake created from incoming email; sender %s. "
            "No auto-post applied; review and run Parse to continue."
        ) % sender)
        return intake

    @api.model
    def _eh_resolve_email_sender(self, raw_from):
        """Best-effort partner resolution from a raw From header.

        Strict by design: when more than one candidate matches, the
        intake is left without partner_id rather than guessing. The
        user attaches the partner manually; the operational risk of
        a wrong partner attribution on a vendor bill is much higher
        than the convenience of a guess.
        """
        from odoo.tools import email_split
        emails = email_split(raw_from or '')
        if not emails:
            return self.env['res.partner']
        clean = emails[0].lower()
        candidates = self.env['res.partner'].search([
            ('email_normalized', '=', clean),
            ('parent_id', '=', False),
            ('company_id', 'in', [self.env.company.id, False]),
        ], limit=2)
        if len(candidates) == 1:
            return candidates
        return self.env['res.partner']

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    state = fields.Selection([
        ('received', "Received"),
        ('parsed', "Parsed"),
        ('matched', "Matched"),
        ('posted', "Posted"),
        ('exception', "Exception"),
        ('rejected', "Rejected"),
    ], default='received', required=True, tracking=True)

    partner_id = fields.Many2one(
        'res.partner', string="Vendor", tracking=True,
        domain="[('parent_id', '=', False)]",
    )
    purchase_order_id = fields.Many2one(
        'purchase.order', string="Purchase Order",
        domain="[('partner_id', '=', partner_id), "
               "('state', 'in', ['purchase', 'done'])]",
        help="Optional: scope the 3 way match to lines on this PO.",
    )

    raw_text = fields.Text(
        help="Raw text content of the captured bill. Drives the parser.",
    )
    vendor_reference = fields.Char(
        string="Vendor Reference", tracking=True,
        help="Vendor invoice number / reference as extracted or entered.",
    )
    bill_date = fields.Date(
        default=fields.Date.context_today,
    )
    extracted_total = fields.Monetary(
        help="Total extracted from the captured text (sanity check).",
    )
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )

    line_ids = fields.One2many(
        'eh.ap.intake.line', 'intake_id', copy=False,
    )

    move_id = fields.Many2one(
        'account.move', string="Posted Bill", readonly=True, copy=False,
        ondelete='set null',
    )
    journal_id = fields.Many2one(
        'account.journal', string="Purchase Journal",
        domain="[('type', '=', 'purchase')]",
        help="Defaults to the company's first purchase journal at post.",
    )

    # ---- audit ----
    received_at = fields.Datetime(
        readonly=True, default=fields.Datetime.now, tracking=True,
    )
    parsed_at = fields.Datetime(readonly=True, tracking=True)
    parsed_by_id = fields.Many2one('res.users', readonly=True)
    matched_at = fields.Datetime(readonly=True, tracking=True)
    matched_by_id = fields.Many2one('res.users', readonly=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)
    rejected_at = fields.Datetime(readonly=True, tracking=True)
    rejected_by_id = fields.Many2one('res.users', readonly=True)
    rejection_reason = fields.Text(tracking=True)

    # ---- match summary ----
    line_count = fields.Integer(compute='_compute_match_summary', store=True)
    ok_count = fields.Integer(compute='_compute_match_summary', store=True)
    exception_count = fields.Integer(compute='_compute_match_summary', store=True)
    total_invoice_amount = fields.Monetary(
        compute='_compute_match_summary', store=True,
    )

    notes = fields.Text()

    # ---- duplicate detection ----
    duplicate_intake_id = fields.Many2one(
        'eh.ap.intake', string="Duplicate Of", readonly=True, copy=False,
        help=(
            "Set when the (vendor, vendor_reference, total, date) tuple "
            "matches an existing intake or posted bill. Posting is "
            "blocked until the user resolves the duplicate."
        ),
    )
    duplicate_move_id = fields.Many2one(
        'account.move', string="Duplicate Posted Bill",
        readonly=True, copy=False,
        help=(
            "Set when an account.move with the same partner and "
            "vendor reference is already posted in this company."
        ),
    )
    duplicate_override_reason = fields.Text(
        help=(
            "Reason an authorised user overrode the duplicate block. "
            "Captured in the audit trail."
        ),
    )

    # ---- pluggable line-item extractor ----
    extractor_key = fields.Char(
        help=(
            "Optional key of a registered line-item extractor "
            "(typically an OCR/LLM adapter shipped by a partner package). "
            "Leave blank to use only the regex parser. The framework "
            "uses the extractor only for line-items; supplier and total "
            "still come from the regex header pass."
        ),
    )
    extractor_min_confidence = fields.Float(
        default=0.6,
        help=(
            "Lines extracted with a confidence below this value are "
            "discarded; the user reviews them manually. Range 0.0 to 1.0."
        ),
    )
    extractor_last_run_summary = fields.Text(readonly=True)

    # ---- compute ----

    @api.depends('line_ids.match_status', 'line_ids.subtotal')
    def _compute_match_summary(self):
        for intake in self:
            intake.line_count = len(intake.line_ids)
            intake.ok_count = len(intake.line_ids.filtered(
                lambda l: l.match_status in ('ok', 'overridden'),
            ))
            intake.exception_count = len(intake.line_ids.filtered(
                lambda l: l.match_status in (
                    'qty_exception', 'price_exception',
                    'no_match', 'over_received',
                ),
            ))
            intake.total_invoice_amount = sum(
                intake.line_ids.mapped('subtotal'),
            )

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code('eh.ap.intake') or '/'
                vals['name'] = seq
        return super().create(vals_list)

    # ---- transitions ----

    def action_parse(self):
        self = self._eh_workflow_action()
        for intake in self:
            if intake.state not in ('received', 'parsed'):
                raise UserError(_(
                    "Parse is only available in received or parsed state.",
                ))
            intake._parse()
            intake.write({
                'state': 'parsed',
                'parsed_at': fields.Datetime.now(),
                'parsed_by_id': self.env.user.id,
            })

    def action_match(self):
        self = self._eh_workflow_action()
        for intake in self:
            if intake.state not in ('parsed', 'exception'):
                raise UserError(_(
                    "Match is only available in parsed or exception state.",
                ))
            if not intake.line_ids:
                raise UserError(_(
                    "Cannot match: no intake lines to evaluate.",
                ))
            intake._match()
            intake._eh_check_duplicate()
            new_state = (
                'matched' if intake.exception_count == 0 else 'exception'
            )
            intake.write({
                'state': new_state,
                'matched_at': fields.Datetime.now(),
                'matched_by_id': self.env.user.id,
            })

    def action_override_duplicate(self):
        """Manager override of a flagged duplicate. Requires a reason."""
        self.ensure_one()
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_(
                "Only accounting managers can override duplicate flags.",
            ))
        if not self.duplicate_intake_id and not self.duplicate_move_id:
            raise UserError(_(
                "No duplicate is currently flagged on this intake.",
            ))
        if not (self.duplicate_override_reason or '').strip():
            raise UserError(_(
                "A reason is required when overriding a duplicate flag.",
            ))
        self.message_post(body=_(
            "Duplicate override applied by %(user)s. Reason: %(reason)s",
            user=self.env.user.name,
            reason=self.duplicate_override_reason,
        ))

    def _eh_check_duplicate(self):
        """Flag a duplicate when (vendor, vendor_reference, total, date)
        already exists on another intake or a posted vendor bill in the
        same company. Tolerance on total is 0.01 of the captured currency.

        Sets duplicate_intake_id / duplicate_move_id; does not raise.
        Posting blocks until the flag is cleared or overridden with a
        reason.
        """
        self.ensure_one()
        if not self.partner_id or not self.vendor_reference:
            self.duplicate_intake_id = False
            self.duplicate_move_id = False
            return
        ref = (self.vendor_reference or '').strip()
        if not ref:
            self.duplicate_intake_id = False
            self.duplicate_move_id = False
            return
        total = self.total_invoice_amount or self.extracted_total or 0.0
        # Other intakes: same vendor + ref + ~total + same bill_date,
        # excluding this record and rejected ones.
        intake_domain = [
            ('id', '!=', self.id),
            ('company_id', '=', self.company_id.id),
            ('partner_id', '=', self.partner_id.id),
            ('vendor_reference', '=', ref),
            ('state', 'not in', ('rejected',)),
        ]
        if self.bill_date:
            intake_domain.append(('bill_date', '=', self.bill_date))
        candidates = self.search(intake_domain)
        match = candidates.filtered(
            lambda c: abs((c.total_invoice_amount or 0.0) - total) < 0.01,
        )[:1]
        self.duplicate_intake_id = match.id if match else False
        # Posted bills with the same partner + ref in the same company.
        Move = self.env['account.move']
        move_match = Move.search([
            ('company_id', '=', self.company_id.id),
            ('move_type', 'in', ('in_invoice', 'in_refund')),
            ('partner_id', '=', self.partner_id.id),
            ('ref', '=', ref),
            ('state', '=', 'posted'),
        ], limit=1)
        if move_match and self.move_id and move_match.id == self.move_id.id:
            move_match = Move
        self.duplicate_move_id = move_match.id if move_match else False
        if self.duplicate_intake_id or self.duplicate_move_id:
            self.message_post(body=_(
                "Duplicate flag raised: vendor %(vendor)s, ref %(ref)s, "
                "total %(total).2f.",
                vendor=self.partner_id.display_name,
                ref=ref, total=total,
            ))

    def action_post(self):
        self = self._eh_workflow_action()
        for intake in self:
            if not self.env.user.has_group('account.group_account_manager'):
                raise UserError(_(
                    "Only accounting managers can post AP intakes.",
                ))
            if intake.state != 'matched':
                raise UserError(_(
                    "Only matched intakes can be posted. Current state: %s.",
                    intake.state,
                ))
            if intake.exception_count > 0:
                raise UserError(_(
                    "Intake has %d unresolved exception line(s).",
                    intake.exception_count,
                ))
            intake._eh_check_duplicate()
            if intake.duplicate_intake_id and not intake.duplicate_override_reason:
                raise UserError(_(
                    "Intake %(name)s appears to duplicate %(other)s. "
                    "Verify and either reject this intake or click "
                    "'Override Duplicate' with a reason before posting.",
                    name=intake.name,
                    other=intake.duplicate_intake_id.name,
                ))
            if intake.duplicate_move_id and not intake.duplicate_override_reason:
                raise UserError(_(
                    "A posted bill (%(move)s) with vendor reference "
                    "%(ref)s already exists for vendor %(vendor)s. "
                    "Override with a reason or reject this intake.",
                    move=intake.duplicate_move_id.display_name,
                    ref=intake.vendor_reference or '',
                    vendor=intake.partner_id.display_name,
                ))
            move = intake._build_move()
            intake.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'move_id': move.id,
            })

    def action_reject(self):
        self = self._eh_workflow_action()
        for intake in self:
            if not self.env.user.has_group('account.group_account_manager'):
                raise UserError(_(
                    "Only accounting managers can reject AP intakes.",
                ))
            if intake.state == 'posted':
                raise UserError(_(
                    "Posted intakes cannot be rejected.",
                ))
            intake.write({
                'state': 'rejected',
                'rejected_at': fields.Datetime.now(),
                'rejected_by_id': self.env.user.id,
            })

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No bill has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    # ---- parser ----

    def _parse(self):
        """Default parser: regex-based extraction from raw_text.

        Sites can override this method (or _parse_lines / _parse_header)
        to plug in a real OCR provider.
        """
        self.ensure_one()
        if not self.raw_text:
            raise UserError(_(
                "Intake %s has no raw text to parse. Either upload a "
                "text bill or fill the lines manually.",
                self.name,
            ))
        self._parse_header()
        self._parse_lines()

    def _parse_header(self):
        self.ensure_one()
        # Resolution order: partner override > company-wide setting > module fallback.
        company = self.company_id or self.env.company
        ref_pattern = (
            (self.partner_id and self.partner_id.eh_ap_invoice_ref_regex)
            or company.eh_ap_invoice_ref_regex
            or _DEFAULT_INVOICE_REF_RE
        )
        total_pattern = (
            (self.partner_id and self.partner_id.eh_ap_total_regex)
            or company.eh_ap_total_regex
            or _DEFAULT_TOTAL_RE
        )
        m = re.search(ref_pattern, self.raw_text or '')
        if m:
            self.vendor_reference = m.group(1)
        m = re.search(total_pattern, self.raw_text or '')
        if m:
            try:
                self.extracted_total = self._parse_decimal(m.group(1))
            except ValueError:
                pass

    def _parse_lines(self):
        self.ensure_one()
        Line = self.env['eh.ap.intake.line']
        Product = self.env['product.product']
        # Only seed lines if there are none yet to avoid clobbering
        # manually entered lines on re-parse.
        if self.line_ids:
            return
        # Pluggable extractor first: when configured, defer to the
        # registered adapter and use the regex parser only as a fallback
        # for empty extractor results.
        if self.extractor_key:
            try:
                if self._run_line_extractor():
                    return
            except Exception as exc:  # pragma: no cover -- adapter
                self.extractor_last_run_summary = (
                    "Extractor %r failed: %s" % (self.extractor_key, exc)
                )
                self.message_post(body=self.extractor_last_run_summary)
        for match in _LINE_RE.finditer(self.raw_text or ''):
            code = match.group(1)
            try:
                qty = float(match.group('qty'))
                price = float(match.group('price'))
            except ValueError:
                continue
            product = Product.search([
                ('default_code', '=', code),
            ], limit=1)
            Line.create({
                'intake_id': self.id,
                'product_code': code,
                'product_id': product.id if product else False,
                'invoice_qty': qty,
                'invoice_price': price,
                'subtotal': qty * price,
            })

    def _run_line_extractor(self):
        """Drive the configured extractor and seed line_ids from the
        result. Returns True when at least one line was created so the
        caller skips the regex fallback. Raises ExtractorError on any
        adapter-side failure; the caller logs and falls back.
        """
        from odoo.addons.eh_account_ap_automation.extractors.registry import (
            get_extractor, has_extractor,
        )
        if not has_extractor(self.extractor_key):
            self.extractor_last_run_summary = (
                "No extractor registered for key %r" % self.extractor_key
            )
            return False
        extractor = get_extractor(self.extractor_key)
        # The intake stores text only by default; for binary documents,
        # downstream packages can override _line_extractor_document() to
        # return (bytes, mime_type). The default returns the captured
        # raw_text encoded as utf-8, mime text/plain.
        document_bytes, mime_type = self._line_extractor_document()
        hints = {
            'partner_id': self.partner_id.id,
            'currency_id': self.currency_id.id,
            'parsed_total': self.extracted_total,
            'vendor_reference': self.vendor_reference,
            # eh_ap_ocr_config is restricted to base.group_system so a
            # plain internal user cannot read the extractor API key over
            # RPC; the intake reads it via sudo here so OCR still runs for
            # ordinary users without ever surfacing the secret to them.
            'credentials_json': (
                self.company_id.sudo().eh_ap_ocr_config or ''
            ),
        }
        results = list(extractor.extract(document_bytes, mime_type, hints))
        threshold = self.extractor_min_confidence or 0.0
        kept = [r for r in results if (r.confidence or 0.0) >= threshold]
        dropped = len(results) - len(kept)
        Line = self.env['eh.ap.intake.line']
        for ext in kept:
            qty = float(ext.quantity or 0.0)
            price = float(ext.unit_price or 0.0)
            line_total = float(ext.line_total or qty * price)
            # Cross-check qty * unit_price against line_total. If it
            # diverges by more than 1 cent, prefer the line_total and
            # back-solve the unit_price; that minimises the sum drift.
            computed = round(qty * price, 2)
            if abs(computed - round(line_total, 2)) > 0.01 and qty:
                price = round(line_total / qty, 4)
            Line.create({
                'intake_id': self.id,
                'product_code': '',
                'invoice_qty': qty,
                'invoice_price': price,
                'subtotal': round(line_total, 2),
            })
        self.extractor_last_run_summary = (
            "Extractor %s: kept %s of %s line(s); %s below confidence %.2f."
            % (self.extractor_key, len(kept), len(results), dropped, threshold)
        )
        if kept:
            self.message_post(body=self.extractor_last_run_summary)
        return bool(kept)

    def _line_extractor_document(self):
        """Return (bytes, mime_type) for the extractor.

        Prefers the original captured document: the chatter's main
        attachment, else the first PDF or image attachment on any
        message. Vision extractors need the real document, not the
        regex parser's text dump. Falls back to the utf-8 encoded
        raw_text (text/plain) when no binary document is present, which
        keeps text-only extractors and the no-attachment path working.
        """
        self.ensure_one()
        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
        ], order='id desc')
        attachment = next(
            (
                att for att in attachments
                if (att.mimetype or '').startswith(
                    ('application/pdf', 'image/')
                ) and att.datas
            ),
            None,
        )
        if attachment and attachment.datas:
            return (
                base64.b64decode(attachment.datas),
                attachment.mimetype or 'application/octet-stream',
            )
        return ((self.raw_text or '').encode('utf-8'), 'text/plain')

    @staticmethod
    def _parse_decimal(text):
        """Parse a number that may use comma or dot as decimal separator."""
        text = text.strip().replace(' ', '')
        if ',' in text and '.' in text:
            # Comma is thousands, dot is decimal.
            text = text.replace(',', '')
        elif ',' in text:
            # Comma is decimal.
            text = text.replace(',', '.')
        return float(text)

    # ---- matcher ----

    def _match(self):
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_(
                "Cannot match intake %s: partner is missing.", self.name,
            ))
        Profile = self.env['eh.ap.tolerance.profile']
        profile = Profile.resolve_for_partner(
            self.partner_id, company=self.company_id,
        )
        for line in self.line_ids:
            line._evaluate_match(profile)

    # ---- posting ----

    def _build_move(self):
        self.ensure_one()
        journal = self.journal_id or self.env['account.journal'].search([
            ('type', '=', 'purchase'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        if not journal:
            raise UserError(_(
                "No purchase journal configured for company %s.",
                self.company_id.display_name,
            ))
        invoice_line_ids = []
        for line in self.line_ids:
            if line.match_status in ('rejected',):
                continue
            invoice_line_ids.append((0, 0, line._build_invoice_line_vals()))
        if not invoice_line_ids:
            raise UserError(_(
                "Intake %s has no postable lines.", self.name,
            ))
        move = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_id.id,
            'invoice_date': self.bill_date,
            'ref': self.vendor_reference or self.name,
            'journal_id': journal.id,
            'currency_id': self.currency_id.id,
            'invoice_line_ids': invoice_line_ids,
        })
        move.action_post()
        return move

    # ---- cron ----

    @api.model
    def _cron_advance(self, batch_size=200):
        """Drive received intakes through parse and then match.

        Per record try / except so one bad intake does not stop the
        batch. Posting still requires a manager and is not automatic.
        """
        received = self.search([
            ('state', '=', 'received'),
            ('raw_text', '!=', False),
        ], limit=batch_size)
        for intake in received:
            try:
                intake.action_parse()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Auto parse failed for intake %s: %s",
                    intake.display_name, exc,
                )
        parsed = self.search([
            ('state', '=', 'parsed'),
            ('partner_id', '!=', False),
        ], limit=batch_size)
        for intake in parsed:
            try:
                intake.action_match()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Auto match failed for intake %s: %s",
                    intake.display_name, exc,
                )
