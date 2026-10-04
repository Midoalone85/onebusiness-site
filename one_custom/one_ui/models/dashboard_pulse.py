import os

from odoo import _, api, fields, models
from odoo.tools.misc import format_amount


class ResCompany(models.Model):
    _inherit = "res.company"

    @api.model
    def one_get_dashboard_summary(self):
        """Return current-company dashboard counters in one RPC."""
        today = fields.Date.context_today(self)
        company = self.env.company
        strict_company = [("company_id", "=", company.id)]
        shared_master = (
            strict_company
            if self.env.user.one_is_trial
            else [("company_id", "in", [False, company.id])]
        )

        unpaid_invoice_domain = strict_company + [
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ("not_paid", "partial")),
        ]

        counters = {
            "contacts": ("res.partner", shared_master),
            "products": ("product.template", shared_master),
            "warehouses": ("stock.warehouse", strict_company),
            "sales": ("sale.order", strict_company),
            "purchases": ("purchase.order", strict_company),
            "transfers": ("stock.picking", strict_company),
            "invoices": ("account.move", strict_company),
            "manufacturing": ("mrp.production", strict_company),
            "opportunities": ("crm.lead", strict_company),
            "employees": ("hr.employee", strict_company),
            "saudiProfiles": ("one.saudi.profile", strict_company),
            "subscriptions": ("one.subscription", []),
            "activeSubscriptions": ("one.subscription", [("status", "=", "active")]),
            "unpaidInvoices": ("account.move", unpaid_invoice_domain),
            "overdueInvoices": (
                "account.move",
                unpaid_invoice_domain + [("invoice_date_due", "<", today)],
            ),
            "draftQuotations": (
                "sale.order",
                strict_company + [("state", "in", ("draft", "sent"))],
            ),
            "pendingPurchases": (
                "purchase.order",
                strict_company + [("state", "in", ("draft", "sent", "to approve"))],
            ),
            "pendingReceipts": (
                "stock.picking",
                strict_company + [
                    ("picking_type_code", "=", "incoming"),
                    ("state", "not in", ("done", "cancel")),
                ],
            ),
            "pendingApprovals": (
                "one.approval.request",
                strict_company + [("state", "=", "submitted")],
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
        result["persistentDatabase"] = bool(
            os.environ.get("DATABASE_URL") or os.environ.get("ONE_DB_HOST")
        )
        if pos_access:
            try:
                result["posConfigs"] = self.env["pos.config"].search_count(strict_company)
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


    @api.model
    def one_get_decision_brief(self, radar=None):
        """Turn current company signals into three concrete management priorities."""
        company = self.env.company
        radar = radar or self.one_get_business_radar()

        guidance = {
            "overdue": {
                "title": _("Accelerate collections"),
                "detail": _("Start with the highest overdue balances and assign a follow-up date."),
                "action": "one_ui.action_one_accounting",
                "severity": "danger",
            },
            "margin": {
                "title": _("Protect gross margin"),
                "detail": _("Review negative-margin sales before approving more pricing or orders."),
                "action": "one_ui.action_one_sales",
                "severity": "danger",
            },
            "approvals": {
                "title": _("Clear decision bottlenecks"),
                "detail": _("Resolve pending approvals that can block purchasing or operations."),
                "action": "one_ui.action_one_approvals",
                "severity": "warning",
            },
            "receipts": {
                "title": _("Unblock incoming stock"),
                "detail": _("Review open receipts and identify anything delaying supply or delivery."),
                "action": "one_ui.action_one_inventory",
                "severity": "info",
            },
            "compliance_missing": {
                "title": _("Complete Saudi compliance setup"),
                "detail": _("Finish the ZATCA and National Address profile before relying on e-invoicing."),
                "action": "one_ui.action_one_saudi_profile",
                "severity": "warning",
            },
            "compliance_incomplete": {
                "title": _("Close compliance gaps"),
                "detail": _("Complete the remaining Saudi readiness items in the compliance center."),
                "action": "one_ui.action_one_saudi_profile",
                "severity": "warning",
            },
        }

        items = []
        used = set()
        for alert in radar.get("alerts", []):
            key = alert.get("key")
            item = guidance.get(key)
            if item and key not in used:
                items.append({"key": key, **item})
                used.add(key)
            if len(items) >= 3:
                break

        if len(items) < 3:
            quotations = self.env["sale.order"].search_count([
                ("company_id", "=", company.id),
                ("state", "in", ("draft", "sent")),
            ])
            if quotations:
                items.append({
                    "key": "quotations",
                    "title": _("Move quotations forward"),
                    "detail": _("%(count)s open quotation(s) need a next step or follow-up.") % {"count": quotations},
                    "action": "one_ui.action_one_sales",
                    "severity": "info",
                })

        if len(items) < 3:
            purchases = self.env["purchase.order"].search_count([
                ("company_id", "=", company.id),
                ("state", "in", ("draft", "sent", "to approve")),
            ])
            if purchases:
                items.append({
                    "key": "purchases",
                    "title": _("Review pending purchases"),
                    "detail": _("%(count)s purchase order(s) are still waiting in the pipeline.") % {"count": purchases},
                    "action": "one_ui.action_one_purchase",
                    "severity": "info",
                })

        if len(items) < 3:
            unpaid = self.env["account.move"].search_count([
                ("company_id", "=", company.id),
                ("move_type", "=", "out_invoice"),
                ("state", "=", "posted"),
                ("payment_state", "in", ("not_paid", "partial")),
            ])
            if unpaid:
                items.append({
                    "key": "unpaid",
                    "title": _("Prioritize unpaid invoices"),
                    "detail": _("%(count)s unpaid invoice(s) should be ranked by value and due date.") % {"count": unpaid},
                    "action": "one_ui.action_one_accounting",
                    "severity": "info",
                })

        if len(items) < 3:
            items.append({
                "key": "momentum",
                "title": _("Maintain commercial momentum"),
                "detail": _("No critical signal is dominating; focus on CRM opportunities and open quotations."),
                "action": "one_ui.action_one_crm",
                "severity": "success",
            })

        return {
            "score": radar["score"],
            "status": radar["status"],
            "items": items[:3],
        }


    @api.model
    def one_get_dashboard_payload(self):
        """Return the ONE home-screen payload in one RPC for a faster first paint."""
        radar = self.one_get_business_radar()
        return {
            "summary": self.one_get_dashboard_summary(),
            "pulse": self.one_get_pulse(),
            "zatca": self.env["one.saudi.profile"].one_get_readiness_summary(),
            "radar": radar,
            "decision": self.one_get_decision_brief(radar=radar),
        }
