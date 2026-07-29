# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Shared workflow / posting mixins for the ERP Heritage accounting suite.

These close three systemic defect classes that were each re-solved (wrongly,
and with drift) across the sub-ledger modules:

eh.workflow.guard
    A state machine that is protected only by a ``readonly`` widget and a
    write() guard that blocks leaving a frozen state is NOT protected: a
    draft record's state is not frozen, so any user can RPC
    ``write({'state': 'posted'})`` straight past ``action_post`` and its
    checks and journal entry. This mixin blocks EVERY write to a guarded
    field unless the write originates from one of the record's own actions
    (which flag the write via ``_eh_workflow_write`` / the action context),
    closing the bypass for the whole suite in one place.

eh.post.once
    Nothing stopped the same source record/period being posted by two
    different runs, so variances, eliminations and reclasses could be
    double-booked. This mixin gives a one-line idempotency assertion:
    refuse to post a source already consumed by another posted record.

eh.gl.reversal
    Reversal entries were created but never re-sealed, so the frozen-figure
    guarantee held on the original move but was breakable on the reversal.
    This mixin reverses a sealed move AND re-seals the reversal.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


# Context flag set while a legitimate action writes a guarded field.
EH_WORKFLOW_ACTION = 'eh_workflow_action'


class EhWorkflowGuard(models.AbstractModel):
    """Block direct RPC/ORM writes to workflow-critical fields.

    Concrete models add ``'eh.workflow.guard'`` to ``_inherit`` and declare
    ``_eh_guarded_fields``. Every state-changing action must either wrap its
    write with :meth:`_eh_workflow_write` or open with
    ``self = self.with_context(eh_workflow_action=True)`` so the guarded
    write carries the flag.
    """

    _name = 'eh.workflow.guard'
    _description = "Workflow state-write guard"

    # Fields that may only change through the record's own actions, never a
    # direct write. Override per model (as a tuple) to widen, e.g.
    # ('state', 'current_step', 'submitted_amount').
    _eh_guarded_fields = ('state',)
    # Fields stripped from a non-superuser CREATE (a record must not be born
    # in a guarded state). Defaults to _eh_guarded_fields. A model whose
    # write-guard covers identity fields that ARE legitimately set at create
    # (e.g. a request's move_id/policy_id/rule_id) narrows this to just the
    # state-machine fields so creation still works while repointing-by-write
    # stays blocked.
    _eh_create_guarded_fields = None

    @api.model_create_multi
    def create(self, vals_list):
        # Close the create-side bypass: a low-privilege user must not be able
        # to make a record BORN in a guarded state (e.g.
        # create({'state': 'approved'}) to skip the whole workflow). A plain
        # non-superuser create gets its guarded fields stripped so the model
        # default applies; the record then starts at its initial state and can
        # only advance through the sanctioned actions (which run under sudo).
        #
        # SECURITY: provenance is proven by env.su, NOT a context flag. Odoo
        # passes client-supplied context straight into call_kw, so any context
        # sentinel (the old 'eh_workflow_action' key) is forgeable by the
        # client and provides no real protection.
        if not self.env.su:
            guarded = set(self._eh_create_guarded_fields
                          if self._eh_create_guarded_fields is not None
                          else self._eh_guarded_fields)
            vals_list = [
                {k: v for k, v in (vals or {}).items() if k not in guarded}
                for vals in vals_list
            ]
        return super().create(vals_list)

    def write(self, vals):
        # Guarded (state/workflow) fields may only be written by the record's
        # own actions, which run under sudo (env.su). A direct non-superuser
        # write is refused. Provenance is env.su, not a context flag, for the
        # forgeability reason documented on create().
        if not self.env.su:
            blocked = set(vals) & set(self._eh_guarded_fields)
            if blocked:
                raise AccessError(_(
                    "%(model)s: fields %(fields)s can only change through "
                    "the record's own actions, not a direct write. Use the "
                    "provided buttons/methods.",
                    model=self._description,
                    fields=', '.join(sorted(blocked)),
                ))
        return super().write(vals)

    def _eh_workflow_write(self, vals):
        """Write guarded fields from inside a sanctioned action (as su)."""
        return self.sudo().write(vals)

    def _eh_workflow_action(self):
        """Return an su recordset for a state-changing action.

        Use at the top of an action:  ``self = self._eh_workflow_action()``
        so subsequent ``rec.state = ...`` assignments (and any helper the
        recordset calls) write past the guard. Access is already gated by the
        action's own permission checks; sudo only proves the write is
        server-initiated - which a forgeable context key cannot - and keeps
        the real env.user for audit stamps (sudo bypasses access rights but
        does not change the current user).
        """
        return self.sudo()


class EhPostOnce(models.AbstractModel):
    """Idempotency helper: a source may be booked to the ledger only once."""

    _name = 'eh.post.once'
    _description = "Post-once idempotency helper"

    def _eh_assert_source_unposted(self, source_field, posted_states=('posted',),
                                   state_field='state'):
        """Refuse if another posted record already consumed the same source.

        :param source_field: name of the x2many/x2one field carrying the
            source records this run posts (e.g. 'actual_ids').
        :param posted_states: states that count as already-booked.
        :param state_field: the state field name on this model.
        """
        self.ensure_one()
        source = self[source_field]
        source_ids = source.ids if hasattr(source, 'ids') else [source.id]
        if not source_ids:
            return
        dup = self.search([
            (state_field, 'in', list(posted_states)),
            ('id', '!=', self.id),
            (source_field, 'in', source_ids),
        ], limit=5)
        if dup:
            raise UserError(_(
                "The source records this posts were already booked by "
                "%(recs)s. A period/source can only be posted once; reverse "
                "the existing posting first.",
                recs=', '.join(dup.mapped('display_name')),
            ))


class EhGlReversal(models.AbstractModel):
    """Reverse a sealed journal entry AND re-seal the reversal.

    eh_account_base seals posted moves (eh_sealed) so their figures freeze.
    Reversals must be sealed too, or the frozen-figure guarantee is
    breakable on the reversal side.
    """

    _name = 'eh.gl.reversal'
    _description = "GL reversal + re-seal helper"

    def _eh_seal_reversal(self, reversal_moves):
        """Seal already-created reversal move(s) so they are as immutable as
        the sealed entry they unwind.

        Drop-in for adopters that keep their own ``_reverse_moves`` call: it
        adds the seal (closing the "reversal side stays editable" gap) without
        changing the reversal's own dating / cancel / reconciliation
        semantics. Idempotent; a no-op on moves that lack eh_sealed or are
        already sealed.
        """
        if not reversal_moves:
            return reversal_moves
        sealable = reversal_moves.filtered(
            lambda m: 'eh_sealed' in m._fields and not m.eh_sealed)
        if sealable:
            sealable.sudo().with_context(eh_seal_internal=True).write(
                {'eh_sealed': True})
        return reversal_moves

    def _eh_reverse_sealed_move(self, move, date=None, ref=None, cancel=True):
        """Reverse a (possibly sealed) move and seal the reversal.

        Returns the reversal move(s). ``cancel=True`` posts the reversal and
        reconciles it flat against the original.
        """
        if not move:
            return move.browse()
        reversal = move._reverse_moves([{
            'date': date or fields.Date.context_today(self),
            'ref': ref or _("Reversal of %s", move.name or move.display_name),
        }], cancel=cancel)
        return self._eh_seal_reversal(reversal)
