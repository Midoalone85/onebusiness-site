from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare


class OneConstructionProject(models.Model):
    _name = "one.construction.project"
    _description = "ONE Construction Project"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    code = fields.Char(default=lambda self: _("New"), copy=False, readonly=True)
    partner_id = fields.Many2one("res.partner", required=True, tracking=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id", readonly=True)
    analytic_account_id = fields.Many2one("account.analytic.account", string="Cost Center / Analytic Account")
    contract_date = fields.Date(default=fields.Date.context_today)
    date_start = fields.Date()
    date_end = fields.Date()
    state = fields.Selection([
        ("draft", "Draft"), ("active", "In Progress"),
        ("done", "Completed"), ("cancelled", "Cancelled"),
    ], default="draft", required=True, tracking=True)
    boq_line_ids = fields.One2many("one.construction.boq.line", "project_id", string="Work Items")
    certificate_ids = fields.One2many("one.construction.certificate", "project_id", string="Progress Certificates")
    contract_amount = fields.Monetary(compute="_compute_contract_amount", store=True, currency_field="currency_id")
    approved_amount = fields.Monetary(compute="_compute_approved_amount", currency_field="currency_id")
    remaining_amount = fields.Monetary(compute="_compute_approved_amount", currency_field="currency_id")
    actual_cost_amount = fields.Monetary(compute="_compute_actual_cost", currency_field="currency_id",
                                         help="Preliminary cost: negative analytic entries on the linked cost center.")
    contribution_to_date = fields.Monetary(compute="_compute_contribution", currency_field="currency_id",
                                            help="Approved work minus recorded analytic costs, not final project profit.")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("code") or vals.get("code") == _("New"):
                vals["code"] = self.env["ir.sequence"].next_by_code("one.construction.project") or _("New")
        return super().create(vals_list)

    @api.constrains("date_start", "date_end")
    def _check_dates(self):
        for project in self:
            if project.date_start and project.date_end and project.date_start > project.date_end:
                raise ValidationError(_("The project end date cannot precede the start date."))

    @api.depends("boq_line_ids.price_subtotal")
    def _compute_contract_amount(self):
        for project in self:
            project.contract_amount = sum(project.boq_line_ids.mapped("price_subtotal"))

    @api.depends("contract_amount", "certificate_ids.state", "certificate_ids.work_amount")
    def _compute_approved_amount(self):
        for project in self:
            project.approved_amount = sum(
                project.certificate_ids.filtered(
                    lambda c: c.state in ("approved", "invoiced")
                ).mapped("work_amount")
            )
            project.remaining_amount = project.contract_amount - project.approved_amount

    @api.depends("analytic_account_id")
    def _compute_actual_cost(self):
        analytic_lines = self.env["account.analytic.line"]
        for project in self:
            if not project.analytic_account_id:
                project.actual_cost_amount = 0.0
                continue
            lines = analytic_lines.search([("account_id", "=", project.analytic_account_id.id)])
            project.actual_cost_amount = -sum(line.amount for line in lines if line.amount < 0)

    @api.depends("approved_amount", "actual_cost_amount")
    def _compute_contribution(self):
        for project in self:
            project.contribution_to_date = project.approved_amount - project.actual_cost_amount

    def action_activate(self):
        self.write({"state": "active"})

    def action_finish(self):
        for project in self:
            if project.state != "active":
                raise UserError(_("Only in-progress projects can be completed."))
        self.write({"state": "done"})

    def action_open_certificates(self):
        self.ensure_one()
        action = self.env.ref("one_construction.action_one_construction_certificate").read()[0]
        action["domain"] = [("project_id", "=", self.id)]
        action["context"] = {"default_project_id": self.id}
        return action


class OneConstructionBOQLine(models.Model):
    _name = "one.construction.boq.line"
    _description = "ONE Construction Bill of Quantities Line"
    _order = "sequence, id"
    _check_company_auto = True

    sequence = fields.Integer(default=10)
    project_id = fields.Many2one("one.construction.project", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="project_id.company_id", store=True, readonly=True)
    phase = fields.Char(string="Work Phase")
    product_id = fields.Many2one("product.product", required=True,
                                  domain=[("type", "=", "service")], string="Service")
    name = fields.Char(required=True, string="Work Description")
    uom_id = fields.Many2one("uom.uom", required=True, string="Unit of Measure")
    quantity = fields.Float(required=True, default=1.0, digits="Product Unit of Measure")
    price_unit = fields.Monetary(required=True, currency_field="currency_id")
    currency_id = fields.Many2one(related="project_id.currency_id", readonly=True)
    price_subtotal = fields.Monetary(compute="_compute_subtotal", store=True, currency_field="currency_id")
    certificate_line_ids = fields.One2many("one.construction.certificate.line", "boq_line_id")
    approved_quantity = fields.Float(compute="_compute_approved_quantity", digits="Product Unit of Measure")
    remaining_quantity = fields.Float(compute="_compute_approved_quantity", digits="Product Unit of Measure")

    @api.onchange("product_id")
    def _onchange_product(self):
        if self.product_id:
            self.name = self.product_id.display_name
            self.uom_id = self.product_id.uom_id
            self.price_unit = self.product_id.lst_price

    @api.constrains("product_id", "quantity", "price_unit")
    def _check_work_item(self):
        for line in self:
            if line.product_id and line.product_id.type != "service":
                raise ValidationError(_("Construction work items must use service products."))
            if line.quantity <= 0 or line.price_unit < 0:
                raise ValidationError(_("Contract quantities must be positive and prices cannot be negative."))

    @api.depends("quantity", "price_unit")
    def _compute_subtotal(self):
        for line in self:
            line.price_subtotal = line.quantity * line.price_unit

    @api.depends("certificate_line_ids.quantity", "certificate_line_ids.certificate_id.state", "quantity")
    def _compute_approved_quantity(self):
        for line in self:
            line.approved_quantity = sum(
                line.certificate_line_ids.filtered(
                    lambda l: l.certificate_id.state in ("approved", "invoiced")
                ).mapped("quantity")
            )
            line.remaining_quantity = line.quantity - line.approved_quantity

    def write(self, vals):
        protected = {"product_id", "quantity", "price_unit", "uom_id", "project_id"}
        if protected.intersection(vals):
            for line in self:
                if any(c.certificate_id.state in ("approved", "invoiced") for c in line.certificate_line_ids):
                    raise UserError(_("An approved certificate exists. Reverse it before changing this contract item."))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if any(c.certificate_id.state in ("approved", "invoiced") for c in line.certificate_line_ids):
                raise UserError(_("Work items linked to approved certificates cannot be deleted."))
        return super().unlink()


class OneConstructionCertificate(models.Model):
    _name = "one.construction.certificate"
    _description = "ONE Construction Progress Certificate"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"
    _check_company_auto = True

    name = fields.Char(default=lambda self: _("New"), required=True, copy=False, readonly=True)
    project_id = fields.Many2one("one.construction.project", required=True, ondelete="restrict", tracking=True)
    partner_id = fields.Many2one(related="project_id.partner_id", readonly=True)
    company_id = fields.Many2one(related="project_id.company_id", store=True, readonly=True)
    currency_id = fields.Many2one(related="project_id.currency_id", readonly=True)
    date = fields.Date(default=fields.Date.context_today, required=True)
    state = fields.Selection([
        ("draft", "Draft"), ("submitted", "Submitted"),
        ("approved", "Approved"), ("invoiced", "Draft Invoice Created"),
        ("cancelled", "Cancelled"),
    ], default="draft", required=True, tracking=True)
    line_ids = fields.One2many("one.construction.certificate.line", "certificate_id", string="Executed Work")
    work_amount = fields.Monetary(compute="_compute_amounts", store=True, currency_field="currency_id")
    retention_percent = fields.Float(string="Retention %", digits=(5, 2),
        help="For tracking only in MVP. Automatic invoicing of retained amounts requires fiscal configuration.")
    advance_deduction = fields.Monetary(currency_field="currency_id",
        help="For tracking only in MVP. Automatic advance recovery needs configured accounting entries.")
    retention_amount = fields.Monetary(compute="_compute_amounts", currency_field="currency_id")
    net_amount = fields.Monetary(compute="_compute_amounts", currency_field="currency_id")
    invoice_id = fields.Many2one("account.move", readonly=True, copy=False)
    approved_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code("one.construction.certificate") or _("New")
        return super().create(vals_list)

    @api.depends("line_ids.subtotal", "retention_percent", "advance_deduction")
    def _compute_amounts(self):
        for cert in self:
            cert.work_amount = sum(cert.line_ids.mapped("subtotal"))
            cert.retention_amount = cert.work_amount * cert.retention_percent / 100.0
            cert.net_amount = cert.work_amount - cert.retention_amount - cert.advance_deduction

    @api.constrains("retention_percent", "advance_deduction")
    def _check_deductions(self):
        for cert in self:
            if not 0 <= cert.retention_percent <= 100:
                raise ValidationError(_("Retention must be between 0 and 100%."))
            if cert.advance_deduction < 0 or cert.net_amount < 0:
                raise ValidationError(_("Advance deductions cannot be negative or exceed the certificate balance."))

    def write(self, vals):
        protected = {"project_id", "line_ids", "retention_percent", "advance_deduction", "date"}
        if protected.intersection(vals):
            for cert in self:
                if cert.state in ("approved", "invoiced"):
                    raise UserError(_("An approved certificate cannot be edited."))
        return super().write(vals)

    def _check_executed_work(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Add executed work items before submitting a certificate."))
        if self.project_id.state != "active":
            raise UserError(_("The construction project must be in progress."))
        for line in self.line_ids:
            if line.boq_line_id.project_id != self.project_id:
                raise ValidationError(_("Work items must belong to this certificate's project."))
            if line.quantity <= 0:
                raise ValidationError(_("Executed quantities must be positive."))
            other_lines = self.env["one.construction.certificate.line"].search([
                ("boq_line_id", "=", line.boq_line_id.id),
                ("certificate_id", "!=", self.id),
                ("certificate_id.state", "in", ("approved", "invoiced")),
            ])
            committed = sum(other_lines.mapped("quantity"))
            rounding = line.boq_line_id.uom_id.rounding or 0.01
            if float_compare(committed + line.quantity, line.boq_line_id.quantity,
                             precision_rounding=rounding) > 0:
                raise ValidationError(_(
                    "Executed quantity exceeds remaining contract quantity for %(item)s."
                ) % {"item": line.boq_line_id.name})

    def action_submit(self):
        for cert in self:
            if cert.state != "draft":
                raise UserError(_("Only draft certificates may be submitted."))
            cert._check_executed_work()
        self.write({"state": "submitted"})

    def action_approve(self):
        if not (self.env.user.has_group("one_construction.group_construction_manager")
                or self.env.user.has_group("base.group_system")):
            raise AccessError(_("Only a construction manager can approve progress certificates."))
        for cert in self:
            if cert.state != "submitted":
                raise UserError(_("Only submitted certificates may be approved."))
            cert._check_executed_work()
            cert.write({"state": "approved", "approved_by_id": self.env.user.id,
                        "approved_at": fields.Datetime.now()})

    def action_create_draft_invoice(self):
        self.ensure_one()
        if not (self.env.user.has_group("account.group_account_invoice")
                or self.env.user.has_group("base.group_system")):
            raise AccessError(_("Invoicing access is required."))
        if self.state != "approved" or self.invoice_id:
            raise UserError(_("Only an approved, uninvoiced certificate may create an invoice."))
        if self.retention_percent or self.advance_deduction:
            raise UserError(_(
                "Retention and advance recovery require configured fiscal accounting. "
                "Settle these before automatic invoicing to avoid incorrect VAT or balances."
            ))
        invoice_lines = []
        for line in self.line_ids:
            item = line.boq_line_id
            taxes = item.product_id.taxes_id.filtered(lambda t: t.company_id == self.company_id)
            values = {
                "product_id": item.product_id.id,
                "name": item.name,
                "quantity": line.quantity,
                "product_uom_id": item.uom_id.id,
                "price_unit": line.price_unit,
                "tax_ids": [(6, 0, taxes.ids)],
            }
            if self.project_id.analytic_account_id:
                values["analytic_distribution"] = {str(self.project_id.analytic_account_id.id): 100.0}
            invoice_lines.append((0, 0, values))
        invoice = self.env["account.move"].with_company(self.company_id).create({
            "move_type": "out_invoice",
            "company_id": self.company_id.id,
            "partner_id": self.partner_id.id,
            "invoice_date": self.date,
            "invoice_origin": "%s / %s" % (self.project_id.code, self.name),
            "invoice_line_ids": invoice_lines,
        })
        self.write({"invoice_id": invoice.id, "state": "invoiced"})
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": invoice.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_cancel(self):
        for cert in self:
            if cert.state in ("approved", "invoiced"):
                raise UserError(_("Approved certificates require controlled reversal; cancellation is blocked."))
        self.write({"state": "cancelled"})


class OneConstructionCertificateLine(models.Model):
    _name = "one.construction.certificate.line"
    _description = "ONE Construction Certified Quantity"
    _order = "id"
    _sql_constraints = [
        ("unique_boq_in_certificate", "unique(certificate_id, boq_line_id)",
         "A contract work item can appear only once in a certificate."),
    ]

    certificate_id = fields.Many2one("one.construction.certificate", required=True, ondelete="cascade")
    boq_line_id = fields.Many2one("one.construction.boq.line", required=True, string="Contract Work Item")
    quantity = fields.Float(required=True, default=1.0, digits="Product Unit of Measure")
    price_unit = fields.Monetary(required=True, currency_field="currency_id")
    currency_id = fields.Many2one(related="certificate_id.currency_id", readonly=True)
    subtotal = fields.Monetary(compute="_compute_subtotal", store=True, currency_field="currency_id")
    uom_id = fields.Many2one(related="boq_line_id.uom_id", readonly=True)

    @api.onchange("boq_line_id")
    def _onchange_boq_line(self):
        if self.boq_line_id:
            self.price_unit = self.boq_line_id.price_unit

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("boq_line_id") and "price_unit" not in vals:
                item = self.env["one.construction.boq.line"].browse(vals["boq_line_id"])
                vals["price_unit"] = item.price_unit
        return super().create(vals_list)

    @api.depends("quantity", "price_unit")
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.quantity * line.price_unit

    @api.constrains("boq_line_id", "quantity", "price_unit", "certificate_id")
    def _check_line(self):
        for line in self:
            if line.quantity <= 0 or line.price_unit < 0:
                raise ValidationError(_("Certificate quantities must be positive and prices non-negative."))
            if line.certificate_id and line.boq_line_id.project_id != line.certificate_id.project_id:
                raise ValidationError(_("The work item belongs to another project."))
            if line.boq_line_id and line.price_unit != line.boq_line_id.price_unit:
                raise ValidationError(_("Certificate prices must match the agreed contract price."))

    def write(self, vals):
        for line in self:
            if line.certificate_id.state not in ("draft", "submitted"):
                raise UserError(_("Approved certificate lines cannot be edited."))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if line.certificate_id.state not in ("draft", "submitted"):
                raise UserError(_("Approved certificate lines cannot be deleted."))
        return super().unlink()


class ResCompany(models.Model):
    _inherit = "res.company"

    one_construction_default_retention = fields.Float(string="Default Construction Retention %", digits=(5, 2))


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    one_construction_default_retention = fields.Float(
        related="company_id.one_construction_default_retention", readonly=False,
        string="Default Construction Retention %", digits=(5, 2),
    )
