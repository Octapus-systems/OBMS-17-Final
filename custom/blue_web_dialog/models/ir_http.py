from odoo import models
from odoo.http import request  # noqa: F401


class IrHttp(models.AbstractModel):

    _inherit = "ir.http"

    # ----------------------------------------------------------
    # Functions
    # ----------------------------------------------------------

    def session_info(self):
        result = super(IrHttp, self).session_info()
        result['dialog_size'] = self.env.user.dialog_size
        return result
