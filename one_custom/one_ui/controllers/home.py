from odoo.http import request, route
from odoo.addons.web.controllers.home import Home
from odoo.addons.web.controllers.utils import ensure_db, is_user_internal


PLAN_META = {
    "starter": {
        "ar": "البداية",
        "en": "Starter",
        "fallback_price": 149,
        "tag_ar": "للانطلاق",
        "tag_en": "Launch",
    },
    "business": {
        "ar": "الأعمال",
        "en": "Business",
        "fallback_price": 349,
        "tag_ar": "الأكثر توازنًا",
        "tag_en": "Balanced",
    },
    "professional": {
        "ar": "الاحترافية",
        "en": "Professional",
        "fallback_price": 599,
        "tag_ar": "الأكثر طلبًا",
        "tag_en": "Most popular",
    },
    "manufacturing": {
        "ar": "التصنيع",
        "en": "Manufacturing",
        "fallback_price": 899,
        "tag_ar": "للمصانع",
        "tag_en": "For makers",
    },
    "saudi": {
        "ar": "السعودية",
        "en": "Saudi Pack",
        "fallback_price": 499,
        "tag_ar": "امتثال محلي",
        "tag_en": "Local compliance",
    },
    "enterprise": {
        "ar": "المؤسسات",
        "en": "Enterprise",
        "fallback_price": None,
        "tag_ar": "حسب الاحتياج",
        "tag_en": "Custom",
    },
}

FEATURE_LABELS = {
    "feature_sales": ("المبيعات", "Sales"),
    "feature_purchase": ("المشتريات", "Purchases"),
    "feature_inventory": ("المخزون", "Inventory"),
    "feature_accounting": ("المحاسبة", "Accounting"),
    "feature_crm": ("إدارة العملاء CRM", "CRM"),
    "feature_hr": ("الموارد البشرية", "HR"),
    "feature_mrp": ("التصنيع", "Manufacturing"),
    "feature_reports": ("تقارير متقدمة", "Advanced reports"),
    "feature_saudi": ("ZATCA والامتثال السعودي", "Saudi / ZATCA"),
    "feature_api": ("واجهات API", "API"),
    "feature_multi_company": ("تعدد الشركات", "Multi-company"),
}


class OneHome(Home):
    """ONE ERP public product gateway and clean internal application root."""

    @route("/one/health", type="http", auth="none", csrf=False)
    def one_health(self, **kw):
        """Lightweight readiness endpoint for ONE ERP staging and monitoring."""
        ensure_db()
        installed = bool(
            request.env["ir.module.module"].sudo().search_count([
                ("name", "=", "one_ui"),
                ("state", "=", "installed"),
            ])
        )
        return request.make_json_response({
            "service": "ONE ERP",
            "status": "ok" if installed else "degraded",
            "ready": installed,
        })

    @route("/one/login", type="http", auth="none", csrf=False, methods=["GET"])
    def one_login(self, **kw):
        """Always start ONE ERP login with a clean browser session."""
        request.session.logout(keep_db=True)
        response = request.redirect("/web/login?db=one_erp_db&one=1")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    def _login_redirect(self, uid, redirect=None):
        if redirect and redirect.startswith("/") and not redirect.startswith("//"):
            return redirect
        return "/"

    def _public_plans(self, is_ar):
        Plan = request.env["one.subscription.plan"].sudo()
        domain = [("active", "=", True)] if "active" in Plan._fields else []
        plans = Plan.search(domain, order="sequence, id")
        cards = []
        for plan in plans:
            meta = PLAN_META.get(plan.code, {})
            features = []
            for field_name, labels in FEATURE_LABELS.items():
                if getattr(plan, field_name, False):
                    features.append(labels[0] if is_ar else labels[1])
            fallback = meta.get("fallback_price")
            price = plan.monthly_price if plan.monthly_price else fallback
            cards.append({
                "code": plan.code,
                "name": meta.get("ar" if is_ar else "en") or plan.name,
                "tag": meta.get("tag_ar" if is_ar else "tag_en") or "",
                "price": price,
                "custom": price is None,
                "features": features[:6],
                "featured": plan.code == "professional",
            })
        return cards

    @route("/", type="http", auth="none")
    def index(self, s_action=None, db=None, **kw):
        ensure_db()

        if not request.session.uid:
            lang = (kw.get("lang") or request.httprequest.args.get("lang") or "ar").lower()
            is_ar = not lang.startswith("en")
            return request.render(
                "one_ui.one_landing_page",
                {
                    "is_ar": is_ar,
                    "plans": self._public_plans(is_ar),
                    "trial_hours": 24,
                },
            )

        if not is_user_internal(request.session.uid):
            return request.redirect_query(
                "/web/login_successful",
                query=request.params,
            )

        dashboard = request.env.ref("one_ui.action_one_dashboard", raise_if_not_found=False)
        if dashboard:
            return request.redirect(f"/odoo/action-{dashboard.id}")

        return self.web_client(s_action=s_action, **kw)
