# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move -> canonical EN 16931 InvoiceModel.

The single place that reads Odoo fields for outbound e-invoicing. Lives
in tools so it can be unit-tested with stub records; the Odoo-facing
adapters pass a real recordset.

Optional, module-specific fields (the French SIRET, the Peppol endpoint
id and scheme, the stock procurement references) are read defensively
with getattr so the mapper works whether or not those modules are
installed. Standard account.move / res.partner fields are read directly.

Line unit codes are emitted as 'C62' to match the historical CII
mapping; per unit-of-measure mapping (HUR, DAY, ...) is a Peppol-layer
concern applied when that format adopts the model.
"""

from .model import (
    Address,
    InvoiceModel,
    Line,
    Party,
    TaxSubtotal,
    Totals,
)


# EN 16931 type codes are resolved by the serializers from move_type; the
# mapper carries move_type unchanged.

_PRODUCT_DISPLAY_TYPES = (False, None, '', 'product')


def _safe_attr(record, attr, default=None):
    """Read an attribute that might not exist on a stub or when the
    declaring module is not installed."""
    return getattr(record, attr, default)


def _is_product_line(line):
    """True for an actual product line across Odoo versions. Odoo 16 sets
    display_type to 'product' on real lines; 17+ uses False. Section and
    note rows carry their own display_type and are excluded."""
    return _safe_attr(line, 'display_type', False) in _PRODUCT_DISPLAY_TYPES


def _tax_category(tax):
    """UNTDID 5305 category for an account.tax.

    Prefers the explicit EN 16931 category set on the tax
    (eh_edi_tax_category, covering S/Z/E/AE/G/O), then the French
    e-reporting override (eh_fr_einv_category), then falls back to the
    historical rate rule (zero rate -> 'Z', otherwise 'S'). Read
    defensively with getattr so this works whether or not the Peppol /
    FR modules are installed and with stub records in unit tests."""
    if not tax:
        return 'S'
    explicit = _safe_attr(tax, 'eh_edi_tax_category')
    if explicit:
        return explicit
    override = _safe_attr(tax, 'eh_fr_einv_category')
    if override:
        return override
    if (tax.amount or 0.0) == 0:
        return 'Z'
    return 'S'


def _tax_exemption_reason(tax):
    """EN 16931 exemption reason (BT-120) for an account.tax, or ''.

    Only categories that charge no tax (E, AE, G, O) carry a reason; the
    field is read defensively so stubs and installs without the Peppol
    module still work."""
    if not tax:
        return ''
    return _safe_attr(tax, 'eh_edi_tax_exemption_reason') or ''


def _party(partner, siret_field=None, fallback_company=None):
    """Build a Party from a res.partner. siret_field + fallback_company
    let the seller read its SIRET off res.company when that is where it
    lives."""
    legal_id = ''
    if siret_field and fallback_company is not None:
        legal_id = _safe_attr(fallback_company, siret_field) or ''
    if not legal_id:
        legal_id = (
            _safe_attr(partner, 'fr_siret')
            or _safe_attr(partner, 'siret')
            or ''
        )
    country = partner.country_id.code if partner.country_id else ''
    return Party(
        name=partner.name or '',
        vat=partner.vat or '',
        legal_id=legal_id,
        electronic_id=_safe_attr(partner, 'eh_peppol_endpoint_id') or '',
        electronic_scheme=(
            _safe_attr(partner, 'eh_peppol_endpoint_scheme') or ''
        ),
        email=partner.email or '',
        country_code=country or '',
        address=Address(
            street=partner.street or '',
            street2=partner.street2 or '',
            city=partner.city or '',
            zip=partner.zip or '',
            country_code=country or '',
        ),
    )


def _unit_code(line):
    """UN/ECE Recommendation 20 unit code from the line unit of measure.

    Defaults to 'EA' (each). The CII adapter ignores this and emits
    'C62'; the UBL adapter uses it. A deployment with richer unit data
    overrides by enriching the model.
    """
    uom = _safe_attr(line, 'product_uom_id')
    if uom and getattr(uom, 'name', None):
        name = uom.name.lower()
        if 'hour' in name or 'hr' in name:
            return 'HUR'
        if 'day' in name:
            return 'DAY'
        if 'month' in name:
            return 'MON'
        if 'kg' in name or 'kilogram' in name:
            return 'KGM'
    return 'EA'


def _line(line):
    tax = (line.tax_ids and line.tax_ids[0]) or None
    rate = (tax.amount if tax else 0.0)
    # With a tax, resolve its EN 16931 category. Without any tax the line
    # is untaxed: keep the historical rate rule (zero -> 'Z') so output is
    # unchanged for moves that carry no tax lines.
    category = _tax_category(tax) if tax else ('Z' if rate == 0.0 else 'S')
    return Line(
        name=line.name or (line.product_id.name if line.product_id else ''),
        quantity=line.quantity or 0.0,
        unit_code=_unit_code(line),
        price_unit=line.price_unit or 0.0,
        line_net=line.price_subtotal or 0.0,
        tax_rate=rate,
        tax_category=category,
        tax_exemption_reason=_tax_exemption_reason(tax),
    )


def _tax_breakdown(product_lines):
    buckets = {}
    order = []
    for line in product_lines:
        for tax in (line.tax_ids or []):
            rate = tax.amount or 0.0
            category = _tax_category(tax)
            key = (rate, category)
            if key not in buckets:
                buckets[key] = {
                    'basis': 0.0, 'amount': 0.0,
                    'reason': _tax_exemption_reason(tax),
                }
                order.append(key)
            buckets[key]['basis'] += line.price_subtotal or 0.0
            buckets[key]['amount'] += (
                (line.price_subtotal or 0.0) * rate / 100.0
            )
    return tuple(
        TaxSubtotal(
            category=key[1], rate=key[0],
            taxable_amount=buckets[key]['basis'],
            tax_amount=buckets[key]['amount'],
            exemption_reason=buckets[key]['reason'],
        )
        for key in order
    )


def map_move(move):
    """Convert an account.move record to a canonical InvoiceModel."""
    company = move.company_id
    partner = move.partner_id
    product_lines = [
        line for line in move.invoice_line_ids if _is_product_line(line)
    ]
    buyer_reference = (
        _safe_attr(move, 'buyer_reference')
        or _safe_attr(move, 'ref')
        or ''
    )
    return InvoiceModel(
        invoice_number=move.name or '',
        invoice_date=move.invoice_date,
        due_date=_safe_attr(move, 'invoice_date_due'),
        move_type=move.move_type,
        currency=(move.currency_id.name if move.currency_id else 'EUR'),
        note=_safe_attr(move, 'narration') or '',
        buyer_reference=buyer_reference,
        contract_reference=_safe_attr(move, 'contract_reference') or '',
        # Purchase order reference only, to reproduce the CII mapping
        # exactly. The Peppol layer maps its order reference from
        # invoice_origin when that format adopts the model.
        order_reference=_safe_attr(move, 'purchase_order_reference') or '',
        seller=_party(
            company.partner_id, siret_field='fr_siret',
            fallback_company=company,
        ),
        buyer=_party(partner),
        lines=tuple(_line(line) for line in product_lines),
        tax_breakdown=_tax_breakdown(product_lines),
        totals=Totals(
            sum_line_net=sum(
                line.price_subtotal or 0.0 for line in product_lines
            ),
            tax_basis=move.amount_untaxed or 0.0,
            tax_amount=move.amount_tax or 0.0,
            grand_total=move.amount_total or 0.0,
            payable_amount=move.amount_total or 0.0,
        ),
    )
