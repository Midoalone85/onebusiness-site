from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class OneApprovalRequest(models.Model):
    _name = "one.approval.request"
    _description = "ONE ERP Approval Request"
    _order = "requested_at desc, id desc"

    name = fields.Char(
        string="Request Number",
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _("New"),
    )
    approval_type = fields.Selection(
        [
            ("purchase", "Purchase"),
            ("payment", "Payment"),
            ("expense", "Expense"),
            ("discount", "Discount"),
            ("return", "Return"),
            ("other", "Other"),
        ],
        string="Approval Type",
        required=True,
        default="purchase",
    )
    description = fields.Text(string="Description", required=True)
    amount = fields.Monetary(string="Amount")
    currency_id = fields.Many2one(
        "res.currency",
        required=True,
        default=lambda self: self.env.company.currency_id,
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )
    requester_id = fields.Many2one(
        "res.users",
        string="Requested By",
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
    )
    approver_id = fields.Many2one(
        "res.users",
        string="Approver",
        required=True,
        domain="[('share', '=', False)]",
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("submitted", "Pending Approval"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("cancelled", "Cancelled"),
        ],
        string="Status",
        required=True,
        default="draft",
        copy=False,
    )
    requested_at = fields.Datetime(string="Requested At", readonly=True, copy=False)
    decided_at = fields.Datetime(string="Decision At", readonly=True, copy=False)
    decision_note = fields.Text(string="Decision Note", copy=False)
    document_reference = fields.Char(string="Document Reference")
    source_url = fields.Char(string="Source Link")

    @api.model_create_multi
    def create(self, vals_list):
        sequence = self.env["ir.sequence"]
        is_system = self.env.user.has_group("base.group_system")
        allowed_company_ids = set(self.env.companies.ids)

        for vals in vals_list:
            company_id = vals.get("company_id") or self.env.company.id
            if not is_system and company_id not in allowed_company_ids:
                raise AccessError(_("You cannot create approval requests for this company."))

            company = self.env["res.company"].browse(company_id)
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = sequence.next_by_code("one.approval.request") or _("New")

            if not is_system:
                vals["requester_id"] = self.env.user.id
            else:
                vals.setdefault("requester_id", self.env.user.id)

            vals["company_id"] = company.id
            vals["currency_id"] = company.currency_id.id

        return super().create(vals_list)

    def _is_system_manager(self):
        return self.env.user.has_group("base.group_system")

    def _check_request_owner(self):
        for request in self:
            if request.requester_id != self.env.user and not request._is_system_manager():
                raise AccessError(_("Only the requester can perform this action."))

    def _check_assigned_approver(self):
        for request in self:
            if request.approver_id != self.env.user and not request._is_system_manager():
                raise AccessError(_("Only the assigned approver can perform this action."))

    def action_submit(self):
        self._check_request_owner()
        for request in self:
            if request.state != "draft":
                raise UserError(_("Only draft requests can be submitted."))
            if request.approver_id == request.requester_id and not request._is_system_manager():
                raise UserError(_("The requester and approver must be different users."))
            request.write(
                {
                    "state": "submitted",
                    "requested_at": fields.Datetime.now(),
                    "decided_at": False,
                    "decision_note": False,
                }
            )
        return True

    def action_approve(self):
        self._check_assigned_approver()
        for request in self:
            if request.state != "submitted":
                raise UserError(_("Only pending requests can be approved."))
            request.write(
                {
                    "state": "approved",
                    "decided_at": fields.Datetime.now(),
                }
            )
        return True

    def action_reject(self):
        self._check_assigned_approver()
        for request in self:
            if request.state != "submitted":
                raise UserError(_("Only pending requests can be rejected."))
            if not request.decision_note:
                raise UserError(_("Add a decision note before rejecting a request."))
            request.write(
                {
                    "state": "rejected",
                    "decided_at": fields.Datetime.now(),
                }
            )
        return True

    def action_cancel(self):
        self._check_request_owner()
        for request in self:
            if request.state not in ("draft", "submitted"):
                raise UserError(_("Only draft or pending requests can be cancelled."))
            request.write({"state": "cancelled"})
        return True

    def action_reset_draft(self):
        self._check_request_owner()
        for request in self:
            if request.state not in ("rejected", "cancelled"):
                raise UserError(_("Only rejected or cancelled requests can return to draft."))
            request.write(
                {
                    "state": "draft",
                    "requested_at": False,
                    "decided_at": False,
                }
            )
        return True

    def write(self, vals):
        if self._is_system_manager():
            return super().write(vals)

        protected = {"name", "requester_id", "company_id", "currency_id"}
        if protected.intersection(vals):
            raise AccessError(_("These approval fields cannot be changed directly."))

        for request in self:
            is_requester = request.requester_id == self.env.user
            is_approver = request.approver_id == self.env.user

            if not is_requester and not is_approver:
                raise AccessError(_("You do not have permission to edit this approval request."))

            requested_state = vals.get("state")
            if requested_state and requested_state != request.state:
                allowed_transition = False
                if is_requester:
                    allowed_transition = (
                        (request.state == "draft" and requested_state == "submitted")
                        or (request.state in ("draft", "submitted") and requested_state == "cancelled")
                        or (request.state in ("rejected", "cancelled") and requested_state == "draft")
                    )
                if is_approver:
                    allowed_transition = allowed_transition or (
                        request.state == "submitted"
                        and requested_state in ("approved", "rejected")
                    )
                if not allowed_transition:
                    raise AccessError(_("This approval status change is not allowed."))

            state_fields = {"state", "requested_at", "decided_at", "decision_note"}
            business_fields = set(vals) - state_fields

            if business_fields and not (is_requester and request.state == "draft"):
                raise AccessError(_("Only the requester can edit a draft approval request."))

            if "decision_note" in vals and not (
                is_approver and request.state == "submitted"
            ):
                raise AccessError(_("Only the assigned approver can edit the decision note."))

        return super().write(vals)

    def unlink(self):
        for request in self:
            if request.state != "draft" and not request._is_system_manager():
                raise UserError(_("Only draft requests can be deleted."))
        return super().unlink()
