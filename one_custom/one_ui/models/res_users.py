from odoo import _, models
from odoo.exceptions import AccessError, UserError


class ResUsers(models.Model):
    _inherit = "res.users"

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
