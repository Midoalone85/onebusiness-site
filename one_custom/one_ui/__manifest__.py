{
    "name": "ONE ERP Experience",
    "version": "20.0.1.49.0",
    "summary": "ONE ERP business workspace with fast unified dashboard intelligence, Decision Briefs, Ask ONE, 7-day trials, Saudi compliance and production-readiness safeguards",
    "author": "ONE Business",
    "license": "LGPL-3",
    "category": "ONE Business",
    "depends": [
        "web", "contacts", "sale_management", "purchase_stock",
        "stock", "account", "mrp", "crm", "hr", "l10n_sa_edi", "point_of_sale"
    ],
    "data": [
        "security/ir.access.csv",
        "data/branding_data.xml",
        "data/subscription_plans.xml",
        "data/approval_data.xml",
        "data/trial_cron.xml",
        "views/login_templates.xml",
        "views/landing_templates.xml",
        "views/actions.xml",
        "views/saudi_profile_views.xml",
        "views/subscription_views.xml",
        "views/finance_report_views.xml",
        "views/approval_views.xml",
        "views/ask_one_views.xml",
        "report/journal_entry_report.xml",
        "report/financial_reports.xml",
        "report/partner_reports.xml",
        "report/statement_reports.xml",
        "views/menu_views.xml"
    ],
    "assets": {
        "web.assets_backend": [
            "one_ui/static/src/scss/one_erp.scss",
            "one_ui/static/src/js/menu_branding.js",
            "one_ui/static/src/js/dashboard.js",
            "one_ui/static/src/js/title.js",
            "one_ui/static/src/xml/dashboard.xml"
        ],
        "web.assets_frontend": [
            "one_ui/static/src/scss/one_login.scss",
            "one_ui/static/src/css/one_login.css"
        ]
    },
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "application": True,
    "installable": True
}
