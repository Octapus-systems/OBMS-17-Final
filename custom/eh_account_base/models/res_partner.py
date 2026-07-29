# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Compatibility shim: ensure required upstream fields on res.partner pick
up their declared defaults when the partner is auto-created via cascade
(e.g. res.users / res.company creation paths). Some Enterprise modules
override the low-level _create such that field defaults declared on
inherited modules are not always applied to cascade-created partners,
producing NOT NULL violations on columns the upstream module marked
required.

Known offenders:
* group_rfq  (purchase_stock) - default 'default'
* group_on   (purchase_stock) - default 'default'

The Python-level setdefault is helpful for direct calls; the
table-level DEFAULT is the belt-and-braces guarantee for paths that
go through Enterprise overrides which strip Python-side vals before
the INSERT.

Both layers are no-ops when the offending column is not in the
registry / table (i.e. when purchase_stock is not installed).
"""

from psycopg2 import sql

from odoo import api, models


# Required upstream fields that ship with `default='default'` and that
# Enterprise's _create override sometimes strips. New entries can be
# added here as the matrix grows; the shim handles each generically.
_REQUIRED_PARTNER_DEFAULTS = (
    ('group_rfq', 'default'),
    ('group_on', 'default'),
)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.model_create_multi
    def create(self, vals_list):
        for column, default in _REQUIRED_PARTNER_DEFAULTS:
            if column in self._fields:
                for vals in vals_list:
                    vals.setdefault(column, default)
        return super().create(vals_list)

    def _auto_init(self):
        result = super()._auto_init()
        # Belt-and-braces: if the offending column exists, set a
        # PostgreSQL column DEFAULT so a missing value at INSERT time
        # falls back to the declared default instead of violating
        # NOT NULL. The probe avoids touching the column when the
        # source module is absent; the ALTER is idempotent.
        for column, default in _REQUIRED_PARTNER_DEFAULTS:
            self.env.cr.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'res_partner' AND column_name = %s
                """,
                [column],
            )
            if self.env.cr.fetchone():
                # Identifier interpolation goes through psycopg2.sql so
                # the column name is properly quoted regardless of how
                # the upstream module names it.
                self.env.cr.execute(
                    sql.SQL(
                        "ALTER TABLE res_partner "
                        "ALTER COLUMN {col} SET DEFAULT %s"
                    ).format(col=sql.Identifier(column)),
                    [default],
                )
        return result
