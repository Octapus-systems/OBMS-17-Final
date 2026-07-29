# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Browser tour run for the consolidation entity register (IFRS 10/10 UI
layer).

Runs the eh_consolidation_test_tour in a real headless Chrome via HttpCase.
The eh_tour tag keeps browser cycles out of the default per-module runs;
the matrix runner selects them with --tours (test-tags eh_tour).
"""

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_consolidation', 'post_install', '-at_install')
class TestConsolidationTour(HttpCase):

    def test_consol_entity_create_tour(self):
        before = self.env['eh.consol.entity'].search([])
        self.start_tour('/web', 'eh_consolidation_test_tour', login='admin')
        entity = self.env['eh.consol.entity'].search([]) - before
        self.assertEqual(len(entity), 1,
                         'tour did not create the consolidation entity')
        self.assertEqual(entity.name, 'Tour Group Consolidated')
        self.assertEqual(entity.code, 'tour_group')
        self.assertTrue(entity.parent_company_id,
                        'default parent company was not applied')
        self.assertTrue(entity.presentation_currency_id,
                        'default presentation currency was not applied')
