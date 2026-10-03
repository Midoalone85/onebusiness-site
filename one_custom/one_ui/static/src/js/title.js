/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { WebClient } from "@web/webclient/webclient";

patch(WebClient.prototype, {
    setup() {
        super.setup(...arguments);
        document.title = "ONE ERP";
        const title = document.querySelector("title");
        if (title) {
            const observer = new MutationObserver(() => {
                if (/odoo/i.test(document.title)) {
                    document.title = document.title.replace(/odoo/gi, "ONE ERP");
                }
            });
            observer.observe(title, { childList: true });
        }
    },
});
