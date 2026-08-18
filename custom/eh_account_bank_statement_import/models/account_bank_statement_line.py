# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Extension on `account.bank.statement.line` to record the live-connector
provider reference. Used as the de-duplication key on every fetch run.
"""

from datetime import timedelta

from odoo import api, fields, models
from odoo.tools.mail import html2plaintext


# Window (each side, in days) used by the fuzzy duplicate detector.
# Three days catches typical bank settlement lag between two import
# sources (e.g., a same-day OFX feed and a next-day CSV statement)
# without picking up legitimate recurring same-amount payments a week
# apart.
_DUPLICATE_DATE_WINDOW_DAYS = 3


class AccountBankStatementLine(models.Model):
    _inherit = 'account.bank.statement.line'

    eh_provider_reference = fields.Char(
        index=True, copy=False,
        help=(
            "Identifier supplied by the bank's live API for this "
            "transaction. The live-connector framework rejects any "
            "transaction whose reference has been imported before for "
            "the same journal."
        ),
    )

    unique_import_ref = fields.Char(
        index=True, copy=False,
        help=(
            "Stable per-line idempotency key built from the row content "
            "(date, amount, reference, narration) plus an occurrence "
            "counter that distinguishes genuinely identical rows in the "
            "same file. It does not depend on the row's position, so "
            "re-importing the same statement, or one with rows added or "
            "removed elsewhere, does not duplicate transactions on the "
            "same journal."
        ),
    )

    eh_duplicate_of_id = fields.Many2one(
        'account.bank.statement.line',
        index=True,
        copy=False,
        ondelete='set null',
        help=(
            "When the import wizard's fuzzy duplicate scan flags this "
            "line as a probable duplicate of an earlier line, the "
            "match is recorded here. The line is NOT auto-deleted: "
            "the operator confirms, because two payments with the "
            "same date and amount can be legitimate."
        ),
    )

    # Single-column uniqueness: the wizard prefixes the ref with the
    # journal id ("J<id>:<sha1>") so scoping stays per-journal while the
    # constraint works on every series (journal_id has no SQL column on
    # the statement line in Odoo 16/17, so a two-column unique constraint
    # silently fails to install there).
    _sql_constraints = [
        ('eh_unique_import_ref', 'unique(unique_import_ref)', 'A bank statement line with this import reference already exists for the journal.'),  # noqa: E501
    ]

    @api.model
    def _eh_find_probable_duplicate(self, journal_id, line_date, amount,
                                    payment_ref=None, narration=None,
                                    exclude_id=None):
        """Locate the most likely existing duplicate for an inbound line.

        Match criteria, applied in order:

        * Same journal.
        * Date within `_DUPLICATE_DATE_WINDOW_DAYS` either side; banks
          and import formats sometimes shift the value date by a day or
          two between feeds (CSV books on settlement day, OFX on value
          date).
        * Same amount to the cent. We do NOT relax this; an amount
          mismatch is the strongest signal of two distinct payments,
          and tolerance here would surface false positives the operator
          ignores.
        * If both rows have a non-empty payment_ref or narration, at
          least one must overlap (case-insensitive substring). Empty
          strings on either side don't disqualify; many CSV exports
          ship blank narration.

        Returns the matching line record (singleton) or empty recordset.
        """
        if not (journal_id and line_date and amount is not None):
            return self.browse()
        if not isinstance(line_date, (str, type(None))):
            min_date = line_date - timedelta(days=_DUPLICATE_DATE_WINDOW_DAYS)
            max_date = line_date + timedelta(days=_DUPLICATE_DATE_WINDOW_DAYS)
        else:
            return self.browse()
        rounded = round(float(amount), 2)
        domain = [
            ('journal_id', '=', journal_id),
            ('date', '>=', min_date),
            ('date', '<=', max_date),
            ('amount', '>=', rounded - 0.005),
            ('amount', '<=', rounded + 0.005),
        ]
        if exclude_id:
            domain.append(('id', '!=', exclude_id))
        # Order by id only: in Odoo 19 the line's `date` is a non-
        # stored related field on move_id and cannot be used in the
        # SQL ORDER BY. id ascending preserves a deterministic match
        # order (oldest line wins ties) without needing the related
        # column.
        candidates = self.search(domain, order='id asc')
        if not candidates:
            return self.browse()

        # `/` is a common placeholder for an empty payment_ref both
        # in Odoo's stock auto-generated lines and in some bank
        # imports; treat it as empty for the purposes of fuzzy
        # matching.
        def _normalise(value):
            value = value or ''
            # narration is delegated from account.move where it is a
            # fields.Html, so a stored candidate carries markup (e.g.
            # '<p>FOO</p>') that would never match a freshly imported
            # plain-text narration. Strip tags before comparing.
            if '<' in value and '>' in value:
                value = html2plaintext(value)
            stripped = value.strip().lower()
            if stripped in ('/', '-', '.', ''):
                return ''
            return stripped

        narration_lc = _normalise(narration)
        ref_lc = _normalise(payment_ref)
        for cand in candidates:
            cand_ref_lc = _normalise(cand.payment_ref)
            cand_narration_lc = _normalise(cand.narration)
            # If neither side carries a reference or narration, the
            # date+amount alone is enough.
            if not (ref_lc or narration_lc) and not (
                cand_ref_lc or cand_narration_lc
            ):
                return cand
            if ref_lc and cand_ref_lc:
                if ref_lc == cand_ref_lc or ref_lc in cand_ref_lc \
                        or cand_ref_lc in ref_lc:
                    return cand
            if narration_lc and cand_narration_lc:
                if narration_lc == cand_narration_lc \
                        or narration_lc in cand_narration_lc \
                        or cand_narration_lc in narration_lc:
                    return cand
        return self.browse()
