/** @odoo-module **/

import { download } from "@web/core/network/download";

const originalDownload = download._download;

function appendHiddenField(form, name, value) {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value ?? "";
    form.appendChild(input);
}

function submitReportThroughForm(options) {
    return new Promise((resolve) => {
        const frameName = "one_report_download_" + Date.now() + "_" + Math.random().toString(36).slice(2);
        const iframe = document.createElement("iframe");
        iframe.name = frameName;
        iframe.style.display = "none";

        const form = document.createElement("form");
        form.method = "POST";
        form.action = options.url;
        form.target = frameName;
        form.style.display = "none";

        for (const [key, value] of Object.entries(options.data || {})) {
            appendHiddenField(form, key, value);
        }
        appendHiddenField(form, "token", "one-report-download");
        if (window.odoo?.csrf_token) {
            appendHiddenField(form, "csrf_token", window.odoo.csrf_token);
        }

        document.body.appendChild(iframe);
        document.body.appendChild(form);
        form.submit();

        window.setTimeout(() => {
            form.remove();
            iframe.remove();
        }, 60000);

        // The normal browser form download is intentionally fire-and-forget.
        // The server still returns the real PDF with Content-Disposition.
        resolve();
    });
}

download._download = async (options) => {
    try {
        return await originalDownload(options);
    } catch (error) {
        const debug = String(error?.data?.debug || "").trim();
        const isReportDownload = options?.url === "/report/download";
        const isFalse204 = debug === "204" || debug.startsWith("204\n");

        if (isReportDownload && isFalse204) {
            return submitReportThroughForm(options);
        }
        throw error;
    }
};
