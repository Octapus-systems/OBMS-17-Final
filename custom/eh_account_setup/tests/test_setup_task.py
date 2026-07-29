# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Smoke tests for the accounting setup guide."""

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_setup', 'unit', 'post_install', '-at_install')
class TestSetupTaskLine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Task = cls.env['eh.account.setup.task']
        cls.Line = cls.env['eh.account.setup.task.line']

    def test_post_install_seeds_one_line_per_task_per_company(self):
        tasks = self.Task.search([])
        if not tasks:
            self.skipTest("no task definitions seeded")
        for company in self.env['res.company'].search([]):
            with self.subTest(company=company.name):
                lines = self.Line.search([('company_id', '=', company.id)])
                self.assertEqual(
                    len(lines), len(tasks),
                    f"company {company.name} expected {len(tasks)} lines, got {len(lines)}",
                )

    def test_post_init_hook_is_idempotent(self):
        from odoo.release import version_info
        from odoo.addons.eh_account_setup.hooks import post_init_hook
        before = len(self.Line.search([]))
        # Odoo 17+ post_init_hook(env); Odoo 16 post_init_hook(cr, registry).
        if version_info[0] >= 17:
            post_init_hook(self.env)
        else:
            post_init_hook(self.env.cr, self.env.registry)
        after = len(self.Line.search([]))
        self.assertEqual(before, after, "post_init_hook is not idempotent")

    def test_unique_per_company_constraint(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        with self.assertRaises(Exception):
            self.Line.create({
                'task_id': line.task_id.id,
                'company_id': line.company_id.id,
                'state': 'todo',
            })

    def test_action_open_resolves_or_errors_clearly(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        try:
            line.action_open()
        except UserError as exc:
            # Acceptable when the seeded action_xmlid is not resolvable
            # in this test database. The error must name the missing
            # xmlid so the operator can fix it.
            self.assertIn(line.action_xmlid, str(exc))

    def test_mark_done_records_audit(self):
        line = self.Line.search([('state', '=', 'todo')], limit=1)
        if not line:
            self.skipTest("no todo lines to test")
        line.action_mark_done()
        self.assertEqual(line.state, 'done')
        self.assertTrue(line.completed_at)
        self.assertEqual(line.completed_by_id, self.env.user)

    def test_skip_then_reset(self):
        line = self.Line.search([('state', '=', 'todo')], limit=1)
        if not line:
            self.skipTest("no todo lines to test")
        line.action_mark_skipped()
        self.assertEqual(line.state, 'skipped')
        self.assertTrue(line.completed_at)
        line.action_reset()
        self.assertEqual(line.state, 'todo')
        self.assertFalse(line.completed_at)
        self.assertFalse(line.completed_by_id)

    def test_is_relevant_matches_installed_modules(self):
        installed = set(self.env['ir.module.module'].sudo().search([
            ('state', '=', 'installed'),
        ]).mapped('name'))
        for line in self.Line.search([], limit=20):
            with self.subTest(task=line.task_id.key):
                required = (line.required_modules or '').split()
                expected = all(m in installed for m in required)
                self.assertEqual(line.is_relevant, expected)

    def test_search_is_relevant_returns_only_matching(self):
        true_ids = set(self.Line.search([('is_relevant', '=', True)]).ids)
        all_ids = set(self.Line.search([]).ids)
        false_ids = set(self.Line.search([('is_relevant', '=', False)]).ids)
        self.assertEqual(true_ids | false_ids, all_ids)
        self.assertFalse(true_ids & false_ids)

    def test_new_company_seeds_lines(self):
        tasks_count = len(self.Task.search([]))
        if not tasks_count:
            self.skipTest("no task definitions seeded")
        new_company = self.env['res.company'].create({
            'name': 'eh_account_setup test company',
        })
        seeded = self.Line.search([('company_id', '=', new_company.id)])
        self.assertEqual(
            len(seeded), tasks_count,
            "creating a company should seed one line per task definition",
        )

    def test_task_line_counts_track_state_changes(self):
        task = self.Task.search([], limit=1)
        if not task:
            self.skipTest("no task definitions seeded")
        before_done = task.line_done_count
        line = self.Line.search([
            ('task_id', '=', task.id),
            ('state', '=', 'todo'),
        ], limit=1)
        if not line:
            self.skipTest("no todo lines for the picked task")
        line.action_mark_done()
        task.invalidate_recordset()
        self.assertEqual(task.line_done_count, before_done + 1)
        self.assertGreaterEqual(task.line_count, task.line_done_count)
        if task.line_count:
            expected_ratio = 100.0 * task.line_done_count / task.line_count
            self.assertAlmostEqual(task.completion_ratio, expected_ratio, places=2)

    def test_sibling_line_ids_excludes_self_and_other_tasks(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        siblings = line.sibling_line_ids
        self.assertNotIn(line, siblings)
        for sibling in siblings:
            self.assertEqual(sibling.task_id, line.task_id)
            self.assertNotEqual(sibling.id, line.id)

    def test_peer_line_ids_excludes_self_other_companies_other_categories(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        peers = line.peer_line_ids
        self.assertNotIn(line, peers)
        for peer in peers:
            self.assertEqual(peer.company_id, line.company_id)
            self.assertEqual(peer.category, line.category)
            self.assertNotEqual(peer.id, line.id)

    def test_company_progress_counts_consistent(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        actual_total = self.Line.search_count([
            ('company_id', '=', line.company_id.id),
            ('is_relevant', '=', True),
        ])
        actual_done = self.Line.search_count([
            ('company_id', '=', line.company_id.id),
            ('is_relevant', '=', True),
            ('state', '=', 'done'),
        ])
        self.assertEqual(line.company_total_count, actual_total)
        self.assertEqual(line.company_done_count, actual_done)

    def test_task_progress_counts_consistent(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        actual_total = self.Line.search_count([
            ('task_id', '=', line.task_id.id),
        ])
        actual_done = self.Line.search_count([
            ('task_id', '=', line.task_id.id),
            ('state', '=', 'done'),
        ])
        self.assertEqual(line.task_total_count, actual_total)
        self.assertEqual(line.task_done_count, actual_done)

    def test_action_view_company_lines_returns_filtered_action(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        action = line.action_view_company_lines()
        self.assertEqual(action['res_model'], 'eh.account.setup.task.line')
        self.assertIn(('company_id', '=', line.company_id.id), action['domain'])

    def test_action_view_task_lines_returns_filtered_action(self):
        line = self.Line.search([], limit=1)
        if not line:
            self.skipTest("no lines to test")
        action = line.action_view_task_lines()
        self.assertEqual(action['res_model'], 'eh.account.setup.task.line')
        self.assertIn(('task_id', '=', line.task_id.id), action['domain'])

    def test_action_view_lines_filters_by_state_when_requested(self):
        task = self.Task.search([], limit=1)
        if not task:
            self.skipTest("no task definitions seeded")
        action_done = task.action_view_lines_done()
        self.assertIn(('state', '=', 'done'), action_done['domain'])
        action_todo = task.action_view_lines_todo()
        self.assertIn(('state', '=', 'todo'), action_todo['domain'])
        action_skipped = task.action_view_lines_skipped()
        self.assertIn(('state', '=', 'skipped'), action_skipped['domain'])
        action_all = task.action_view_lines()
        self.assertNotIn(('state', '=', 'done'), action_all['domain'])
        self.assertNotIn(('state', '=', 'todo'), action_all['domain'])
