# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
Pure-Python tests for the Peppol BIS Billing 3.0 (UBL 2.1) parser.

The parser is the inverse of the generator. We round-trip a generated
XML payload through the parser and assert the parsed dict reproduces
the original supplier / customer / line shape (within UBL's lossy
serialisation: numerics become Decimal-from-text and addresses lose
optional sub-fields the generator omits).
"""

import datetime
import unittest  # noqa: F401

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
    make_invoice_payload, render_invoice_xml,
)
from odoo.addons.eh_account_einvoice_peppol.tools.ubl_parser import (
    PeppolParserError, parse_invoice_xml,
)


def _supplier():
    return {
        'name': 'Heritage Books Pty Ltd',
        'endpoint_id': 'AU12345678901',
        'endpoint_scheme': '0151',
        'country_code': 'AU',
        'vat_id': 'AU12345678901',
        'address': {
            'street': '1 Heritage Lane', 'city': 'Melbourne',
            'postcode': '3000', 'country': 'AU',
        },
    }


def _customer():
    return {
        'name': 'Buyer Co',
        'endpoint_id': 'AU98765432109',
        'endpoint_scheme': '0151',
        'country_code': 'AU',
        'vat_id': 'AU98765432109',
        'address': {
            'street': '2 Buyer Street', 'city': 'Sydney',
            'postcode': '2000', 'country': 'AU',
        },
    }


def _lines():
    return [
        {
            'id': '1', 'description': 'Consulting',
            'quantity': '10', 'unit_code': 'HUR',
            'unit_price': '100.00', 'line_total': '1000.00',
            'tax_category_code': 'S', 'tax_rate_pct': '10.00',
        },
        {
            'id': '2', 'description': 'License fee',
            'quantity': '1', 'unit_code': 'EA',
            'unit_price': '500.00', 'line_total': '500.00',
            'tax_category_code': 'S', 'tax_rate_pct': '10.00',
        },
    ]


def _categories():
    return [
        {
            'category_code': 'S', 'rate_pct': '10.00',
            'taxable_amount': '1500.00', 'tax_amount': '150.00',
        },
    ]


def _make_xml(document_type='invoice'):
    payload = make_invoice_payload(
        invoice_number='INV-2026-00042',
        issue_date=datetime.date(2026, 5, 1),
        due_date=datetime.date(2026, 5, 31),
        currency_code='AUD',
        supplier=_supplier(), customer=_customer(),
        lines=_lines(), tax_categories=_categories(),
        document_type=document_type,
        order_reference='PO-2026-100',
        note='Net 30',
    )
    return render_invoice_xml(payload)


@tagged('post_install', '-at_install')
class PeppolParserHappyPathTest(TransactionCase):

    def test_invoice_root_yields_invoice_document_type(self):
        result = parse_invoice_xml(_make_xml('invoice'))
        self.assertEqual(result['document_type'], 'invoice')

    def test_credit_note_root_yields_credit_note_document_type(self):
        result = parse_invoice_xml(_make_xml('credit_note'))
        self.assertEqual(result['document_type'], 'credit_note')

    def test_header_round_trip(self):
        result = parse_invoice_xml(_make_xml())
        self.assertEqual(result['invoice_number'], 'INV-2026-00042')
        self.assertEqual(
            result['issue_date'], datetime.date(2026, 5, 1),
        )
        self.assertEqual(
            result['due_date'], datetime.date(2026, 5, 31),
        )
        self.assertEqual(result['currency_code'], 'AUD')
        self.assertEqual(result['note'], 'Net 30')
        self.assertEqual(result['order_reference'], 'PO-2026-100')

    def test_supplier_and_customer_round_trip(self):
        result = parse_invoice_xml(_make_xml())
        self.assertEqual(
            result['supplier']['name'], 'Heritage Books Pty Ltd',
        )
        self.assertEqual(
            result['supplier']['endpoint_id'], 'AU12345678901',
        )
        self.assertEqual(
            result['supplier']['endpoint_scheme'], '0151',
        )
        self.assertEqual(
            result['customer']['name'], 'Buyer Co',
        )
        self.assertEqual(
            result['customer']['address']['city'], 'Sydney',
        )

    def test_lines_round_trip(self):
        result = parse_invoice_xml(_make_xml())
        self.assertEqual(len(result['lines']), 2)
        first = result['lines'][0]
        self.assertEqual(first['id'], '1')
        self.assertEqual(first['description'], 'Consulting')
        self.assertEqual(first['unit_code'], 'HUR')
        self.assertAlmostEqual(first['quantity'], 10.0, places=2)
        self.assertAlmostEqual(first['unit_price'], 100.0, places=2)
        self.assertAlmostEqual(first['line_total'], 1000.0, places=2)
        self.assertEqual(first['tax_category_code'], 'S')
        self.assertAlmostEqual(first['tax_rate_pct'], 10.0, places=2)

    def test_tax_categories_round_trip(self):
        result = parse_invoice_xml(_make_xml())
        self.assertEqual(len(result['tax_categories']), 1)
        cat = result['tax_categories'][0]
        self.assertEqual(cat['category_code'], 'S')
        self.assertAlmostEqual(cat['rate_pct'], 10.0, places=2)
        self.assertAlmostEqual(cat['taxable_amount'], 1500.0, places=2)
        self.assertAlmostEqual(cat['tax_amount'], 150.0, places=2)


@tagged('post_install', '-at_install')
class PeppolParserErrorTest(TransactionCase):

    def test_unparseable_xml_raises(self):
        with self.assertRaises(PeppolParserError):
            parse_invoice_xml(b"<not valid xml")

    def test_wrong_root_raises(self):
        with self.assertRaises(PeppolParserError):
            parse_invoice_xml(
                b'<?xml version="1.0"?><Foo xmlns="bar"/>',
            )

    def test_missing_invoice_number_raises(self):
        # Strip the cbc:ID before the parse.
        xml = _make_xml().decode('utf-8')
        cbc = (
            'urn:oasis:names:specification:ubl:schema:xsd:'
            'CommonBasicComponents-2'
        )
        # Remove the FIRST cbc:ID (the document-level one); leaves
        # the per-line ones intact.
        marker = '<cbc:ID xmlns:cbc="%s">' % cbc  # noqa: F841
        # Actually the generator may use the prefixed cbc element from
        # the document namespace declaration, not a per-element xmlns.
        # Strip the first cbc:ID via a simpler regex on the prefixed
        # form.
        import re
        xml_no_id = re.sub(r'<cbc:ID>.*?</cbc:ID>', '', xml, count=1)
        with self.assertRaises(PeppolParserError):
            parse_invoice_xml(xml_no_id)
