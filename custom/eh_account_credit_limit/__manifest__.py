# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. Credit-limit enforcement is a
# standard pattern from corporate finance practice; the implementation
# is composed against the public Odoo account.move post pipeline. No
# code or comments derive from any proprietary or third-party module.
#
##############################################################################
{
    'name': 'Customer Credit Limit',
    'summary': 'Hard customer credit limit enforcement for Odoo 17 Community that blocks the invoice at post time instead of just showing a number. Per-company credit policy, optional per-partner limit override, warn-or-block enforcement, manager override with reason recorded in an append-only audit log, and a live OK/Warn/Over exposure badge on the partner form. Search terms: odoo customer credit limit, block invoice over credit limit, accounts receivable credit control, hard credit limit enforcement, credit hold, per-partner credit limit, credit policy per company, customer exposure receivable, credit limit manager override audit log.',  # noqa: E501
    'description': """The standard credit_limit field on a partner is informational only. Nothing stops an over-limit customer invoice from posting. This module turns that field into a real posting gate.  # noqa: E501

Define one credit policy per company. The policy sets the default limit, whether enforcement warns or blocks, whether draft customer invoices and the not-yet-invoiced portion of confirmed sales orders count toward exposure (both opt-in, both off by default), and which group may override. Any partner can carry its own limit and policy override; otherwise the company default applies.  # noqa: E501

When a customer invoice (out_invoice or out_refund) posts, the gate computes the partner's exposure and compares it against the effective limit. In block mode the post raises a clear error naming the exposure, the limit, and the excess, until a manager records an override reason on that specific invoice. In warn mode the breach is logged to the move chatter and the post proceeds. Exposure aggregates against the commercial partner, so child contacts share the parent company limit.  # noqa: E501

Every override writes one row to an append-only log capturing user, timestamp, exposure, limit, move amount, excess, and reason. The log model refuses write to anything but the comment and refuses unlink, so the record of who waved a customer through, and why, cannot be quietly edited away. The override is per invoice: blessing one move does not bless the next.""",  # noqa: E501
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.2.0',
    'depends': ['eh_account_base', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'views/credit_policy_views.xml',
        'views/res_partner_views.xml',
        'views/account_move_views.xml',
        'data/menus.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
