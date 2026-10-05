import logging
import os
from contextlib import nullcontext

from odoo import api
from odoo.http import request, route
from odoo.http.stream import STATIC_CACHE_LONG
from odoo.addons.web.controllers.binary import Binary

_logger = logging.getLogger(__name__)


class OneStagingAssetBinary(Binary):
    """Serve staging assets directly and bypass Odoo hash redirect checks."""

    @route(
        "/web/assets/<string:unique>/<string:filename>",
        type="http",
        auth="public",
        readonly=True,
    )
    def content_assets(self, filename=None, unique=None, nocache=False, assets_params=None):
        if os.getenv("RENDER_SERVICE_NAME") != "one-erp-staging" or unique == "debug":
            return super().content_assets(
                filename=filename,
                unique=unique,
                nocache=nocache,
                assets_params=assets_params,
            )

        env = request.env
        assets_params = assets_params or {}
        stream = None

        # Ignore the incoming version hash entirely on staging. Search any
        # existing attachment for this exact bundle filename first.
        attachment = env["ir.attachment"].sudo().search([
            ("public", "=", True),
            ("url", "!=", False),
            ("url", "=like", f"/web/assets/%/{filename}"),
            ("res_model", "=", "ir.ui.view"),
            ("res_id", "=", 0),
            ("create_uid", "=", api.SUPERUSER_ID),
        ], order="id desc", limit=1)

        if attachment:
            try:
                stream = env["ir.binary"]._get_stream_from(attachment, "raw", filename)
            except FileNotFoundError:
                attachment.unlink()
                stream = None

        if stream is None:
            if env.cr.readonly:
                env.cr.rollback()
                cursor_manager = env.registry.cursor(readonly=False)
            else:
                cursor_manager = nullcontext(env.cr)

            with cursor_manager as rw_cr:
                rw_env = api.Environment(rw_cr, env.user.id, request.env.context)
                bundle_name, rtl, asset_type, autoprefix = rw_env["ir.asset"]._parse_bundle_name(
                    filename, False
                )
                css = asset_type == "css"
                js = asset_type == "js"
                binary = asset_type == "binary"
                extension = "" if "." not in filename else filename.split(".")[-1]
                if binary:
                    asset_type = extension

                bundle = rw_env["ir.qweb"]._get_asset_bundle(
                    bundle_name,
                    css=css,
                    js=js,
                    binary=binary,
                    debug_assets=False,
                    rtl=rtl,
                    autoprefix=autoprefix,
                    assets_params=assets_params,
                )

                generated = None
                if css and bundle.stylesheets:
                    generated = bundle.css()
                elif js and bundle.javascripts:
                    generated = bundle.js()
                elif binary and bundle.binaries:
                    generated = bundle.bin(extension)

                if generated:
                    stream = rw_env["ir.binary"]._get_stream_from(generated, "raw", filename)

        if stream is None:
            raise request.not_found()

        _logger.info(
            "ONE ERP staging direct asset 200: requested=%s file=%s",
            unique,
            filename,
        )
        send_file_kwargs = {
            "as_attachment": False,
            "content_security_policy": None,
            "immutable": False,
            "max_age": None if nocache else 0,
        }
        return stream.get_response(**send_file_kwargs)
