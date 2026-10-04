import datetime
import re
import secrets

from odoo import fields
from odoo.http import Controller, request, route
from odoo.addons.web.controllers.utils import ensure_db


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OneMarketing(Controller):
    """Public ONE ERP trial flow. Trial users are isolated by company and expire automatically."""

    def _trial_values(self, is_ar, error=None):
        return {
            "is_ar": is_ar,
            "error": error,
            "trial_hours": 24,
        }

    @route("/one/trial", type="http", auth="none", methods=["GET"])
    def trial(self, **kw):
        ensure_db()
        if request.session.uid:
            return request.redirect("/", code=303)
        lang = (kw.get("lang") or request.httprequest.args.get("lang") or "ar").lower()
        return request.render(
            "one_ui.one_trial_page",
            self._trial_values(not lang.startswith("en")),
        )

    @route("/one/trial/start", type="http", auth="none", methods=["POST"], csrf=True)
    def trial_start(self, **post):
        ensure_db()
        is_ar = not (post.get("lang") or "ar").lower().startswith("en")

        name = (post.get("name") or "").strip()[:120]
        email = (post.get("email") or "").strip().lower()[:160]
        company_name = (post.get("company") or "").strip()[:160]

        if not name or not company_name or not EMAIL_RE.match(email):
            message = (
                "اكتب الاسم والبريد الإلكتروني الصحيح واسم الشركة."
                if is_ar else
                "Enter your name, a valid email address, and company name."
            )
            return request.render(
                "one_ui.one_trial_page",
                self._trial_values(is_ar, message),
            )

        Users = request.env["res.users"].sudo()
        if Users.search_count([("login", "=", email)]):
            message = (
                "هذا البريد مستخدم بالفعل. جرّب تسجيل الدخول أو استخدم بريدًا آخر."
                if is_ar else
                "This email is already registered. Sign in or use another email."
            )
            return request.render(
                "one_ui.one_trial_page",
                self._trial_values(is_ar, message),
            )

        now = fields.Datetime.now()
        active_trials = Users.search_count([
            ("one_is_trial", "=", True),
            ("active", "=", True),
            ("one_trial_expires_at", ">", now),
        ])
        if active_trials >= 50:
            message = (
                "وصلت بيئة العرض للحد المؤقت. جرّب لاحقًا أو تواصل معنا لتجربة مخصصة."
                if is_ar else
                "The shared demo environment is temporarily full. Try again later or request a private demo."
            )
            return request.render(
                "one_ui.one_trial_page",
                self._trial_values(is_ar, message),
            )

        env = request.env
        company = env["res.company"].sudo().create({"name": company_name})

        group_refs = [
            "base.group_user",
            "sales_team.group_sale_salesman",
            "purchase.group_purchase_user",
            "stock.group_stock_user",
            "account.group_account_user",
            "mrp.group_mrp_user",
            "hr.group_hr_user",
            "point_of_sale.group_pos_user",
        ]
        group_ids = []
        for xmlid in group_refs:
            group = env.ref(xmlid, raise_if_not_found=False)
            if group:
                group_ids.append(group.id)

        expires_at = now + datetime.timedelta(hours=24)
        password = "One!" + secrets.token_urlsafe(9)
        dashboard_action = env.ref("one_ui.action_one_dashboard", raise_if_not_found=False)

        user = Users.with_context(no_reset_password=True).create({
            "name": name,
            "login": email,
            "email": email,
            "password": password,
            "company_id": company.id,
            "company_ids": [(6, 0, [company.id])],
            "group_ids": [(6, 0, group_ids)],
            "lang": "ar_001" if is_ar else "en_US",
            "one_is_trial": True,
            "one_trial_expires_at": expires_at,
            "action_id": dashboard_action.id if dashboard_action else False,
        })

        plan = env.ref("one_ui.plan_professional", raise_if_not_found=False)
        if plan:
            env["one.subscription"].sudo().create({
                "name": "24h ONE ERP Trial",
                "partner_id": user.partner_id.id,
                "plan_id": plan.id,
                "date_start": fields.Date.today(),
                "date_end": fields.Date.today() + datetime.timedelta(days=1),
                "status": "trial",
                "users_limit": 1,
                "notes": "Self-service 24-hour product trial.",
            })

        return request.render(
            "one_ui.one_trial_success",
            {
                "is_ar": is_ar,
                "login": email,
                "password": password,
                "expires_at": fields.Datetime.to_string(expires_at),
            },
        )



    def _demo_values(self, is_ar, error=None):
        return {"is_ar": is_ar, "error": error}

    @route("/one/demo", type="http", auth="none", methods=["GET"])
    def demo(self, **kw):
        ensure_db()
        lang = (kw.get("lang") or request.httprequest.args.get("lang") or "ar").lower()
        return request.render("one_ui.one_demo_page", self._demo_values(not lang.startswith("en")))

    @route("/one/demo/request", type="http", auth="none", methods=["POST"], csrf=True)
    def demo_request(self, **post):
        ensure_db()
        is_ar = not (post.get("lang") or "ar").lower().startswith("en")

        # Honeypot: silently accept bots without creating CRM noise.
        if (post.get("website") or "").strip():
            return request.render("one_ui.one_demo_success", {"is_ar": is_ar})

        name = (post.get("name") or "").strip()[:120]
        email = (post.get("email") or "").strip().lower()[:160]
        company = (post.get("company") or "").strip()[:160]
        phone = (post.get("phone") or "").strip()[:40]
        role = (post.get("role") or "").strip()[:100]
        employees = (post.get("employees") or "").strip()[:20]
        message = (post.get("message") or "").strip()[:1000]

        if not name or not company or not EMAIL_RE.match(email):
            error = (
                "اكتب الاسم واسم الشركة وبريدًا مهنيًا صحيحًا."
                if is_ar else
                "Enter your name, company and a valid work email."
            )
            return request.render("one_ui.one_demo_page", self._demo_values(is_ar, error))

        description = "\n".join([
            "ONE ERP Commercial Demo Request",
            f"Role: {role or '-'}",
            f"Team size: {employees or '-'}",
            "",
            message or "No additional notes.",
        ])
        request.env["crm.lead"].sudo().create({
            "name": f"ONE ERP Commercial Demo - {company}",
            "contact_name": name,
            "email_from": email,
            "phone": phone or False,
            "partner_name": company,
            "description": description,
            "priority": "2",
        })
        return request.render("one_ui.one_demo_success", {"is_ar": is_ar})

    @route("/one/health", type="http", auth="none", methods=["GET"])
    def health(self, **kw):
        ensure_db()
        env = request.env
        required_refs = [
            "one_ui.action_one_dashboard",
            "one_ui.plan_professional",
            "sales_team.group_sale_salesman",
            "purchase.group_purchase_user",
            "stock.group_stock_user",
            "account.group_account_user",
            "mrp.group_mrp_user",
            "hr.group_hr_user",
            "point_of_sale.group_pos_user",
        ]
        refs_ready = all(env.ref(xmlid, raise_if_not_found=False) for xmlid in required_refs)
        cron = env.ref("one_ui.ir_cron_expire_one_trial_users", raise_if_not_found=False)
        plans = env["one.subscription.plan"].sudo().search([("active", "=", True)], order="sequence, id")
        plan_prices = {
            plan.code: (None if plan.code == "enterprise" and not plan.monthly_price else plan.monthly_price)
            for plan in plans
        }
        smoke = env["ir.config_parameter"].sudo().get_param("one.trial_smoke_test")
        smoke_at = env["ir.config_parameter"].sudo().get_param("one.trial_smoke_test_at")
        module = env["ir.module.module"].sudo().search([("name", "=", "one_ui")], limit=1)
        healthy = refs_ready and bool(cron) and len(plans) >= 6 and smoke == "passed"
        return request.make_json_response({
            "service": "ONE ERP",
            "status": "ok" if healthy else "degraded",
            "version": module.latest_version or "20.0.1.24.0",
            "trial_hours": 24,
            "trial_prerequisites": bool(refs_ready and cron),
            "trial_smoke_test": smoke or "not_run",
            "trial_smoke_test_at": smoke_at or None,
            "plans": len(plans),
            "plan_prices_sar": plan_prices,
            "ask_one_lite": "ready",
            "paid_ai_api_required": False,
        })
