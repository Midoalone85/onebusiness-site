{
    "name": "ONE Construction",
    "summary": "Construction contracts, service work items, progress certificates and draft invoicing",
    "version": "20.0.1.0.0",
    "author": "ONE Business",
    "license": "LGPL-3",
    "category": "ONE Business",
    "depends": ["base", "mail", "product", "account", "analytic"],
    "data": [
        "security/construction_security.xml",
        "security/ir.model.access.csv",
        "data/construction_sequence.xml",
        "views/construction_views.xml",
        "views/construction_settings_views.xml",
    ],
    "application": True,
    "installable": True,
}
