# ONE ERP Next: independent foundation

A standalone prototype of tenant access rules and seven-day trial policies. This is independent of Odoo, but **not yet a deployed or production-ready ERP**. The current website, Oracle server and Odoo instance are not changed.

Test locally with Python >=3.11:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

See ARCHITECTURE.md for what is implemented and what is not.

## Public interactive sample (no real user data)

A **fictional-data UI-only preview** is published from the separate static
`main/one-erp-preview/index.html` path on GitHub Pages:

https://onebusiness.site/one-erp-preview/

This has interactive module navigation, Arabic RTL / English LTR, sample
accounting statements, purchasing, inventory, CRM, hospital/clinic/lab/pharmacy
sector layouts, offline illustrative Ask ONE responses, and local sample CSV
export. It is **not connected** to the independent finance backend, any
tenant database, user identity provider, Oracle instance, AI model or payment
gateway. Sample numbers, invoices and patients are invented.

The backend branch remains a **draft** and is not deployed. Do not infer real
system readiness, authentication, accounting correctness or compliance from
the preview. No private finance information belongs on GitHub Pages.

Latest control-plane provisioning and signing-key work requires actual
trusted IdP wiring, operator authorization/MFA, per-customer database
provisioning and backup/restore tests before any commercial release.
