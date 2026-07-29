# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Regression: the res.partner "Active Collections" smart-button count must
respect the eh.collections.case company record rule.

The count is computed under sudo() so that internal users without
collections model access do not hit an AccessError on any partner form.
The historic defect was that the sudo() also bypassed the global company
isolation rule, so for a partner shared across companies the count leaked
another company's non-resolved cases - a figure the drill-down action
(which runs without sudo) correctly withholds. The compute now constrains
the aggregate to the viewer's allowed companies, so the count agrees with
what the record rule would show.
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_collections', 'post_install', '-at_install')
class TestPartnerCollectionsCount(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Case = cls.env['eh.collections.case']
        cls.company_a = cls.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'EH Collections Co B',
        })
        # partner_a is a normal partner with company_id = False, i.e. shared
        # across every company - the ordinary case for the leak.
        cls.shared_partner = cls.partner_a

        # Two non-resolved cases against the shared partner, one per company.
        cls.case_a = cls.Case.create({
            'partner_id': cls.shared_partner.id,
            'company_id': cls.company_a.id,
        })
        cls.case_b = cls.Case.create({
            'partner_id': cls.shared_partner.id,
            'company_id': cls.company_b.id,
        })

        # Alice can only see Company A (single company_id => single-company
        # company_ids). She is an EH collections user so she legitimately has
        # read access to the case model within her own company.
        cls.alice = cls.env['res.users'].create({
            'name': 'Alice CoA',
            'login': 'coll_count_alice@test',
            'email': 'coll_count_alice@test',
            'company_id': cls.company_a.id,
            'groups_id': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

    def _sanity_non_resolved(self):
        # The default stage that new cases land in must be non-resolved for
        # this fixture to be meaningful.
        self.assertFalse(self.case_a.is_resolved)
        self.assertFalse(self.case_b.is_resolved)

    def test_count_excludes_other_company_cases(self):
        """Alice (Company A only) sees a count of 1, not 2: Company B's case
        against the shared partner must not leak into the smart button."""
        self._sanity_non_resolved()
        partner = self.shared_partner.with_user(self.alice)
        # env.companies for Alice is just Company A.
        self.assertEqual(partner.env.companies.ids, self.company_a.ids)
        partner.invalidate_recordset(['eh_active_collections_count'])
        self.assertEqual(partner.eh_active_collections_count, 1)

    def test_count_matches_drilldown_domain(self):
        """The count must agree with the record-rule-scoped drill-down: both
        reflect exactly one case for Alice."""
        partner = self.shared_partner.with_user(self.alice)
        partner.invalidate_recordset(['eh_active_collections_count'])
        count = partner.eh_active_collections_count
        action = partner.action_view_eh_active_collections()
        visible = self.Case.with_user(self.alice).search_count(
            action['domain'])
        self.assertEqual(count, visible)
        self.assertEqual(count, 1)

    def test_count_follows_multi_company_selection(self):
        """When Alice is also granted Company B and both are active, the
        count widens to include Company B's case - proving the figure tracks
        the allowed-company scope rather than a fixed single company."""
        # Grant Alice access to Company B as well (ATTRIBUTE assignment, not
        # a two-element (6,0,...) command on create - see cross-version trap).
        self.alice.company_ids = self.company_a + self.company_b
        partner = self.shared_partner.with_user(self.alice).with_context(
            allowed_company_ids=(self.company_a + self.company_b).ids)
        partner.invalidate_recordset(['eh_active_collections_count'])
        self.assertEqual(partner.eh_active_collections_count, 2)
