import datetime
import logging
import os
import secrets

from odoo import fields
from odoo.exceptions import AccessDenied

_logger = logging.getLogger(__name__)

TRIAL_DAYS = 7


class _RollbackTrialSmoke(Exception):
    pass


def _run_trial_smoke_test(env):
    """Prove the 7-day trial path without leaving any test records behind."""
    try:
        with env.cr.savepoint():
            currency = env.ref("base.SAR", raise_if_not_found=False)
            if not currency:
                currency = env["res.currency"].sudo().search([("active", "=", True)], limit=1)
            if not currency:
                raise RuntimeError("No active currency available for trial smoke test.")

            company = env["res.company"].sudo().create({
                "name": "ONE ERP Trial Smoke",
                "currency_id": currency.id,
            })

            group_refs = [
                "base.group_user",
                "sales_team.group_sale_salesman",
                "purchase.group_purchase_user",
                "stock.group_stock_user",
                "account.group_account_user",
                "mrp.group_mrp_user",
                "hr.group_hr_user",
                "point_of_sale.group_pos_user",
            ]
            groups = env["res.groups"]
            for xmlid in group_refs:
                group = env.ref(xmlid, raise_if_not_found=False)
                if not group:
                    raise RuntimeError(f"Missing trial prerequisite group: {xmlid}")
                groups |= group

            expires_at = fields.Datetime.now() + datetime.timedelta(days=TRIAL_DAYS)
            password = "OneSmoke!" + secrets.token_urlsafe(8)
            user = env["res.users"].sudo().with_context(no_reset_password=True).create({
                "name": "ONE ERP Trial Smoke",
                "login": "one.trial.smoke@invalid.example",
                "email": "one.trial.smoke@invalid.example",
                "password": password,
                "company_id": company.id,
                "company_ids": [(6, 0, [company.id])],
                "group_ids": [(6, 0, groups.ids)],
                "one_is_trial": True,
                "one_trial_expires_at": expires_at,
            })

            if not user.one_is_trial or not user.one_trial_expires_at:
                raise RuntimeError("Trial markers were not persisted.")
            if env.ref("base.group_system") in user.all_group_ids:
                raise RuntimeError("Trial user unexpectedly received administrator access.")

            auth_user = user.with_user(user)
            auth_info = auth_user._check_credentials(
                {"type": "password", "password": password},
                {"interactive": True},
            )
            if auth_info.get("uid") != user.id:
                raise RuntimeError("Trial password authentication did not return the trial user.")

            user.sudo().write({
                "one_trial_expires_at": fields.Datetime.now() - datetime.timedelta(seconds=1)
            })
            expired_user = user.with_user(user)
            try:
                expired_user._check_credentials(
                    {"type": "password", "password": password},
                    {"interactive": True},
                )
            except AccessDenied:
                pass
            else:
                raise RuntimeError("Expired trial credentials were accepted.")

            raise _RollbackTrialSmoke()
    except _RollbackTrialSmoke:
        env["ir.config_parameter"].sudo().set_str("one.trial_smoke_test", "passed")
        env["ir.config_parameter"].sudo().set_str(
            "one.trial_smoke_test_at", fields.Datetime.to_string(fields.Datetime.now())
        )
        _logger.info("ONE ERP 7-day trial smoke test passed.")


def post_init_hook(env):
    """Prepare ONE ERP defaults and verify critical trial behavior after installation."""
    env["res.lang"]._activate_and_install_lang("ar_001")

    bootstrap_password = os.environ.get("ONE_BOOTSTRAP_ADMIN_PASSWORD")
    if bootstrap_password:
        admin = env.ref("base.user_admin", raise_if_not_found=False)
        if admin:
            admin.sudo().write({"password": bootstrap_password})

    _run_trial_smoke_test(env)


def uninstall_hook(env):
    return
