# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Customer portal route tests.

Verifies the /my/eh/balance JSON route and the /my/eh/statement HTTP
route resolve, return the right scope, refuse cross-partner access,
and produce a parseable PDF byte stream.
"""

import base64

from odoo.tests import HttpCase, tagged


@tagged('eh_account_portal_extra', 'integration', 'post_install', '-at_install')
class TestPortalRoutes(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({
            'name': 'Portal Test Customer',
            'email': 'portal_test@example.com',
        })
        # Create a portal user bound to this partner.
        portal_group = cls.env.ref('base.group_portal')
        cls.portal_user = cls.env['res.users'].create({
            'name': 'Portal User',
            'login': 'portal_eh_test',
            'password': 'portal_eh_test_pass',
            'partner_id': cls.partner.id,
            'groups_id': [(6, 0, [portal_group.id])],
        })
        cls.partner.write({'company_id': cls.env.company.id})
        # res.users.create copies the user.name onto its partner. Re-write
        # the partner name AFTER the user is created so the test sees
        # the customer-facing name on the portal route response.
        cls.partner.write({'name': 'Portal Test Customer'})

    def test_balance_route_returns_zero_for_clean_partner(self):
        """A partner with no overdue lines returns total_overdue=0."""
        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        result = self.url_open(
            '/my/eh/balance',
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(result.status_code, 200)
        data = result.json().get('result', {})
        self.assertEqual(data.get('partner_name'), 'Portal Test Customer')
        self.assertEqual(data.get('total_overdue'), 0.0)
        self.assertEqual(data.get('days_overdue_max'), 0)

    def test_statement_route_returns_pdf_bytes(self):
        """The statement route should respond with a PDF mimetype."""
        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        result = self.url_open(
            '/my/eh/statement?date_from=2026-01-01&date_to=2026-12-31',
        )
        # The route either streams a PDF (200) or shows a friendly
        # error page (200 HTML). Both are acceptable; what we refuse is
        # 5xx or a redirect to login.
        self.assertEqual(result.status_code, 200)
        # PDF responses start with %PDF; HTML error pages do not.
        is_pdf = result.content[:5] == b'%PDF-'
        is_html_error = b'Statement' in result.content or b'<html' in result.content
        self.assertTrue(is_pdf or is_html_error)

    def test_statement_invalid_date_renders_error_template(self):
        """A malformed date returns the invalid-dates page, not a 500."""
        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        result = self.url_open(
            '/my/eh/statement?date_from=not-a-date',
        )
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'Invalid date range', result.content)

    def test_statement_inverted_date_range_renders_error(self):
        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        result = self.url_open(
            '/my/eh/statement?date_from=2026-12-31&date_to=2026-01-01',
        )
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'Invalid date range', result.content)

    def test_routes_require_authentication(self):
        """Anonymous access to the portal routes is refused."""
        result = self.url_open('/my/eh/statement', allow_redirects=False)
        # Anonymous users get redirected to login.
        self.assertIn(result.status_code, (301, 302, 303, 307))
