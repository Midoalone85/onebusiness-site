from odoo import api, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.user.one_is_trial:
            company_id = self.env.company.id
            vals_list = [
                {**vals, "company_id": vals.get("company_id") or company_id}
                for vals in vals_list
            ]
        return super().create(vals_list)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.user.one_is_trial:
            company_id = self.env.company.id
            vals_list = [
                {**vals, "company_id": vals.get("company_id") or company_id}
                for vals in vals_list
            ]
        return super().create(vals_list)
