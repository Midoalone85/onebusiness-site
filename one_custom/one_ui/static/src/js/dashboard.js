/** @odoo-module **/

import { Component, onMounted, useState } from "@odoo/owl";
import { location } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";

export class OneDashboard extends Component {
    static template = "one_ui.OneDashboard";

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");
        this.state = useState({
            lang: user.context.lang || "en_US",
            contacts: "…",
            sales: "…",
            purchases: "…",
            transfers: "…",
            invoices: "…",
            manufacturing: "…",
            opportunities: "…",
            employees: "…",
            saudiProfiles: "…",
            zatcaConnected: "…",
            zatcaReady: "…",
            subscriptions: "…",
            activeSubscriptions: "…",
            unpaidInvoices: "…",
            overdueInvoices: "…",
            draftQuotations: "…",
            pendingPurchases: "…",
            pendingReceipts: "…",
            pendingApprovals: "…",
            posAccess: false,
            posConfigs: "…",
            pulseReceivables: "…",
            pulseOverdue: "…",
            pulsePayables: "…",
            pulseMonthRevenue: "…",
            pulseMonthProfit: "…",
        });

        onMounted(() => {
            void this.loadDashboardData();
        });
    }

    async loadDashboardData() {
            const now = new Date();
            const today = [
                now.getFullYear(),
                String(now.getMonth() + 1).padStart(2, "0"),
                String(now.getDate()).padStart(2, "0"),
            ].join("-");

            const unpaidInvoiceDomain = [
                ["move_type", "=", "out_invoice"],
                ["state", "=", "posted"],
                ["payment_state", "in", ["not_paid", "partial"]],
            ];

            try {
                const [isPosUser, isPosManager] = await Promise.all([
                    user.hasGroup("point_of_sale.group_pos_user"),
                    user.hasGroup("point_of_sale.group_pos_manager"),
                ]);
                this.state.posAccess = isPosUser || isPosManager;
            } catch {
                this.state.posAccess = false;
            }

            const counters = {
                contacts: ["res.partner", []],
                sales: ["sale.order", []],
                purchases: ["purchase.order", []],
                transfers: ["stock.picking", []],
                invoices: ["account.move", []],
                manufacturing: ["mrp.production", []],
                opportunities: ["crm.lead", []],
                employees: ["hr.employee", []],
                saudiProfiles: ["one.saudi.profile", []],
                subscriptions: ["one.subscription", []],
                activeSubscriptions: ["one.subscription", [["status", "=", "active"]]],
                unpaidInvoices: ["account.move", unpaidInvoiceDomain],
                overdueInvoices: [
                    "account.move",
                    [...unpaidInvoiceDomain, ["invoice_date_due", "<", today]],
                ],
                draftQuotations: ["sale.order", [["state", "in", ["draft", "sent"]]]],
                pendingPurchases: [
                    "purchase.order",
                    [["state", "in", ["draft", "sent", "to approve"]]],
                ],
                pendingReceipts: [
                    "stock.picking",
                    [
                        ["picking_type_code", "=", "incoming"],
                        ["state", "not in", ["done", "cancel"]],
                    ],
                ],
                pendingApprovals: [
                    "one.approval.request",
                    [["state", "=", "submitted"]],
                ],
                ...(this.state.posAccess ? { posConfigs: ["pos.config", []] } : {}),
            };

            await Promise.all(
                Object.entries(counters).map(async ([key, [model, domain]]) => {
                    try {
                        this.state[key] = await this.orm.searchCount(model, domain);
                    } catch {
                        this.state[key] = "—";
                    }
                })
            );

            try {
                const pulse = await this.orm.call("res.company", "one_get_pulse", []);
                this.state.pulseReceivables = pulse.receivables;
                this.state.pulseOverdue = pulse.overdue;
                this.state.pulsePayables = pulse.payables;
                this.state.pulseMonthRevenue = pulse.month_revenue;
                this.state.pulseMonthProfit = pulse.month_profit;
            } catch {
                this.state.pulseReceivables = "—";
                this.state.pulseOverdue = "—";
                this.state.pulsePayables = "—";
                this.state.pulseMonthRevenue = "—";
                this.state.pulseMonthProfit = "—";
            }

            try {
                const zatca = await this.orm.call("one.saudi.profile", "one_get_readiness_summary", []);
                this.state.saudiProfiles = zatca.profiles;
                this.state.zatcaReady = zatca.ready;
                this.state.zatcaConnected = zatca.connected;
            } catch {
                this.state.zatcaReady = "—";
                this.state.zatcaConnected = "—";
            }
    }

    async switchLanguage(code) {
        if (this.state.lang === code) {
            return;
        }

        await rpc("/web/dataset/call_kw/res.users/one_switch_language", {
            model: "res.users",
            method: "one_switch_language",
            args: [[user.userId], code],
            kwargs: { context: user.context },
        });

        location.reload();
    }

    open(xmlId) {
        return this.action.doAction(xmlId);
    }
}

registry.category("actions").add("one_ui.dashboard", OneDashboard);
