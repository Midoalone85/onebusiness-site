/** @odoo-module **/

import { Component, onMounted, proxy } from "@odoo/owl";
import { location } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";

export class OneDashboard extends Component {
    static template = "one_ui.OneDashboard";

    setup() {
        this.action = useService("action");
        this.notification = useService("notification");
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
            persistentDatabase: true,
            posConfigs: "…",
            products: "…",
            warehouses: "…",
            pulseReceivables: "…",
            pulseOverdue: "…",
            pulsePayables: "…",
            pulseMonthRevenue: "…",
            pulseMonthProfit: "…",
            businessHealthScore: "…",
            businessHealthStatus: _t("Unavailable"),
            radarAlerts: [],
            decisionItems: [],
        });

        onMounted(() => {
            void this.loadDashboardData();
        });
    }

    async loadDashboardData() {
        try {
            const payload = await this.orm.call("res.company", "one_get_dashboard_payload", []);
            const summary = payload.summary || {};
            const pulse = payload.pulse || {};
            const zatca = payload.zatca || {};
            const radar = payload.radar || {};
            const decision = payload.decision || {};

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
            this.state.persistentDatabase = summary.persistentDatabase !== false;

            this.state.pulseReceivables = pulse.receivables || "—";
            this.state.pulseOverdue = pulse.overdue || "—";
            this.state.pulsePayables = pulse.payables || "—";
            this.state.pulseMonthRevenue = pulse.month_revenue || "—";
            this.state.pulseMonthProfit = pulse.month_profit || "—";

            this.state.saudiProfiles = zatca.profiles ?? this.state.saudiProfiles;
            this.state.zatcaReady = zatca.ready ?? "—";
            this.state.zatcaConnected = zatca.connected ?? "—";

            this.state.businessHealthScore = radar.score ?? "—";
            this.state.businessHealthStatus = radar.status || _t("Unavailable");
            this.state.radarAlerts = radar.alerts || [];
            this.state.decisionItems = decision.items || [];
        } catch {
            this.state.posAccess = false;
            this.state.adminAccess = false;
            this.state.persistentDatabase = true;
            this.state.pulseReceivables = "—";
            this.state.pulseOverdue = "—";
            this.state.pulsePayables = "—";
            this.state.pulseMonthRevenue = "—";
            this.state.pulseMonthProfit = "—";
            this.state.zatcaReady = "—";
            this.state.zatcaConnected = "—";
            this.state.businessHealthScore = "—";
            this.state.businessHealthStatus = _t("Unavailable");
            this.state.radarAlerts = [];
            this.state.decisionItems = [];
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

    async open(xmlId) {
        if (!xmlId) {
            return;
        }
        try {
            return await this.action.doAction(xmlId, { stackPosition: "replaceCurrentAction" });
        } catch (error) {
            console.error("ONE ERP navigation failed", xmlId, error);
            this.notification.add(_t("Opening workspace…"), {
                type: "warning",
                title: _t("ONE ERP"),
            });
            location.href = `/odoo/action-${encodeURIComponent(xmlId)}`;
        }
    }

    openAction(event) {
        event.preventDefault();
        event.stopPropagation();
        const xmlId = event.currentTarget?.dataset?.oneAction;
        return this.open(xmlId);
    }
}

registry.category("actions").add("one_ui.dashboard", OneDashboard);


export class OneControlCenter extends Component {
    static template = "one_ui.OneControlCenter";

    setup() {
        this.action = useService("action");
        this.notification = useService("notification");
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

    async open(xmlId) {
        if (!xmlId) {
            return;
        }
        try {
            return await this.action.doAction(xmlId, { stackPosition: "replaceCurrentAction" });
        } catch (error) {
            console.error("ONE ERP navigation failed", xmlId, error);
            this.notification.add(_t("Opening workspace…"), {
                type: "warning",
                title: _t("ONE ERP"),
            });
            location.href = `/odoo/action-${encodeURIComponent(xmlId)}`;
        }
    }

    openAction(event) {
        event.preventDefault();
        event.stopPropagation();
        const xmlId = event.currentTarget?.dataset?.oneAction;
        return this.open(xmlId);
    }
}

registry.category("actions").add("one_ui.control_center", OneControlCenter);

// ONE ERP mobile cache generation 2026-10-07-1742
