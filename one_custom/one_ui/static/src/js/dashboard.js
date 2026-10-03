/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class OneDashboard extends Component {
    static template = "one_ui.OneDashboard";

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");
        this.state = useState({
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
            subscriptions: "…",
            activeSubscriptions: "…",
        });

        onWillStart(async () => {
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
                zatcaConnected: ["one.saudi.profile", [["zatca_status", "=", "connected"]]],
                subscriptions: ["one.subscription", []],
                activeSubscriptions: ["one.subscription", [["status", "=", "active"]]],
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
        });
    }

    open(xmlId) {
        return this.action.doAction(xmlId);
    }
}

registry.category("actions").add("one_ui.dashboard", OneDashboard);
