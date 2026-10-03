def post_init_hook(env):
    """Prepare ONE ERP defaults after installation on Odoo 20."""
    # Keep Arabic available from day one while English remains the base language.
    env["res.lang"]._activate_and_install_lang("ar_001")


def uninstall_hook(env):
    return
