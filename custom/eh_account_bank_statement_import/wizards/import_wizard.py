# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank statement import wizard.

User uploads a file, picks the format and (for CSV) the per-bank
profile, picks the target bank journal, clicks Import. The wizard:

1. Hashes the file content for the idempotent reimport guard.
2. Dispatches to the parser for the chosen format.
3. Creates an account.bank.statement record with the parsed lines.
4. Logs the result on eh.account.bank.statement.import.log so the audit
   trail records every attempt regardless of outcome.

Errors at parse time are caught and recorded on the log row. Lines that
match an already-imported unique_import_ref are skipped; the user sees
the skipped count in the wizard's success message.
"""

import base64
import hashlib
import logging

from odoo import _, api, fields, models  # noqa: F401
from odoo.exceptions import UserError

from odoo.addons.eh_account_bank_statement_import.parsers import registry

_logger = logging.getLogger(__name__)


def _format_choices(model):
    # Odoo's Selection-callable contract passes the model recordset
    # as the single positional argument; the parser registry has no
    # use for it but we accept it to match the signature.
    return registry.format_choices()


class EhBankStatementImportWizard(models.TransientModel):
    _name = 'eh.account.bank.statement.import.wizard'
    _description = "Bank statement import wizard"

    journal_id = fields.Many2one(
        'account.journal', required=True,
        domain="[('type', 'in', ['bank', 'cash'])]",
    )
    profile_id = fields.Many2one(
        'eh.account.bank.statement.import.profile',
    )
    format_key = fields.Selection(
        selection=_format_choices, required=True, default='csv',
        string="File format",
    )
    file_data = fields.Binary(required=True, attachment=False)
    filename = fields.Char()

    statement_name = fields.Char(
        help="Optional. Defaults to the filename minus extension.",
    )

    def action_import(self):
        self.ensure_one()
        if not self.file_data:
            raise UserError(_("Pick a file to import."))
        if self.format_key == 'csv' and not self.profile_id:
            raise UserError(_(
                "CSV import requires a profile that maps the columns. "
                "Configure one under Configuration > Bank Statement "
                "Import Profiles, then try again.",
            ))

        content_bytes = base64.b64decode(self.file_data)
        file_hash = hashlib.sha256(content_bytes).hexdigest()

        # The import log is an append-only audit sink: ordinary accounting
        # users (group_eh_user) have read-only ACL on it, but the importer is
        # granted to them and writes a log on EVERY path (duplicate, error,
        # empty, success). Creating it in the user's own rights therefore
        # raised AccessError the instant a non-manager clicked Import, making
        # the feature unusable for the group it targets. Write the log via
        # sudo so recording an audit row never requires write rights on it
        # (and users still cannot tamper with existing rows).
        Log = self.env['eh.account.bank.statement.import.log'].sudo()
        existing_log = Log.search([
            ('file_hash', '=', file_hash),
            ('journal_id', '=', self.journal_id.id),
            ('state', '=', 'done'),
        ], limit=1)
        if existing_log:
            Log.create({
                'journal_id': self.journal_id.id,
                'profile_id': self.profile_id.id,
                'format_key': self.format_key,
                'filename': self.filename,
                'file_hash': file_hash,
                'state': 'duplicate',
                'statement_id': existing_log.statement_id.id,
            })
            return self._goto_log()

        try:
            parser = registry.get_parser(self.format_key)
            parsed = parser.parse(
                content_bytes,
                profile=self.profile_id if self.profile_id else None,
            )
        except Exception as exc:
            Log.create({
                'journal_id': self.journal_id.id,
                'profile_id': self.profile_id.id,
                'format_key': self.format_key,
                'filename': self.filename,
                'file_hash': file_hash,
                'state': 'error',
                'error_message': str(exc),
            })
            # Flush the log row before raising so the audit trail is
            # visible even when the caller catches the exception.
            self.env.flush_all()
            raise UserError(
                _("Import failed: %s") % str(exc),
            ) from exc

        try:
            # Run the create inside a savepoint so a database-level error
            # (e.g. a CHECK constraint) rolls back cleanly to here rather
            # than poisoning the whole transaction; otherwise the error
            # Log.create below would itself fail with InFailedSqlTransaction.
            with self.env.cr.savepoint():
                statement, line_count, skipped = self._materialise(parsed)
        except Exception as exc:
            Log.create({
                'journal_id': self.journal_id.id,
                'profile_id': self.profile_id.id,
                'format_key': self.format_key,
                'filename': self.filename,
                'file_hash': file_hash,
                'state': 'error',
                'error_message': str(exc),
            })
            self.env.flush_all()
            raise UserError(
                _("Import failed while creating the statement: %s")
                % str(exc),
            ) from exc

        # Every line was already imported (or the file had no lines): there
        # is nothing to create. A bank statement with no lines is invalid in
        # Odoo 18+ (the chk_bank_statement_valid constraint requires at least
        # one line), so we record the outcome and surface a notice instead of
        # creating an empty, illegal statement.
        if not statement:
            Log.create({
                'journal_id': self.journal_id.id,
                'profile_id': self.profile_id.id,
                'format_key': self.format_key,
                'filename': self.filename,
                'file_hash': file_hash,
                'state': 'duplicate' if skipped else 'done',
                'statement_id': False,
                'line_count': 0,
                'skipped_count': skipped,
            })
            message = (
                _("All %s line(s) in this file were already imported; no "
                  "statement was created.") % skipped
                if skipped else
                _("The file contained no statement lines to import.")
            )
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Nothing to import"),
                    'message': message,
                    'type': 'warning',
                    'sticky': False,
                    'next': self._goto_log(),
                },
            }

        Log.create({
            'journal_id': self.journal_id.id,
            'profile_id': self.profile_id.id,
            'format_key': self.format_key,
            'filename': self.filename,
            'file_hash': file_hash,
            'state': 'done',
            'statement_id': statement.id,
            'line_count': line_count,
            'skipped_count': skipped,
        })
        return self._goto_statement(statement)

    def _validate_currency(self, parsed):
        """Guard against importing a file into a journal of the wrong
        currency.

        Every parser populates currency_code when the source declares one
        (CSV via the profile, OFX/CAMT/MT940 from the file). A bank
        journal's effective currency is its own currency_id, or the
        company currency when the journal leaves currency_id blank. If the
        file states a currency that is neither, importing would book the
        lines at face value in the wrong currency and silently corrupt the
        balance, so we stop with a precise error instead.
        """
        code = (parsed.get('currency_code') or '').strip().upper()
        if not code:
            return
        journal_currency = (
            self.journal_id.currency_id
            or self.journal_id.company_id.currency_id
        )
        if not journal_currency or not journal_currency.name:
            return
        if journal_currency.name.upper() == code:
            return
        raise UserError(_(
            "The statement file is in %(file_currency)s but the selected "
            "journal '%(journal)s' is in %(journal_currency)s. Choose a "
            "%(file_currency)s journal, or import a %(journal_currency)s "
            "file.",
            file_currency=code,
            journal=self.journal_id.display_name,
            journal_currency=journal_currency.name,
        ))

    def _materialise(self, parsed):
        """Create account.bank.statement and lines from the parser dict.

        Returns (statement, line_count, skipped_count).
        """
        self._validate_currency(parsed)
        StatementLine = self.env['account.bank.statement.line']
        Statement = self.env['account.bank.statement']

        # Reimport guard at the line level: skip lines whose
        # unique_import_ref already exists on the journal. Stored refs are
        # prefixed with the journal id ("J<id>:<sha1>") so the single-column
        # unique constraint stays journal-scoped on every Odoo series
        # (journal_id has no SQL column on the line in 16/17).
        ref_field = 'unique_import_ref' if 'unique_import_ref' in StatementLine._fields else None
        ref_prefix = 'J%s:' % self.journal_id.id
        existing_refs = set()
        if ref_field:
            refs = [
                ref_prefix + line['unique_import_ref']
                for line in parsed.get('lines', [])
                if line.get('unique_import_ref')
            ]
            if refs:
                existing = StatementLine.search([
                    (ref_field, 'in', refs),
                ])
                existing_refs = set(existing.mapped(ref_field))

        statement_vals = {
            'journal_id': self.journal_id.id,
            'name': self.statement_name or self.filename or _("Imported statement"),
            'date': parsed.get('statement_date') or fields.Date.context_today(self),
        }
        if parsed.get('opening_balance') is not None:
            statement_vals['balance_start'] = parsed['opening_balance']

        # Pre-resolve partner names in one pass so a 5,000-line CSV
        # does not fire 5,000 res.partner lookups. Real statements
        # repeat the same payer dozens of times; we cache the lookup
        # per unique name.
        unique_partner_names = {
            line['partner_name']
            for line in parsed.get('lines', [])
            if line.get('partner_name')
        }
        partner_id_by_name = {}
        if unique_partner_names:
            Partner = self.env['res.partner']
            company_id = self.journal_id.company_id.id  # noqa: F841
            for name in unique_partner_names:
                partner = Partner.search([
                    ('name', '=ilike', name),
                    '|', ('company_id', '=', False),
                ], limit=1)
                if partner:
                    partner_id_by_name[name] = partner.id

        skipped = 0
        line_vals_list = []
        for line in parsed.get('lines', []):
            unique_ref = line.get('unique_import_ref')
            if unique_ref:
                unique_ref = ref_prefix + unique_ref
            if unique_ref and unique_ref in existing_refs:
                skipped += 1
                continue
            line_vals = {
                'date': line['date'],
                'amount': line['amount'],
                'payment_ref': (line.get('payment_ref') or '/')[:255],
                'narration': line.get('narration') or '',
                'journal_id': self.journal_id.id,
            }
            if ref_field and unique_ref:
                line_vals[ref_field] = unique_ref
            partner_name = line.get('partner_name')
            if partner_name and partner_name in partner_id_by_name:
                line_vals['partner_id'] = partner_id_by_name[partner_name]
            line_vals_list.append(line_vals)

        # No new lines (all skipped as duplicates, or an empty file): do not
        # create a statement. An empty statement violates the core
        # chk_bank_statement_valid constraint in Odoo 18+. The caller treats
        # an empty recordset as "nothing imported".
        if not line_vals_list:
            return Statement, 0, skipped

        # End balance integrity on a partial-overlap re-import.
        #
        # The parsed closing_balance describes the file as a whole, i.e. the
        # balance after ALL of the file's lines. When some lines are skipped
        # as already-imported duplicates, stamping that full closing balance
        # onto balance_end_real would leave a statement whose end balance no
        # longer matches the lines it actually retained
        # (balance_start + sum(retained amounts) != closing_balance), i.e. an
        # inconsistent statement that reports a phantom reconciliation gap.
        #
        # Only trust the file's closing_balance when nothing was dropped.
        # When duplicates were skipped, derive the end balance from the
        # opening balance plus the amounts of the lines actually imported so
        # the statement stays internally consistent. If we have no opening
        # balance to anchor to, leave balance_end_real unset (Odoo defaults
        # it to the computed running balance) rather than stamping a figure
        # we know is wrong.
        closing_balance = parsed.get('closing_balance')
        if closing_balance is not None:
            if not skipped:
                statement_vals['balance_end_real'] = closing_balance
            elif parsed.get('opening_balance') is not None:
                currency = (
                    self.journal_id.currency_id
                    or self.journal_id.company_id.currency_id
                )
                retained_total = sum(
                    vals['amount'] for vals in line_vals_list
                )
                consistent_end = parsed['opening_balance'] + retained_total
                statement_vals['balance_end_real'] = (
                    currency.round(consistent_end)
                    if currency else consistent_end
                )

        statement_vals['line_ids'] = [(0, 0, vals) for vals in line_vals_list]
        statement = Statement.create(statement_vals)

        # Fuzzy duplicate scan. The unique_import_ref check above
        # catches re-imports of the exact same file. This second pass
        # catches the more interesting case: the same payment imported
        # from two different sources (CSV today, OFX tomorrow) with
        # different per-line references. Each new line is compared
        # against the journal's prior lines; matches are recorded
        # via eh_duplicate_of_id without deleting either side, so the
        # operator decides whether to drop the duplicate or keep it.
        flagged = 0
        for new_line in statement.line_ids:
            existing = StatementLine._eh_find_probable_duplicate(
                journal_id=self.journal_id.id,
                line_date=new_line.date,
                amount=new_line.amount,
                payment_ref=new_line.payment_ref,
                narration=new_line.narration,
                exclude_id=new_line.id,
            )
            if existing:
                new_line.eh_duplicate_of_id = existing[:1].id
                flagged += 1
        if flagged:
            statement.message_post(body=_(
                "Heritage import detected %(count)s probable duplicate "
                "line(s) against earlier transactions on this journal. "
                "Review the lines flagged with 'Duplicate of' before "
                "reconciling.",
                count=flagged,
            ))
        return statement, len(line_vals_list), skipped

    def _goto_log(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Import Log"),
            'res_model': 'eh.account.bank.statement.import.log',
            'view_mode': 'list,form',
            'domain': [('journal_id', '=', self.journal_id.id)],
        }

    def _goto_statement(self, statement):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Imported Statement"),
            'res_model': 'account.bank.statement',
            'res_id': statement.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }
