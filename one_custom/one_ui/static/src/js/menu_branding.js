/** @odoo-module **/

import { computed } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { patch } from "@web/core/utils/patch";
import { menuService } from "@web/webclient/menus/menu_service";

patch(menuService, {
    async start(...args) {
        const service = await super.start(...args);
        const originalGetApps = service.getApps;

        service.getApps = computed(() => {
            const apps = originalGetApps();
            const oneApps = apps.filter((app) => app.xmlid === "one_ui.menu_one_root");
            return oneApps.length ? oneApps : apps;
        });

        return service;
    },
});

const userMenu = registry.category("user_menuitems");
if (userMenu.contains("support")) {
    userMenu.remove("support");
}
