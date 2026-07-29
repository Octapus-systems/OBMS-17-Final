# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Inter-company Sales/Purchase Mirroring',
    'summary': 'Extend inter-company automation to sales and purchase orders: confirmed SO drafts PO in partner company and vice versa, building on Inter-Company Rules invoice mirroring. Idempotent, no back-mirroring, Odoo 17 Community.',
    'description': """Extends the ERP Heritage inter-company engine from invoice and move mirroring to sales and purchase orders, completing the multi-company order cycle.

When a sales order is confirmed to a customer that represents another company (per inter-company configuration), a draft purchase order is created in that company with the source company as the vendor. Confirming a purchase order to such a supplier creates the matching draft sales order. Order lines map product, quantity, unit price, and name to the mirror. Mirrors are created in draft for the receiving company to review, never mirrored back (no back-mirror when the draft PO is confirmed), and created with the configured inter-company user when one is set, or sudo() otherwise.

Reuses the inter-company configuration and destination-company resolution from eh_account_intercompany (Represented Company field on partner), so a single setup drives both invoice and order mirroring.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '17.0.1.0.0',
    'depends': ['eh_account_intercompany', 'sale', 'purchase'],
    'data': [],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif'],
}
