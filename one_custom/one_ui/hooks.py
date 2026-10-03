def post_init_hook(env):
    dashboard = env.ref("one_ui.action_one_dashboard", raise_if_not_found=False)
    admin = env.ref("base.user_admin", raise_if_not_found=False)
    if dashboard and admin and hasattr(admin, "action_id"):
        admin.action_id = dashboard

    one_root = env.ref("one_ui.menu_one_root", raise_if_not_found=False)
    if one_root:
        other_roots = env["ir.ui.menu"].sudo().search([
            ("parent_id", "=", False),
            ("id", "!=", one_root.id),
        ])
        other_roots.write({"active": False})


def uninstall_hook(env):
    env["ir.ui.menu"].sudo().search([("parent_id", "=", False)]).write({"active": True})
