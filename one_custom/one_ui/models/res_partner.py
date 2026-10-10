"""Unified ONE ERP contact enrichment for Saudi invoicing.

Keep customers, suppliers and people on the standard res.partner model.
No custom accounting tables or posting overrides are introduced here.
"""
import re

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ResPartner(models.Model):
    _inherit = "res.partner"

    one_contact_role = fields.Selection(
        selection=[
            ("customer", "Customer"),
            ("supplier", "Supplier"),
            ("both", "Customer and Supplier"),
            ("other", "Other Contact"),
        ],
        string="ONE ERP Relationship",
        default="other",
        index=True,
        help="An organisational label. Odoo keeps its own customer/supplier activity rankings.",
    )
    one_legal_name_ar = fields.Char(string="Legal Name (Arabic)")
    one_legal_name_en = fields.Char(string="Legal Name (English)")
    one_national_district = fields.Char(string="Saudi National Address District")
    one_national_building_number = fields.Char(string="Saudi Building Number")
    one_national_additional_number = fields.Char(string="Saudi Additional Number")
    one_national_short_address = fields.Char(string="SPL Short Address")
    one_billing_address_preview = fields.Text(
        string="National Address Preview",
        compute="_compute_one_billing_address_preview",
        help="Read-only summary. Invoices use the actual saved partner address fields.",
    )

    @api.depends(
        "street", "street2", "city", "zip", "country_id",
        "one_national_district", "one_national_building_number",
        "one_national_additional_number",
    )
    def _compute_one_billing_address_preview(self):
        for partner in self:
            lines = [
                partner.street,
                partner.street2,
                partner.one_national_district,
                partner.city,
                partner.zip,
                partner.country_id.name,
            ]
            address = "، ".join(value.strip() for value in lines if value and value.strip())
            suffix = []
            if partner.one_national_building_number:
                suffix.append(_("Building: %s") % partner.one_national_building_number)
            if partner.one_national_additional_number:
                suffix.append(_("Additional: %s") % partner.one_national_additional_number)
            partner.one_billing_address_preview = " | ".join(
                part for part in [address, "، ".join(suffix)] if part
            )

    @api.constrains(
        "country_id", "one_national_building_number",
        "one_national_additional_number", "one_national_short_address",
    )
    def _check_one_saudi_national_address(self):
        """Validate supplied SPL codes without forcing all existing contacts
        to have a complete Saudi address during ordinary editing/imports."""
        for partner in self:
            if partner.country_id.code != "SA":
                continue
            for value, pattern, label in (
                (partner.one_national_building_number, r"\d{4}", _("Building number")),
                (partner.one_national_additional_number, r"\d{4}", _("Additional number")),
                (partner.one_national_short_address, r"[A-Za-z]{4}\d{4}", _("SPL short address")),
            ):
                if value and not re.fullmatch(pattern, value.strip()):
                    raise ValidationError(
                        _("%s has an invalid national-address format.") % label
                    )
