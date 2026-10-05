import os

from odoo import models


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    def _storage(self):
        if os.getenv("RENDER_SERVICE_NAME") == "one-erp-staging":
            return "db"
        return super()._storage()


class IrAsset(models.Model):
    _inherit = "ir.asset"

    def _get_asset_bundle_url(self, filename, unique, assets_params, ignore_params=False):
        if os.getenv("RENDER_SERVICE_NAME") == "one-erp-staging" and unique not in ("debug", "any", "%"):
            unique = "any"
        return super()._get_asset_bundle_url(
            filename,
            unique,
            assets_params,
            ignore_params=ignore_params,
        )
