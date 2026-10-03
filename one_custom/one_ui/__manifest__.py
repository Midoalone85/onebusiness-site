{
    "name": "ONE ERP Experience",
    "version": "20.0.1.0.0",
    "summary": "ONE ERP branded workspace, dashboard, Saudi setup and subscriptions",
    "author": "ONE Business",
    "license": "LGPL-3",
    "category": "ONE Business",
    "depends": [
        "one_all", "web", "contacts", "sale_management", "purchase_stock",
        "stock", "account", "mrp", "crm", "hr"
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/branding_data.xml",
        "data/subscription_plans.xml",
        "views/actions.xml",
        "views/saudi_profile_views.xml",
        "views/subscription_views.xml",
        "views/menu_views.xml"
    ],
    "assets": {
        "web.assets_backend": [
            "one_ui/static/src/scss/one_erp.scss",
            "one_ui/static/src/js/dashboard.js",
            "one_ui/static/src/js/title.js",
            "one_ui/static/src/xml/dashboard.xml"
        ],
        "web.assets_frontend": [
            "one_ui/static/src/scss/one_login.scss"
        ]
    },
    "post_init_hook": "post_init_hook",
    "application": true,
    "installable": true
}
