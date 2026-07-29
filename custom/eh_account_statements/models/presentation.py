# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Shared presentation helpers for the IAS 1 primary statements.

Four concerns, each shared by the statement of comprehensive income and the
statement of changes in equity:

1. Structural OCI recycling (IAS 1.82A): two account tags, EH OCI Recyclable
   and EH OCI Non-Recyclable, classify the ledger OCI reserve accounts. SOCI
   lines that reference a tagged source account derive their reclassification
   section from the tag instead of a hand flag. A registry of the suite's own
   OCI account settings drives default tag assignment (CTA and cash-flow /
   net-investment hedge reserves recyclable; FVOCI-debt reserve recyclable;
   FVOCI-equity reserve, revaluation surplus and defined-benefit remeasurement
   reserve non-recyclable).

2. Current / non-current completeness (IAS 1.60): confirming a statement
   scans the posted ledger for accounts carrying balances whose account type
   falls outside the recognised current / non-current / equity / P&L sets.
   Such balances would silently escape a classified statement of financial
   position, so confirmation blocks until they are reclassified or a manager
   overrides with a recorded reason.

3. NCI linkage: when eh_account_consolidation is installed and a settled
   consolidation run covers the statement period, the run's non-controlling
   interest carve-out prefills the statement's NCI figure. The lookup is a
   soft registry probe, never a hard dependency; a manually keyed figure is
   kept and the discrepancy against the run is surfaced instead.

4. IAS 34 thin interim support: statements carry a period type (annual by
   default, preserving prior behaviour). An interim statement is labelled as
   such, can point at its two IAS 34.20 comparatives (the comparable interim
   period of the immediately preceding financial year, and the immediately
   preceding annual period), and can be flagged condensed per IAS 34.8, which
   collapses the presentation to the mandatory minimum line items (headings
   and subtotals). All of this is presentation only: no measurement changes.
