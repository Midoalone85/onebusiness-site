/** @odoo-module **/

import { computed } from "@odoo/owl";
import { location } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { user } from "@web/core/user";
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

async function switchOneLanguage(code) {
    if (user.context.lang === code) {
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

function arabicLanguageItem() {
    return {
        type: "item",
        id: "one_language_ar",
        description: user.context.lang === "ar_001" ? "✓ العربية" : "العربية",
        callback: () => switchOneLanguage("ar_001"),
        sequence: 54,
    };
}

function englishLanguageItem() {
    return {
        type: "item",
        id: "one_language_en",
        description: user.context.lang === "en_US" ? "✓ English" : "English",
        callback: () => switchOneLanguage("en_US"),
        sequence: 55,
    };
}

const userMenu = registry.category("user_menuitems");
if (userMenu.contains("support")) {
    userMenu.remove("support");
}

userMenu
    .add("one_language_ar", arabicLanguageItem)
    .add("one_language_en", englishLanguageItem);
