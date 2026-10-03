def post_init_hook(env):
    dashboard = env.ref("one_ui.action_one_dashboard", raise_if_not_found=False)
    admin = env.ref("base.user_admin", raise_if_not_found=False)
    if dashboard and admin and hasattr(admin, "action_id"):
        admin.action_id = dashboard
