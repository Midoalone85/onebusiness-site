from odoo import _, api, fields, models
from odoo.tools.misc import format_amount


class ResCompany(models.Model):
    _inherit = "res.company"

    @api.model
    def one_get_dashboard_summary(self):
        """Return dashboard counters in one RPC to avoid browser-side request storms."""
        today = fields.Date.context_today(self)

        unpaid_invoice_domain = [
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ("not_paid", "partial")),
        ]

        counters = {
            "contacts": ("res.partner", []),
            "products": ("product.template", []),
            "warehouses": ("stock.warehouse", []),
            "sales": ("sale.order", []),
            "purchases": ("purchase.order", []),
            "transfers": ("stock.picking", []),
            "invoices": ("account.move", []),
            "manufacturing": ("mrp.production", []),
            "opportunities": ("crm.lead", []),
            "employees": ("hr.employee", []),
            "saudiProfiles": ("one.saudi.profile", []),
            "subscriptions": ("one.subscription", []),
            "activeSubscriptions": ("one.subscription", [("status", "=", "active")]),
            "unpaidInvoices": ("account.move", unpaid_invoice_domain),
            "overdueInvoices": (
                "account.move",
                unpaid_invoice_domain + [("invoice_date_due", "<", today)],
            ),
            "draftQuotations": ("sale.order", [("state", "in", ("draft", "sent"))]),
            "pendingPurchases": (
                "purchase.order",
                [("state", "in", ("draft", "sent", "to approve"))],
            ),
            "pendingReceipts": (
                "stock.picking",
                [
                    ("picking_type_code", "=", "incoming"),
                    ("state", "not in", ("done", "cancel")),
                ],
            ),
            "pendingApprovals": (
                "one.approval.request",
                [("state", "=", "submitted")],
            ),
        }

        result = {}
        for key, (model_name, domain) in counters.items():
            try:
                result[key] = self.env[model_name].search_count(domain)
            except Exception:
                result[key] = False

        pos_access = (
            self.env.user.has_group("point_of_sale.group_pos_user")
            or self.env.user.has_group("point_of_sale.group_pos_manager")
        )
        result["posAccess"] = pos_access
        result["adminAccess"] = self.env.user.has_group("base.group_system")
        if pos_access:
            try:
                result["posConfigs"] = self.env["pos.config"].search_count([])
            except Exception:
                result["posConfigs"] = False
        else:
            result["posConfigs"] = False

        return result

    @api.model
    def one_get_pulse(self):
        company = self.env.company
        currency = company.currency_id
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)

        MoveLine = self.env["account.move.line"]
        receivable_domain = [
            ("company_id", "=", company.id),
            ("move_id.state", "=", "posted"),
            ("account_id.account_type", "=", "asset_receivable"),
            ("amount_residual", "!=", 0),
        ]
        payable_domain = [
            ("company_id", "=", company.id),
            ("move_id.state", "=", "posted"),
            ("account_id.account_type", "=", "liability_payable"),
            ("amount_residual", "!=", 0),
        ]

        receivable_group = MoveLine._read_group(
            receivable_domain,
            aggregates=["amount_residual:sum"],
        )
        payable_group = MoveLine._read_group(
            payable_domain,
            aggregates=["amount_residual:sum"],
        )
        overdue_group = MoveLine._read_group(
            receivable_domain + [("date_maturity", "<", today)],
            aggregates=["amount_residual:sum"],
        )

        receivables = (receivable_group[0][0] if receivable_group else 0.0) or 0.0
        payables = -((payable_group[0][0] if payable_group else 0.0) or 0.0)
        overdue = (overdue_group[0][0] if overdue_group else 0.0) or 0.0

        revenue_group = MoveLine._read_group(
            [
                ("company_id", "=", company.id),
                ("move_id.state", "=", "posted"),
                ("date", ">=", month_start),
                ("date", "<=", today),
                ("account_id.account_type", "in", ("income", "income_other")),
            ],
            aggregates=["balance:sum"],
        )
        month_revenue = -((revenue_group[0][0] if revenue_group else 0.0) or 0.0)

        profit_group = MoveLine._read_group(
            [
                ("company_id", "=", company.id),
                ("move_id.state", "=", "posted"),
                ("date", ">=", month_start),
                ("date", "<=", today),
                (
                    "account_id.account_type",
                    "in",
                    (
                        "income",
                        "income_other",
                        "expense_direct_cost",
                        "expense",
                        "expense_other",
                        "expense_depreciation",
                    ),
                ),
            ],
            aggregates=["balance:sum"],
        )
        month_profit = -((profit_group[0][0] if profit_group else 0.0) or 0.0)

        return {
            "receivables": format_amount(self.env, receivables, currency),
            "overdue": format_amount(self.env, overdue, currency),
            "payables": format_amount(self.env, payables, currency),
            "month_revenue": format_amount(self.env, month_revenue, currency),
            "month_profit": format_amount(self.env, month_profit, currency),
            "currency": currency.name,
        }


    @api.model
    def one_get_business_radar(self):
        company = self.env.company
        currency = company.currency_id
        today = fields.Date.context_today(self)

        Move = self.env["account.move"]
        overdue_domain = [
            ("company_id", "=", company.id),
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ("not_paid", "partial")),
            ("invoice_date_due", "<", today),
        ]
        overdue_count = Move.search_count(overdue_domain)
        overdue_group = Move._read_group(
            overdue_domain,
            aggregates=["amount_residual_signed:sum"],
        )
        overdue_amount = abs((overdue_group[0][0] if overdue_group else 0.0) or 0.0)

        pending_receipts = self.env["stock.picking"].search_count([
            ("company_id", "=", company.id),
            ("picking_type_code", "=", "incoming"),
            ("state", "not in", ("done", "cancel")),
        ])
        pending_approvals = self.env["one.approval.request"].search_count([
            ("company_id", "=", company.id),
            ("state", "=", "submitted"),
        ])

        Sale = self.env["sale.order"]
        negative_margin_orders = 0
        if "margin" in Sale._fields:
            negative_margin_orders = Sale.search_count([
                ("company_id", "=", company.id),
                ("state", "=", "sale"),
                ("margin", "<", 0),
            ])

        compliance = self.env["one.saudi.profile"].one_get_readiness_summary()

        score = 100
        alerts = []

        if overdue_count:
            score -= min(30, 8 + overdue_count * 2)
            alerts.append({
                "key": "overdue",
                "severity": "danger",
                "title": _("Overdue receivables"),
                "detail": _("%(count)s invoice(s) · %(amount)s") % {
                    "count": overdue_count,
                    "amount": format_amount(self.env, overdue_amount, currency),
                },
                "action": "one_ui.action_one_accounting",
            })

        if negative_margin_orders:
            score -= min(20, negative_margin_orders * 5)
            alerts.append({
                "key": "margin",
                "severity": "danger",
                "title": _("Negative-margin sales"),
                "detail": _("%(count)s confirmed order(s)") % {"count": negative_margin_orders},
                "action": "one_ui.action_one_sales",
            })

        if pending_approvals:
            score -= min(10, pending_approvals * 2)
            alerts.append({
                "key": "approvals",
                "severity": "warning",
                "title": _("Approvals waiting"),
                "detail": _("%(count)s request(s) need a decision") % {"count": pending_approvals},
                "action": "one_ui.action_one_approvals",
            })

        if pending_receipts:
            score -= min(8, pending_receipts)
            alerts.append({
                "key": "receipts",
                "severity": "info",
                "title": _("Incoming stock pending"),
                "detail": _("%(count)s receipt(s) still open") % {"count": pending_receipts},
                "action": "one_ui.action_one_inventory",
            })

        if not compliance["profiles"]:
            score -= 15
            alerts.append({
                "key": "compliance_missing",
                "severity": "warning",
                "title": _("Saudi compliance setup"),
                "detail": _("Company ZATCA and National Address profile is not configured yet"),
                "action": "one_ui.action_one_saudi_profile",
            })
        elif compliance["ready"] < compliance["profiles"]:
            score -= 12
            alerts.append({
                "key": "compliance_incomplete",
                "severity": "warning",
                "title": _("Saudi compliance incomplete"),
                "detail": _("%(ready)s of %(profiles)s profile(s) are ready") % {
                    "ready": compliance["ready"],
                    "profiles": compliance["profiles"],
                },
                "action": "one_ui.action_one_saudi_profile",
            })

        score = max(0, min(100, score))
        if score >= 85:
            status = _("Healthy")
        elif score >= 65:
            status = _("Watch")
        else:
            status = _("Action needed")

        return {
            "score": score,
            "status": status,
            "alerts": alerts[:5],
            "overdue_count": overdue_count,
            "negative_margin_orders": negative_margin_orders,
            "pending_approvals": pending_approvals,
            "pending_receipts": pending_receipts,
        }
