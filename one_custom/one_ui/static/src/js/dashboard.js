/** @odoo-module **/

import { Component, onMounted, onWillStart, useState } from "@odoo/owl";
import { location } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";

/**
 * ONE ERP V3 dashboard: focused home screen, searchable application library,
 * language-independent Arabic/English labels and progressive metric loading.
 * Every tile reuses an existing action; this file never writes accounting data.
 */
export class OneDashboard extends Component {
    static template = "one_ui.OneDashboard";

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");
        this.state = useState({
            lang: user.context.lang || "en_US",
            showAllApps: false,
            appQuery: "",
            category: "all",
            access: {account: false, admin: false, pos: false},
            sales: "…", purchases: "…", transfers: "…", invoices: "…",
            opportunities: "…", employees: "…", pendingApprovals: "…",
            overdueInvoices: "…", unpaidInvoices: "…",
            draftQuotations: "…", pendingPurchases: "…",
            pendingReceipts: "…", pulseReceivables: "…",
            pulseOverdue: "…", pulsePayables: "…",
            pulseMonthRevenue: "…", pulseMonthProfit: "…",
            saudiProfiles: "…", zatcaConnected: "…", zatcaReady: "…",
        });

        // The application library points only to actions declared by one_ui.
        // Show finance, administration and POS applications to their groups.
        this.apps = [
            {id:"accounting", ar:"المحاسبة", en:"Accounting", descriptionAr:"القيود والفواتير", descriptionEn:"Journals and invoices", icon:"fa fa-calculator", action:"one_ui.action_one_accounting", category:"finance", group:"account", primary:true},
            {id:"sales", ar:"المبيعات", en:"Sales", descriptionAr:"العروض وأوامر البيع", descriptionEn:"Quotations and orders", icon:"fa fa-line-chart", action:"one_ui.action_one_sales", category:"operations", primary:true},
            {id:"purchases", ar:"المشتريات", en:"Purchases", descriptionAr:"الموردون والطلبات", descriptionEn:"Suppliers and orders", icon:"fa fa-shopping-cart", action:"one_ui.action_one_purchase", category:"operations", primary:true},
            {id:"inventory", ar:"المخزون", en:"Inventory", descriptionAr:"الأصناف والتحويلات", descriptionEn:"Stock and transfers", icon:"fa fa-cubes", action:"one_ui.action_one_inventory", category:"operations", primary:true},
            {id:"crm", ar:"إدارة العملاء", en:"CRM", descriptionAr:"الفرص والعملاء", descriptionEn:"Opportunities and leads", icon:"fa fa-bullseye", action:"one_ui.action_one_crm", category:"operations", primary:true},
            {id:"mrp", ar:"التصنيع", en:"Manufacturing", descriptionAr:"أوامر التصنيع", descriptionEn:"Production orders", icon:"fa fa-industry", action:"one_ui.action_one_mrp", category:"operations", primary:true},
            {id:"hr", ar:"الموارد البشرية", en:"Human Resources", descriptionAr:"الموظفون والفِرق", descriptionEn:"Employees and teams", icon:"fa fa-users", action:"one_ui.action_one_hr", category:"people", primary:true},
            {id:"contacts", ar:"جهات الاتصال", en:"Contacts", descriptionAr:"العملاء والموردون", descriptionEn:"Customers and vendors", icon:"fa fa-address-book", action:"one_ui.action_one_contacts", category:"people", primary:true},
            {id:"customer-invoices", ar:"فواتير العملاء", en:"Customer Invoices", descriptionAr:"الفواتير والتحصيل", descriptionEn:"Sales invoices", icon:"fa fa-file-text-o", action:"one_ui.action_one_customer_invoices", category:"finance", group:"account"},
            {id:"vendor-bills", ar:"فواتير الموردين", en:"Vendor Bills", descriptionAr:"المستحقات والمصاريف", descriptionEn:"Purchase bills", icon:"fa fa-file-text", action:"one_ui.action_one_vendor_bills", category:"finance", group:"account"},
            {id:"journal-entries", ar:"القيود اليومية", en:"Journal Entries", descriptionAr:"العمليات المحاسبية", descriptionEn:"Journal postings", icon:"fa fa-book", action:"one_ui.action_one_journal_entries", category:"finance", group:"account"},
            {id:"coa", ar:"شجرة الحسابات", en:"Chart of Accounts", descriptionAr:"الحسابات والتصنيف", descriptionEn:"Account structure", icon:"fa fa-sitemap", action:"one_ui.action_one_chart_accounts", category:"finance", group:"account"},
            {id:"reports", ar:"التقارير المالية", en:"Financial Reports", descriptionAr:"قوائم وتحليلات", descriptionEn:"Statements and insights", icon:"fa fa-pie-chart", action:"one_ui.action_one_financial_reports", category:"finance", group:"account"},
            {id:"pos", ar:"نقاط البيع", en:"Point of Sale", descriptionAr:"نقاط الدفع", descriptionEn:"Sales terminals", icon:"fa fa-credit-card", action:"one_ui.action_one_pos", category:"operations", group:"pos"},
            {id:"ask", ar:"اسأل ONE", en:"Ask ONE", descriptionAr:"مساعد النظام", descriptionEn:"Business assistant", icon:"fa fa-comments-o", action:"one_ui.action_one_ask", category:"system"},
            {id:"approvals", ar:"الموافقات", en:"Approvals", descriptionAr:"الطلبات والاعتمادات", descriptionEn:"Requests and reviews", icon:"fa fa-check-square-o", action:"one_ui.action_one_approvals", category:"system"},
            {id:"zatca", ar:"الامتثال السعودي", en:"Saudi Compliance", descriptionAr:"الفوترة والعنوان الوطني", descriptionEn:"ZATCA and national address", icon:"fa fa-shield", action:"one_ui.action_one_saudi_profile", category:"system", group:"admin"},
            {id:"subscriptions", ar:"الاشتراكات", en:"Subscriptions", descriptionAr:"الباقات والاشتراكات", descriptionEn:"Plans and subscriptions", icon:"fa fa-key", action:"one_ui.action_one_subscriptions", category:"system", group:"admin"},
            {id:"settings", ar:"الإعدادات", en:"Settings", descriptionAr:"الصلاحيات والتفضيلات", descriptionEn:"Preferences and access", icon:"fa fa-cog", action:"one_ui.action_one_settings", category:"system", group:"admin"},
        ];

