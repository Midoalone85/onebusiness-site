from odoo import _, api, fields, models
from odoo.exceptions import UserError


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
    state_id = fields.Many2one("res.country.state", string="State / Region")
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
    ], compute="_compute_zatca_readiness", string="ZATCA Status")
    phase = fields.Selection([
        ("phase1", "Phase 1"),
        ("phase2", "Phase 2"),
    ], default="phase2", required=True, string="E-Invoicing Phase")
    notes = fields.Text(string="Compliance Notes")

    configuration_ready = fields.Boolean(
        string="Configuration Ready",
        compute="_compute_zatca_readiness",
    )
    journal_onboarded = fields.Boolean(
        string="Sales Journal Onboarded",
        compute="_compute_zatca_readiness",
    )
    readiness_missing = fields.Text(
        string="Missing Requirements",
        compute="_compute_zatca_readiness",
    )
    native_api_mode = fields.Selection(
        related="company_id.l10n_sa_api_mode",
        string="Native ZATCA API Mode",
        readonly=True,
    )

    _one_saudi_company_unique = models.Constraint(
        "UNIQUE(company_id)",
        "Only one Saudi profile is allowed per company.",
    )

    @api.onchange("vat_number")
    def _onchange_vat_number(self):
        if self.vat_number:
            self.vat_number = self.vat_number.replace(" ", "")

    @api.depends(
        "company_id",
        "vat_number",
        "building_number",
        "street_name",
        "city",
        "state_id",
        "postal_code",
        "additional_number",
        "commercial_registration",
    )
    def _compute_zatca_readiness(self):
        for profile in self:
            company = profile.company_id
            missing = []

            vat = profile.vat_number or company.vat
            street = profile.street_name or company.street
            district = profile.district or company.street2
            city = profile.city or company.city
            state = profile.state_id or company.state_id
            country = company.country_id
            building = profile.building_number or company.l10n_sa_edi_building_number
            secondary = profile.additional_number or company.l10n_sa_edi_plot_identification
            postal = profile.postal_code or company.zip

            if not vat:
                missing.append(_("VAT Registration Number"))
            else:
                vat = vat.replace(" ", "")
                if len(vat) != 15 or not vat.isdigit() or not (vat.startswith("3") and vat.endswith("3")):
                    missing.append(_("Valid 15-digit Saudi VAT number"))

            if not street:
                missing.append(_("Street"))
            if not district:
                missing.append(_("District"))
            if not city:
                missing.append(_("City"))
            if not state:
                missing.append(_("State / Region"))
            if not country or country.code != "SA":
                missing.append(_("Saudi Arabia country"))
            if not building:
                missing.append(_("Building Number"))
            elif not (building.isdigit() and len(building) == 4):
                missing.append(_("4-digit Building Number"))
            if not secondary:
                missing.append(_("Additional Number"))
            elif not (secondary.isdigit() and len(secondary) == 4):
                missing.append(_("4-digit Additional Number"))
            if not postal:
                missing.append(_("Postal Code"))
            if not profile.commercial_registration:
                missing.append(_("Commercial Registration"))

            sale_journals = self.env["account.journal"].sudo().search([
                ("company_id", "=", company.id),
                ("type", "=", "sale"),
            ])
            journal_onboarded = any(
                journal._l10n_sa_ready_to_submit_einvoices()
                for journal in sale_journals
            )

            profile.configuration_ready = not missing
            profile.journal_onboarded = journal_onboarded
            profile.readiness_missing = "\n".join(f"• {item}" for item in missing)

            if journal_onboarded:
                profile.zatca_status = "connected"
            elif not missing:
                profile.zatca_status = "configuration_ready"
            else:
                profile.zatca_status = "not_connected"


    @api.model
    def one_get_readiness_summary(self):
        profiles = self.search([("company_id", "in", self.env.companies.ids)])
        ready = connected = 0
        for profile in profiles:
            if profile.configuration_ready:
                ready += 1
            if profile.journal_onboarded:
                connected += 1
        return {
            "profiles": len(profiles),
            "ready": ready,
            "connected": connected,
        }

    def action_apply_to_company(self):
        self.ensure_one()
        company = self.company_id
        country = self.env["res.country"].search([("code", "=", self.country_code or "SA")], limit=1)
        if not country or country.code != "SA":
            raise UserError(_("Saudi compliance requires Saudi Arabia as the company country."))

        mode_map = {
            "sandbox": "sandbox",
            "simulation": "preprod",
            "production": "prod",
        }
        vals = {
            "vat": (self.vat_number or "").replace(" ", "") or False,
            "street": self.street_name or False,
            "street2": self.district or False,
            "city": self.city or False,
            "state_id": self.state_id.id or False,
            "zip": self.postal_code or False,
            "country_id": country.id,
            "l10n_sa_edi_building_number": self.building_number or False,
            "l10n_sa_edi_plot_identification": self.additional_number or False,
            "l10n_sa_edi_additional_identification_scheme": "CRN",
            "l10n_sa_edi_additional_identification_number": self.commercial_registration or False,
            "l10n_sa_api_mode": mode_map[self.zatca_environment],
        }
        company.write(vals)
        return True

    def action_pull_from_company(self):
        self.ensure_one()
        company = self.company_id
        mode_map = {
            "sandbox": "sandbox",
            "preprod": "simulation",
            "prod": "production",
        }
        self.write({
            "commercial_registration": (
                company.l10n_sa_edi_additional_identification_number
                if company.l10n_sa_edi_additional_identification_scheme == "CRN"
                else self.commercial_registration
            ),
            "vat_number": company.vat or False,
            "building_number": company.l10n_sa_edi_building_number or False,
            "street_name": company.street or False,
            "district": company.street2 or False,
            "city": company.city or False,
            "state_id": company.state_id.id or False,
            "postal_code": company.zip or False,
            "additional_number": company.l10n_sa_edi_plot_identification or False,
            "country_code": company.country_id.code or "SA",
            "zatca_environment": mode_map.get(company.l10n_sa_api_mode, "sandbox"),
        })
        return True
