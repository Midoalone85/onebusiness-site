import os

from odoo import models


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    def _storage(self):
        if os.getenv("RENDER_SERVICE_NAME") == "one-erp-staging":
            return "db"
        return super()._storage()
