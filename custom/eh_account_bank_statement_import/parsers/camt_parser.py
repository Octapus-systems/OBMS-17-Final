# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
CAMT.053 (ISO 20022) statement parser.

CAMT.053 is the standard XML format EU banks export for end-of-day
statements. The structure has nested namespaces and bank-specific
extensions; this parser implements the common subset that handles the
overwhelming majority of EU bank exports. For edge cases, customers can
inherit and override the methods that need adjusting.

References:

* ISO 20022 BankToCustomerStatementV02 (and later) schemas.
* Iso 20022 message reference manual at iso20022.org.

The implementation here uses lxml for XML parsing because Odoo already
ships with it.
"""

import datetime
import hashlib

from .base import StatementParser, StatementParserError


_CAMT_NS = {
    'urn:iso:std:iso:20022:tech:xsd:camt.053.001.02',
    'urn:iso:std:iso:20022:tech:xsd:camt.053.001.06',
    'urn:iso:std:iso:20022:tech:xsd:camt.053.001.08',
}


class Camt053StatementParser(StatementParser):
    FORMAT_KEY = 'camt053'
    FORMAT_LABEL = "CAMT.053 (ISO 20022)"

    def parse(self, content_bytes, profile=None):
        try:
            from lxml import etree
        except ImportError as exc:
            raise StatementParserError(
                "CAMT.053 import requires lxml, which Odoo bundles by "
                "default. If you see this error, your Python environment "
                "is missing it: pip install lxml"
            ) from exc

        # Uploaded statement files are untrusted external input. Parse
        # through an explicitly hardened parser so an internal-subset
        # SYSTEM entity cannot read local files (XXE) or fetch over the
        # network (SSRF), and so entity amplification cannot exhaust the
        # worker. This does not rely on version-dependent libxml2 defaults,
        # closing the gap across the whole 16-19 support matrix.
        secure_parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            dtd_validation=False,
            huge_tree=False,
        )
        try:
            root = etree.fromstring(content_bytes, secure_parser)
        except etree.XMLSyntaxError as exc:
            raise StatementParserError(
                "CAMT.053 file could not be parsed as XML: %s" % exc,
            ) from exc

        # Defence in depth: reject any DOCTYPE/entity declaration outright,
        # even though resolve_entities=False already neutralises expansion.
        roottree = getattr(root, 'getroottree', lambda: None)()
        docinfo = getattr(roottree, 'docinfo', None)
        if docinfo is not None and docinfo.doctype:
            raise StatementParserError(
                "CAMT.053 file with a DOCTYPE declaration is rejected "
                "(XXE guard).",
            )

        ns = self._detect_namespace(root)
        nsmap = {'ns': ns} if ns else {}

        stmt = root.find('.//{%s}Stmt' % ns) if ns else root.find('.//Stmt')
        if stmt is None:
            raise StatementParserError(
                "CAMT.053 file contains no <Stmt> section.",
            )

        currency = self._first_text(stmt, ns, './ns:Acct/ns:Ccy')
        statement_date = self._parse_iso_date(
            self._first_text(stmt, ns, './ns:CreDtTm')
            or self._first_text(stmt, ns, './ns:FrToDt/ns:ToDtTm'),
        )
        opening_balance, closing_balance = self._extract_balances(
            stmt, ns,
        )

        lines = []
        for ntry in self._findall(stmt, ns, './ns:Ntry'):
            booking_date_text = (
                self._first_text(ntry, ns, './ns:BookgDt/ns:Dt')
                or self._first_text(ntry, ns, './ns:BookgDt/ns:DtTm')
                or self._first_text(ntry, ns, './ns:ValDt/ns:Dt')
            )
            try:
                line_date = self._parse_iso_date(booking_date_text)
            except ValueError as exc:
                raise StatementParserError(
                    "CAMT.053 entry has invalid booking date %r: %s"
                    % (booking_date_text, exc),
                )
            amount_text = self._first_text(ntry, ns, './ns:Amt')
            cdt_dbt = self._first_text(ntry, ns, './ns:CdtDbtInd')
            try:
                amount = float(amount_text or '0')
            except ValueError as exc:
                raise StatementParserError(
                    "CAMT.053 entry has invalid amount %r: %s"
                    % (amount_text, exc),
                )
            if cdt_dbt == 'DBIT':
                amount = -amount
            payment_ref = (
                self._first_text(ntry, ns, './ns:NtryDtls/ns:TxDtls/ns:RmtInf/ns:Ustrd')
                or self._first_text(ntry, ns, './ns:NtryDtls/ns:TxDtls/ns:RmtInf/ns:Strd/ns:CdtrRefInf/ns:Ref')
                or self._first_text(ntry, ns, './ns:AcctSvcrRef')
                or ''
            )
            narration = self._first_text(
                ntry, ns,
                './ns:NtryDtls/ns:TxDtls/ns:RltdPties/ns:Cdtr/ns:Nm',
            ) or self._first_text(
                ntry, ns,
                './ns:NtryDtls/ns:TxDtls/ns:RltdPties/ns:Dbtr/ns:Nm',
            ) or ''
            unique_ref = (
                self._first_text(ntry, ns, './ns:NtryDtls/ns:TxDtls/ns:Refs/ns:AcctSvcrRef')
                or self._first_text(ntry, ns, './ns:AcctSvcrRef')
                or self._fingerprint(line_date, amount, payment_ref)
            )
            lines.append({
                'date': line_date,
                'amount': amount,
                'payment_ref': payment_ref.strip(),
                'narration': narration.strip(),
                'partner_name': narration.strip() or None,
                'unique_import_ref': unique_ref,
            })

        return {
            'statement_date': statement_date,
            'opening_balance': opening_balance,
            'closing_balance': closing_balance,
            'currency_code': currency,
            'lines': lines,
        }

    # ---- helpers ----

    @staticmethod
    def _detect_namespace(root):
        for ns in _CAMT_NS:
            if root.tag.startswith('{%s}' % ns) or any(
                child.tag.startswith('{%s}' % ns) for child in root
            ):
                return ns
        # Fallback: extract from root tag if it has any namespace.
        if root.tag.startswith('{'):
            return root.tag.split('}', 1)[0][1:]
        return ''

    @staticmethod
    def _findall(element, ns, xpath):
        if ns:
            return element.findall(xpath, namespaces={'ns': ns})
        return element.findall(xpath.replace('ns:', ''))

    @staticmethod
    def _first_text(element, ns, xpath):
        if element is None:
            return ''
        if ns:
            found = element.find(xpath, namespaces={'ns': ns})
        else:
            found = element.find(xpath.replace('ns:', ''))
        if found is None or found.text is None:
            return ''
        return found.text.strip()

    @staticmethod
    def _parse_iso_date(text):
        if not text:
            return None
        # Allow date or datetime with timezone.
        for fmt in ('%Y-%m-%d', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f'):
            try:
                return datetime.datetime.strptime(text[:len(fmt) + 4], fmt).date()
            except ValueError:
                continue
        if 'T' in text:
            return datetime.datetime.fromisoformat(
                text.replace('Z', '+00:00'),
            ).date()
        raise ValueError("unrecognised ISO date %r" % text)

    def _extract_balances(self, stmt, ns):
        """Pull opening and closing balances. CAMT codes are OPBD or
        ITBD for opening, CLBD for closing."""
        opening = None
        closing = None
        for bal in self._findall(stmt, ns, './ns:Bal'):
            code = self._first_text(bal, ns, './ns:Tp/ns:CdOrPrtry/ns:Cd')
            amount_text = self._first_text(bal, ns, './ns:Amt')
            cdt_dbt = self._first_text(bal, ns, './ns:CdtDbtInd')
            try:
                amount = float(amount_text or '0')
            except ValueError:
                continue
            if cdt_dbt == 'DBIT':
                amount = -amount
            if code in ('OPBD', 'ITBD') and opening is None:
                opening = amount
            elif code == 'CLBD':
                closing = amount
        return opening, closing

    @staticmethod
    def _fingerprint(date_value, amount, ref):
        h = hashlib.sha1()
        if date_value:
            h.update(date_value.isoformat().encode('utf-8'))
        h.update(b'|')
        h.update(("%.4f" % amount).encode('utf-8'))
        h.update(b'|')
        h.update((ref or '').encode('utf-8'))
        return h.hexdigest()
