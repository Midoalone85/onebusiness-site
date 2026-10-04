import os


def post_init_hook(env):
    """Prepare ONE ERP defaults after installation on Odoo 20."""
    # Keep Arabic available from day one while English remains the base language.
    env["res.lang"]._activate_and_install_lang("ar_001")

    # Never leave a fresh public ONE ERP instance on Odoo's bootstrap password.
    # The secret lives in the hosting environment and is never committed to Git.
    bootstrap_password = os.environ.get("ONE_BOOTSTRAP_ADMIN_PASSWORD")
    if bootstrap_password:
        admin = env.ref("base.user_admin", raise_if_not_found=False)
        if admin:
            admin.sudo().write({"password": bootstrap_password})


def uninstall_hook(env):
    return
