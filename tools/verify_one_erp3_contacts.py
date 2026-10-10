#!/usr/bin/env python3
"""Offline structural checks for ONE ERP3's unified res.partner extension.

These checks intentionally avoid starting Odoo or connecting to production.
Successful checks do not establish that the invoice/UBL integration was tested.
"""
import ast
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1] / "one_custom" / "one_ui"


def check(ok, message):
    if not ok:
        raise AssertionError(message)


def main():
    model_path = ROOT / "models/res_partner.py"
    code = model_path.read_text(encoding="utf-8")
    ast.parse(code, filename=str(model_path))
    init = (ROOT / "models/__init__.py").read_text(encoding="utf-8")
    check("from . import res_partner" in init, "Partner model is not imported")

    expected = (
        "one_contact_role",
        "one_legal_name_ar",
        "one_legal_name_en",
        "one_national_district",
        "one_national_building_number",
        "one_national_additional_number",
        "one_national_short_address",
        "one_billing_address_preview",
    )
    for field in expected:
        check(re.search(r"^\s+" + field + r"\s*=\s*fields\.", code, re.M),
              f"Missing res.partner field: {field}")
    check("@api.constrains" in code, "Saudi input validation missing")

    view = ROOT / "views/one_partner_views.xml"
    tree = ET.parse(view)
    xml = view.read_text(encoding="utf-8")
    check('ref="base.view_partner_form"' in xml, "Must inherit the native partner form")
    check('expr="//sheet/notebook"' in xml, "Unified form must extend the notebook")
    check('name="one_national_address"' in xml, "National address section missing")
    check('name="one_partner_accounting"' in xml, "Accounting section missing")
    for field in expected:
        check(f'name="{field}"' in xml, f"Partner UI does not show {field}")
    for field in ("property_account_receivable_id", "property_account_payable_id",
                  "property_payment_term_id", "property_supplier_payment_term_id"):
        check(f'name="{field}"' in xml, f"Missing native accounting field: {field}")
    check("'asset_receivable'" in xml and "'liability_payable'" in xml,
          "Receivable/payable filters must use the correct account types")
    check('groups="account.group_account_user"' in xml, "Missing accounting access group")
    check(not tree.findall(".//label[@string]"), "Invalid stand-alone labels in Odoo form view")

    invoice_path = ROOT / "report/one_invoice_national_address.xml"
    report = ET.parse(invoice_path)
    inherits = report.findall(".//template")
    check(any(t.attrib.get("inherit_id") == "account.report_invoice_document"
              for t in inherits), "Printed invoice must extend standard invoice report")
    check(report.findall(".//xpath[@expr=\"//div[@id='informations']\"]"),
          "National address must be inserted after the invoice details")

    manifest = ast.literal_eval((ROOT / "__manifest__.py").read_text(encoding="utf-8"))
    for item in ("views/one_partner_views.xml", "report/one_invoice_national_address.xml"):
        check(item in manifest["data"], f"Unregistered contact/report template: {item}")
    check("one_ui/static/src/scss/one_accessible_workspace.scss" in
          manifest["assets"]["web.assets_backend"], "Accessibility styles not registered")

    for path in (ROOT / "models").glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print("PASS: unified native contact form, Saudi fields, receivable/payable account choices")
    print("PASS: Arabic/English legal-name fields and national address invoice PDF extension")
    print("PASS: module manifest links, structural XML, Python source syntax")
    print("NOTE: Odoo installation, live form, ZATCA XML and invoice rendering remain untested.")


if __name__ == "__main__":
    main()
