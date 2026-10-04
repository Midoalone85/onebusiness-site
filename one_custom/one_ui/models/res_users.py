from odoo import _, api, fields, models
from odoo.exceptions import AccessDenied, AccessError, UserError


class ResUsers(models.Model):
    _inherit = "res.users"

    one_is_trial = fields.Boolean(string="ONE ERP Trial User", default=False, copy=False, index=True)
    one_trial_expires_at = fields.Datetime(string="Trial Expires At", copy=False, index=True)

    def _check_credentials(self, credential, env):
        self.ensure_one()
        if (
            self.one_is_trial
            and self.one_trial_expires_at
            and self.one_trial_expires_at <= fields.Datetime.now()
        ):
            raise AccessDenied()
        return super()._check_credentials(credential, env)

    @api.model
    def _cron_expire_one_trials(self):
        expired = self.sudo().search([
            ("one_is_trial", "=", True),
            ("active", "=", True),
            ("one_trial_expires_at", "!=", False),
            ("one_trial_expires_at", "<=", fields.Datetime.now()),
        ])
        if expired:
            expired.write({"active": False})
        return len(expired)

    def one_switch_language(self, code):
        """Switch the current ONE ERP user between Arabic and English."""
        self.ensure_one()

        if self.id != self.env.user.id and not self.env.user.has_group("base.group_system"):
            raise AccessError(_("You can only change your own interface language."))

        allowed = {"en_US", "ar_001"}
        if code not in allowed:
            raise UserError(_("Unsupported ONE ERP language."))

        Lang = self.env["res.lang"]
        lang = (
            Lang._activate_and_install_lang(code)
            if code == "ar_001"
            else Lang._activate_lang(code)
        )
        if not lang:
            raise UserError(_("The requested language is not available."))

        self.sudo().write({"lang": code})
        return {
            "lang": code,
            "direction": lang.direction,
        }
