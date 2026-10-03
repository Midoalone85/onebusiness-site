from odoo import fields, models


class OneSubscriptionPlan(models.Model):
    _name = "one.subscription.plan"
    _description = "ONE ERP Subscription Plan"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    monthly_price = fields.Monetary(default=0)
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id.id, required=True
    )
    feature_sales = fields.Boolean(string="Sales")
    feature_purchase = fields.Boolean(string="Purchases")
    feature_inventory = fields.Boolean(string="Inventory")
    feature_accounting = fields.Boolean(string="Accounting")
    feature_crm = fields.Boolean(string="CRM")
    feature_hr = fields.Boolean(string="HR")
    feature_mrp = fields.Boolean(string="Manufacturing")
    feature_reports = fields.Boolean(string="Advanced Reports")
    feature_saudi = fields.Boolean(string="Saudi Compliance")
    feature_api = fields.Boolean(string="API")
    feature_multi_company = fields.Boolean(string="Multi Company")

    _one_plan_code_unique = models.Constraint(
        "UNIQUE(code)",
        "Plan code must be unique.",
    )


class OneSubscription(models.Model):
    _name = "one.subscription"
    _description = "ONE ERP Customer Subscription"
    _order = "date_start desc, id desc"

    name = fields.Char(required=True, default="ONE ERP Subscription")
    partner_id = fields.Many2one("res.partner", required=True, ondelete="cascade")
    plan_id = fields.Many2one("one.subscription.plan", required=True, ondelete="restrict")
    date_start = fields.Date(required=True, default=fields.Date.context_today)
    date_end = fields.Date()
    status = fields.Selection([
        ("trial", "Trial"),
        ("active", "Active"),
        ("paused", "Paused"),
        ("expired", "Expired"),
    ], default="trial", required=True)
    users_limit = fields.Integer(default=1)
    notes = fields.Text()