        onWillStart(async () => {
            const [account, admin, pos] = await Promise.all([
                user.hasGroup("account.group_account_user").catch(() => false),
                user.hasGroup("base.group_system").catch(() => false),
                Promise.all([
                    user.hasGroup("point_of_sale.group_pos_user").catch(() => false),
                    user.hasGroup("point_of_sale.group_pos_manager").catch(() => false),
                ]).then(([a,b]) => a || b),
            ]);
            this.state.access.account = account || admin;
            this.state.access.admin = admin;
            this.state.access.pos = pos || admin;
        });

        // Never hold up the first paint while waiting for dashboard analytics.
        onMounted(() => { void this.loadMetrics(); });
    }

    get isArabic() {
        return this.state.lang.startsWith("ar");
    }

    get availableApps() {
        return this.apps.filter(app => !app.group || !!this.state.access[app.group]);
    }

    get homeApps() {
        return this.availableApps.filter(app => app.primary).slice(0, 8);
    }

    get filteredApps() {
        const q = this.state.appQuery.trim().toLocaleLowerCase();
        return this.availableApps.filter(app =>
            (this.state.category === "all" || app.category === this.state.category) &&
            (!q || [app.ar, app.en, app.descriptionAr, app.descriptionEn]
                .some(label => label.toLocaleLowerCase().includes(q)))
        );
    }

    showAllApps() {
        this.state.showAllApps = true;
        this.state.category = "all";
        this.state.appQuery = "";
    }

    showHome() {
        this.state.showAllApps = false;
    }

    setCategory(category) {
        this.state.category = category;
    }

    async loadMetrics() {
        const now = new Date();
        const today = [
            now.getFullYear(), String(now.getMonth() + 1).padStart(2, "0"),
            String(now.getDate()).padStart(2, "0"),
        ].join("-");
        const unpaid = [
            ["move_type", "=", "out_invoice"],
            ["state", "=", "posted"],
            ["payment_state", "in", ["not_paid", "partial"]],
        ];
        const counters = [
            ["sales", "sale.order", []],
            ["purchases", "purchase.order", []],
            ["transfers", "stock.picking", []],
            ["opportunities", "crm.lead", []],
            ["employees", "hr.employee", []],
            ["draftQuotations", "sale.order", [["state", "in", ["draft","sent"]]]],
            ["pendingPurchases", "purchase.order", [["state", "in", ["draft","sent","to approve"]]]],
            ["pendingReceipts", "stock.picking", [["picking_type_code","=","incoming"],["state","not in",["done","cancel"]]]],
            ["pendingApprovals", "one.approval.request", [["state","=","submitted"]]],
        ];
        if (this.state.access.account) {
            counters.push(
                ["invoices", "account.move", []],
                ["unpaidInvoices", "account.move", unpaid],
                ["overdueInvoices", "account.move", [...unpaid, ["invoice_date_due","<",today]]],
            );
        }
        // Three concurrent reads avoid saturating a small Oracle DB connection pool.
        let next = 0;
        await Promise.all(Array.from({length: Math.min(3, counters.length)}, async () => {
            while (next < counters.length) {
                const [key, model, domain] = counters[next++];
                try {
                    this.state[key] = await this.orm.searchCount(model, domain);
                } catch {
                    this.state[key] = "—";
                }
            }
        }));

        if (this.state.access.account) {
            try {
                const pulse = await this.orm.call("res.company", "one_get_pulse", []);
                this.state.pulseReceivables = pulse.receivables ?? "—";
                this.state.pulseOverdue = pulse.overdue ?? "—";
                this.state.pulsePayables = pulse.payables ?? "—";
                this.state.pulseMonthRevenue = pulse.month_revenue ?? "—";
                this.state.pulseMonthProfit = pulse.month_profit ?? "—";
            } catch {
                for (const key of ["pulseReceivables","pulseOverdue","pulsePayables","pulseMonthRevenue","pulseMonthProfit"]) {
                    this.state[key] = "—";
                }
            }
        }
        if (this.state.access.admin) {
            try {
                const zatca = await this.orm.call("one.saudi.profile", "one_get_readiness_summary", []);
                this.state.saudiProfiles = zatca.profiles ?? "—";
                this.state.zatcaReady = zatca.ready ?? "—";
                this.state.zatcaConnected = zatca.connected ?? "—";
            } catch {
                this.state.saudiProfiles = "—";
                this.state.zatcaReady = "—";
                this.state.zatcaConnected = "—";
            }
        }
    }

    async switchLanguage(code) {
        if (this.state.lang === code) return;
        await rpc("/web/dataset/call_kw/res.users/one_switch_language", {
            model: "res.users",
            method: "one_switch_language",
            args: [[user.userId], code],
            kwargs: {context: user.context},
        });
        location.reload();
    }

    open(xmlId) {
        return this.action.doAction(xmlId);
    }
}

registry.category("actions").add("one_ui.dashboard", OneDashboard);
