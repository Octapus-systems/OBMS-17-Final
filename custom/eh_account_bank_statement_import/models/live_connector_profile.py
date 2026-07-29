# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.bank.live.connector.profile -- Odoo-side persistence for one live
bank connector configuration.

A profile binds:

* a connector key (which connector implementation to invoke)
* a journal (where the imported lines land)
* a credentials_json blob (whatever the connector needs to authenticate)
* a since_date pointer (the framework only fetches forward from here,
  preventing duplicate imports across cron runs)

The framework does not expose credentials_json on any UI surface that
escapes back to the server; only the dedicated form view shows it, and
even there it is rendered behind a password widget. For real-world
deployments deal with secrets through the Odoo passwords manager or a
dedicated vault; this field exists so a small deployment can run
without that infrastructure.
"""

import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from odoo.addons.eh_account_bank_statement_import.connectors.registry import (
    connector_choices, get_connector,
)
from odoo.addons.eh_account_bank_statement_import.connectors.base import (
    ConnectorError,
)

_logger = logging.getLogger(__name__)


class EhBankLiveConnectorProfile(models.Model):
    _name = 'eh.bank.live.connector.profile'
    _description = "Live bank connector profile"
    _order = 'name'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.cron.batch.mixin']

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True, tracking=True)
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    journal_id = fields.Many2one(
        'account.journal',
        required=True,
        domain="[('type', 'in', ('bank', 'cash'))]",
        ondelete='restrict',
        tracking=True,
    )
    connector_key = fields.Selection(
        selection='_selection_connector_keys',
        required=True,
        tracking=True,
    )
    credentials_json = fields.Text(
        help=(
            "Connector-specific credentials as JSON. The framework hands "
            "this verbatim to the connector's authenticate() method. "
            "Treated as a secret on every UI surface."
        ),
    )

    # ---- friendly credential entry ----
    # These labelled fields are the human-friendly face of credentials_json:
    # the operator fills in the named boxes for their connector and the
    # framework assembles the JSON. credentials_json stays the single source
    # of truth (and remains available to power users under Advanced).
    cred_client_id = fields.Char(
        "Client ID", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False)
    cred_secret = fields.Char(
        "Secret", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False)
    cred_access_token = fields.Char(
        "Access Token", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False)
    cred_account_id = fields.Char(
        "Account ID", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False)
    cred_environment = fields.Selection(
        [('sandbox', "Sandbox"), ('development', "Development"),
         ('production', "Production")],
        string="Environment", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False)
    cred_api_key = fields.Char(
        "API Key", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False)
    cred_user_id = fields.Char(
        "User ID", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False)
    cred_secret_id = fields.Char(
        "Secret ID", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False)
    cred_secret_key = fields.Char(
        "Secret Key", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False)

    _CRED_FIELD_MAP = {
        'cred_client_id': 'client_id',
        'cred_secret': 'secret',
        'cred_access_token': 'access_token',
        'cred_account_id': 'account_id',
        'cred_environment': 'environment',
        'cred_api_key': 'api_key',
        'cred_user_id': 'user_id',
        'cred_secret_id': 'secret_id',
        'cred_secret_key': 'secret_key',
    }

    @api.depends('credentials_json')
    def _compute_credential_fields(self):
        import json
        for rec in self:
            data = {}
            if rec.credentials_json:
                try:
                    data = json.loads(rec.credentials_json)
                except ValueError:
                    data = {}
            for field, key in rec._CRED_FIELD_MAP.items():
                rec[field] = data.get(key) or False

    def _inverse_credential_fields(self):
        import json
        for rec in self:
            data = {}
            # Keep any keys already in the JSON that we don't surface as
            # fields, so a hand-edited blob is never silently truncated.
            if rec.credentials_json:
                try:
                    data = json.loads(rec.credentials_json)
                except ValueError:
                    data = {}
            for field, key in rec._CRED_FIELD_MAP.items():
                value = rec[field]
                if value:
                    data[key] = value
                elif key in data:
                    data.pop(key)
            rec.credentials_json = json.dumps(data) if data else False
    since_date = fields.Date(
        help=(
            "Earliest date the framework will request from the bank on "
            "the next run. Auto-advances to the latest fetched "
            "posted_date after each successful run."
        ),
    )
    last_run_at = fields.Datetime(readonly=True, tracking=True)
    last_run_state = fields.Selection(
        [('idle', "Idle"), ('ok', "Success"), ('error', "Error")],
        default='idle', readonly=True, tracking=True,
    )
    last_run_message = fields.Text(readonly=True)
    transactions_imported_count = fields.Integer(
        readonly=True,
        help="Cumulative count across all runs of this profile.",
    )

    _sql_constraints = [
        ('unique_journal', 'unique(journal_id)', 'A journal can only be bound to one live connector profile.'),
    ]

    @api.model
    def _selection_connector_keys(self):
        choices = connector_choices()
        if choices:
            return choices
        # No connectors registered: still allow the field to exist so
        # the form view does not crash. The user gets an explicit error
        # at run time naming the missing addon.
        return [('__none__', "(no connectors registered)")]

    @api.constrains('credentials_json')
    def _check_credentials_json(self):
        import json
        for rec in self:
            if not rec.credentials_json:
                continue
            try:
                json.loads(rec.credentials_json)
            except ValueError as exc:
                raise ValidationError(_(
                    "credentials_json must be valid JSON: %s",
                ) % exc)

    def action_fetch_now(self):
        """Run the connector once, immediately, with chatter feedback."""
        for rec in self:
            rec._run_fetch()
        return True

    @api.model
    def cron_fetch_due_profiles(self):
        """Run every active profile in turn, with one savepoint per
        profile so a single bank failure does not freeze the queue.
        """
        profiles = self.search([
            ('active', '=', True),
            ('connector_key', '!=', '__none__'),
        ])
        # Per-profile savepoint via the shared batch mixin so one bad
        # bank does not freeze the queue: its failure rolls back only
        # that profile and the run continues. Previously the bare
        # savepoint still let an unexpected error propagate and abort
        # the whole cron.
        self._eh_for_each_savepoint(
            profiles,
            lambda profile: profile._run_fetch(),
            log_label="Live connector fetch",
        )
        return True

    def _run_fetch(self):
        self.ensure_one()
        import json
        try:
            connector = get_connector(self.connector_key)
        except KeyError as exc:
            self._record_error(str(exc))
            return
        creds = {}
        if self.credentials_json:
            try:
                creds = json.loads(self.credentials_json)
            except ValueError as exc:
                self._record_error("credentials_json invalid: %s" % exc)
                return

        # Build a passthrough struct so connectors do not import the ORM.
        profile_view = type('LiveProfileView', (), {
            'connector_key': self.connector_key,
            'company_id': self.company_id.id,
            'journal_id': self.journal_id.id,
            'currency_code': self.journal_id.currency_id.name
                or self.company_id.currency_id.name,
            'credentials': creds,
        })()

        today = fields.Date.context_today(self)
        since = self.since_date or (today - timedelta(days=30))
        try:
            session = connector.authenticate(profile_view)
            transactions = connector.fetch_transactions(
                profile_view, since, today, session,
            )
            count, latest_date = self._materialise(list(transactions))
            connector.disconnect(profile_view, session)
        except ConnectorError as exc:
            self._record_error(str(exc))
            return
        except Exception as exc:  # pragma: no cover -- unexpected
            _logger.exception("live connector %s crashed", self.id)
            self._record_error("unexpected error: %s" % exc)
            return

        self.write({
            'last_run_at': fields.Datetime.now(),
            'last_run_state': 'ok',
            'last_run_message': _(
                "Imported %(count)s transactions; latest %(latest)s.",
                count=count, latest=latest_date or 'n/a',
            ),
            'transactions_imported_count': (
                self.transactions_imported_count + count
            ),
            'since_date': latest_date or since,
        })
        self.message_post(body=self.last_run_message)

    def _materialise(self, transactions):
        """Convert connector BankTransaction records into bank statement
        lines on the bound journal. De-duplicates by provider_reference;
        a transaction whose reference has been seen before is skipped.

        Returns (imported_count, latest_posted_date).
        """
        if not transactions:
            return 0, False

        # Validate everything up front so we do not partially insert
        # then bail. Each invalid line surfaces as a ConnectorError so
        # the caller records the problem and stops the run.
        for tx in transactions:
            tx.validate()

        StatementLine = self.env['account.bank.statement.line']
        # search_read returns a list of dicts; build the set from the
        # values, never wrap the dict list in set() (TypeError:
        # unhashable type: 'dict').
        existing_rows = StatementLine.search_read(
            [('journal_id', '=', self.journal_id.id),
             ('eh_provider_reference', '!=', False)],
            ['eh_provider_reference'],
        )
        seen = {r['eh_provider_reference'] for r in existing_rows}

        latest = None
        new_vals = []
        for tx in transactions:
            if tx.provider_reference in seen:
                continue
            seen.add(tx.provider_reference)
            new_vals.append({
                'journal_id': self.journal_id.id,
                'date': tx.posted_date,
                'amount': tx.amount,
                'payment_ref': (tx.description or tx.counterparty_name
                                or tx.provider_reference),
                'partner_name': tx.counterparty_name or False,
                'eh_provider_reference': tx.provider_reference,
            })
            if latest is None or tx.posted_date > latest:
                latest = tx.posted_date

        if new_vals:
            StatementLine.create(new_vals)
        return len(new_vals), latest

    def _record_error(self, message):
        self.write({
            'last_run_at': fields.Datetime.now(),
            'last_run_state': 'error',
            'last_run_message': message,
        })
        self.message_post(body=_("Live fetch failed: %s") % message)
