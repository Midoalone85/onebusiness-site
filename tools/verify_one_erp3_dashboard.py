#!/usr/bin/env python3
"""Static, read-only checks for ONE ERP3 dashboard assets.

This does NOT replace an Odoo live login, browser-layout, or database smoke test.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "one_custom" / "one_ui"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    manifest = ast.literal_eval((MODULE / "__manifest__.py").read_text(encoding="utf-8"))
    require(manifest["name"].startswith("ONE"), "ONE ERP manifest name is missing")
    backend = manifest["assets"]["web.assets_backend"]
    required = [
        "one_ui/static/src/js/dashboard.js",
        "one_ui/static/src/xml/dashboard.xml",
        "one_ui/static/src/scss/one_erp.scss",
    ]
    for filename in required:
        require(filename in backend, f"Not registered as a backend asset: {filename}")
        require((MODULE.parent / filename).is_file(), f"Missing dashboard asset: {filename}")

    for filename in manifest["data"]:
        path = MODULE / filename
        require(path.is_file(), f"Missing declared module data file: {filename}")
        if path.suffix == ".xml":
            try:
                ET.parse(path)
            except ET.ParseError as exc:
                raise AssertionError(f"Invalid ONE ERP XML file {path.relative_to(REPO)}: {exc}") from exc

    for path in (MODULE / "static" / "src" / "xml").glob("*.xml"):
        try:
            ET.parse(path)
        except ET.ParseError as exc:
            raise AssertionError(f"Invalid ONE ERP XML file {path.relative_to(REPO)}: {exc}") from exc

    dashboard_xml = (MODULE / "static/src/xml/dashboard.xml").read_text(encoding="utf-8")
    dashboard_js = (MODULE / "static/src/js/dashboard.js").read_text(encoding="utf-8")
    dashboard_css = (MODULE / "static/src/scss/one_erp.scss").read_text(encoding="utf-8")
    require('class="one_dashboard one_v3"' in dashboard_xml, "ONE ERP3 dashboard root missing")
    require('t-att-dir=' in dashboard_xml, "RTL/LTR direction binding missing")
    require("switchLanguage('ar_001')" in dashboard_xml, "Arabic language control missing")
    require("switchLanguage('en_US')" in dashboard_xml, "English language control missing")
    require("this.showAllApps()" in dashboard_xml, "All applications navigation missing")
    require("filteredApps" in dashboard_xml, "Searchable app library missing")
    require(re.search(r"onMounted\\(\\(\\)\\s*=>\\s*\\{[^}]*void this\\.loadMetrics\\(\\)", dashboard_js, re.S) is not None,
            "Metrics should start after first paint, not block initial dashboard")
    require("void this.loadRecentInvoices()" in dashboard_js,
            "Recent invoices should load after first paint")
    require("get activityBars()" in dashboard_js and "activityBars" in dashboard_xml,
            "Read-only operations chart missing")
    require("state.access.account" in dashboard_xml and "state.recentInvoices" in dashboard_xml,
            "Permission-gated invoice table missing")
    require("one_v3_insights" in (MODULE / "static/src/scss/one_accessible_workspace.scss").read_text(encoding="utf-8"),
            "Responsive dashboard insights styles missing")
    require("overflow-y: auto" in dashboard_css, "Scrollable dashboard viewport missing")
    # Navigation must live above the workspace, mirror direction and keep
    # submenu actions filtered by the existing OWL permission checks.
    require('class="one_v3_topnav"' in dashboard_xml, "Horizontal top navigation missing")
    require('class="one_v3_navdropdown"' in dashboard_xml, "Dropdown submenu missing")
    require('t-att-dir=' in dashboard_xml, "RTL/LTR binding missing")
    require("get navGroups()" in dashboard_js, "Role-filtered nav grouping missing")
    require("this.availableApps.map(" in dashboard_js, "Nav must reuse permission-filtered apps")
    require("openFromMenu(xmlId)" in dashboard_js, "Submenu routing missing")
    require("onNavigationKeydown(event)" in dashboard_js, "Escape-to-close navigation missing")
    require("one_v3_navbutton" in (MODULE / "static/src/scss/one_accessible_workspace.scss").read_text(encoding="utf-8"), "Top nav responsive styles missing")
    require("100dvh" in dashboard_css, "Viewport-aware height missing")
    require("scrollbar-color:" in dashboard_css, "Visible scroll indicator missing")
    require("@media (max-width: 720px)" in dashboard_css, "Mobile grid styles missing")

    definitions = set()
    for filename in manifest["data"]:
        if filename.startswith("views/"):
            parsed = ET.parse(MODULE / filename)
            for element in parsed.iter():
                identity = element.get("id")
                if identity:
                    definitions.add(identity)
    actions = set(re.findall(r'action:"one_ui\.([^"]+)"', dashboard_js))
    actions.update(re.findall(r"this\.open\('one_ui\.([^']+)'\)", dashboard_xml))
    missing = sorted(actions - definitions)
    require(not missing, "Undefined ONE ERP actions: " + ", ".join(missing))
    require(len(actions) >= 14, "Unexpectedly few dashboard application actions")
    for name in ["accounting", "sales", "purchases", "inventory", "crm", "mrp", "hr"]:
        require(f'id:"{name}"' in dashboard_js, f"Missing primary application: {name}")
    require("group:\"account\"" in dashboard_js, "Finance permission group missing")

    print("PASS: ONE ERP3 dashboard static checks")
    print(f"PASS: {len(actions)} application actions resolve to declared XML IDs")
    print("PASS: Arabic/English, responsive grid, visible scroll, asset XML parse")
    print("NOTE: Live Oracle deployment and Odoo integration remain untested.")


if __name__ == "__main__":
    main()
