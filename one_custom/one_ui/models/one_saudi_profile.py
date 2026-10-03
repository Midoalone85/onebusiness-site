from odoo import api, fields, models


class OneSaudiProfile(models.Model):
    _name = "one.saudi.profile"
    _description = "ONE ERP Saudi Compliance Profile"
    _rec_name = "company_id"

    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company,
        ondelete="cascade", index=True
    )
    commercial_registration = fields.Char(string="Commercial Registration")
    vat_number = fields.Char(string="VAT Registration Number")
    building_number = fields.Char(string="Building Number")
    street_name = fields.Char(string="Street")
    district = fields.Char(string="District")
    city = fields.Char(string="City")
    postal_code = fields.Char(string="Postal Code")
    additional_number = fields.Char(string="Additional Number")
    country_code = fields.Char(string="Country Code", default="SA")
    zatca_environment = fields.Selection([
        ("sandbox", "Sandbox"),
        ("simulation", "Simulation"),
        ("production", "Production"),
    ], default="sandbox", required=True, string="ZATCA Environment")
    zatca_status = fields.Selection([
        ("not_connected", "Not Connected"),
        ("configuration_ready", "Configuration Ready"),
        ("connected", "Connected"),
    ], default="not_connected", required=True, string="ZATCA Status")
    phase = fields.Selection([
        ("phase1", "Phase 1"),
        ("phase2", "Phase 2"),
    ], default="phase2", required=True, string="E-Invoicing Phase")
    notes = fields.Text(string="Compliance Notes")

    _one_saudi_company_unique = models.Constraint(
        "UNIQUE(company_id)",
        "Only one Saudi profile is allowed per company.",
    )

    @api.onchange("vat_number")
    def _onchange_vat_number(self):
        if self.vat_number:
            self.vat_number = self.vat_number.replace(" ", "")
