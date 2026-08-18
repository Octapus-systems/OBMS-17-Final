# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
Integration test for the account.move adapter in eh_account_einvoice_peppol.
"""

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


class PeppolMoveExportTest(EhAccountIntegrationTestCase):

    def setUp(self):
        super().setUp()
        # The Peppol export validates BOTH parties' participant ids.
        # Configure the company (sender) with a valid Australian
        # Business Number so the seller party passes the ABN
        # mod-89 checksum, and the buyer with a complementary one.
        au = self.env.ref('base.au')
        self.env.company.write({
            'country_id': au.id,
            # Bare 11 digit ABN, no country prefix: this is the format
            # base_vat enforces for AU when it is installed (l10n_fr,
            # co-installed with fr e-invoicing, pulls it in). Passes the
            # ABN mod-89 checksum.
            'vat': '83914571673',
            'street': '1 Seller Street',
            'city': 'Sydney',
            'zip': '2000',
        })
        self.env.company.partner_id.write({
            'eh_peppol_endpoint_scheme': '0151',
            'eh_peppol_endpoint_id': '83914571673',
        })
        self.partner = self.env['res.partner'].create({
            'name': 'Buyer Co',
            'is_company': True,
            'country_id': au.id,
            'vat': '53004085616',  # bare ABN, base_vat valid, passes mod-89
            'eh_peppol_endpoint_scheme': '0151',
            'eh_peppol_endpoint_id': '53004085616',
            'street': '2 Buyer Street',
            'city': 'Sydney',
            'zip': '2000',
        })
        # Ensure a sale journal exists so account.move auto-resolution
        # works in fresh databases that ship neither demo data nor a
        # chart of accounts.
        self.sale_journal = self.env['account.journal'].search(
            [('type', '=', 'sale'),
             ('company_id', '=', self.env.company.id)], limit=1,
        )
        if not self.sale_journal:
            self.sale_journal = self.env['account.journal'].create({
                'name': 'Sales',
                'code': 'INV',
                'type': 'sale',
                'company_id': self.env.company.id,
            })

    def _post_invoice(self):
        currency = self.env.company.currency_id
        product = self.env['product.product'].create({
            'name': 'Test Service', 'type': 'service',
        })
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': currency.id,
            'invoice_date': '2026-05-01',
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id,
                'name': 'Consulting',
                'quantity': 10.0,
                'price_unit': 100.0,
                'account_id': self.account_revenue.id,
            })],
        })
        move.action_post()
        return move

    def test_xml_export_action_returns_attachment(self):
        move = self._post_invoice()
        result = move.action_eh_export_peppol_xml()
        self.assertIsInstance(result, dict)
        self.assertIn('url', result)

    def _legacy_lines_and_tax(self, move):
        """Snapshot of the pre-shared-mapper line/tax extraction, kept
        as the parity reference. The production code now sources line
        semantics from eh_edi_core; this must stay byte identical."""
        lines = []
        tax_buckets = {}
        for i, line in enumerate(move.invoice_line_ids.filtered(
            lambda line_item: line_item.display_type not in ('line_section', 'line_note'),
        ), start=1):
            tax = line.tax_ids[:1]
            rate_pct = float(tax.amount) if tax else 0.0
            cat_code = 'Z' if rate_pct == 0.0 else 'S'
            line_total = float(line.price_subtotal)
            unit_price = float(line.price_unit)
            unit_code = 'EA'
            if line.product_uom_id and line.product_uom_id.name:
                name = line.product_uom_id.name.lower()
                if 'hour' in name or 'hr' in name:
                    unit_code = 'HUR'
                elif 'day' in name:
                    unit_code = 'DAY'
                elif 'month' in name:
                    unit_code = 'MON'
                elif 'kg' in name or 'kilogram' in name:
                    unit_code = 'KGM'
            lines.append({
                'id': i,
                'description': line.name or (line.product_id.name or ''),
                'quantity': float(line.quantity),
                'unit_code': unit_code,
                'unit_price': unit_price,
                'line_total': line_total,
                'tax_category_code': cat_code,
                'tax_rate_pct': rate_pct,
            })
            bucket_key = (cat_code, rate_pct)
            bucket = tax_buckets.setdefault(bucket_key, {
                'category_code': cat_code,
                'rate_pct': rate_pct,
                'taxable_amount': 0.0,
                'tax_amount': 0.0,
                'exemption_reason': '',
            })
            bucket['taxable_amount'] += line_total
            bucket['tax_amount'] += round(line_total * rate_pct / 100.0, 2)
        return lines, list(tax_buckets.values())

    def test_lines_tax_parity_with_legacy(self):
        # The shared-mapper line/tax extraction must reproduce the legacy
        # output exactly, for single and multi line moves.
        inv = self._post_invoice()
        new_lines, new_tax = inv._eh_build_peppol_lines_and_tax()
        old_lines, old_tax = self._legacy_lines_and_tax(inv)
        self.assertEqual(new_lines, old_lines)
        self.assertEqual(new_tax, old_tax)

        multi = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': self.env.company.currency_id.id,
            'invoice_date': '2026-05-01',
            'invoice_line_ids': [
                (0, 0, {'name': 'A', 'quantity': 2.0, 'price_unit': 33.33,
                        'account_id': self.account_revenue.id}),
                (0, 0, {'name': 'B', 'quantity': 1.0, 'price_unit': 50.0,
                        'account_id': self.account_revenue.id}),
            ],
        })
        multi.action_post()
        new_lines, new_tax = multi._eh_build_peppol_lines_and_tax()
        old_lines, old_tax = self._legacy_lines_and_tax(multi)
        self.assertEqual(new_lines, old_lines)
        self.assertEqual(new_tax, old_tax)

    def _tax_group(self):
        company = self.env.company
        country = company.account_fiscal_country_id or self.env.ref('base.us')
        # account.tax.group carries company_id/country_id from Odoo 17;
        # on Odoo 16 it is global with neither field.
        Group = self.env['account.tax.group'].sudo()
        if 'company_id' in Group._fields:
            return Group.search(
                [('company_id', '=', company.id)], limit=1,
            ) or Group.create({
                'name': 'EH EDI Test Tax Group',
                'company_id': company.id,
                'country_id': country.id,
            })
        return Group.search([], limit=1) or Group.create({
            'name': 'EH EDI Test Tax Group',
        })

    def _make_tax(self, name, amount, category=None, reason=None):
        company = self.env.company
        country = company.account_fiscal_country_id or self.env.ref('base.us')
        vals = {
            'name': name,
            'amount': amount,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': company.id,
            'country_id': country.id,
            'tax_group_id': self._tax_group().id,
        }
        if category is not None:
            vals['eh_edi_tax_category'] = category
        if reason is not None:
            vals['eh_edi_tax_exemption_reason'] = reason
        return self.env['account.tax'].sudo().create(vals)

    def test_en16931_tax_categories_are_distinct(self):
        # EN 16931 categories must not collapse to Z/S by rate: a standard,
        # zero-rated, exempt and reverse-charge line must each carry their
        # own UNTDID 5305 category, and exempt / reverse charge must carry
        # the exemption reason.
        std = self._make_tax('EH Std 20', 20.0)  # defaults to S by rate
        zero = self._make_tax('EH Zero', 0.0, category='Z')
        exempt = self._make_tax(
            'EH Exempt', 0.0, category='E',
            reason='Exempt under Division 38',
        )
        reverse = self._make_tax(
            'EH Reverse', 0.0, category='AE',
            reason='Reverse charge, buyer accounts for VAT',
        )
        product = self.env['product.product'].create({
            'name': 'Cat Service', 'type': 'service',
        })
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': self.env.company.currency_id.id,
            'invoice_date': '2026-05-01',
            'invoice_line_ids': [
                (0, 0, {'product_id': product.id, 'name': 'Standard',
                        'quantity': 1.0, 'price_unit': 100.0,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, std.ids)]}),
                (0, 0, {'product_id': product.id, 'name': 'Zero',
                        'quantity': 1.0, 'price_unit': 100.0,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, zero.ids)]}),
                (0, 0, {'product_id': product.id, 'name': 'Exempt',
                        'quantity': 1.0, 'price_unit': 100.0,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, exempt.ids)]}),
                (0, 0, {'product_id': product.id, 'name': 'Reverse',
                        'quantity': 1.0, 'price_unit': 100.0,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, reverse.ids)]}),
            ],
        })
        move.action_post()

        lines, tax_cats = move._eh_build_peppol_lines_and_tax()
        by_desc = {line_item['description']: line_item['tax_category_code'] for line_item in lines}
        self.assertEqual(by_desc['Standard'], 'S')
        self.assertEqual(by_desc['Zero'], 'Z')
        self.assertEqual(by_desc['Exempt'], 'E')
        self.assertEqual(by_desc['Reverse'], 'AE')
        # All four are distinct: the rate-based collapse would have made
        # Zero, Exempt and Reverse all 'Z'.
        self.assertEqual(
            len({by_desc['Zero'], by_desc['Exempt'], by_desc['Reverse']}),
            3,
        )
        # Exemption reason survives into the document tax breakdown.
        cats_by_code = {c['category_code']: c for c in tax_cats}
        self.assertEqual(
            cats_by_code['E']['exemption_reason'],
            'Exempt under Division 38',
        )
        self.assertEqual(
            cats_by_code['AE']['exemption_reason'],
            'Reverse charge, buyer accounts for VAT',
        )

        # And it renders into the UBL as TaxExemptionReason.
        from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
            render_invoice_xml,
        )
        xml = render_invoice_xml(move._eh_build_peppol_payload())
        self.assertIn(b'Exempt under Division 38', xml)
        self.assertIn(b'>AE<', xml)
        self.assertIn(b'>E<', xml)

    def test_ubl_tax_ties_to_booked_move_tax_on_rounded_line(self):
        # EN 16931 BR-CO-14: the category tax on the e-invoice must equal
        # the tax actually booked on the move. Build a line whose base is
        # constructed so a naive round(base * rate / 100, 2) disagrees with
        # what the tax engine books, then assert the rendered UBL carries
        # the booked figure, not the recomputed one.
        from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
            render_invoice_xml,
        )
        std = self._make_tax('EH Std 10 Round', 10.0)  # 10 percent, cat S
        product = self.env['product.product'].create({
            'name': 'Rounded Service', 'type': 'service',
        })
        # Two lines of 5.55 at 10 percent. Odoo rounds tax per line
        # (round_per_line is the default), so each line books 0.56 and the
        # move's booked tax is 1.12. A naive per-bucket recompute of
        # round(sum_base * rate / 100, 2) = round(11.10 * 0.10, 2) = 1.11,
        # which disagrees with the ledger by one cent. The UBL must carry
        # the booked 1.12, not the recomputed 1.11.
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': self.env.company.currency_id.id,
            'invoice_date': '2026-05-01',
            'invoice_line_ids': [
                (0, 0, {'product_id': product.id, 'name': 'R1',
                        'quantity': 1.0, 'price_unit': 5.55,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, std.ids)]}),
                (0, 0, {'product_id': product.id, 'name': 'R2',
                        'quantity': 1.0, 'price_unit': 5.55,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, std.ids)]}),
            ],
        })
        move.action_post()
        currency = move.currency_id

        _lines, tax_cats = move._eh_build_peppol_lines_and_tax()
        std_bucket = next(c for c in tax_cats if c['category_code'] == 'S')

        # The bucket tax must equal the move's booked amount_tax exactly.
        # Without the fix, tax_amount is round(11.10 * 0.10, 2) and this
        # fails whenever the ledger rounded per line to a different cent.
        self.assertEqual(
            currency.round(std_bucket['tax_amount']),
            currency.round(move.amount_tax),
        )

        # And the rendered UBL serializes the booked figure.
        booked = currency.round(move.amount_tax)
        xml = render_invoice_xml(move._eh_build_peppol_payload())
        self.assertIn(('%.2f' % booked).encode(), xml)

    def test_multi_tax_line_splits_base_per_category(self):
        # EN 16931 BR-CO-14 / BR-S-08: a line carrying two taxes must land
        # its base and tax in EACH tax's own category bucket, split per the
        # booked ledger tax lines. The pre-fix code keyed the whole line
        # base to the first tax's category, leaving the second category
        # with tax but no base -- a wrong per-category base/tax split that
        # still passed structural validation.
        from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
            render_invoice_xml, validate_rendered,
        )
        # Two standard-rated taxes on the same line: 10 percent and
        # 5 percent, both category S but distinct rate buckets. Each is
        # applied to the full line net (non-compound), so both buckets take
        # the whole base.
        ten = self._make_tax('EH Split 10', 10.0)
        five = self._make_tax('EH Split 5', 5.0)
        product = self.env['product.product'].create({
            'name': 'Split Service', 'type': 'service',
        })
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': self.env.company.currency_id.id,
            'invoice_date': '2026-05-01',
            'invoice_line_ids': [
                (0, 0, {'product_id': product.id, 'name': 'Dual',
                        'quantity': 1.0, 'price_unit': 200.0,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, (ten + five).ids)]}),
            ],
        })
        move.action_post()
        currency = move.currency_id

        _lines, tax_cats = move._eh_build_peppol_lines_and_tax()
        by_rate = {c['rate_pct']: c for c in tax_cats}

        # Two distinct buckets, one per rate, both carrying the full base.
        self.assertIn(10.0, by_rate)
        self.assertIn(5.0, by_rate)
        self.assertEqual(currency.round(by_rate[10.0]['taxable_amount']),
                         currency.round(200.0))
        self.assertEqual(currency.round(by_rate[5.0]['taxable_amount']),
                         currency.round(200.0))
        # Each bucket's tax ties to the booked ledger tax for that tax.
        booked_10 = abs(sum(move.line_ids.filtered(
            lambda line_item: line_item.tax_line_id == ten).mapped('amount_currency')))
        booked_5 = abs(sum(move.line_ids.filtered(
            lambda line_item: line_item.tax_line_id == five).mapped('amount_currency')))
        self.assertEqual(currency.round(by_rate[10.0]['tax_amount']),
                         currency.round(booked_10))
        self.assertEqual(currency.round(by_rate[5.0]['tax_amount']),
                         currency.round(booked_5))

        # The rendered UBL passes the BR-CO-14 per-category reconciliation.
        # Under the pre-fix code the 5 percent bucket carried a base of 0
        # against a non-zero tax and validate_rendered would reject it.
        xml = render_invoice_xml(move._eh_build_peppol_payload())
        self.assertTrue(validate_rendered(xml))

    def test_exempt_tax_requires_exemption_reason(self):
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self._make_tax('EH Bad Exempt', 0.0, category='E')

    def test_export_blocked_on_draft_move(self):
        currency = self.env.company.currency_id
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': currency.id,
            'invoice_date': '2026-05-01',
        })
        # draft state, the action should fail clean
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            move.action_eh_export_peppol_xml()
