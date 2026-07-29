# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: state is a state machine, not a free field.

Every disclosure register (financial risk, maturity run, credit-risk note,
entity interest, sensitivity analysis, segment report, related party) drives
its state only through the manager-gated Finalise / Reopen actions, which run
under sudo. The inherited eh.workflow.guard mixin blocks a low-privilege user
from RPC-writing state directly, which would otherwise skip the action and
its lock / freeze.

The default test environment runs as SUPERUSER (env.su is True), for which
the mixin deliberately abstains (trusted, server-initiated code). The bypass
this closes is an interactive, low-privilege user, so the guarded write must
be attempted with_user(a normal user) for the guard to fire.
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_disclosures', 'post_install', '-at_install')
class TestWorkflowGuard(TransactionCase):
    """A direct RPC write to the guarded state field is refused for a
    low-privilege user; the sanctioned sudo path is still allowed."""

    # model -> minimal create vals for a draft record.
    GUARDED_MODELS = {
        'eh.fin.risk': {'name': 'Trade receivables'},
        'eh.fin.maturity.run': {},
        'eh.fin.credit.note': {},
        'eh.entity.interest': {'name': 'Subsidiary Co'},
        'eh.fin.sensitivity': {},
        'eh.segment.report': {},
        'eh.related.party': {'name': 'Parent Co'},
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A normal internal user in the EH user group (read/write/create on the
        # models) but NOT superuser, so the mixin guard is exercised rather
        # than the ACL layer.
        try:
            eh_user = cls.env.ref('eh_account_base.group_eh_user')
            cls.normal_user = cls.env['res.users'].with_context(
                mail_create_nosubscribe=True,
                no_reset_password=True,
            ).create({
                'name': 'Disclosure Guard Tester',
                'login': 'disclosure_guard_tester',
                'groups_id': [(6, 0, [eh_user.id])],
            })
        except Exception as exc:  # pragma: no cover - env cascade quirk
            cls.normal_user = None
            cls._user_error = exc

    def _require_user(self):
        if not self.normal_user:
            self.skipTest(
                "environment cannot create a test user: %s"
                % getattr(self, '_user_error', 'unknown'))

    def test_state_write_refused_for_low_privilege_user(self):
        self._require_user()
        for model, vals in self.GUARDED_MODELS.items():
            with self.subTest(model=model):
                rec = self.env[model].create(dict(vals))
                self.assertEqual(
                    rec.state, 'draft',
                    "%s must be born in draft" % model)
                # Direct RPC re-key of the state machine is blocked by the
                # mixin: a plain user cannot flip state past the action.
                with self.assertRaises(AccessError):
                    rec.with_user(self.normal_user).write(
                        {'state': 'finalised'})

    def test_sudo_action_path_still_transitions(self):
        # The sanctioned action path runs under sudo (env.su True), for which
        # the mixin abstains, so a state transition still goes through.
        for model, vals in self.GUARDED_MODELS.items():
            with self.subTest(model=model):
                rec = self.env[model].create(dict(vals))
                rec.sudo().write({'state': 'finalised'})
                self.assertEqual(
                    rec.state, 'finalised',
                    "%s: a sudo (action) write must still transition" % model)
