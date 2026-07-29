# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Extend eh.batch.payment with a SEPA Direct Debit export action.

Inbound batches collect from customer accounts; the export consumes
each payment's matching mandate (advancing the FRST -> RCUR counter
atomically), assembles the PAIN.008 input dict grouped by sequence
type, and renders one file per sequence type.

Why one file per sequence type rather than one big mixed file: SEPA
banks process the file as a batch and some refuse mixed-sequence
files. Splitting per sequence type is the safe path; the export action
generates and stores them as separate eh.sepa.dd.export rows so the
audit trail records exactly what was submitted.
"""

import base64
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.eh_account_sepa_dd.tools import pain_008
from odoo.addons.eh_account_sepa_ct.tools.sepa_charset import (
    sanitize_sepa_text,
)
from odoo.addons.eh_account_sepa_ct.tools.iban_validator import (
    validate_iban, IbanValidationError,
)
from odoo.addons.eh_account_sepa_ct.tools.bic_validator import (
    validate_bic, BicValidationError,
)
from odoo.addons.eh_account_batch_payment.models.batch_payment import (
    POSTED_STATES,
)

_logger = logging.getLogger(__name__)


class EhBatchPayment(models.Model):
    _inherit = 'eh.batch.payment'

    sepa_dd_export_ids = fields.One2many(
        'eh.sepa.dd.export', 'batch_id', copy=False,
    )
    sepa_dd_export_count = fields.Integer(
        compute='_compute_sepa_dd_export_count',
    )

    @api.depends('sepa_dd_export_ids', 'sepa_dd_export_ids.state')
    def _compute_sepa_dd_export_count(self):
        for batch in self:
            batch.sepa_dd_export_count = len(
                batch.sepa_dd_export_ids.filtered(
                    lambda e: e.state in ('generated', 'downloaded'),
                ),
            )

    def action_export_sepa_dd(self):
        """Generate one PAIN.008 file per sequence type in this batch.

        Strict prerequisites:
        * Inbound batch (DD collects from debtors).
        * Posted (we do not export pre-authorisation).
        * Creditor configured for the journal.
        * Every payment has a partner with a non-revoked mandate on
          file for this creditor.

        The action consumes each mandate via consume_for_collection,
        which advances the FRST -> RCUR counter atomically.
        """
        self.ensure_one()
        # Idempotency guard. Generating the PAIN.008 consumes every
        # mandate in the batch (each consume_for_collection advances the
        # mandate's FRST -> RCUR sequence counter and its last collection
        # date) and cuts the collection file the bank debits against
        # debtor accounts. Re-running the export on a batch that was
        # already exported would consume every mandate a SECOND time --
        # double-advancing the sequence counter (a real first collection
        # would be re-rendered as RCUR) and producing a duplicate file
        # that, if submitted, debits each debtor twice. Take a row lock on
        # the batch so a concurrent second Export click serialises and
        # re-reads the committed export rows, then refuse when a live
        # (generated/downloaded) export already exists. A legitimate
        # re-cut goes through an explicit void of the prior export (which
        # flips it to superseded), never a silent re-run.
        self._eh_lock_for_dd_export()
        prior_exports = self.sepa_dd_export_ids.filtered(
            lambda e: e.state in ('generated', 'downloaded'),
        )
        if prior_exports:
            raise UserError(_(
                "This batch already has a live SEPA Direct Debit export "
                "(%(refs)s). Re-exporting would consume every mandate "
                "again and cut a duplicate collection file, so the bank "
                "could debit each debtor twice. Void the existing export "
                "first if you genuinely need to regenerate.",
                refs=', '.join(prior_exports.mapped('message_id')),
            ))
        # Bank-file segregation of duties (maker/checker). Generating the
        # PAIN.008 is a money-moving act: it is the collection instruction
        # the bank executes against debtor accounts. Require the manager
        # group, and require that the exporter is a different user from the
        # people who assembled the batch (confirmer / poster). Without this
        # gate the same user who builds the batch can also cut the bank
        # file, defeating the four-eyes control.
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can generate the SEPA "
                "Direct Debit bank file.",
            ))
        if self.confirmed_by_id and self.confirmed_by_id == self.env.user:
            raise UserError(_(
                "Segregation of duties: the manager who confirmed this "
                "batch cannot also generate the SEPA bank file. A "
                "different EH Accounting Manager must export it.",
            ))
        if self.posted_by_id and self.posted_by_id == self.env.user:
            raise UserError(_(
                "Segregation of duties: the manager who posted this "
                "batch cannot also generate the SEPA bank file. A "
                "different EH Accounting Manager must export it.",
            ))
        if self.batch_type != 'inbound':
            raise UserError(_(
                "SEPA Direct Debit is for inbound batches only.",
            ))
        if self.state != 'posted':
            raise UserError(_(
                "Export the SEPA file only after the batch is posted.",
            ))
        creditor = self.env['eh.sepa.creditor'].search(
            [('journal_id', '=', self.journal_id.id)], limit=1,
        )
        if not creditor:
            raise UserError(_(
                "Journal %s has no SEPA creditor configured. Add one "
                "under Configuration > SEPA Creditors.",
                self.journal_id.display_name,
            ))

        # Pre-notification window. The SEPA Direct Debit rulebook
        # requires the creditor to notify each debtor ahead of the
        # collection date (14 days for CORE/FRST, often 1-2 days for
        # B2B / RCUR variants depending on the bank). The creditor
        # config carries the policy as `pre_notification_days`. We
        # refuse the export when payment_date is closer than that
        # window so the batch never lands at the bank with the
        # debtor still unwarned. The eh_sepa_dd_force_now context
        # key bypasses the check for emergency/test batches; it is
        # intentionally undocumented in the UI to avoid normalising
        # bypass.
        notice_days = max(int(creditor.pre_notification_days or 0), 0)
        if notice_days and not self.env.context.get('eh_sepa_dd_force_now'):
            today = fields.Date.context_today(self)
            min_collection_date = today + timedelta(days=notice_days)
            if not self.payment_date or self.payment_date < min_collection_date:
                raise UserError(_(
                    "Collection date %(date)s violates the %(days)s-day "
                    "pre-notification window required by SEPA. Push the "
                    "batch's payment date out to %(min)s or later, or "
                    "shorten pre_notification_days on the creditor "
                    "config if your bank permits.",
                    date=fields.Date.to_string(self.payment_date) if self.payment_date else _("(unset)"),
                    days=notice_days,
                    min=fields.Date.to_string(min_collection_date),
                ))

        # Single-currency enforcement. The PAIN.008 message-level
        # control sum is computed in one currency; mixing currencies
        # within a single file is invalid per ISO 20022 and rejected
        # by every European bank we know of. Reject at submission
        # time so the user gets a clean message rather than a bank
        # rejection later.
        currencies = self.payment_ids.filtered(
            lambda p: p.state in POSTED_STATES,
        ).mapped('currency_id')
        if len(currencies) > 1:
            raise UserError(_(
                "Batch contains payments in %(count)s different "
                "currencies (%(names)s). Split the batch by currency "
                "before exporting; SEPA DD requires one currency "
                "per file.",
                count=len(currencies),
                names=', '.join(currencies.mapped('name')),
            ))
        # Euro enforcement. SEPA Direct Debit is a euro-denominated
        # scheme and the generated pain.008 hard-codes EUR at the amount
        # level. Refuse a non-euro batch here rather than emit a file
        # that mislabels the collected amounts as EUR.
        if currencies and currencies.name != 'EUR':
            raise UserError(_(
                "SEPA Direct Debit files are euro-denominated, but this "
                "batch is in %(name)s. Collect in EUR or use a non-SEPA "
                "collection method.",
                name=currencies.name,
            ))

        # Group payments by the sequence type their mandate will yield.
        groups = defaultdict(list)
        consume_log = []  # (mandate, sequence_type) tuples; flush only on success
        for payment in self.payment_ids.filtered(
            lambda p: p.state in POSTED_STATES,
        ):
            mandate = self._eh_resolve_mandate(payment, creditor)
            seq_type = mandate.consume_for_collection(self.payment_date)
            # The mandate's own scheme variant (CORE vs B2B) is authoritative
            # and was captured at signing. CORE and B2B mandates must never
            # share a PmtInf block (banks reject mixed-scheme blocks), so the
            # local instrument is part of the grouping key, not the creditor
            # default.
            local_instrument = (
                mandate.local_instrument
                or creditor.default_local_instrument
                or 'CORE'
            )
            consume_log.append((mandate, seq_type))
            groups[(seq_type, local_instrument)].append((payment, mandate))

        if not groups:
            raise UserError(_(
                "Batch contains no posted payments to collect.",
            ))

        exports = self.env['eh.sepa.dd.export']
        for (seq_type, local_instrument), items in groups.items():
            payload = self._eh_build_pain_008_payload(
                creditor=creditor, seq_type=seq_type, items=items,
                local_instrument=local_instrument,
            )
            xml_bytes = pain_008.render(payload)
            attachment = self._eh_persist_dd_attachment(
                payload, xml_bytes, seq_type,
            )
            export = exports.create({
                'batch_id': self.id,
                'message_id': payload['message_id'],
                'transaction_count': sum(
                    len(p['transactions']) for p in payload['payments']
                ),
                'control_sum': float(sum(
                    Decimal(str(tx['amount']))
                    for p in payload['payments']
                    for tx in p['transactions']
                )),
                'file_hash': self.env['eh.sepa.dd.export'].compute_hash(
                    xml_bytes,
                ),
                'sequence_type': seq_type,
                'attachment_id': attachment.id,
            })
            exports |= export
            self.message_post(body=_(
                "SEPA DD %(seq)s file generated. Message ID: %(id)s. "
                "Transactions: %(count)d. Control sum: %(sum).2f.",
                seq=seq_type,
                id=export.message_id,
                count=export.transaction_count,
                sum=export.control_sum,
            ))

        # If only one file, return its download URL directly. Multi-file
        # exports navigate to the export list so the user downloads each
        # in turn.
        if len(exports) == 1:
            return exports.action_download()
        return {
            'type': 'ir.actions.act_window',
            'name': _("SEPA DD exports for %s") % self.name,
            'res_model': 'eh.sepa.dd.export',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('batch_id', '=', self.id)],
        }

    def _eh_lock_for_dd_export(self):
        """Row-lock this batch so a concurrent second Export click
        serialises and re-reads the committed export rows rather than a
        stale pre-export snapshot.

        Closes the double-consume/double-file race in which two
        transactions both read zero prior exports, both consume every
        mandate (advancing each sequence counter twice) and both cut a
        collection file. Mirrors eh_account_fx_revaluation's
        _eh_lock_for_post.
        """
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_batch_payment WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        self.invalidate_recordset()

    def _eh_resolve_mandate(self, payment, creditor):
        """Pick the mandate that authorises this payment.

        Rules, in order:
        * Most recent active mandate for this creditor and partner.
        * If multiple mandates match, choose the one that matches the
          payment's partner_bank_id IBAN; falls back to the first
          active mandate.
        """
        partner = payment.partner_id
        Mandate = self.env['eh.sepa.mandate']
        candidates = Mandate.search(
            [
                ('creditor_id', '=', creditor.id),
                ('partner_id', '=', partner.id),
                ('state', '=', 'active'),
            ],
            order='signature_date desc, id desc',
        )
        if not candidates:
            raise UserError(_(
                "Payment %(name)s: partner %(partner)s has no active "
                "SEPA mandate for creditor %(creditor)s. Issue a "
                "mandate before collecting.",
                name=payment.display_name,
                partner=partner.display_name,
                creditor=creditor.name,
            ))
        if payment.partner_bank_id:
            iban = payment.partner_bank_id.acc_number
            for mandate in candidates:
                try:
                    if validate_iban(mandate.debtor_iban) == validate_iban(iban):
                        return mandate
                except IbanValidationError:
                    continue
        return candidates[0]

    def _eh_build_pain_008_payload(self, creditor, seq_type, items,
                                   local_instrument=None):
        """Compose the PAIN.008 payload dict for one (sequence type,
        local instrument) group. Every transaction in `items` shares both
        the sequence type and the scheme variant, so the resulting PmtInf
        is homogeneous as the SEPA rulebook requires."""
        self.ensure_one()
        try:
            cdtr_iban = validate_iban(creditor.iban)
        except IbanValidationError as exc:
            raise UserError(_(
                "Creditor IBAN failed validation: %s",
            ) % str(exc))
        cdtr_bic = None
        if creditor.bic:
            try:
                cdtr_bic = validate_bic(creditor.bic)
            except BicValidationError as exc:
                raise UserError(_(
                    "Creditor BIC failed validation: %s",
                ) % str(exc))

        transactions = []
        for payment, mandate in items:
            try:
                dbtr_iban = validate_iban(mandate.debtor_iban)
            except IbanValidationError as exc:
                raise UserError(_(
                    "Mandate %(mid)s: debtor IBAN invalid (%(err)s).",
                    mid=mandate.mandate_id, err=str(exc),
                ))
            dbtr_bic = None
            if mandate.debtor_bic:
                try:
                    dbtr_bic = validate_bic(mandate.debtor_bic)
                except BicValidationError as exc:
                    raise UserError(_(
                        "Mandate %(mid)s: debtor BIC invalid (%(err)s).",
                        mid=mandate.mandate_id, err=str(exc),
                    ))
            mandate_block = {
                'id': mandate.mandate_id,
                'signature_date': mandate.signature_date,
            }
            # AmdmntInf: when the mandate carries an amendment trail, the
            # next collection must flag the change so the bank can
            # reconcile the amended mandate to its predecessor.
            amendment = mandate._eh_latest_amendment_for_rendering()
            if amendment:
                mandate_block['amendment'] = amendment
            transactions.append({
                'end_to_end_id': self._eh_clip(
                    payment.name or "PMT%05d" % payment.id, 35,
                ),
                'amount': Decimal(str(payment.amount)),
                'mandate': mandate_block,
                'debtor': {
                    'name': self._eh_clip(payment.partner_id.display_name, 70),
                    'iban': dbtr_iban,
                    'bic': dbtr_bic,
                },
                'remittance_info': self._eh_clip(
                    payment.ref or '', 140,
                ) or None,
            })

        return {
            'message_id': self._eh_sepa_dd_msg_id(seq_type),
            'creation_datetime': datetime.utcnow(),
            'initiating_party': {
                'name': self._eh_clip(creditor.creditor_name, 70),
                'identifier': self._eh_clip(creditor.creditor_identifier, 35),
            },
            'payments': [
                {
                    'payment_info_id': self._eh_clip(
                        "%s-%s-%s" % (
                            self.name.replace('/', '-'), seq_type,
                            local_instrument or creditor.default_local_instrument,
                        ),
                        35,
                    ),
                    'requested_collection_date': self.payment_date,
                    'sequence_type': seq_type,
                    'local_instrument': (
                        local_instrument or creditor.default_local_instrument
                    ),
                    'creditor': {
                        'name': self._eh_clip(creditor.creditor_name, 70),
                        'iban': cdtr_iban,
                        'bic': cdtr_bic,
                        'identifier': creditor.creditor_identifier,
                    },
                    'transactions': transactions,
                },
            ],
        }

    @staticmethod
    def _eh_clip(value, max_length):
        if not value:
            return value
        return sanitize_sepa_text(value)[:max_length]

    def _eh_sepa_dd_msg_id(self, seq_type):
        """Globally unique SEPA MsgId (max 35 chars) for one sequence-type
        file: a short sanitised batch name, the sequence type, a UTC-clock
        timestamp, and a uuid fragment so two exports of the same sequence
        type in the same second never collide.
        """
        self.ensure_one()
        name_part = sanitize_sepa_text(
            (self.name or 'EH').replace('/', '-'),
        )[:6]
        ts = fields.Datetime.now().strftime('%Y%m%d%H%M%S')
        return (
            "%s-%s-%s-%s" % (name_part, seq_type, ts, uuid.uuid4().hex[:6])
        )[:35]

    def _eh_persist_dd_attachment(self, payload, xml_bytes, seq_type):
        self.ensure_one()
        filename = "%s_%s_%s.xml" % (
            self.name.replace('/', '_'),
            seq_type,
            fields.Datetime.now().strftime('%Y%m%d%H%M%S'),
        )
        return self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(xml_bytes),
            'mimetype': 'application/xml',
            'res_model': self._name,
            'res_id': self.id,
        })
