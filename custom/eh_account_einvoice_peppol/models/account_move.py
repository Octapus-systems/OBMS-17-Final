# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Adapter on `account.move` that wraps the Peppol UBL generator.

Two model methods:

* `action_eh_export_peppol_xml` -- render the move to UBL 2.1 bytes,
  attach as `ir.attachment`, return the download action. Available
  on out_invoice / out_refund moves only; raises on draft moves
  because Peppol invoices are by definition issued documents.
* `_eh_build_peppol_payload` -- pure data transform from the move to
  the generator's dict shape. Override this on a localization to
  enrich tax-category mapping or partner-id schemes per jurisdiction.
"""

import base64

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
    PeppolGeneratorError, make_invoice_payload, render_invoice_xml,
    validate_rendered,
)
from odoo.addons.eh_account_einvoice_peppol.tools.peppol_id_validator import (
    PeppolIdentifierError, validate_participant,
)
from odoo.addons.eh_account_einvoice_peppol.tools import access_point_registry
from odoo.addons.eh_edi_core.tools.en16931 import mapper as core_mapper


_MOVE_TYPE_TO_DOC = {
    'out_invoice': 'invoice',
    'out_refund': 'credit_note',
}


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_peppol_endpoint_scheme = fields.Char(
        related='partner_id.eh_peppol_endpoint_scheme', readonly=True,
    )
    eh_peppol_endpoint_id = fields.Char(
        related='partner_id.eh_peppol_endpoint_id', readonly=True,
    )
    eh_peppol_ready = fields.Boolean(
        compute='_compute_eh_peppol_ready',
        help=(
            "True when the partner has both a Peppol endpoint id "
            "and a scheme set. Surfaces on the move form so the "
            "operator knows whether they can press Export Peppol."
        ),
    )

    # ---- transmission audit ----
    eh_peppol_transmission_status = fields.Selection(
        [
            ('not_sent', "Not sent"),
            ('queued', "Queued"),
            ('sent', "Sent"),
            ('delivered', "Delivered"),
            ('error', "Error"),
        ],
        default='not_sent', readonly=True, copy=False, tracking=True,
    )
    eh_peppol_transmission_id = fields.Char(readonly=True, copy=False)
    eh_peppol_ap_key = fields.Char(
        string="Sent via access point", readonly=True, copy=False,
    )
    eh_peppol_sent_at = fields.Datetime(readonly=True, copy=False)
    eh_peppol_transmission_message = fields.Text(readonly=True, copy=False)

    @api.depends('partner_id.eh_peppol_endpoint_id',
                 'partner_id.eh_peppol_endpoint_scheme')
    def _compute_eh_peppol_ready(self):
        for move in self:
            partner = move.partner_id.commercial_partner_id or move.partner_id
            move.eh_peppol_ready = bool(
                partner
                and (partner.eh_peppol_endpoint_id or partner.vat or partner.email)
                and partner.eh_peppol_endpoint_scheme
            )

    def action_eh_export_peppol_xml(self):
        """Render the move to UBL 2.1 XML and offer it as a download."""
        self.ensure_one()
        if self.state != 'posted':
            raise UserError(_(
                "Peppol export requires a posted invoice; the move is %s."
            ) % self.state)
        if self.move_type not in _MOVE_TYPE_TO_DOC:
            raise UserError(_(
                "Peppol export is only defined for customer invoices and "
                "credit notes (got move_type=%s)."
            ) % self.move_type)
        try:
            payload = self._eh_build_peppol_payload()
            xml_bytes = render_invoice_xml(payload)
            # Structural sanity-check: catches bugs in the generator
            # (missing mandatory tags, currency mismatch, sum drift)
            # before the file goes to the access point. Cheap (~ms on
            # a typical invoice); always run it.
            validate_rendered(xml_bytes)
        except PeppolGeneratorError as exc:
            raise UserError(_("Peppol generator rejected the input: %s") % exc)
        attachment = self.env['ir.attachment'].create({
            'name': "%s.peppol.xml" % (self.name or 'invoice'),
            'type': 'binary',
            'datas': base64.b64encode(xml_bytes),
            'mimetype': 'application/xml',
            'res_model': self._name,
            'res_id': self.id,
        })
        self.message_post(body=_(
            "Peppol UBL 2.1 XML generated; %s bytes."
        ) % len(xml_bytes), attachment_ids=[attachment.id])
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'download',
        }

    def _eh_peppol_recipient(self):
        """Resolve the recipient (endpoint_id, scheme) for transmission,
        applying the same country fallback as the party builder.
        """
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id or self.partner_id
        country_code = (partner.country_id.code
                        or self.company_id.country_id.code or '')
        endpoint_id = (partner.eh_peppol_endpoint_id
                       or partner.vat or partner.email or partner.name or '')
        scheme = (partner.eh_peppol_endpoint_scheme
                  or self._EH_PEPPOL_SCHEME_BY_COUNTRY.get(
                      country_code, '0192'))
        return endpoint_id, scheme

    def action_eh_send_peppol(self):
        """Render the move and transmit it through the company's
        registered access-point adapter.

        Works with any registered adapter: the 'manual' default returns
        a 'queued' status (the deployment routes the attached XML by its
        own channel); a live adapter (commercial AP or self-host AS4)
        returns a real transmission id. The result is recorded on the
        move for the audit trail, and the sent XML is attached.
        """
        self.ensure_one()
        if self.state != 'posted':
            raise UserError(_(
                "Peppol transmission requires a posted invoice; the "
                "move is %s.") % self.state)
        if self.move_type not in _MOVE_TYPE_TO_DOC:
            raise UserError(_(
                "Peppol transmission is only defined for customer "
                "invoices and credit notes (got move_type=%s).")
                % self.move_type)
        # Idempotency: refuse to re-transmit a document that already left for
        # the access point. A live adapter genuinely re-submits on every call,
        # so a second click / browser retry / another user opening the invoice
        # would book the same legal e-invoice twice on the buyer side. Take a
        # row lock first so two concurrent sends serialise: the second blocks,
        # re-reads the now-'sent' status, and is refused here. Only 'error' /
        # 'not_sent' may (re-)send; a correction needs an explicit void/recut.
        self.flush_recordset(['eh_peppol_transmission_status'])
        self.env.cr.execute(
            'SELECT id FROM account_move WHERE id = %s FOR UPDATE',
            (self.id,),
        )
        self.invalidate_recordset(['eh_peppol_transmission_status'])
        if self.eh_peppol_transmission_status in (
                'queued', 'sent', 'delivered'):
            raise UserError(_(
                "Invoice %(name)s was already transmitted via Peppol "
                "(status %(status)s, id %(tid)s). Re-sending would emit a "
                "second legal e-invoice to the buyer. Void the "
                "transmission and re-cut the document if a correction is "
                "needed.",
                name=self.name or 'invoice',
                status=self.eh_peppol_transmission_status,
                tid=self.eh_peppol_transmission_id or 'n/a',
            ))
        company = self.company_id
        key = company.eh_peppol_access_point_key or 'manual'
        if not access_point_registry.has_adapter(key):
            raise UserError(_(
                "No Peppol access-point adapter is registered for key "
                "%r. Install the corresponding ERP Heritage adapter "
                "module, or set the company access point to 'manual'.")
                % key)
        try:
            payload = self._eh_build_peppol_payload()
            xml_bytes = render_invoice_xml(payload)
            validate_rendered(xml_bytes)
        except PeppolGeneratorError as exc:
            raise UserError(
                _("Peppol generator rejected the input: %s") % exc)
        endpoint_id, scheme = self._eh_peppol_recipient()
        config = self.env['eh.peppol.inbound']._eh_company_config(company)
        try:
            adapter = access_point_registry.get_adapter(key, config)
            result = adapter.submit(xml_bytes, endpoint_id, scheme) or {}
        except access_point_registry.AccessPointError as exc:
            # Record the failure on the move and surface it as a
            # notification rather than raising: a UserError would roll
            # the transaction back and lose the error status, leaving no
            # trace of the failed attempt.
            self.write({
                'eh_peppol_transmission_status': 'error',
                'eh_peppol_ap_key': key,
                'eh_peppol_sent_at': fields.Datetime.now(),
                'eh_peppol_transmission_message': str(exc)[:8000],
            })
            self.message_post(
                body=_("Peppol transmission failed: %s") % exc)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Peppol transmission failed"),
                    'message': str(exc),
                    'type': 'danger',
                    'sticky': True,
                },
            }
        status = result.get('status') or 'sent'
        if status not in ('queued', 'sent', 'delivered', 'error'):
            status = 'sent'
        raw = result.get('raw_response')
        attachment = self.env['ir.attachment'].create({
            'name': "%s.peppol.xml" % (self.name or 'invoice'),
            'type': 'binary',
            'datas': base64.b64encode(xml_bytes),
            'mimetype': 'application/xml',
            'res_model': self._name,
            'res_id': self.id,
        })
        self.write({
            'eh_peppol_transmission_status': status,
            'eh_peppol_transmission_id': result.get('transmission_id') or '',
            'eh_peppol_ap_key': key,
            'eh_peppol_sent_at': fields.Datetime.now(),
            'eh_peppol_transmission_message': (
                raw[:8000] if isinstance(raw, str) else ''),
        })
        self.message_post(
            body=_(
                "Peppol invoice transmitted via %(ap)s (status "
                "%(status)s, id %(tid)s).",
                ap=key, status=status,
                tid=result.get('transmission_id') or 'n/a',
            ),
            attachment_ids=[attachment.id],
        )
        return True

    def action_eh_void_peppol_transmission(self):
        """Retire an UNCONFIRMED Peppol transmission so the move can be re-cut.

        action_eh_send_peppol refuses to re-transmit a document whose status is
        'queued'/'sent'/'delivered', so a double click, a browser retry, or a
        second operator cannot emit the legal e-invoice twice. That guard has no
        escape hatch on its own, and the out-of-box 'manual' adapter always
        parks the move at 'queued' (the deployment routes the attached XML by
        its own channel). That out-of-band routing can legitimately fail (wrong
        endpoint, lost file), leaving a stranded 'queued' move that no UI path
        could re-send.

        This manager-only action resets such an unconfirmed transmission --
        only 'queued' or 'error' -- back to 'not_sent' so a corrected file can
        be sent again, and records who voided it plus the prior transmission id
        in the chatter. It NEVER touches a confirmed 'sent'/'delivered'
        transmission: a genuinely delivered legal e-invoice can still never be
        re-emitted, so the double-send protection stays intact. The re-send
        guard itself is unchanged; the only way past it remains this deliberate,
        gated, audited reset.
        """
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can void a Peppol "
                "transmission for re-cut."))
        if self.eh_peppol_transmission_status not in ('queued', 'error'):
            raise UserError(_(
                "Invoice %(name)s has no unconfirmed Peppol transmission to "
                "void (status %(status)s). Only a 'queued' or 'error' "
                "transmission may be reset; a confirmed 'sent'/'delivered' "
                "e-invoice cannot be re-cut.",
                name=self.name or 'invoice',
                status=self.eh_peppol_transmission_status,
            ))
        prior_status = self.eh_peppol_transmission_status
        prior_tid = self.eh_peppol_transmission_id or 'n/a'
        self.write({
            'eh_peppol_transmission_status': 'not_sent',
            'eh_peppol_transmission_id': False,
            'eh_peppol_transmission_message': False,
            'eh_peppol_ap_key': False,
            'eh_peppol_sent_at': False,
        })
        self.message_post(body=_(
            "Peppol transmission voided for re-cut by %(user)s (was "
            "%(status)s, id %(tid)s). Any file already routed for this "
            "invoice must NOT be treated as the final e-invoice; the next "
            "Send supersedes it.",
            user=self.env.user.display_name,
            status=prior_status,
            tid=prior_tid,
        ))
        return True

    def _eh_build_peppol_payload(self):
        """Compose the UBL generator dict from this move.

        Override on a localization to map jurisdiction-specific tax
        codes (S/Z/E/AE/G/O) and partner identification schemes.
        The default implementation resolves each line's EN 16931 tax
        category from the tax configuration (the eh_edi_tax_category on
        account.tax, else the rate rule), carries the exemption reason
        for the no-tax categories, and surfaces an explicit error when
        the move has no exportable lines at all.
        """
        self.ensure_one()
        document_type = _MOVE_TYPE_TO_DOC[self.move_type]
        lines, tax_summary = self._eh_build_peppol_lines_and_tax()
        return make_invoice_payload(
            invoice_number=self.name,
            issue_date=self.invoice_date,
            due_date=self.invoice_date_due or self.invoice_date,
            currency_code=self.currency_id.name or 'EUR',
            supplier=self._eh_build_peppol_party(self.company_id.partner_id),
            customer=self._eh_build_peppol_party(self.partner_id),
            lines=lines,
            tax_categories=tax_summary,
            document_type=document_type,
            note=self.narration or '',
            buyer_reference=self.partner_id.ref or '',
            order_reference=self.invoice_origin or '',
        )

    # Country-based fallback scheme when the partner has not picked
    # one explicitly. Maps a country ISO-2 code to the OpenPeppol
    # scheme that participants from that country are typically
    # registered under. Unknown countries fall through to 0192
    # (NO Organisasjonsnummer) which keeps legacy behaviour.
    _EH_PEPPOL_SCHEME_BY_COUNTRY = {
        'AU': '0151',
        'NZ': '0151',
        'DK': '0184',
        'NO': '0192',
        'SE': '0007',
        'DE': '9930',
        'IS': '0196',
    }

    def _eh_build_peppol_party(self, partner):
        partner = partner.commercial_partner_id or partner
        country_code = (partner.country_id.code
                        or self.company_id.country_id.code or '')
        endpoint_id = (partner.eh_peppol_endpoint_id
                       or partner.vat or partner.email
                       or partner.name)
        default_scheme = self._EH_PEPPOL_SCHEME_BY_COUNTRY.get(
            country_code, '0192',
        )
        scheme = partner.eh_peppol_endpoint_scheme or default_scheme
        # Validate the participant identifier against the OpenPeppol
        # scheme rules. A bad ABN, malformed GLN, or made-up scheme
        # would otherwise pass through to the XML and bounce at the
        # access point. Failing here surfaces a precise message naming
        # which scheme rule the partner data violates so the user can
        # fix the record before re-trying. The transformation also
        # canonicalises the identifier (strips spaces, dashes, dots).
        try:
            scheme, endpoint_id = validate_participant(scheme, endpoint_id)
        except PeppolIdentifierError as exc:
            raise UserError(_(
                "Peppol identifier on partner %(partner)s is invalid: "
                "%(reason)s. Fix the participant id on the partner form "
                "(scheme + endpoint id) and retry.",
                partner=partner.display_name,
                reason=str(exc),
            ))
        return {
            'name': partner.name,
            'endpoint_id': endpoint_id,
            'endpoint_scheme': scheme,
            'country_code': country_code,
            'vat_id': partner.vat or '',
            'legal_id': partner.eh_peppol_legal_id or partner.vat or '',
            'address': {
                'street': partner.street or '',
                'city': partner.city or '',
                'postcode': partner.zip or '',
                'country': country_code,
            },
        }

    def _eh_build_peppol_lines_and_tax(self):
        """Return (lines, tax_categories) tuples for the generator.

        Line semantics (description, quantity, unit code, price, totals,
        tax category and rate) come from the shared eh_edi_core EN 16931
        mapper, so the account.move reading lives in one place.

        The per-category tax breakdown is built from the posted move's
        booked tax lines, not from the line side. Each booked tax line
        carries both the base it was computed against (tax_base_amount,
        BT-116) and the tax it booked (amount_currency, BT-117), so a
        line that carries two (or more) taxes contributes its base and
        tax to EACH tax's category bucket, split per the ledger. Keying
        the buckets off the whole line's first tax would credit the entire
        line base to one category and leave every other category with tax
        but no base, so the category base/tax split would be wrong on any
        multi-tax line. Reading the booked figures also ties the UBL
        exactly to the ledger even when a line carries a discount or the
        tax engine rounded per line, which is what EN 16931 BR-CO-14
        (category tax = sum of line tax) requires against the actual
        invoice. Untaxed lines have no booked tax line, so their base is
        carried into the by-rate zero bucket from the line subtotal,
        byte identical to the historical zero-rated output.
        """
        model = core_mapper.map_move(self)
        booked = self._eh_booked_tax_by_bucket()
        lines = []
        tax_buckets = {}
        for i, ln in enumerate(model.lines, start=1):
            # The EN 16931 tax category (S/Z/E/AE/G/O) is resolved once in
            # the shared mapper from the tax configuration, so exempt (E),
            # reverse charge (AE), export (G) and out-of-scope (O) supplies
            # keep their own category instead of collapsing to Z by rate.
            cat_code = ln.tax_category or 'S'
            lines.append({
                'id': i,
                'description': ln.name,
                'quantity': ln.quantity,
                'unit_code': ln.unit_code,
                'unit_price': ln.price_unit,
                'line_total': ln.line_net,
                'tax_category_code': cat_code,
                'tax_rate_pct': ln.tax_rate,
            })
        # Category buckets from the booked tax lines: correct base and tax
        # per tax, so a multi-tax line splits across every category.
        for key, booked_bucket in booked.items():
            tax_buckets[key] = {
                'category_code': key[0],
                'rate_pct': key[1],
                'taxable_amount': booked_bucket['base'],
                'tax_amount': booked_bucket['tax'],
                'exemption_reason': booked_bucket['reason'],
            }
        # Untaxed lines book no tax line, so seed their category bucket
        # from the line subtotal. This is what keeps the zero-rated case
        # (and any line carrying no account.tax) byte identical.
        for ln in model.lines:
            if ln.tax_rate == 0.0:
                key = (ln.tax_category or 'S', ln.tax_rate)
                bucket = tax_buckets.setdefault(key, {
                    'category_code': key[0],
                    'rate_pct': key[1],
                    'taxable_amount': 0.0,
                    'tax_amount': 0.0,
                    'exemption_reason': ln.tax_exemption_reason or '',
                })
                # Only add the base when this line carried no booked tax
                # line under this key (a booked zero-rated tax already put
                # the base in). A zero-amount account.tax books a tax line
                # with tax_base_amount, so guard against double counting.
                if key not in booked:
                    bucket['taxable_amount'] += ln.line_net
        if not lines:
            raise PeppolGeneratorError(
                "Move %s has no exportable invoice lines." % self.name,
            )
        return lines, list(tax_buckets.values())

    def _eh_booked_tax_by_bucket(self):
        """Booked base and tax per (category_code, rate_pct) bucket read
        from the posted move's tax lines, in the move's currency.

        Each posted tax line carries the base it was computed against
        (tax_base_amount, BT-116) and the tax it booked (amount_currency,
        BT-117). Returning both means a line carrying two taxes lands its
        base and tax in each tax's own category bucket -- the ledger has
        one tax line per (line, tax), so the split is exactly what was
        posted. The values are the ledger's own figures, so serializing
        them keeps the e-invoice and the general ledger in agreement.
        Categories are resolved through the same EN 16931 mapper the lines
        use so keys match exactly. Untaxed moves (no tax lines) return an
        empty map.
        """
        buckets = {}
        for line in self.line_ids:
            tax = line.tax_line_id
            if not tax:
                continue
            rate = tax.amount or 0.0
            category = core_mapper._tax_category(tax)
            key = (category, rate)
            bucket = buckets.setdefault(key, {
                'base': 0.0, 'tax': 0.0,
                'reason': core_mapper._tax_exemption_reason(tax),
            })
            # amount_currency / tax_base_amount are signed by move
            # direction (credit for a customer invoice); take the
            # magnitude so the serialized amounts are the positive tax
            # charged and the base it applied to.
            bucket['tax'] += abs(line.amount_currency)
            bucket['base'] += abs(line.tax_base_amount)
        return buckets


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_peppol_endpoint_id = fields.Char(
        string="Peppol endpoint id",
        help=(
            "Partner's Peppol participant id. Defaults at export time to "
            "the partner's VAT or email."
        ),
    )
    eh_peppol_endpoint_scheme = fields.Char(
        string="Peppol endpoint scheme",
        default='0192',
        help=(
            "OpenPeppol scheme id for the endpoint. Examples: '0192' "
            "Norway organisation number, '0088' GLN, '0151' Australian "
            "Business Number, '9930' German VAT-id."
        ),
    )
    eh_peppol_legal_id = fields.Char(
        string="Peppol legal entity id",
        help="Company registration number used in PartyLegalEntity/CompanyID.",
    )
