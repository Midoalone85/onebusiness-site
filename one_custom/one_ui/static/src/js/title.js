/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { WebClient } from "@web/webclient/webclient";

function applyOneIdentity() {
    const clean = (document.title || "").replace(/odoo/gi, "ONE ERP").trim();
    document.title = clean && clean !== "ONE ERP" ? clean : "ONE ERP";

    let appName = document.querySelector('meta[name="application-name"]');
    if (!appName) {
        appName = document.createElement("meta");
        appName.setAttribute("name", "application-name");
        document.head.appendChild(appName);
    }
    appName.setAttribute("content", "ONE ERP");

    let appleTitle = document.querySelector('meta[name="apple-mobile-web-app-title"]');
    if (!appleTitle) {
        appleTitle = document.createElement("meta");
        appleTitle.setAttribute("name", "apple-mobile-web-app-title");
        document.head.appendChild(appleTitle);
    }
    appleTitle.setAttribute("content", "ONE ERP");
}

patch(WebClient.prototype, {
    setup() {
        super.setup(...arguments);
        applyOneIdentity();
        const title = document.querySelector("title");
        if (title) {
            const observer = new MutationObserver(applyOneIdentity);
            observer.observe(title, { childList: true, subtree: true });
        }
    },
});
