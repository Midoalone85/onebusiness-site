from werkzeug.exceptions import NotFound

from odoo import http
from odoo.addons.web.controllers.database import Database


class OneDatabaseSecurity(Database):
    """Hide all public database-management endpoints for ONE ERP."""

    @http.route()
    def selector(self, **kw):
        raise NotFound()

    @http.route()
    def manager(self, **kw):
        raise NotFound()

    @http.route()
    def create(self, **kw):
        raise NotFound()

    @http.route()
    def duplicate(self, **kw):
        raise NotFound()

    @http.route()
    def drop(self, **kw):
        raise NotFound()

    @http.route()
    def rename(self, **kw):
        raise NotFound()

    @http.route()
    def backup(self, **kw):
        raise NotFound()

    @http.route()
    def restore(self, **kw):
        raise NotFound()

    @http.route()
    def change_password(self, **kw):
        raise NotFound()

    @http.route()
    def list(self, **kw):
        raise NotFound()
