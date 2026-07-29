# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Peppol BIS Billing 3.0 (UBL 2.1) Invoice / Credit Note parser.

Inverse of ubl_generator.render_invoice_xml. Takes UBL 2.1 XML bytes
(or a parsed lxml etree) and returns a normalised payload dict matching
the shape that make_invoice_payload produces. Concrete callers (the
inbound model, batch jobs, test fixtures) consume the dict to create
vendor bills.

Originality
-----------

Built from the same spec sources as the generator (UBL 2.1, EN 16931,
Peppol BIS Billing 3.0). No code or comments derive from any third-
party Odoo eInvoicing implementation.

Tolerance
---------

The parser is lenient on optional fields (note, buyer_reference,
contract_reference, payment_terms) and strict on required ones
(invoice_number, issue_date, currency_code, supplier, customer,
lines). Required-field violations raise PeppolParserError naming the
offending element.

Out of scope
------------

* XAdES signature verification (transport-level concern; access point
  validates).
* Country profile extensions (FatturaPA, MyInvois, XRechnung). The
  parser reads the BIS 3.0 core; downstream callers can extract
  country-specific extensions from the rendered tree.
"""

import datetime
from decimal import Decimal, InvalidOperation

from lxml import etree


# Match the namespaces in the generator so a round-trip test can
# verify symmetry.
NS_INVOICE = (
    "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
)
NS_CREDIT = (
    "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
)
NS_CBC = (
    "urn:oasis:names:specification:ubl:schema:xsd:"
    "CommonBasicComponents-2"
)
NS_CAC = (
    "urn:oasis:names:specification:ubl:schema:xsd:"
    "CommonAggregateComponents-2"
)


_NS_MAP = {
    'cbc': NS_CBC,
    'cac': NS_CAC,
    'invoice': NS_INVOICE,
    'credit': NS_CREDIT,
}


class PeppolParserError(ValueError):
    """Raised on malformed or non-conformant inbound XML.

    The message names the offending element or attribute so the user
    can locate the failure in the source XML quickly.
    """


def parse_invoice_xml(source):
    """Parse UBL 2.1 invoice or credit-note XML and return a payload.

    :param source: bytes, str, file-like, or an lxml _Element.
    :return: dict with the same keys as make_invoice_payload's return
        value, i.e. invoice_number, issue_date, due_date,
        currency_code, invoice_type_code, note, buyer_reference,
        order_reference, contract_reference, supplier, customer,
        lines, tax_categories, payment_means, payment_terms,
        document_type.
    :raises PeppolParserError: when the XML is unparseable or a
        required field is missing.
    """
    root = _coerce_root(source)
    document_type = _detect_document_type(root)
    return {
        'document_type': document_type,
        'invoice_number': _required_text(
            root, './cbc:ID', 'invoice_number',
        ),
        'issue_date': _required_date(
            root, './cbc:IssueDate', 'issue_date',
        ),
        'due_date': _optional_date(root, './cbc:DueDate'),
        'currency_code': _required_text(
            root, './cbc:DocumentCurrencyCode', 'currency_code',
        ),
        'invoice_type_code': _optional_text(
            root, './cbc:InvoiceTypeCode',
        ) or _optional_text(root, './cbc:CreditNoteTypeCode') or (
            '380' if document_type == 'invoice' else '381'
        ),
        'note': _optional_text(root, './cbc:Note') or '',
        'buyer_reference': _optional_text(
            root, './cbc:BuyerReference',
        ) or '',
        'order_reference': _optional_text(
            root, './cac:OrderReference/cbc:ID',
        ) or '',
        'contract_reference': _optional_text(
            root, './cac:ContractDocumentReference/cbc:ID',
        ) or '',
        'supplier': _parse_party(
            root, './cac:AccountingSupplierParty', 'supplier',
        ),
        'customer': _parse_party(
            root, './cac:AccountingCustomerParty', 'customer',
        ),
        'payment_means': _parse_payment_means(root),
        'payment_terms': _parse_payment_terms(root),
        'tax_categories': _parse_tax_categories(root),
        'lines': _parse_lines(root, document_type),
    }


# ---- helpers ----


def _secure_parser():
    """lxml parser hardened against XXE and entity-expansion attacks.

    Inbound Peppol documents arrive from external, untrusted senders, so the
    parser must not resolve external entities, must not fetch over the
    network (SSRF via SYSTEM entities), and must reject DTDs and oversized
    trees. Every parse of untrusted UBL goes through here.
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )


