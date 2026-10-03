def post_init_hook(env):
    """Keep installation side effects minimal on Odoo 20.

    The ONE ERP dashboard remains available through its client action and menu.
    Login redirection is handled by the web layer instead of res.users.action_id,
    whose accepted action model changed in Odoo 20.
    """
    return


def uninstall_hook(env):
    return
