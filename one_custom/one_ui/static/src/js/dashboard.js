/** @odoo-module **/

import { Component, onMounted, proxy } from "@odoo/owl";
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
        this.state = proxy({
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
            adminAccess: false,
            posConfigs: "…",
            products: "…",
            warehouses: "…",
            pulseReceivables: "…",
            pulseOverdue: "…",
            pulsePayables: "…",
            pulseMonthRevenue: "…",
            pulseMonthProfit: "…",
            businessHealthScore: "…",
            businessHealthStatus: "…",
            radarAlerts: [],
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

            try {
                const summary = await this.orm.call("res.company", "one_get_dashboard_summary", []);
                const counterKeys = [
                    "contacts", "products", "warehouses", "sales", "purchases",
                    "transfers", "invoices", "manufacturing", "opportunities",
                    "employees", "saudiProfiles", "subscriptions",
                    "activeSubscriptions", "unpaidInvoices", "overdueInvoices",
                    "draftQuotations", "pendingPurchases", "pendingReceipts",
                    "pendingApprovals", "posConfigs",
                ];
                for (const key of counterKeys) {
                    const value = summary[key];
                    this.state[key] = value === false || value === undefined ? "—" : value;
                }
                this.state.posAccess = Boolean(summary.posAccess);
                this.state.adminAccess = Boolean(summary.adminAccess);
            } catch {
                this.state.posAccess = false;
                this.state.adminAccess = false;
            }

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

            try {
                const radar = await this.orm.call("res.company", "one_get_business_radar", []);
                this.state.businessHealthScore = radar.score;
                this.state.businessHealthStatus = radar.status;
                this.state.radarAlerts = radar.alerts || [];
            } catch {
                this.state.businessHealthScore = "—";
                this.state.businessHealthStatus = "Unavailable";
                this.state.radarAlerts = [];
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


export class OneControlCenter extends Component {
    static template = "one_ui.OneControlCenter";

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");
        this.state = proxy({
            companies: "…",
            users: "…",
            warehouses: "…",
            taxes: "…",
            currencies: "…",
            languages: "…",
            saudiProfiles: "…",
            plans: "…",
        });

        onMounted(() => {
            void this.loadControlCenter();
        });
    }

    async loadControlCenter() {
        const counters = {
            companies: ["res.company", []],
            users: ["res.users", [["active", "=", true], ["share", "=", false]]],
            warehouses: ["stock.warehouse", []],
            taxes: ["account.tax", [["active", "=", true]]],
            currencies: ["res.currency", [["active", "=", true]]],
            languages: ["res.lang", [["active", "=", true]]],
            saudiProfiles: ["one.saudi.profile", []],
            plans: ["one.subscription.plan", [["active", "=", true]]],
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
    }

    open(xmlId) {
        return this.action.doAction(xmlId);
    }
}

registry.category("actions").add("one_ui.control_center", OneControlCenter);