def _coerce_root(source):
    """Accept bytes / str / file-like / etree._Element and return the
    root Element. Strict, XXE-hardened parser: an XML error is converted to
    PeppolParserError with the location preserved.
    """
    if hasattr(source, 'tag'):
        return source
    if isinstance(source, str):
        source = source.encode('utf-8')
    try:
        parser = _secure_parser()
        if hasattr(source, 'read'):
            tree = etree.parse(source, parser)
            root = tree.getroot()
        else:
            root = etree.fromstring(source, parser)
    except etree.XMLSyntaxError as exc:
        raise PeppolParserError(
            "UBL XML failed to parse: %s" % exc,
        ) from exc
    # Defence in depth: reject any DOCTYPE/entity declarations outright, even
    # though resolve_entities=False already neutralises expansion.
    doctype = getattr(getattr(root, 'getroottree', lambda: None)(),
                      'docinfo', None)
    if doctype is not None and doctype.doctype:
        raise PeppolParserError(
            "UBL XML with a DOCTYPE declaration is rejected (XXE guard).",
        )
    return root


def _detect_document_type(root):
    """Return 'invoice' or 'credit_note' based on the root element."""
    tag = etree.QName(root.tag)
    if tag.localname == 'Invoice':
        return 'invoice'
    if tag.localname == 'CreditNote':
        return 'credit_note'
    raise PeppolParserError(
        "Unrecognised root element %r; expected Invoice or "
        "CreditNote" % tag.localname,
    )


def _required_text(node, xpath, field):
    elt = node.find(xpath, namespaces=_NS_MAP)
    if elt is None or not (elt.text or '').strip():
        raise PeppolParserError(
            "Required field %s missing (xpath %s)" % (field, xpath),
        )
    return elt.text.strip()


def _optional_text(node, xpath):
    elt = node.find(xpath, namespaces=_NS_MAP)
    if elt is None or elt.text is None:
        return None
    return elt.text.strip() or None


def _required_date(node, xpath, field):
    raw = _required_text(node, xpath, field)
    return _parse_date(raw, field)


def _optional_date(node, xpath):
    raw = _optional_text(node, xpath)
    if not raw:
        return None
    return _parse_date(raw, xpath)


def _parse_date(raw, field):
    try:
        return datetime.date.fromisoformat(raw[:10])
    except (TypeError, ValueError):
        raise PeppolParserError(
            "Field %s carries unparseable date %r (expected "
            "ISO 8601 YYYY-MM-DD)" % (field, raw),
        )


def _required_decimal(node, xpath, field):
    raw = _required_text(node, xpath, field)
    return _parse_decimal(raw, field)


def _optional_decimal(node, xpath, default=Decimal('0')):
    raw = _optional_text(node, xpath)
    if raw is None:
        return default
    return _parse_decimal(raw, xpath)


def _parse_decimal(raw, field):
    try:
        return Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        raise PeppolParserError(
            "Field %s carries unparseable decimal %r" % (field, raw),
        )


# ---- party ----


def _parse_party(root, base_xpath, role):
    party_node = root.find(base_xpath, namespaces=_NS_MAP)
    if party_node is None:
        raise PeppolParserError(
            "Required party block %s missing for role %s"
            % (base_xpath, role),
        )
    inner = party_node.find('./cac:Party', namespaces=_NS_MAP)
    if inner is None:
        raise PeppolParserError(
            "Required cac:Party child missing under %s" % base_xpath,
        )
    name = (
        _optional_text(
            inner,
            './cac:PartyName/cbc:Name',
        )
        or _optional_text(
            inner, './cac:PartyLegalEntity/cbc:RegistrationName',
        )
    )
    if not name:
        raise PeppolParserError(
            "Party %s has no name (cac:PartyName/cbc:Name nor "
            "cac:PartyLegalEntity/cbc:RegistrationName)" % role,
        )
    endpoint_node = inner.find(
        './cbc:EndpointID', namespaces=_NS_MAP,
    )
    endpoint_id = (endpoint_node.text or '').strip() if endpoint_node is not None else ''
    endpoint_scheme = (
        endpoint_node.get('schemeID') if endpoint_node is not None else ''
    )
    address_node = inner.find(
        './cac:PostalAddress', namespaces=_NS_MAP,
    )
    address = {}
    if address_node is not None:
        address = {
            'street': _optional_text(
                address_node, './cbc:StreetName',
            ) or '',
            'city': _optional_text(
                address_node, './cbc:CityName',
            ) or '',
            'postcode': _optional_text(
                address_node, './cbc:PostalZone',
            ) or '',
            'country': _optional_text(
                address_node,
                './cac:Country/cbc:IdentificationCode',
            ) or '',
        }
    country_code = address.get('country', '')
    vat_id = _optional_text(
        inner,
        './cac:PartyTaxScheme/cbc:CompanyID',
    ) or ''
    legal_id = _optional_text(
        inner,
        './cac:PartyLegalEntity/cbc:CompanyID',
    ) or ''
    return {
        'name': name,
        'endpoint_id': endpoint_id,
        'endpoint_scheme': endpoint_scheme or '',
        'country_code': country_code,
        'vat_id': vat_id,
        'legal_id': legal_id,
        'address': address,
    }


