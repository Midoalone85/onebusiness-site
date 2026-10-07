import gzip
import logging
import os
from contextlib import nullcontext

from odoo import api
from odoo.http import request, route
from odoo.addons.web.controllers.binary import Binary

_logger = logging.getLogger(__name__)


class OneStagingAssetBinary(Binary):
    """Serve staging assets as raw bytes to bypass Odoo stream redirects."""

    @route(
        "/web/assets/<string:unique>/<string:filename>",
        type="http",
        auth="public",
        readonly=True,
    )
    def content_assets(self, filename=None, unique=None, nocache=False, assets_params=None):
        if (
            os.getenv("RENDER_SERVICE_NAME") not in {"one-erp-staging", "one-erp-live"}
            or unique == "debug"
        ):
            return super().content_assets(
                filename=filename,
                unique=unique,
                nocache=nocache,
                assets_params=assets_params,
            )

        env = request.env
        assets_params = assets_params or {}
        payload = None
        mimetype = "application/octet-stream"

        attachment = env["ir.attachment"].sudo().search([
            ("public", "=", True),
            ("url", "!=", False),
            ("url", "=", f"/web/assets/{unique}/{filename}"),
            ("res_model", "=", "ir.ui.view"),
            ("res_id", "=", 0),
            ("create_uid", "=", api.SUPERUSER_ID),
        ], order="id desc", limit=1)

        if attachment:
            try:
                payload = bytes(attachment.raw)
                mimetype = attachment.mimetype or mimetype
            except (FileNotFoundError, OSError):
                attachment.unlink()
                payload = None

        if payload is None:
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
                    payload = bytes(generated.raw)
                    mimetype = generated.mimetype or mimetype

        if payload is None:
            raise request.not_found()

        if filename.endswith(".css"):
            mimetype = "text/css"
        elif filename.endswith(".js"):
            mimetype = "application/javascript"

        _logger.info(
            "ONE ERP staging raw asset 200: requested=%s file=%s bytes=%s",
            unique,
            filename,
            len(payload),
        )
        response_payload = payload
        headers = [
            ("Content-Type", mimetype),
            ("Cache-Control", "public, max-age=31536000, immutable"),
            ("Vary", "Accept-Encoding"),
        ]
        accept_encoding = request.httprequest.headers.get("Accept-Encoding", "")
        if (
            len(payload) >= 1024
            and "gzip" in accept_encoding.lower()
            and filename.endswith((".js", ".css", ".json", ".svg"))
        ):
            response_payload = gzip.compress(payload, compresslevel=5)
            headers.append(("Content-Encoding", "gzip"))

        return request.make_response(response_payload, headers=headers, status=200)
