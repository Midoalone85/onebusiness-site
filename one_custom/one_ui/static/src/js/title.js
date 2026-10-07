/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { WebClient } from "@web/webclient/webclient";

function applyOneIdentity() {
    const current = document.title || "";
    const clean = current.replace(/odoo/gi, "ONE ERP").trim();
    const desired = clean && clean !== "ONE ERP" ? clean : "ONE ERP";

    // Avoid writing the same title again. The old code rewrote <title> from
    // inside its own MutationObserver callback and could create a render loop.
    if (document.title !== desired) {
        document.title = desired;
    }

    let appName = document.querySelector('meta[name="application-name"]');
    if (!appName) {
        appName = document.createElement("meta");
        appName.setAttribute("name", "application-name");
        document.head.appendChild(appName);
    }
    if (appName.getAttribute("content") !== "ONE ERP") {
        appName.setAttribute("content", "ONE ERP");
    }

    let appleTitle = document.querySelector('meta[name="apple-mobile-web-app-title"]');
    if (!appleTitle) {
        appleTitle = document.createElement("meta");
        appleTitle.setAttribute("name", "apple-mobile-web-app-title");
        document.head.appendChild(appleTitle);
    }
    if (appleTitle.getAttribute("content") !== "ONE ERP") {
        appleTitle.setAttribute("content", "ONE ERP");
    }
}

patch(WebClient.prototype, {
    setup() {
        super.setup(...arguments);
        applyOneIdentity();

        const title = document.querySelector("title");
        if (title) {
            const observer = new MutationObserver(() => {
                const current = document.title || "";
                if (!current.trim() || /odoo/i.test(current)) {
                    applyOneIdentity();
                }
            });
            observer.observe(title, {
                childList: true,
                characterData: true,
                subtree: true,
            });
        }
    },
});