# ---- payment ----


def _parse_payment_means(root):
    pm = root.find('./cac:PaymentMeans', namespaces=_NS_MAP)
    if pm is None:
        return None
    payee_account = pm.find(
        './cac:PayeeFinancialAccount', namespaces=_NS_MAP,
    )
    iban = _optional_text(payee_account, './cbc:ID') if payee_account is not None else None
    return {
        'payment_means_code': _optional_text(
            pm, './cbc:PaymentMeansCode',
        ) or '',
        'payment_id': _optional_text(pm, './cbc:PaymentID') or '',
        'payee_account_id': iban or '',
    }


def _parse_payment_terms(root):
    pt = root.find('./cac:PaymentTerms', namespaces=_NS_MAP)
    if pt is None:
        return None
    return {
        'note': _optional_text(pt, './cbc:Note') or '',
    }


# ---- tax categories ----


def _parse_tax_categories(root):
    """Return [{category_code, rate_pct, taxable_amount, tax_amount}].

    Reads cac:TaxTotal/cac:TaxSubtotal entries at the document level.
    BIS Billing 3.0 requires the per-rate breakdown here; the per-line
    classified-tax-category references this table by code.
    """
    out = []
    for subtotal in root.findall(
        './cac:TaxTotal/cac:TaxSubtotal', namespaces=_NS_MAP,
    ):
        category = subtotal.find(
            './cac:TaxCategory', namespaces=_NS_MAP,
        )
        category_code = ''
        rate_pct = Decimal('0')
        if category is not None:
            category_code = _optional_text(
                category, './cbc:ID',
            ) or ''
            rate_pct = _optional_decimal(
                category, './cbc:Percent',
            )
        out.append({
            'category_code': category_code,
            'rate_pct': float(rate_pct),
            'taxable_amount': float(_optional_decimal(
                subtotal, './cbc:TaxableAmount',
            )),
            'tax_amount': float(_optional_decimal(
                subtotal, './cbc:TaxAmount',
            )),
        })
    return out


# ---- lines ----


def _parse_lines(root, document_type):
    """Read InvoiceLine or CreditNoteLine entries.

    Returns a list of dicts: {id, description, quantity, unit_code,
    unit_price, line_total, tax_category_code, tax_rate_pct}.
    """
    line_xpath = (
        './cac:InvoiceLine'
        if document_type == 'invoice'
        else './cac:CreditNoteLine'
    )
    qty_xpath = (
        './cbc:InvoicedQuantity'
        if document_type == 'invoice'
        else './cbc:CreditedQuantity'
    )
    out = []
    for line in root.findall(line_xpath, namespaces=_NS_MAP):
        qty_node = line.find(qty_xpath, namespaces=_NS_MAP)
        if qty_node is None or qty_node.text is None:
            raise PeppolParserError(
                "Line missing quantity element %s" % qty_xpath,
            )
        unit_code = qty_node.get('unitCode') or 'EA'
        quantity = _parse_decimal(qty_node.text, 'line.quantity')
        line_total = _required_decimal(
            line, './cbc:LineExtensionAmount', 'line.line_total',
        )
        price_node = line.find(
            './cac:Price/cbc:PriceAmount', namespaces=_NS_MAP,
        )
        unit_price = (
            _parse_decimal(price_node.text, 'line.unit_price')
            if price_node is not None and price_node.text
            else Decimal('0')
        )
        item_node = line.find('./cac:Item', namespaces=_NS_MAP)
        description = ''
        tax_category_code = ''
        tax_rate_pct = Decimal('0')
        if item_node is not None:
            description = _optional_text(
                item_node, './cbc:Name',
            ) or _optional_text(
                item_node, './cbc:Description',
            ) or ''
            cat = item_node.find(
                './cac:ClassifiedTaxCategory', namespaces=_NS_MAP,
            )
            if cat is not None:
                tax_category_code = _optional_text(
                    cat, './cbc:ID',
                ) or ''
                tax_rate_pct = _optional_decimal(
                    cat, './cbc:Percent',
                )
        out.append({
            'id': _optional_text(line, './cbc:ID') or '',
            'description': description,
            'quantity': float(quantity),
            'unit_code': unit_code,
            'unit_price': float(unit_price),
            'line_total': float(line_total),
            'tax_category_code': tax_category_code,
            'tax_rate_pct': float(tax_rate_pct),
        })
    if not out:
        raise PeppolParserError(
            "Document carries no %s lines" % (
                'invoice' if document_type == 'invoice'
                else 'credit-note',
            ),
        )
    return out
