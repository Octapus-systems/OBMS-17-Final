# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Partner action: print a customer or vendor statement PDF.

Adds two methods on res.partner so the statement can be triggered from a
button on the partner form. The methods resolve the right report record
(customer or vendor) and delegate to the dynamic report's PDF pipeline,
returning a download action the user can click through.
"""

from odoo import _, api, fields, models  # noqa: F401
from odoo.exceptions import UserError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def action_print_customer_statement(self):
        return self._print_statement(
            xml_id='eh_account_dynamic_reports.report_customer_statement',
            label="Customer Statement",
        )

    def action_print_vendor_statement(self):
        return self._print_statement(
            xml_id='eh_account_dynamic_reports.report_vendor_statement',
            label="Vendor Statement",
        )

    def _print_statement(self, xml_id, label):
        self.ensure_one()
        report = self.env.ref(xml_id, raise_if_not_found=False)
        if not report:
            raise UserError(_(
                "Statement report %s is not registered. Reinstall "
                "eh_account_dynamic_reports to fix this.",
            ) % xml_id)
        today = fields.Date.today()
        options = report.get_default_options()
        options['partner_id'] = self.id
        options['date'] = {
            'mode': 'range',
            'date_from': today.replace(day=1).isoformat(),
            'date_to': today.isoformat(),
        }
        content = report.render_pdf(options)
        import base64
        attachment = self.env['ir.attachment'].create({
            'name': "%s_%s_%s.pdf" % (
                label.replace(' ', '_'),
                self.display_name.replace(' ', '_'),
                today.isoformat(),
            ),
            'type': 'binary',
            'datas': base64.b64encode(content),
            'mimetype': 'application/pdf',
            'res_model': 'res.partner',
            'res_id': self.id,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'download',
        }
