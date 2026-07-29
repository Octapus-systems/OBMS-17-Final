# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Canonical EN 16931 model + mapper unit tests.

The mapper is pure: it reads duck-typed attributes off a stub record, so
no ORM is needed. These tests pin two things:

* map_move() extracts the right EN 16931 semantics from a move.
* model.to_cii_input() reproduces the historical Factur-X / CII mapper
  output shape exactly. That equality is what lets France swap its
  hand-rolled map_move() for this shared model without shifting a single
  byte of CII output (proven end to end by the golden harness once the
  swap lands).
"""

import datetime
import types

from odoo.tests import BaseCase, tagged

from odoo.addons.eh_edi_core.tools.en16931.mapper import map_move
from odoo.addons.eh_edi_core.tools.en16931.model import InvoiceModel


def _tax(amount, category=None):
    return types.SimpleNamespace(
        amount=amount, eh_fr_einv_category=category,
    )


def _country(code):
    return types.SimpleNamespace(code=code)


def _partner(**kw):
    base = {
        'name': '', 'vat': '', 'email': '',
        'street': '', 'street2': '', 'city': '', 'zip': '',
        'country_id': None, 'fr_siret': None, 'siret': None,
        'eh_peppol_endpoint_id': None, 'eh_peppol_endpoint_scheme': None,
    }
    base.update(kw)
    return types.SimpleNamespace(**base)


def _line(**kw):
    base = {
        'display_type': False, 'name': '', 'product_id': None,
        'quantity': 0.0, 'price_unit': 0.0, 'price_subtotal': 0.0,
        'tax_ids': [],
    }
    base.update(kw)
    return types.SimpleNamespace(**base)


def _sample_move():
    seller = _partner(
        name='Demo SARL', vat='FR12345678900',
        country_id=_country('FR'),
        street='10 Rue de la Paix', zip='75001', city='Paris',
    )
    buyer = _partner(
        name='Customer SAS', vat='FR98765432100',
        country_id=_country('FR'), fr_siret='98765432100013',
        street='20 Avenue des Champs', zip='69001', city='Lyon',
    )
    company = types.SimpleNamespace(
        partner_id=seller, fr_siret='12345678900014',
    )
    line = _line(
        name='Consulting day', quantity=5.0, price_unit=800.0,
        price_subtotal=4000.0, tax_ids=[_tax(20.0)],
    )
    return types.SimpleNamespace(
        name='INV-2026-00001',
        invoice_date=datetime.date(2026, 5, 27),
        invoice_date_due=datetime.date(2026, 6, 30),
        move_type='out_invoice',
        currency_id=types.SimpleNamespace(name='EUR'),
        narration='',
        buyer_reference='PO-12345',
        contract_reference='',
        purchase_order_reference='',
        ref=None,
        company_id=company,
        partner_id=buyer,
        invoice_line_ids=[line],
        amount_untaxed=4000.0,
        amount_tax=800.0,
        amount_total=4800.0,
    )


@tagged('eh_edi_core', 'post_install', '-at_install')
class TestEn16931Model(BaseCase):

    def test_map_move_extracts_semantics(self):
        model = map_move(_sample_move())
        self.assertIsInstance(model, InvoiceModel)
        self.assertEqual(model.invoice_number, 'INV-2026-00001')
        self.assertEqual(model.currency, 'EUR')
        self.assertEqual(model.buyer_reference, 'PO-12345')
        self.assertEqual(model.seller.legal_id, '12345678900014')
        self.assertEqual(model.buyer.legal_id, '98765432100013')
        self.assertEqual(len(model.lines), 1)
        self.assertEqual(model.lines[0].tax_category, 'S')
        self.assertEqual(model.lines[0].tax_rate, 20.0)
        self.assertEqual(len(model.tax_breakdown), 1)
        self.assertAlmostEqual(model.tax_breakdown[0].tax_amount, 800.0)
        self.assertAlmostEqual(model.totals.payable_amount, 4800.0)

    def test_to_cii_input_reproduces_legacy_shape(self):
        model = map_move(_sample_move())
        expected = {
            'invoice_number': 'INV-2026-00001',
            'invoice_date': datetime.date(2026, 5, 27),
            'invoice_date_due': datetime.date(2026, 6, 30),
            'move_type': 'out_invoice',
            'currency': 'EUR',
            'buyer_reference': 'PO-12345',
            'contract_reference': '',
            'purchase_order_reference': '',
            'seller': {
                'name': 'Demo SARL',
                'siret': '12345678900014',
                'vat': 'FR12345678900',
                'contact_email': '',
                'address': {
                    'street': '10 Rue de la Paix',
                    'street2': '',
                    'zip': '75001',
                    'city': 'Paris',
                    'country_code': 'FR',
                },
            },
            'buyer': {
                'name': 'Customer SAS',
                'siret': '98765432100013',
                'vat': 'FR98765432100',
                'contact_email': '',
                'address': {
                    'street': '20 Avenue des Champs',
                    'street2': '',
                    'zip': '69001',
                    'city': 'Lyon',
                    'country_code': 'FR',
                },
            },
            'lines': [
                {
                    'name': 'Consulting day',
                    'quantity': 5.0,
                    'unit_code': 'C62',
                    'price_unit': 800.0,
                    'tax_rate': 20.0,
                    'tax_category': 'S',
                    'line_total': 4000.0,
                },
            ],
            'totals': {
                'sum_line_net': 4000.0,
                'tax_basis': 4000.0,
                'tax_amount': 800.0,
                'invoice_total': 4800.0,
                'payable_amount': 4800.0,
            },
            'tax_breakdown': [
                {'rate': 20.0, 'category': 'S',
                 'basis': 4000.0, 'amount': 800.0},
            ],
        }
        self.assertEqual(model.to_cii_input(), expected)

    def test_credit_note_maps_to_credit_note_doc_type(self):
        move = _sample_move()
        move.move_type = 'out_refund'
        ubl = map_move(move).to_ubl_inputs()
        self.assertEqual(ubl['document_type'], 'credit_note')

    def test_to_ubl_inputs_base_shape(self):
        ubl = map_move(_sample_move()).to_ubl_inputs()
        self.assertEqual(ubl['document_type'], 'invoice')
        self.assertEqual(ubl['currency_code'], 'EUR')
        self.assertEqual(ubl['supplier']['vat_id'], 'FR12345678900')
        self.assertEqual(len(ubl['lines']), 1)
        self.assertEqual(ubl['lines'][0]['id'], 1)
        # No unit of measure on the stub line, so the UBL unit code is
        # the EA default; the CII side stays C62 regardless.
        self.assertEqual(ubl['lines'][0]['unit_code'], 'EA')
        self.assertEqual(ubl['lines'][0]['tax_category_code'], 'S')
        self.assertEqual(len(ubl['tax_categories']), 1)
        self.assertAlmostEqual(
            ubl['tax_categories'][0]['tax_amount'], 800.0,
        )

    def test_zero_rate_maps_to_z_category(self):
        move = _sample_move()
        move.invoice_line_ids[0].tax_ids = [_tax(0.0)]
        model = map_move(move)
        self.assertEqual(model.lines[0].tax_category, 'Z')
