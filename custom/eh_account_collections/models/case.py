# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.collections.case: the main collections record.

One open case per (company, partner) at any time. The auto creator
aggregates open receivable journal items per partner and either updates
an existing open case or creates a new one. Resolved cases stay resolved
even when new overdue invoices appear; a fresh case is created instead
so the resolution history remains intact.

Compute fields read from the live ledger via a single SQL pass per case
batch, so a screen of 200 cases refreshes its totals in one query.
"""

from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery


class EhCollectionsCase(models.Model):
    _name = 'eh.collections.case'
    _description = "Collections case"
    _order = 'priority desc, days_overdue_max desc, id desc'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.cron.batch.mixin']
    _rec_name = 'name'

    name = fields.Char(
        compute='_compute_name',
        store=True,
        help="Auto computed from partner and company.",
    )

    partner_id = fields.Many2one(
        'res.partner',
        required=True,
        index=True,
        ondelete='cascade',
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id',
        store=True,
        readonly=True,
    )

    stage_id = fields.Many2one(
        'eh.collections.stage',
        required=True,
        tracking=True,
        group_expand='_read_group_stage_ids',
        default=lambda self: self._default_stage_id(),
        ondelete='restrict',
    )

    collector_id = fields.Many2one(
        'res.users',
        index=True,
        tracking=True,
        help="The user responsible for following up on this case.",
    )

    priority = fields.Selection(
        [
            ('0', "Low"),
            ('1', "Medium"),
            ('2', "High"),
            ('3', "Urgent"),
        ],
        default='1',
        index=True,
    )

    # Live AR aggregation
    total_overdue_amount = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Sum of amount_residual on overdue receivable AML for this "
            "partner. Refreshed by the auto creator and on demand."
        ),
    )
    oldest_overdue_date = fields.Date(
        help="Earliest due date among the open receivables for this partner.",
    )
    days_overdue_max = fields.Integer(
        help="Days between today and oldest_overdue_date.",
    )

    # Promise tracking
    promised_amount = fields.Monetary(
        currency_field='currency_id',
        compute='_compute_promise',
        store=False,
    )
    promised_date = fields.Date(
        compute='_compute_promise',
        store=False,
    )
    has_active_promise = fields.Boolean(
        compute='_compute_promise',
        store=False,
        search='_search_has_active_promise',
    )
    broken_promise = fields.Boolean(
        compute='_compute_promise',
        store=False,
        search='_search_broken_promise',
        help=(
            "True when the latest promise to pay has lapsed (promise "
            "date is in the past) and the case has not been resolved. "
            "The broken-promise cron escalates these cases."
        ),
    )
    last_broken_promise_handled_at = fields.Datetime(
        readonly=True, copy=False,
        help=(
            "When the broken-promise cron last escalated this case. "
            "Prevents repeated escalation on the same broken promise."
        ),
    )

    def _search_has_active_promise(self, operator, value):
        from odoo.exceptions import UserError
        if operator not in ('=', '!='):
            raise UserError("Unsupported search operator on has_active_promise.")
        positive = bool(value) if operator == '=' else not bool(value)
        today = fields.Date.context_today(self)
        domain = [
            ('action_ids.promise_amount', '!=', 0.0),
            ('action_ids.promise_date', '>=', today),
        ]
        return domain if positive else ['!'] + domain

    def _search_broken_promise(self, operator, value):
        from odoo.exceptions import UserError
        if operator not in ('=', '!='):
            raise UserError("Unsupported search operator on broken_promise.")
        positive = bool(value) if operator == '=' else not bool(value)
        today = fields.Date.context_today(self)
        # A broken promise: at least one promise on the case whose
        # date < today, AND no promise with date >= today (no later
        # promise replacing the lapsed one), AND the case is not
        # resolved.
        broken_ids = self.env['eh.collections.case'].search([
            ('is_resolved', '=', False),
            ('action_ids.promise_amount', '!=', 0.0),
            ('action_ids.promise_date', '<', today),
        ]).filtered(
            lambda c: not c.has_active_promise,
        ).ids
        domain = [('id', 'in', broken_ids)]
        return domain if positive else ['!'] + domain

    # Activity timeline
    last_action_at = fields.Datetime(
        compute='_compute_last_action',
        store=False,
    )
    last_action_type = fields.Selection(
        related='action_ids.action_type',
        readonly=True,
    )
    next_action_date = fields.Date(
        tracking=True,
        help="Manually set next follow up date for this case.",
    )

    action_ids = fields.One2many(
        'eh.collections.action', 'case_id',
    )
    action_count = fields.Integer(compute='_compute_action_count')

    notes = fields.Html()

    is_resolved = fields.Boolean(
        related='stage_id.is_resolved',
        store=True,
        help="True when the case is in a terminal stage.",
    )

    color = fields.Integer()  # for kanban customisation

    # ---- follow-up engine state ----

    current_followup_level_id = fields.Many2one(
        'eh.collections.followup.level',
        ondelete='restrict',
        index=True,
        tracking=True,
        help=(
            "The most recent follow-up level applied to this case. The "
            "next cron pass picks the next applicable level above this "
            "one whose days_overdue threshold is met."
        ),
    )
    last_followup_sent_at = fields.Datetime(
        readonly=True, copy=False,
        help="Timestamp of the most recent follow-up email sent on this case.",
    )
    next_followup_date = fields.Date(
        compute='_compute_next_followup_date',
        store=True,
        help=(
            "Date on which the next follow-up becomes due, derived from "
            "last_followup_sent_at plus the current level's delay_days. "
            "Indexed via the cron's domain filter."
        ),
    )
    followup_count = fields.Integer(
        default=0, readonly=True, copy=False,
        help="Total follow-up messages sent across this case's lifetime.",
    )
    followup_paused = fields.Boolean(
        default=False, tracking=True,
        help=(
            "Pause auto follow-ups on this case (for example while "
            "negotiating a payment plan). The cron skips paused cases; "
            "manual sends are still permitted."
        ),
    )

    _sql_constraints = [
        ('open_case_per_partner_company', 'EXCLUDE (partner_id WITH =, company_id WITH =)\n               WHERE (NOT is_resolved)', 'Only one open case per partner per company.'),
    ]

    # ---- defaults / lifecycle ----

    @api.model
    def _default_stage_id(self):
        return self.env['eh.collections.stage'].search(
            [('is_default', '=', True)], limit=1, order='sequence',
        ).id or self.env['eh.collections.stage'].search(
            [], limit=1, order='sequence',
        ).id

    @api.model
    def _read_group_stage_ids(self, stages, domain, order=None):
        """Show every stage in the kanban view, even empty ones, so the
        column structure is stable as cases move."""
        all_stages = self.env['eh.collections.stage'].search(
            [], order='sequence',
        )
        return all_stages

    @api.depends('partner_id', 'company_id')
    def _compute_name(self):
        for rec in self:
            partner = rec.partner_id.name or ''
            company = rec.company_id.name or ''
            rec.name = (
                "%s / %s" % (partner, company) if company else partner
            )

    # ---- onchange (live form feedback) ----

    @api.onchange('partner_id')
    def _onchange_partner_id_load_aging(self):
        """Pre-fill overdue figures + currency when partner is picked.

        Without this, a user creating a case manually has to save and
        click Refresh to see the partner's actual overdue position.
        Pre-fill turns it into a single-form-page operation.
        """
        for rec in self:
            if not rec.partner_id:
                continue
            commercial = rec.partner_id.commercial_partner_id or rec.partner_id
            company = rec.company_id or self.env.company
            rec.currency_id = company.currency_id
            rows = self._fetch_overdue_aggregation(
                [company.id], fields.Date.context_today(rec),
            )
            row = next(
                (r for r in rows
                 if r['partner_id'] == commercial.id
                 and r['company_id'] == company.id),
                None,
            )
            if row:
                rec.total_overdue_amount = round(row['total_amount'], 2)
                rec.oldest_overdue_date = row['oldest_due_date']
                rec.days_overdue_max = row['max_days_overdue']
            else:
                rec.total_overdue_amount = 0.0
                rec.oldest_overdue_date = False
                rec.days_overdue_max = 0

    @api.onchange('total_overdue_amount', 'days_overdue_max')
    def _onchange_overdue_priority(self):
        """Auto-suggest priority based on overdue magnitude + age.

        The user can still override; the onchange just primes the
        field to a sensible default so a fresh case lands in the
        right column on the kanban without manual sorting.
        """
        for rec in self:
            if not rec.total_overdue_amount and not rec.days_overdue_max:
                continue
            if rec.days_overdue_max > 90 or rec.total_overdue_amount > 50000:
                rec.priority = '3'
            elif rec.days_overdue_max > 60 or rec.total_overdue_amount > 10000:
                rec.priority = '2'
            elif rec.days_overdue_max > 30 or rec.total_overdue_amount > 1000:
                rec.priority = '1'
            else:
                rec.priority = '0'

    @api.depends('action_ids')
    def _compute_action_count(self):
        for rec in self:
            rec.action_count = len(rec.action_ids)

    @api.depends('action_ids.action_date')
    def _compute_last_action(self):
        for rec in self:
            most_recent = rec.action_ids.sorted(
                'action_date', reverse=True,
            )[:1]
            rec.last_action_at = (
                most_recent.action_date if most_recent else False
            )

    @api.depends('current_followup_level_id',
                 'current_followup_level_id.is_terminal',
                 'current_followup_level_id.delay_days',
                 'last_followup_sent_at')
    def _compute_next_followup_date(self):
        for rec in self:
            if (
                not rec.current_followup_level_id
                or rec.current_followup_level_id.is_terminal
            ):
                rec.next_followup_date = False
                continue
            anchor = rec.last_followup_sent_at
            if not anchor:
                rec.next_followup_date = fields.Date.context_today(rec)
                continue
            anchor_date = (
                anchor.date() if hasattr(anchor, 'date') else anchor
            )
            from datetime import timedelta
            rec.next_followup_date = anchor_date + timedelta(
                days=rec.current_followup_level_id.delay_days,
            )

    @api.depends(
        'action_ids.promise_amount', 'action_ids.promise_date',
        'action_ids.action_date', 'is_resolved',
    )
    def _compute_promise(self):
        today = fields.Date.context_today(self)
        for rec in self:
            promises = rec.action_ids.filtered(
                lambda a: a.promise_amount and a.promise_date,
            ).sorted('action_date', reverse=True)
            if promises:
                latest = promises[0]
                rec.promised_amount = latest.promise_amount
                rec.promised_date = latest.promise_date
                rec.has_active_promise = (
                    latest.promise_date >= today
                )
                rec.broken_promise = (
                    not rec.is_resolved
                    and latest.promise_date < today
                )
            else:
                rec.promised_amount = 0.0
                rec.promised_date = False
                rec.has_active_promise = False
                rec.broken_promise = False

    # ---- actions ----

    def action_log_action(self, action_type, summary,
                          detail=None,
                          contact_made=False,
                          promise_amount=0.0,
                          promise_date=None):
        """Programmatic helper to log an action against a case.

        Used by tests and by future SMS / email integrations. The form
        view exposes a richer action log dialog.
        """
        self.ensure_one()
        return self.env['eh.collections.action'].create({
            'case_id': self.id,
            'action_type': action_type,
            'summary': summary,
            'detail': detail or False,
            'contact_made': bool(contact_made),
            'promise_amount': promise_amount or 0.0,
            'promise_date': promise_date or False,
        })

    def action_open_reminder_wizard(self):
        """Open the manual reminder wizard pre-filled with this case."""
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id or self.partner_id
        return {
            'type': 'ir.actions.act_window',
            'name': _("Send Reminder"),
            'res_model': 'eh.collections.reminder.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {
                'default_case_id': self.id,
                'default_partner_id': partner.id,
            },
        }

    def action_log_call(self):
        """Open the action form pre filled as a call."""
        self.ensure_one()
        return self._open_new_action_action('call', "Phone call")

    def action_log_email(self):
        self.ensure_one()
        return self._open_new_action_action('email', "Email sent")

    def action_log_sms(self):
        self.ensure_one()
        return self._open_new_action_action('sms', "SMS sent")

    def _open_new_action_action(self, action_type, default_summary):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Log Action"),
            'res_model': 'eh.collections.action',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {
                'default_case_id': self.id,
                'default_action_type': action_type,
                'default_summary': default_summary,
            },
        }

    def action_view_actions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Action Log"),
            'res_model': 'eh.collections.action',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('case_id', '=', self.id)],
            'context': {'default_case_id': self.id},
        }

    def action_view_open_invoices(self):
        """Open the AML list filtered to this partner's open receivables."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Open Invoices"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('partner_id', '=', self.partner_id.id),
                ('company_id', '=', self.company_id.id),
                ('account_id.account_type', '=', 'asset_receivable'),
                ('amount_residual', '!=', 0),
                ('parent_state', '=', 'posted'),
            ],
        }

    def action_resolve(self):
        """Move the case to the first resolved stage."""
        resolved_stage = self.env['eh.collections.stage'].search(
            [('is_resolved', '=', True)], limit=1, order='sequence',
        )
        if not resolved_stage:
            raise UserError(_(
                "No resolved stage configured. Add one with is_resolved=True.",
            ))
        for rec in self:
            rec.stage_id = resolved_stage.id
        return True

    def action_escalate(self):
        """Bump priority and move to an escalation stage if available."""
        for rec in self:
            current_priority = int(rec.priority or '1')
            rec.priority = str(min(current_priority + 1, 3))
        return True

    def action_refresh_overdue(self):
        """Refresh total_overdue_amount and aging info for the selected
        cases by re-running the auto creator's aggregation logic.
        """
        self._refresh_overdue_amounts(self)
        return True

    # ---- follow-up engine ----

    def action_send_followup_now(self):
        """Manually trigger the next applicable follow-up level.

        Used from the case form when a collector wants to send a
        templated reminder immediately rather than wait for the cron.
        """
        for rec in self:
            level = rec._next_followup_level()
            if not level:
                raise UserError(_(
                    "No applicable follow-up level for case %s. Either "
                    "the case is not overdue enough, the chain is "
                    "exhausted (terminal level), or there are no levels "
                    "configured for this company.",
                    rec.display_name,
                ))
            rec._apply_followup_level(level, source='manual')
        return True

    def action_pause_followups(self):
        for rec in self:
            rec.followup_paused = True

    def action_resume_followups(self):
        for rec in self:
            rec.followup_paused = False

    def _next_followup_level(self):
        """Return the followup level that should fire next on this case.

        The next level is the first configured level for the company,
        ordered by sequence then days_overdue, that is BOTH:

        * Past the case's days_overdue_max threshold.
        * After (or at) the current_followup_level_id by sequence; never
          regresses, never repeats. The current level itself is not a
          candidate to fire again.
        """
        self.ensure_one()
        if self.followup_paused or self.is_resolved:
            return self.env['eh.collections.followup.level']
        Level = self.env['eh.collections.followup.level']
        levels = Level.search(
            [
                ('company_id', '=', self.company_id.id),
                ('active', '=', True),
                ('days_overdue', '<=', self.days_overdue_max or 0),
            ],
            order='sequence, days_overdue, id',
        )
        if not levels:
            return Level.browse()
        if not self.current_followup_level_id:
            return levels[:1]
        # Find the first level strictly after the current one in order.
        passed_current = False
        for level in levels:
            if passed_current:
                return level
            if level == self.current_followup_level_id:
                passed_current = True
        return Level.browse()

    def _apply_followup_level(self, level, source='cron'):
        """Apply the given level to this case.

        Sends an email when the level prescribes one, schedules a call
        activity when the level prescribes one, escalates the stage
        when the level prescribes that. Always logs an action row so
        the audit timeline shows the follow-up alongside manual contacts.
        """
        self.ensure_one()
        action_summary = _("Follow-up: %s") % level.name
        contact_made = False

        if level.action_type in ('email', 'email_and_activity'):
            if not level.mail_template_id:
                raise UserError(_(
                    "Level %s has no mail_template_id configured.",
                    level.name,
                ))
            level.mail_template_id.send_mail(
                self.id,
                force_send=False,
                email_layout_xmlid='mail.mail_notification_layout',
            )
            contact_made = True

        if level.action_type in ('email_and_activity', 'activity'):
            self._schedule_followup_activity(level)

        if level.also_send_sms and level.sms_template_id:
            if self._eh_send_sms_followup(level):
                contact_made = True

        if level.action_type == 'escalate':
            target = level.escalate_to_stage_id or self.env[
                'eh.collections.stage'
            ].search(
                [('is_disputed', '=', True)], limit=1, order='sequence',
            )
            if target:
                self.stage_id = target.id

        # Atomic counter increment via SQL to avoid the read-modify-write
        # race that two concurrent cron passes (manual + scheduled) could
        # otherwise hit on the same case.
        self.env.cr.execute(SQL(
            "UPDATE eh_collections_case "
            "SET followup_count = followup_count + 1, "
            "    last_followup_sent_at = NOW() AT TIME ZONE 'UTC', "
            "    current_followup_level_id = %s "
            "WHERE id = %s",
            level.id, self.id,
        ))
        self.invalidate_recordset(
            ['followup_count', 'last_followup_sent_at',
             'current_followup_level_id'],
        )

        self.env['eh.collections.action'].create({
            'case_id': self.id,
            'action_type': 'email' if level.action_type in (
                'email', 'email_and_activity',
            ) else 'note',
            'summary': "%s [%s]" % (action_summary, source),
            'contact_made': contact_made,
        })
        self.message_post(
            body=_(
                "Follow-up applied: %(name)s (level %(seq)s). Source: %(src)s.",
                name=level.name,
                seq=level.sequence,
                src=source,
            ),
        )

    def _schedule_followup_activity(self, level):
        """Schedule an activity per the level's configuration.

        Falls back to the standard 'Call' activity type when the level
        does not specify one, so the cron does not silently drop the
        scheduling step.
        """
        self.ensure_one()
        from datetime import timedelta
        ActType = self.env['mail.activity.type']
        act_type = level.activity_type_id or ActType.search(
            [('category', '=', 'phonecall')], limit=1,
        ) or ActType.search([], limit=1)
        if not act_type:
            return
        deadline = fields.Date.context_today(self) + timedelta(
            days=max(0, level.activity_offset_days or 0),
        )
        responsible = self._eh_resolve_responsible(level)
        self.activity_schedule(
            activity_type_id=act_type.id,
            date_deadline=deadline,
            summary=level.activity_summary or _("Follow-up call"),
            user_id=responsible.id if responsible else self.env.uid,
        )

    def _eh_resolve_responsible(self, level):
        """Resolve the user the follow-up activity is assigned to, per the
        level's responsible_type. Falls back to the case collector, then
        the current user."""
        self.ensure_one()
        rtype = level.responsible_type or 'collector'
        if rtype == 'salesperson':
            partner = self.partner_id.commercial_partner_id or self.partner_id
            if partner.user_id:
                return partner.user_id
        elif rtype == 'account_manager':
            manager = self.env['res.users'].search(
                [('groups_id', 'in',
                  self.env.ref('eh_account_base.group_eh_manager').id),
                 ('company_ids', 'in', self.company_id.ids)],
                limit=1, order='id')
            if manager:
                return manager
        return self.collector_id or self.env.user

    def _eh_send_sms_followup(self, level):
        """Queue an SMS to the partner's mobile from the level's SMS
        template. Creates an sms.sms in the outgoing queue (the standard
        SMS cron sends it), so no network call happens inline."""
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id or self.partner_id
        number = partner.phone or (
            partner['mobile'] if 'mobile' in partner._fields else False)
        if not number or not level.sms_template_id:
            return False
        body = level.sms_template_id._render_field(
            'body', [self.id])[self.id]
        self.env['sms.sms'].sudo().create({
            'partner_id': partner.id,
            'number': number,
            'body': body,
        })
        return True

    # ---- broken-promise handling ----

    def action_handle_broken_promise(self):
        """Manually run the broken-promise escalation on this case.

        The cron normally handles this; the button is for cases where
        the collector wants to escalate immediately without waiting
        for the next cron tick.
        """
        for rec in self:
            if not rec.broken_promise:
                raise UserError(_(
                    "Case %s does not have a broken promise to handle.",
                    rec.display_name,
                ))
            rec._handle_broken_promise()
        return True

    def _handle_broken_promise(self):
        """Escalate a case whose latest promise to pay has lapsed.

        Bumps priority by one (capped at 3), forces the next applicable
        follow-up level to fire, logs an action row tagged
        broken_promise so the audit timeline shows the trigger, and
        records the handled-at timestamp so the same lapsed promise
        does not re-escalate on every cron pass.
        """
        self.ensure_one()
        # Bump priority.
        current_priority = int(self.priority or '0')
        self.priority = str(min(current_priority + 1, 3))
        # Move to an escalated stage when one is configured.
        escalated_stage = self.env['eh.collections.stage'].search(
            [('is_escalated', '=', True)], limit=1, order='sequence',
        )
        if escalated_stage and self.stage_id != escalated_stage:
            self.stage_id = escalated_stage.id
        # Apply the next follow-up level (skips if paused or chain done).
        if not self.followup_paused:
            level = self._next_followup_level()
            if level:
                self._apply_followup_level(level, source='broken_promise')
        # Log the broken-promise action.
        self.env['eh.collections.action'].create({
            'case_id': self.id,
            'action_type': 'note',
            'summary': _("Broken promise: amount %(amount).2f, due %(date)s",
                         amount=self.promised_amount or 0.0,
                         date=self.promised_date),
            'contact_made': False,
        })
        self.message_post(body=_(
            "Broken promise detected. Promised %(amount).2f by %(date)s; "
            "case escalated.",
            amount=self.promised_amount or 0.0,
            date=self.promised_date,
        ))
        self.last_broken_promise_handled_at = fields.Datetime.now()

    @api.model
    def _cron_handle_broken_promises(self, batch_size=200):
        """Cron entry point. Escalate cases whose promise to pay has lapsed.

        Skips cases already handled in the last 24 hours so a single
        broken promise does not re-escalate on every cron tick. Once
        the customer makes a fresh promise (which sets a new
        promise_date >= today), broken_promise flips to False and the
        case leaves the candidate set automatically.

        Per-record savepoint isolation so one bad case never freezes
        the queue.
        """
        from datetime import timedelta
        today = fields.Date.context_today(self)
        twenty_four_hours_ago = (
            fields.Datetime.now() - timedelta(hours=24)
        )
        candidates = self.search([
            ('is_resolved', '=', False),
            ('action_ids.promise_amount', '!=', 0.0),
            ('action_ids.promise_date', '<', today),
            '|',
            ('last_broken_promise_handled_at', '=', False),
            ('last_broken_promise_handled_at', '<', twenty_four_hours_ago),
        ], limit=batch_size)
        # Filter on the computed broken_promise field to exclude cases
        # that have a fresher promise overriding the lapsed one.
        candidates = candidates.filtered('broken_promise')
        self._eh_for_each_savepoint(
            candidates,
            lambda case: case._handle_broken_promise(),
            log_label="Broken-promise cron",
        )
        return {'escalated': len(candidates)}

    @api.model
    def _cron_send_followups(self, batch_size=200):
        """Cron entry point. Send due follow-ups across all companies.

        Per record try / except so one bad case never freezes the queue.
        Each level transition is recorded on its own savepoint so a
        partial failure (mail server down for one partner) rolls back
        only that case's update, not the whole batch.
        """
        today = fields.Date.context_today(self)
        candidates = self.search(
            [
                ('is_resolved', '=', False),
                ('followup_paused', '=', False),
                '|', ('next_followup_date', '<=', today),
                     ('next_followup_date', '=', False),
            ],
            limit=batch_size,
        )
        def _send(case):
            level = case._next_followup_level()
            if not level:
                return
            case._apply_followup_level(level, source='cron')

        self._eh_for_each_savepoint(
            candidates, _send, log_label="Follow-up cron",
        )

    # ---- auto creator ----

    @api.model
    def auto_create_cases(self, company_ids=None):
        """Scan open receivables in the given companies and ensure one
        open case per (partner, company). Existing open cases are
        refreshed; resolved cases are left alone (a new case is created
        instead so the resolution history persists).

        Returns a dict with created and refreshed counts.
        """
        if company_ids is None:
            company_ids = self.env.companies.ids
        if not company_ids:
            return {'created': 0, 'refreshed': 0}
        today = fields.Date.context_today(self)

        rows = self._fetch_overdue_aggregation(company_ids, today)
        if not rows:
            return {'created': 0, 'refreshed': 0}

        # Fetch every existing open case in scope in ONE query keyed by
        # (company, partner), instead of one search per overdue partner
        # (the previous code issued O(partners) searches a night).
        partner_ids = [row['partner_id'] for row in rows]
        existing_cases = self.search([
            ('company_id', 'in', company_ids),
            ('partner_id', 'in', partner_ids),
            ('is_resolved', '=', False),
        ])
        existing_by_key = {}
        for case in existing_cases:
            existing_by_key.setdefault(
                (case.company_id.id, case.partner_id.id), case,
            )

        created = 0
        refreshed = 0
        create_vals = []
        for row in rows:
            company_id = row['company_id']
            partner_id = row['partner_id']
            vals = {
                'total_overdue_amount': round(row['total_amount'], 2),
                'oldest_overdue_date': row['oldest_due_date'],
                'days_overdue_max': row['max_days_overdue'],
            }
            existing = existing_by_key.get((company_id, partner_id))
            if existing:
                existing.write(vals)
                refreshed += 1
            else:
                vals.update({
                    'company_id': company_id,
                    'partner_id': partner_id,
                })
                create_vals.append(vals)
                created += 1
        if create_vals:
            # One bulk create instead of one create() per new partner.
            self.create(create_vals)
        return {'created': created, 'refreshed': refreshed}

    @api.model
    def _cron_auto_create_cases(self):
        """Cron entry point. Runs across all accessible companies."""
        return self.auto_create_cases()

    @api.model
    def _cron_refresh_open_cases(self):
        """Re-aggregate overdue amounts on every open case nightly.

        auto_create_cases only writes to cases whose partner appears
        in today's overdue aggregation. A partner who has paid down
        their balance overnight has no row in the aggregation, so
        their existing case still displays yesterday's stale total
        until someone manually clicks Refresh. This cron closes that
        loop: it fetches every non-resolved case and runs the same
        zero-out-or-update pass that the manual button uses.

        Cheap because _fetch_overdue_aggregation runs one SQL with
        a per-company GROUP BY; the Python loop reads from the result
        dict in O(1) per case.
        """
        open_cases = self.search([('is_resolved', '=', False)])
        if not open_cases:
            return {'refreshed': 0}
        self._refresh_overdue_amounts(open_cases)
        return {'refreshed': len(open_cases)}

    @api.model
    def _fetch_overdue_aggregation(self, company_ids, today):
        """Aggregate open receivable AML lines per (company, partner).

        One SQL GROUP BY query, one Python pass to wrap rows. The
        previous implementation loaded every overdue AML record and
        looped in Python, which was O(N) memory and CPU and broke past
        roughly fifty thousand overdue lines. With GROUP BY, scale is
        bounded by the number of distinct (company, partner) pairs.
        """
        company_ids = list(company_ids or ())
        if not company_ids:
            return []
        # The SQL builder already handles posted-only, company scope,
        # cancellation exclusion, account type filter, and account.code
        # join. We add the GROUP BY here and the receivable / overdue
        # filters via the helpers.
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_account_types(('asset_receivable',))
        query.where_posted_only()
        query.where_raw(SQL("aml.partner_id IS NOT NULL"))
        query.where_raw(SQL(
            "COALESCE(aml.amount_residual, aml.debit - aml.credit) <> 0"
        ))
        query.where_raw(SQL(
            "COALESCE(aml.date_maturity, aml.date) < %s", today,
        ))
        # Aggregate by the COMMERCIAL partner, not the raw line partner.
        # Receivables booked against a child contact must roll up to the
        # invoiced company so the case partner matches the onchange aging
        # loader and the statement report, both of which key on
        # commercial_partner_id. res_partner.commercial_partner_id is a
        # stored column, so the correlated lookup is index-backed.
        commercial_partner_sql = SQL(
            "COALESCE("
            "(SELECT rp.commercial_partner_id FROM res_partner rp "
            "WHERE rp.id = aml.partner_id), aml.partner_id)"
        )
        query.select_field('company_id')
        query.select(commercial_partner_sql, 'partner_id')
        query.select(
            SQL("SUM(COALESCE(aml.amount_residual, aml.debit - aml.credit))"),
            'total_amount',
        )
        query.select(
            SQL("MIN(COALESCE(aml.date_maturity, aml.date))"),
            'oldest_due_date',
        )
        query.select(
            SQL(
                "MAX(%s::date - COALESCE(aml.date_maturity, aml.date))",
                today,
            ),
            'max_days_overdue',
        )
        query.group_by(SQL("aml.company_id"), commercial_partner_sql)
        rows = query.execute()
        return [
            {
                'company_id': row['company_id'],
                'partner_id': row['partner_id'],
                'total_amount': float(row.get('total_amount') or 0.0),
                'oldest_due_date': row.get('oldest_due_date'),
                'max_days_overdue': int(row.get('max_days_overdue') or 0),
            }
            for row in rows
        ]

    @api.model
    def _refresh_overdue_amounts(self, cases):
        """Refresh overdue amounts on a recordset of cases without going
        through the full auto creator (no creates, no scope by company).
        """
        if not cases:
            return
        today = fields.Date.context_today(self)
        partner_ids = list({c.partner_id.id for c in cases})
        company_ids = list({c.company_id.id for c in cases})
        rows = self._fetch_overdue_aggregation(company_ids, today)
        by_key = {
            (row['company_id'], row['partner_id']): row for row in rows
        }
        for case in cases:
            row = by_key.get((case.company_id.id, case.partner_id.id))
            if not row:
                case.write({
                    'total_overdue_amount': 0.0,
                    'oldest_overdue_date': False,
                    'days_overdue_max': 0,
                })
                continue
            case.write({
                'total_overdue_amount': round(row['total_amount'], 2),
                'oldest_overdue_date': row['oldest_due_date'],
                'days_overdue_max': row['max_days_overdue'],
            })

    def _eh_open_invoices_for_report(self):
        """Return open AR invoices for this case's partner.

        Used by the case statement QWeb template; centralised here so
        the search domain stays in Python code instead of being inlined
        into a t-set expression. Sudo-promoted because the report
        renderer runs as the printing user, who may not have read access
        to invoices on a different team's partner; the case-level ACL
        already gates which cases can be printed.
        """
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id
        if not partner:
            return self.env['account.move']
        return self.env['account.move'].sudo().search(
            [
                ('partner_id', '=', partner.id),
                ('move_type', 'in', ('out_invoice', 'out_refund')),
                ('state', '=', 'posted'),
                ('payment_state', 'in', ('not_paid', 'partial', 'in_payment')),
                ('company_id', '=', self.company_id.id),
            ],
            order='invoice_date_due asc, name asc',
        )