"""

import logging

from odoo import _, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_is_zero

_logger = logging.getLogger(__name__)

# XML ids of the two structural OCI recycling tags shipped by this module.
OCI_TAG_RECYCLABLE = 'eh_account_statements.tag_eh_oci_recyclable'
OCI_TAG_NON_RECYCLABLE = 'eh_account_statements.tag_eh_oci_non_recyclable'

# ---------------------------------------------------------------------------
# IAS 1.60 recognised classification sets. An account whose type is in none
# of these sets cannot land in a classified statement of financial position
# (current / non-current), in equity or in profit or loss, so a posted
# balance on it is an IAS 1 completeness breach.
# ---------------------------------------------------------------------------
CURRENT_ACCOUNT_TYPES = frozenset({
    'asset_receivable', 'asset_cash', 'asset_current', 'asset_prepayments',
    'liability_payable', 'liability_credit_card', 'liability_current',
})
NON_CURRENT_ACCOUNT_TYPES = frozenset({
    'asset_non_current', 'asset_fixed', 'liability_non_current',
})
EQUITY_ACCOUNT_TYPES = frozenset({'equity', 'equity_unaffected'})
PL_ACCOUNT_TYPES = frozenset({
    'income', 'income_other',
    'expense', 'expense_other', 'expense_depreciation',
    'expense_direct_cost',
})
RECOGNISED_ACCOUNT_TYPES = (
    CURRENT_ACCOUNT_TYPES | NON_CURRENT_ACCOUNT_TYPES
    | EQUITY_ACCOUNT_TYPES | PL_ACCOUNT_TYPES
)

# IAS 34 period types. 'annual' is the default so every existing statement
# and every statement created without touching the field behaves exactly as
# before this feature existed.
PERIOD_TYPES = [
    ('annual', "Annual"),
    ('interim', "Interim (IAS 34)"),
]

# ---------------------------------------------------------------------------
# Default OCI recycling tag sources. Each entry names a model of another
# suite module (soft dependency: skipped when not installed), the field on it
# that holds a ledger OCI reserve account, the recycling verdict for that
# reserve, and an extra domain narrowing which records feed the verdict.
#
# Verdicts follow IAS 1.82A as applied by each source standard:
# - CTA / translation reserve: recyclable (IAS 21.48 reclassifies the reserve
#   to P&L on disposal of the foreign operation).
# - Cash flow hedge and net investment hedge reserves: recyclable
#   (IFRS 9.6.5.11(d) / IAS 21.48).
# - FVOCI-debt reserve: recyclable (IFRS 9.5.7.10).
# - FVOCI-equity election reserve: non-recyclable (IFRS 9.B5.7.1, transfers
#   stay within equity).
# - Defined benefit remeasurement reserve: non-recyclable (IAS 19.122).
# - Revaluation surplus: non-recyclable (IAS 16.41 / IAS 38.87, transfers to
#   retained earnings stay within equity). The asset module books the surplus
#   through a wizard-selected account rather than a stored setting, so the
#   surplus reserve is discovered by the same name heuristic the
#   consolidation module already uses for CTA / NCI accounts.
# ---------------------------------------------------------------------------
OCI_TAG_SOURCES = (
    # (model, account field, verdict, extra domain)
    ('eh.fx.cta.position', 'cta_account_id', 'recyclable', []),
    ('eh.consol.entity', 'cta_account_id', 'recyclable', []),
    ('eh.fx.hedge', 'oci_account_id', 'recyclable',
     [('hedge_type', 'in', ('cash_flow', 'net_investment'))]),
    ('eh.fair.value.item', 'oci_account_id', 'recyclable',
     ['|', ('ifrs9_classification', '=', 'fvoci_debt'),
      '&', ('ifrs9_classification', '=', False),
      ('fvoci_classification', '=', 'fvoci_debt')]),
    ('eh.fair.value.item', 'oci_account_id', 'non_recyclable',
     ['|', ('ifrs9_classification', '=', 'fvoci_equity'),
      '&', ('ifrs9_classification', '=', False),
      ('fvoci_classification', '=', 'fvoci_equity')]),
    ('eh.benefit.plan', 'oci_account_id', 'non_recyclable', []),
)


def _account_company_leaf(Account, company):
    """Company-scoping domain leaf for account.account across series.

    account.account became multi-company (company_ids, Many2many) in Odoo 18;
    before that it carries a single company_id. Resolve the field at runtime
    so the helper works across series.
    """
    if 'company_ids' in Account._fields:
        return ('company_ids', 'in', [company.id])
    return ('company_id', '=', company.id)


def oci_recycling_tags(env):
    """Return (recyclable_tag, non_recyclable_tag), either may be empty."""
    return (
        env.ref(OCI_TAG_RECYCLABLE, raise_if_not_found=False),
        env.ref(OCI_TAG_NON_RECYCLABLE, raise_if_not_found=False),
    )


def apply_default_oci_recycling_tags(env, company):
    """Scan the installed suite modules' OCI account settings and tag the
    reserve accounts that carry NO recycling tag yet.

    Default assignment only: an account already carrying either tag (whether
    from an earlier run or set by hand) is never re-tagged, so a deliberate
    manual classification is never fought. The per-line discrepancy flag on
    the SOCI surfaces any tag that disagrees with a preparer's judgement.

    Returns the number of accounts newly tagged.
    """
    rec_tag, non_tag = oci_recycling_tags(env)
    if not (rec_tag and non_tag):
        return 0
    both = rec_tag + non_tag
    # account -> verdict; the first source to claim an account wins so a
    # conflicting later source never flip-flops the assignment within one run.
    verdicts = {}
    for model_name, field_name, verdict, extra_domain in OCI_TAG_SOURCES:
        if model_name not in env.registry:
            continue
        Model = env[model_name].sudo()
        if field_name not in Model._fields:
            continue
        domain = list(extra_domain)
        if 'company_id' in Model._fields:
            domain.append(('company_id', '=', company.id))
        elif 'parent_company_id' in Model._fields:
            domain.append(('parent_company_id', '=', company.id))
        for account in Model.search(domain).mapped(field_name):
            verdicts.setdefault(account.id, verdict)
    # Revaluation surplus heuristic (see OCI_TAG_SOURCES comment): equity
    # accounts named for the revaluation reserve are non-recyclable.
    Account = env['account.account'].sudo()
    surplus_domain = [
        ('account_type', '=', 'equity'),
        ('name', 'ilike', 'revaluation'),
        _account_company_leaf(Account, company),
    ]
    for account in Account.search(surplus_domain):
        verdicts.setdefault(account.id, 'non_recyclable')

    applied = 0
    for account_id, verdict in verdicts.items():
        account = Account.browse(account_id)
        if account.tag_ids & both:
            continue  # already classified; never overwrite
        tag = rec_tag if verdict == 'recyclable' else non_tag
        account.write({'tag_ids': [(4, tag.id)]})
        applied += 1
    return applied


# ---------------------------------------------------------------------------
# IAS 1.60 completeness guard
# ---------------------------------------------------------------------------

def classification_misfit_pairs(statement):
    """Accounts with posted balances at the statement period end whose
    account type falls outside the recognised classification sets.

    Returns a list of (account, balance) pairs, balance in the ledger sign
    convention (debit-positive), zero balances dropped within currency
    rounding.
    """
    statement.ensure_one()
    if not (statement.company_id and statement.period_end):
        return []
    domain = [
        ('company_id', '=', statement.company_id.id),
        ('parent_state', '=', 'posted'),
        ('date', '<=', statement.period_end),
        ('account_id.account_type', 'not in',
         sorted(RECOGNISED_ACCOUNT_TYPES)),
    ]
    rounding = (statement.currency_id
                or statement.company_id.currency_id).rounding or 0.01
    # Public read_group with the classic signature: the tuple-returning
    # _read_group(aggregates=...) form only exists on Odoo 17+ and this
    # helper ships to every series.
    groups = statement.env['account.move.line'].read_group(
        domain, ['balance:sum'], ['account_id'])
    Account = statement.env['account.account']
    pairs = []
    for group in groups:
        if not group.get('account_id'):
            continue
        balance = group.get('balance') or 0.0
        if float_is_zero(balance, precision_rounding=rounding):
            continue
        pairs.append((Account.browse(group['account_id'][0]), balance))
    return pairs


def format_misfit_note(statement, pairs):
    """Human-readable listing of the IAS 1.60 misfit accounts."""
    lines = []
    for account, balance in pairs:
        account = account.with_company(statement.company_id)
        lines.append(_(
            "%(code)s %(name)s (type: %(atype)s, balance: %(balance).2f)",
            code=account.code or '?', name=account.name,
            atype=account.account_type, balance=balance))
    return '\n'.join(lines)


def check_classification_completeness(statement, statement_label):
    """IAS 1.60 completeness gate, called from action_confirm.

    Blocks confirmation while any posted balance sits on an account whose
    type is outside the recognised current / non-current / equity / P&L
    sets, unless the manager override flag is set with a reason; the
    override is then logged to the chatter and the server log.
    """
    statement.ensure_one()
    pairs = classification_misfit_pairs(statement)
    if not pairs:
        return
    listing = format_misfit_note(statement, pairs)
    if not statement.classification_override:
        raise UserError(_(
            "IAS 1.60 completeness: the posted ledger carries balances on "
            "accounts whose type is outside the recognised current / "
            "non-current classification sets, so the %(label)s cannot be "
            "confirmed as complete:\n%(listing)s\n\nReclassify these "
            "accounts, or set the classification override with a reason "
            "to confirm anyway.",
            label=statement_label, listing=listing))
    reason = (statement.classification_override_reason or '').strip()
    if not reason:
        raise UserError(_(
            "The IAS 1.60 classification override requires a reason. "
            "Record why the unclassified balances are acceptable before "
            "confirming."))
    statement.message_post(body=_(
        "IAS 1.60 classification completeness OVERRIDDEN on confirm by "
        "%(user)s. Reason: %(reason)s\nUnclassified balances:\n%(listing)s",
        user=statement.env.user.display_name, reason=reason,
        listing=listing))
    _logger.info(
        "IAS 1.60 classification override on %s %s by %s: %s",
        statement._name, statement.display_name,
        statement.env.user.login, reason)


# ---------------------------------------------------------------------------
# IAS 1.82A OCI recycling-tag completeness guard
#
# The reclassification section a component lands in (items that may be
# reclassified vs items that will not) must be DERIVED from the recycling tag
# on the source OCI account, not left to a hand flag. A component whose source
# account carries no recycling tag - or which names no source account at all -
# has no structural signal: its section rests entirely on the manual
# will_reclassify flag, which is exactly the honour-system placement IAS 1.82A
# is meant to remove. Such a component is a recycling-classification misfit;
# confirming a statement of comprehensive income is blocked while any misfit
# with a non-zero amount exists, unless a manager overrides with a reason.
# ---------------------------------------------------------------------------

def oci_recycling_misfit_lines(soci):
    """OCI lines whose reclassification section is not tag-derived.

    A line is a misfit when it carries a non-zero amount AND its source
    account gives no recycling verdict: either no source account is set, or
    the account carries neither EH OCI recycling tag. A zero-amount line does
    not affect either OCI subtotal, so it is not flagged (it cannot mis-state
    the sections). Returns an ``eh.soci.line`` recordset.
    """
    soci.ensure_one()
    rounding = (soci.currency_id or soci.company_id.currency_id).rounding \
        or 0.01
    misfits = soci.env['eh.soci.line']
    for line in soci.line_ids:
        if float_is_zero(line.amount, precision_rounding=rounding):
            continue
        if line._eh_tag_verdict() is None:
            misfits |= line
    return misfits


def format_oci_misfit_note(misfits):
    """Human-readable listing of the untagged OCI recycling lines."""
    parts = []
    for line in misfits:
        account = line.account_id
        source = (
            _("%(code)s %(name)s (untagged)",
              code=account.code or '?', name=account.name)
            if account else _("no source account"))
        # The interpolation key must not be 'source': the translation
        # alias _() takes its own positional 'source' argument (the string
        # to translate) on Odoo 18, and a same-named kwarg collides.
        parts.append(_(
            "%(line)s (amount %(amount).2f): %(detail)s",
            line=line.name or '?', amount=line.amount, detail=source))
    return '\n'.join(parts)


def check_oci_recycling_completeness(soci):
    """IAS 1.82A OCI recycling completeness gate, called from action_confirm.

    Blocks confirmation while any OCI component with a non-zero amount has no
    tag-derived reclassification section (no source account, or an untagged
    source account), unless the OCI-tag override flag is set with a reason.
    The override is then logged to the chatter and the server log. Statements
    with no OCI lines, or whose OCI lines are all tag-derived, pass silently.
    """
    soci.ensure_one()
    misfits = oci_recycling_misfit_lines(soci)
    if not misfits:
        return
    listing = format_oci_misfit_note(misfits)
    if not soci.oci_tag_override:
        raise UserError(_(
            "IAS 1.82A completeness: these other comprehensive income "
            "components carry no recycling tag on their source account, so "
            "whether they may be reclassified to profit or loss rests on a "
            "manual flag rather than the account classification:\n"
            "%(listing)s\n\nTag the source accounts (EH OCI Recyclable / "
            "Non-Recyclable), or set the OCI recycling override with a "
            "reason to confirm anyway.",
            listing=listing))
    reason = (soci.oci_tag_override_reason or '').strip()
    if not reason:
        raise UserError(_(
            "The IAS 1.82A OCI recycling override requires a reason. Record "
            "why the untagged OCI components are acceptable before "
            "confirming."))
    soci.message_post(body=_(
        "IAS 1.82A OCI recycling completeness OVERRIDDEN on confirm by "
        "%(user)s. Reason: %(reason)s\nUntagged OCI components:\n%(listing)s",
        user=soci.env.user.display_name, reason=reason, listing=listing))
    _logger.info(
        "IAS 1.82A OCI recycling override on %s %s by %s: %s",
        soci._name, soci.display_name, soci.env.user.login, reason)


# ---------------------------------------------------------------------------
# NCI linkage to eh_account_consolidation (soft registry lookup)
# ---------------------------------------------------------------------------

def find_covering_consol_run(statement):
    """Latest settled consolidation run covering the statement period.

    Soft dependency: returns an empty value when eh_account_consolidation is
    not installed. A run covers the period when its own period encloses the
    statement's and its figures are settled (computed / reviewed / closed);
    the parent company of the consolidated entity must be the statement's
    company.
    """
    statement.ensure_one()
    env = statement.env
    if 'eh.consol.run' not in env.registry:
        return None
    if not (statement.company_id and statement.period_start
            and statement.period_end):
        return None
    return env['eh.consol.run'].search([
        ('entity_id.parent_company_id', '=', statement.company_id.id),
        ('state', 'in', ('computed', 'reviewed', 'closed')),
        ('period_from', '<=', statement.period_start),
        ('period_to', '>=', statement.period_end),
    ], order='period_to desc, id desc', limit=1) or None


def consol_nci_carve(run):
    """The run's NCI carve-out as a credit-positive equity figure.

    Consolidation run lines store amounts in the ledger sign convention
    (equity credit-negative), so the header nci_amount of a normal positive
    non-controlling interest is negative; the statements worksheets are
    credit-positive, hence the negation.
    """
    return -(run.nci_amount or 0.0)


# ---------------------------------------------------------------------------
# IAS 34 interim presentation
# ---------------------------------------------------------------------------

def presentation_label(statement, base_label):
    """Statement heading per the period type.

    IAS 34.8 permits a condensed set of interim statements; IAS 34.20 sets
    the comparative convention. The label carries the interim / condensed
    qualification so a reader can never mistake an interim worksheet for the
    annual statement.
    """
    if statement.period_type == 'interim':
        if statement.condensed:
            return _("Condensed interim %(label)s (IAS 34.8)",
                     label=base_label)
        return _("Interim %(label)s (IAS 34)", label=base_label)
    return base_label[:1].upper() + base_label[1:]


def check_interim_fields(statement):
    """Validate the IAS 34 fields; shared @api.constrains body.

    - Condensed flag and comparatives are meaningful on interim statements
      only (IAS 34.8 / 34.20); an annual statement carrying them is refused.
    - The prior-interim comparative must itself be an interim statement of
      the same company ending before this period starts (the comparable
      interim period of the immediately preceding financial year,
      IAS 34.20).
    - The prior-annual comparative must be an annual statement of the same
      company ending before this period starts (the immediately preceding
      annual period, IAS 34.20).
    """
    statement.ensure_one()
    if statement.period_type != 'interim':
        if (statement.condensed or statement.comparative_interim_id
                or statement.comparative_annual_id):
            raise ValidationError(_(
                "The condensed flag and the IAS 34 comparatives apply to "
                "interim statements only. Set the period type to interim "
                "first, or clear them."))
        return
    for comp, wanted_type, label in (
        (statement.comparative_interim_id, 'interim',
         _("prior interim comparative")),
        (statement.comparative_annual_id, 'annual',
         _("prior annual comparative")),
    ):
        if not comp:
            continue
        if comp == statement:
            raise ValidationError(_(
                "A statement cannot be its own IAS 34 comparative."))
        if comp.period_type != wanted_type:
            raise ValidationError(_(
                "The %(label)s must be a statement with period type "
                "'%(wanted)s' (IAS 34.20).",
                label=label, wanted=wanted_type))
        if comp.company_id != statement.company_id:
            raise ValidationError(_(
                "The %(label)s must belong to the same company.",
                label=label))
        if (comp.period_end and statement.period_start
                and fields.Date.to_date(comp.period_end)
                >= fields.Date.to_date(statement.period_start)):
            raise ValidationError(_(
                "The %(label)s must cover a period ending before this "
                "statement's period starts (IAS 34.20).",
                label=label))
