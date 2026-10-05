import os

from odoo.http import route
from odoo.addons.web.controllers.binary import Binary


class OneStagingBinary(Binary):
    """Keep staging asset requests stable behind Render's ephemeral instances."""

    @route()
    def content_assets(self, filename=None, unique="any", nocache=False, assets_params=None):
        if (
            os.getenv("RENDER_SERVICE_NAME") == "one-erp-staging"
            and unique not in ("debug", "any", "%")
        ):
            unique = "any"
        return super().content_assets(
            filename=filename,
            unique=unique,
            nocache=nocache,
            assets_params=assets_params,
        )
