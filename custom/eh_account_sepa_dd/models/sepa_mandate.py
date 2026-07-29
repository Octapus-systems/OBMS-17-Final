# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sepa.mandate: a debtor's authorisation to collect via SEPA DD.

Mandate lifecycle per the SEPA Direct Debit Scheme Rulebook:

    draft -> active -> completed
                    \\-> revoked
                    \\-> expired (36 months without a collection)

Sequence type lifecycle within an active mandate:

    FRST (first collection)
      -> RCUR (subsequent recurring collections)
        -> RCUR
          -> ...
          -> FNAL (last collection; mandate flips to completed)

OOFF (one-off) is a separate mandate flavour: a single collection
authorised by the debtor, marked completed immediately on use.

The model exposes consume_for_collection(amount) which advances the
sequence counter atomically via SQL UPDATE so two concurrent collection
attempts cannot race the FRST -> RCUR transition. The 36-month expiry
rule is enforced at consume time: a mandate that has not been used for
36 calendar months is flipped to expired before consume can succeed.
"""

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import SQL

from odoo.addons.eh_account_sepa_ct.tools.iban_validator import (
    validate_iban, IbanValidationError,
)
from odoo.addons.eh_account_sepa_ct.tools.bic_validator import (
    validate_bic, BicValidationError,
)


_SEPA_MANDATE_EXPIRY_MONTHS = 36


class EhSepaMandate(models.Model):
    _name = 'eh.sepa.mandate'
    _description = "SEPA Direct Debit mandate"
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.cron.batch.mixin',
        'eh.workflow.guard',
    ]
    _order = 'state, signature_date desc, id desc'
    _rec_name = 'mandate_id'

    # State moves only through the sanctioned actions / consume path (each of
    # which runs under sudo). A plain user cannot RPC-write the mandate
    # straight to active/completed to skip activation and the collection
    # counter.
    _eh_guarded_fields = ('state',)

    # ---- identity ----
    mandate_id = fields.Char(
        required=True, copy=False, index=True, tracking=True,
        help=(
            "Unique mandate reference issued to the debtor. Capped at "
            "35 characters per the scheme. Once the mandate is active "
            "this id is immutable; changing it would invalidate every "
            "future collection."
        ),
    )
    creditor_id = fields.Many2one(
        'eh.sepa.creditor', required=True,
        ondelete='restrict', tracking=True,
    )
    company_id = fields.Many2one(
        related='creditor_id.company_id', store=True, readonly=True,
        index=True,
    )
    partner_id = fields.Many2one(
        'res.partner', required=True,
        ondelete='restrict', tracking=True,
        help="The debtor: the party authorising the collection.",
    )

    # ---- bank ----
    debtor_iban = fields.Char(
        string="Debtor IBAN", required=True, tracking=True,
        help=(
            "Account the collections debit. Validated locally on save. "
            "Changing this on an active mandate counts as a mandate "
            "amendment per the scheme; the engine tracks amendment "
            "history but does not block the change."
        ),
    )
    debtor_bic = fields.Char(string="Debtor BIC / SWIFT")

    # ---- scheme variant ----
    local_instrument = fields.Selection(
        [
            ('CORE', "CORE (consumer)"),
            ('B2B', "B2B (business-to-business)"),
            ('COR1', "COR1 (one-day legacy)"),
        ],
        required=True, tracking=True,
        default=lambda self: self.env['eh.sepa.creditor'].search(
            [], limit=1,
        ).default_local_instrument or 'CORE',
    )

    # ---- lifecycle ----
    signature_date = fields.Date(
        required=True, tracking=True,
        help=(
            "Date the debtor signed the mandate. Goes into "
            "MndtRltdInf/DtOfSgntr on every collection that uses this "
            "mandate."
        ),
    )
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('active', "Active"),
            ('completed', "Completed"),
            ('revoked', "Revoked"),
            ('expired', "Expired"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )
    is_one_off = fields.Boolean(
        default=False, tracking=True,
        help=(
            "When True the mandate authorises a single collection only. "
            "First (and last) use renders as OOFF and the mandate "
            "flips to completed immediately."
        ),
    )

    # ---- collection counter ----
    next_sequence_type = fields.Selection(
        [
            ('FRST', "First (FRST)"),
            ('RCUR', "Recurring (RCUR)"),
            ('FNAL', "Final (FNAL)"),
            ('OOFF', "One-off (OOFF)"),
        ],
        compute='_compute_next_sequence_type',
        help=(
            "Sequence type the next collection will use given the "
            "mandate's state, is_one_off flag, and collection_count."
        ),
    )
    collection_count = fields.Integer(
        readonly=True, copy=False, default=0,
        help="How many collections have been rendered against this mandate.",
    )
    last_collection_date = fields.Date(readonly=True, copy=False)
    expires_on = fields.Date(
        compute='_compute_expires_on',
        help=(
            "Date this mandate auto-expires given the SEPA scheme's "
            "36-month dormancy rule. Recomputed from "
            "last_collection_date + 36 months. When state moves out "
            "of active for any other reason, this date stops mattering."
        ),
    )

    # ---- amendments ----
    amendment_ids = fields.One2many(
        'eh.sepa.mandate.amendment', 'mandate_id', readonly=True,
    )

    notes = fields.Text()

    _sql_constraints = [
        ('unique_mandate_id', 'unique(mandate_id, company_id)', 'Mandate id must be unique per company.'),
    ]

    # ---- onchange (live form feedback) ----

    @api.onchange('partner_id')
    def _onchange_partner_id_iban(self):
        """Pre-fill the debtor IBAN/BIC from the partner's primary
        bank account when a partner is picked.

        Without this, an operator creating a mandate has to look up the
        IBAN separately and paste it. With the pre-fill, picking the
        partner is enough; they only edit if the partner has multiple
        banks and the wrong one was first.
        """
        for rec in self:
            if not rec.partner_id:
                continue
            bank = rec.partner_id.bank_ids[:1]
            if bank:
                if not rec.debtor_iban:
                    rec.debtor_iban = bank.acc_number
                if not rec.debtor_bic and bank.bank_bic:
                    rec.debtor_bic = bank.bank_bic

    @api.onchange('creditor_id')
    def _onchange_creditor_id_local_instrument(self):
        """Adopt the creditor's default scheme variant when picked.

        A creditor configured for B2B should not have CORE mandates
        accidentally created against it. The default copy at create
        time only fires for the FIRST creditor; this onchange covers
        the case where the user changes creditor on an in-progress
        mandate form.
        """
        for rec in self:
            if not rec.creditor_id:
                continue
            if rec.creditor_id.default_local_instrument:
                rec.local_instrument = rec.creditor_id.default_local_instrument

    @api.constrains('mandate_id')
    def _check_mandate_id_length(self):
        for rec in self:
            if rec.mandate_id and len(rec.mandate_id) > 35:
                raise ValidationError(_(
                    "Mandate id is capped at 35 characters per the "
                    "SEPA scheme; got %d.",
                ) % len(rec.mandate_id))

    @api.constrains('debtor_iban')
    def _check_iban(self):
        for rec in self:
            try:
                validate_iban(rec.debtor_iban)
            except IbanValidationError as exc:
                raise ValidationError(_(
                    "Debtor IBAN failed validation: %s",
                ) % str(exc))

    @api.constrains('debtor_bic')
    def _check_bic(self):
        for rec in self:
            if not rec.debtor_bic:
                continue
            try:
                validate_bic(rec.debtor_bic)
            except BicValidationError as exc:
                raise ValidationError(_(
                    "Debtor BIC failed validation: %s",
                ) % str(exc))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('debtor_iban'):
                try:
                    vals['debtor_iban'] = validate_iban(vals['debtor_iban'])
                except IbanValidationError as exc:
                    raise ValidationError(_(
                        "Debtor IBAN failed validation: %s",
                    ) % str(exc))
            if vals.get('debtor_bic'):
                try:
                    vals['debtor_bic'] = validate_bic(vals['debtor_bic'])
                except BicValidationError as exc:
                    raise ValidationError(_(
                        "Debtor BIC failed validation: %s",
                    ) % str(exc))
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('debtor_iban'):
            try:
                vals['debtor_iban'] = validate_iban(vals['debtor_iban'])
            except IbanValidationError as exc:
                raise ValidationError(_(
                    "Debtor IBAN failed validation: %s",
                ) % str(exc))
        if vals.get('debtor_bic'):
            try:
                vals['debtor_bic'] = validate_bic(vals['debtor_bic'])
            except BicValidationError as exc:
                raise ValidationError(_(
                    "Debtor BIC failed validation: %s",
                ) % str(exc))
        # Capture pre-write values on active mandates so an amendment
        # trail exists for any IBAN / debtor-name / scheme change. The
        # scheme requires the next collection after such a change to
        # carry AmdmntInf; without a recorded amendment that history
        # could never be reconstructed.
        amend_specs = []
        for mandate in self:
            if mandate.state != 'active':
                continue
            amend_specs.extend(mandate._eh_prepare_amendments(vals))
        res = super().write(vals)
        if amend_specs:
            self.env['eh.sepa.mandate.amendment'].create(amend_specs)
        return res

    def _eh_prepare_amendments(self, vals):
        """Return amendment create-dicts for the changes in `vals`.

        Called before super().write() so old_value reflects the value
        on record prior to the change. Only IBAN, debtor name (via
        partner_id) and scheme (local_instrument) count as scheme
        amendments; a no-op change (new value equal to old) is skipped.
        """
        self.ensure_one()
        specs = []
        if 'debtor_iban' in vals and vals['debtor_iban'] != self.debtor_iban:
            specs.append({
                'mandate_id': self.id,
                'amendment_type': 'iban',
                'old_value': self.debtor_iban,
                'new_value': vals['debtor_iban'],
            })
        if 'debtor_bic' in vals and vals['debtor_bic'] != self.debtor_bic:
            specs.append({
                'mandate_id': self.id,
                'amendment_type': 'agent',
                'old_value': self.debtor_bic,
                'new_value': vals['debtor_bic'],
            })
        if 'partner_id' in vals and vals['partner_id'] != self.partner_id.id:
            new_partner = self.env['res.partner'].browse(vals['partner_id'])
            specs.append({
                'mandate_id': self.id,
                'amendment_type': 'name',
                'old_value': self.partner_id.display_name,
                'new_value': new_partner.display_name,
            })
        if ('local_instrument' in vals
                and vals['local_instrument'] != self.local_instrument):
            specs.append({
                'mandate_id': self.id,
                'amendment_type': 'scheme',
                'old_value': self.local_instrument,
                'new_value': vals['local_instrument'],
            })
        return specs

    def _eh_latest_amendment_for_rendering(self):
        """Return the AmdmntInf dict for the most recent amendment, or
        None if this mandate has never been amended.

        Maps our amendment rows onto the pain_008 amendment keys:
        an IBAN change becomes OrgnlDbtrAcct (original IBAN); a debtor
        bank / agent change (BIC) becomes OrgnlDbtrAgt with the SMNDA
        marker (same mandate, new debtor agent); a scheme or debtor-name
        change is signalled with the original mandate id so the bank can
        match the amended mandate to its predecessor.
        """
        self.ensure_one()
        amendment = self.amendment_ids[:1]
        if not amendment:
            return None
        details = {}
        if amendment.amendment_type == 'agent':
            # SMNDA: the debtor moved bank. Per the scheme the next
            # collection carries OrgnlDbtrAgt/Othr/Id=SMNDA so the new
            # debtor bank knows to honour the existing mandate.
            details['same_mandate_new_debtor_agent'] = True
        elif amendment.amendment_type == 'iban' and amendment.old_value:
            details['original_iban'] = amendment.old_value
        else:
            details['original_mandate_id'] = self.mandate_id
        return details

    @api.depends('state', 'is_one_off', 'collection_count')
    def _compute_next_sequence_type(self):
        for mandate in self:
            if mandate.state != 'active':
                mandate.next_sequence_type = False
                continue
            if mandate.is_one_off:
                mandate.next_sequence_type = 'OOFF'
                continue
            mandate.next_sequence_type = (
                'FRST' if mandate.collection_count == 0 else 'RCUR'
            )

    @api.depends('last_collection_date', 'state')
    def _compute_expires_on(self):
        for mandate in self:
            if mandate.state != 'active' or not mandate.last_collection_date:
                mandate.expires_on = False
                continue
            from dateutil.relativedelta import relativedelta
            mandate.expires_on = (
                mandate.last_collection_date
                + relativedelta(months=_SEPA_MANDATE_EXPIRY_MONTHS)
            )

    # ---- transitions ----

    def action_activate(self):
        self = self._eh_workflow_action()
        for mandate in self:
            if mandate.state != 'draft':
                raise UserError(_(
                    "Only draft mandates can be activated.",
                ))
            mandate.state = 'active'

    def action_revoke(self):
        self = self._eh_workflow_action()
        for mandate in self:
            if mandate.state in ('completed', 'revoked'):
                continue
            mandate.state = 'revoked'

    def action_set_to_draft(self):
        self = self._eh_workflow_action()
        for mandate in self:
            if mandate.state not in ('revoked', 'expired'):
                raise UserError(_(
                    "Only revoked or expired mandates can return to "
                    "draft.",
                ))
            mandate.write({
                'state': 'draft',
                'collection_count': 0,
                'last_collection_date': False,
            })

    # ---- consume ----

    def consume_for_collection(self, collection_date, mark_final=False):
        """Reserve the next sequence type and advance the counter.

        Returns the sequence type to use (FRST / RCUR / FNAL / OOFF).
        Atomic via SQL UPDATE so two concurrent collection runs cannot
        race the FRST -> RCUR transition.

        :param collection_date: date.date the collection will hit.
        :param mark_final: when True, render this collection as FNAL
            and flip the mandate to completed after.
        """
        self.ensure_one()
        # Server-initiated state advance (state -> completed on FNAL/OOFF).
        # Run under sudo so the guard passes; the real env.user is preserved
        # for the message_post audit stamp.
        self = self._eh_workflow_action()
        if self.state != 'active':
            raise UserError(_(
                "Mandate %s is %s; only active mandates can be "
                "consumed.",
                self.mandate_id, self.state,
            ))
        if self._eh_is_expired_at(collection_date):
            # SQL UPDATE so the expired flip persists even when the
            # UserError below triggers an outer rollback. Flush_all
            # before the UPDATE so any pending ORM writes don't race
            # the raw query, then invalidate the cache so callers re-
            # read 'expired' from the DB after catching the UserError.
            self.env.flush_all()
            self.env.cr.execute(
                "UPDATE eh_sepa_mandate SET state='expired' WHERE id=%s",
                [self.id],
            )
            self.invalidate_recordset(['state'])
            raise UserError(_(
                "Mandate %s has been dormant for more than %d months "
                "and is now expired. Issue a new mandate before "
                "collecting again.",
                self.mandate_id, _SEPA_MANDATE_EXPIRY_MONTHS,
            ))

        # Atomic compare-and-set: increment the counter and decide the
        # sequence type from the post-update value returned by the
        # database. Two concurrent runs both see RETURNING and one will
        # get 1 (FRST), the other 2 (RCUR). Pre-update reads cannot
        # race the assignment.
        self.flush_recordset(['collection_count', 'last_collection_date'])
        self.env.cr.execute(SQL(
            "UPDATE eh_sepa_mandate "
            "SET collection_count = collection_count + 1, "
            "    last_collection_date = %s "
            "WHERE id = %s "
            "RETURNING collection_count",
            collection_date, self.id,
        ))
        row = self.env.cr.fetchone()
        if not row:
            raise UserError(_(
                "Mandate %s vanished mid-collection.", self.mandate_id,
            ))
        new_count = int(row[0])
        if self.is_one_off:
            seq_type = 'OOFF'
            mark_final = True
        elif mark_final:
            seq_type = 'FNAL'
        elif new_count == 1:
            seq_type = 'FRST'
        else:
            seq_type = 'RCUR'
        self.invalidate_recordset(
            ['collection_count', 'last_collection_date'],
        )
        if mark_final:
            self.state = 'completed'
        self.message_post(body=_(
            "Mandate consumed for collection on %(date)s as %(seq)s.",
            date=collection_date, seq=seq_type,
        ))
        return seq_type

    def _eh_is_expired_at(self, collection_date):
        self.ensure_one()
        if not self.last_collection_date:
            return False
        from dateutil.relativedelta import relativedelta
        cutoff = self.last_collection_date + relativedelta(
            months=_SEPA_MANDATE_EXPIRY_MONTHS,
        )
        return collection_date > cutoff

    # ---- cron: dormancy expiry ----

    @api.model
    def _cron_expire_dormant(self, batch_size=500):
        """Flip dormant mandates to expired so they cannot be used.

        A live cron pass per day enforces the SEPA 36-month rule; a
        mandate that has not been used in 36 months cannot be consumed
        until a fresh mandate is signed. The check is also done at
        consume time, but the cron makes the lifecycle visible in the
        list view without waiting for a collection attempt.
        """
        from dateutil.relativedelta import relativedelta
        cutoff = fields.Date.context_today(self) - relativedelta(
            months=_SEPA_MANDATE_EXPIRY_MONTHS,
        )
        candidates = self.search(
            [
                ('state', '=', 'active'),
                ('last_collection_date', '!=', False),
                ('last_collection_date', '<', cutoff),
            ],
            limit=batch_size,
        )
        def _expire(mandate):
            mandate = mandate._eh_workflow_action()
            mandate.state = 'expired'
            mandate.message_post(body=_(
                "Auto-expired after %d months of dormancy.",
                _SEPA_MANDATE_EXPIRY_MONTHS,
            ))

        self._eh_for_each_savepoint(
            candidates, _expire, log_label="Mandate dormancy expiry",
        )


class EhSepaMandateAmendment(models.Model):
    """Amendment trail per SEPA scheme.

    The scheme requires that every collection following an amendment
    (IBAN change, name change, or scheme change) carries the AmdmntInf
    block in MndtRltdInf. eh.sepa.mandate.write() records a row here
    whenever one of those fields changes on an active mandate, and the
    batch export renders the latest amendment into AmdmntInfDtls.
    """
    _name = 'eh.sepa.mandate.amendment'
    _description = "SEPA mandate amendment record"
    _order = 'amended_at desc'

    mandate_id = fields.Many2one(
        'eh.sepa.mandate', required=True, ondelete='cascade',
        index=True,
    )
    amended_at = fields.Datetime(
        required=True, default=fields.Datetime.now,
    )
    amended_by_id = fields.Many2one(
        'res.users', required=True,
        default=lambda self: self.env.user,
    )
    amendment_type = fields.Selection(
        [
            ('iban', "Debtor IBAN changed"),
            ('agent', "Debtor bank / agent changed"),
            ('name', "Debtor name changed"),
            ('scheme', "Scheme variant changed"),
            ('other', "Other"),
        ],
        required=True, default='other',
    )
    old_value = fields.Char()
    new_value = fields.Char()
    notes = fields.Text()
