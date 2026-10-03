from odoo.http import request, route
from odoo.addons.web.controllers.home import Home
from odoo.addons.web.controllers.utils import ensure_db, is_user_internal


class OneHome(Home):
    """Keep ONE ERP on the clean root URL instead of redirecting users to /odoo."""

    @route("/", type="http", auth="none")
    def index(self, s_action=None, db=None, **kw):
        ensure_db()

        if not request.session.uid:
            return request.redirect_query(
                "/web/login",
                query={"redirect": "/"},
                code=303,
            )

        if not is_user_internal(request.session.uid):
            return request.redirect_query(
                "/web/login_successful",
                query=request.params,
            )

        return self.web_client(s_action=s_action, **kw)
