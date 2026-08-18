# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.report.builder: user-defined custom reports.

A builder record describes a report as an ordered list of lines:

* section_header: a non-numeric label that opens a section.
* account_aggregate: a numeric line that sums account.move.line balances
  filtered by account codes or account types, with a sign flip.
* formula: a numeric line whose value is computed from previously named
  lines using a small whitelisted arithmetic DSL. Used for subtotals,
  computed totals like Net Profit, and ratios.

Publishing a builder registers a corresponding eh.account.dynamic.report
record pointing at the generic builder handler. The published report
shows up in the OWL viewer like any other dynamic report and supports
XLSX, PDF, drill down (where applicable), and the orchestrator's cache.

v1 layout: every builder produces a single value column (Description plus
Amount). Multi-period and comparative columns are a v1.1 expansion.

Cache caveat: the orchestrator's per-execution result cache is keyed on
the move version counter, not on the builder definition version. If you
edit a builder's lines, run with use_cache=False to see fresh results,
or post any new journal entry to bump the counter and invalidate cache.
"""

import ast
import re
import sys

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


_CODE_RE = re.compile(r'^[a-z][a-z0-9_]*$')

# ``ast.Num`` (and its siblings) were deprecated in Python 3.8 and start
# raising DeprecationWarning in 3.12 ahead of outright removal. On any
# Python >= 3.8 the parser only ever emits ``ast.Constant`` for numeric
# literals, so ``ast.Num`` nodes are never produced by ``ast.parse``
# here. We still keep a *guarded* reference so hand-built or pre-3.8
# parse trees evaluate, while never touching the deprecated alias on
# 3.12+ (where doing so would warn now and fail once it is removed).
# ``isinstance(x, ())`` is always False, so the empty-tuple fallback is a
# no-op both in the whitelist and in the evaluator dispatch below.
_LEGACY_NUMERIC_NODES = ast.Num if sys.version_info < (3, 12) else ()

# AST nodes allowed inside a builder formula. Anything else (function
# calls, attribute access, comprehensions, assignments) is rejected
# before evaluation, so a malicious formula cannot escape the
# evaluator into Python at large.
_ALLOWED_FORMULA_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Name,
    ast.Load,
    ast.Constant,
    _LEGACY_NUMERIC_NODES,
    ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.USub, ast.UAdd,
)


class FormulaError(UserError):
    """Raised when a builder formula is rejected or fails to evaluate."""


def safe_eval_formula(formula, names):
    """Evaluate a whitelisted arithmetic formula against a names dict.

    Allowed: numeric constants, identifiers (looked up in `names`),
    binary +, -, *, /, unary +, -. Anything else raises FormulaError.
    Identifier lookups default to 0.0 when missing.
    """
    if not formula or not str(formula).strip():
        return 0.0
    try:
        tree = ast.parse(str(formula), mode='eval')
    except SyntaxError as exc:
        raise FormulaError(_(
            "Formula does not parse: %s",
        ) % exc)
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_FORMULA_NODES):
            raise FormulaError(_(
                "Formula uses a disallowed construct: %s",
            ) % type(node).__name__)

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise FormulaError(_("Only numeric constants are allowed."))
            return float(v)
        if _LEGACY_NUMERIC_NODES and isinstance(node, _LEGACY_NUMERIC_NODES):
            return float(node.n)  # pre-3.8 parse trees only
        if isinstance(node, ast.Name):
            return float(names.get(node.id, 0.0) or 0.0)
        if isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return (left / right) if right else 0.0
            raise FormulaError(_("Operator not allowed."))
        if isinstance(node, ast.UnaryOp):
            v = _eval(node.operand)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return +v
            raise FormulaError(_("Unary operator not allowed."))
        raise FormulaError(_("Node type not allowed."))

    return _eval(tree)


class EhReportBuilder(models.Model):
    _name = 'eh.report.builder'
    _description = "Custom dynamic report builder"
    _order = 'sequence, name'

    name = fields.Char(required=True)
    code = fields.Char(
        required=True,
        copy=False,
        help=(
            "Stable identifier used to publish the report. Must be "
            "lowercase, start with a letter, and contain only letters, "
            "digits, or underscores. Example: my_custom_pl"
        ),
    )
    description = fields.Text()
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        index=True,
    )

    line_ids = fields.One2many(
        'eh.report.builder.line',
        'builder_id',
        copy=True,
    )

    is_published = fields.Boolean(default=False, readonly=True)
    published_report_id = fields.Many2one(
        'eh.account.dynamic.report',
        readonly=True,
        copy=False,
        ondelete='set null',
    )

    label_column_name = fields.Char(
        default="Description",
        help="Header label for the leftmost column.",
    )
    amount_column_name = fields.Char(
        default="Amount",
        help="Header label for the value column.",
    )

    _sql_constraints = [
        ('unique_code', 'unique(code)', 'Builder code must be unique.'),
    ]

    @api.constrains('code')
    def _check_code_format(self):
        for rec in self:
            if not _CODE_RE.match(rec.code or ''):
                raise ValidationError(_(
                    "Builder code must match [a-z][a-z0-9_]* (got %r).",
                ) % rec.code)

    # ---- publish lifecycle ----

    def action_publish(self):
        DynamicReport = self.env['eh.account.dynamic.report']
        for rec in self:
            if rec.published_report_id:
                rec.published_report_id.write({
                    'name': rec.name,
                    'description': rec.description or '',
                    'sequence': rec.sequence,
                    'active': True,
                })
            else:
                report = DynamicReport.create({
                    'code': rec.code,
                    'name': rec.name,
                    'handler_model': 'eh.account.dynamic.report.handler.builder',
                    'sequence': rec.sequence,
                    'description': rec.description or '',
                })
                rec.published_report_id = report.id
            rec.is_published = True
        return True

    def action_unpublish(self):
        for rec in self:
            if rec.published_report_id:
                rec.published_report_id.active = False
            rec.is_published = False
        return True

    def action_open_viewer(self):
        self.ensure_one()
        if not self.is_published or not self.published_report_id:
            raise UserError(_(
                "Builder must be published before it can be viewed.",
            ))
        return {
            'type': 'ir.actions.client',
            'tag': 'eh_account_dynamic_report',
            'name': self.name,
            'context': {'report_code': self.code},
        }

    def action_duplicate(self):
        """Clone a builder under a new code so users can iterate on a
        copy without losing the original."""
        new_records = self.env[self._name]
        for rec in self:
            copy = rec.copy({
                'name': rec.name + " (copy)",
                'code': rec.code + '_copy',
                'is_published': False,
                'published_report_id': False,
            })
            new_records |= copy
        if len(new_records) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': self._name,
                'res_id': new_records.id,
                'view_mode': 'form',
                'views': [(False, 'form')],
                'target': 'current',
            }
        return True


class EhReportBuilderLine(models.Model):
    _name = 'eh.report.builder.line'
    _description = "Custom report builder line"
    _order = 'sequence, id'

    builder_id = fields.Many2one(
        'eh.report.builder',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(default=10)

    name = fields.Char(required=True)
    code = fields.Char(
        help=(
            "Optional stable name referenced by formula lines. Must match "
            "[a-z][a-z0-9_]* if set."
        ),
    )

    line_type = fields.Selection(
        [
            ('section_header', "Section Header"),
            ('account_aggregate', "Account Aggregate"),
            ('formula', "Formula"),
        ],
        required=True,
        default='account_aggregate',
    )
    level = fields.Integer(
        default=1,
        help=(
            "0 for section headers and computed totals (rendered bold), "
            "1 for data rows."
        ),
    )

    # account_aggregate fields
    account_scope = fields.Selection(
        [
            ('codes', "Account Codes"),
            ('types', "Account Types"),
        ],
        default='codes',
    )
    account_codes = fields.Char(
        help=(
            "Comma separated code prefixes (matches account.code LIKE "
            "'<prefix>%'). Example: 1000,1100,2000"
        ),
    )
    account_types = fields.Char(
        help=(
            "Comma separated account_type values. Example: "
            "income,income_other or expense,expense_direct_cost"
        ),
    )
    sign = fields.Selection(
        [
            ('+', "Positive (+)"),
            ('-', "Negative (-)"),
        ],
        default='+',
    )

    # formula fields
    formula = fields.Char(
        help=(
            "Whitelisted arithmetic referencing other line codes. Example: "
            "income - expenses, or revenue * 0.1 for a 10 percent flag. "
            "Allowed: + - * / and parentheses. Identifiers map to other "
            "lines' computed values; missing identifiers resolve to 0."
        ),
    )
    is_section_total = fields.Boolean(
        default=False,
        help=(
            "Style this formula line as a section total (top border, "
            "bold). Otherwise it renders as a regular computed line."
        ),
    )

    @api.constrains('code')
    def _check_code_format(self):
        for rec in self:
            if rec.code and not _CODE_RE.match(rec.code):
                raise ValidationError(_(
                    "Line code must match [a-z][a-z0-9_]* (got %r).",
                ) % rec.code)

    @api.constrains('builder_id', 'code')
    def _check_unique_code_per_builder(self):
        for rec in self:
            if not rec.code:
                continue
            others = rec.builder_id.line_ids.filtered(
                lambda line_item: line_item.code == rec.code and line_item.id != rec.id,
            )
            if others:
                raise ValidationError(_(
                    "Line code %r is duplicated within builder %s.",
                ) % (rec.code, rec.builder_id.name))
