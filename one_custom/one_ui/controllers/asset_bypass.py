import logging
import os

from odoo.http import route
from odoo.addons.web.controllers.binary import Binary

_logger = logging.getLogger(__name__)


class OneStagingAssetBinary(Binary):
    """Serve ONE ERP staging asset bundles without hash-to-hash redirect loops."""

    @route(
        "/web/assets/<string:unique>/<string:filename>",
        type="http",
        auth="public",
        readonly=True,
    )
    def content_assets(self, filename=None, unique="any", nocache=False, assets_params=None):
        if os.getenv("RENDER_SERVICE_NAME") == "one-erp-staging" and unique != "debug":
            _logger.info(
                "ONE ERP staging asset bypass: requested=%s file=%s",
                unique,
                filename,
            )
            # Odoo treats "any" as ANY_UNIQUE and therefore serves the current
            # bundle directly instead of redirecting a stale hash to another
            # hash. This is staging-only and leaves production cache semantics
            # unchanged.
            return super().content_assets(
                filename=filename,
                unique="any",
                nocache=nocache,
                assets_params=assets_params,
            )
        return super().content_assets(
            filename=filename,
            unique=unique,
            nocache=nocache,
            assets_params=assets_params,
        )
