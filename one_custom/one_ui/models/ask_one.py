import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError
from odoo.tools.misc import format_amount


ARABIC_RE = re.compile(r"[\u0600-\u06FF]")


class OneAskWizard(models.TransientModel):
    _name = "one.ask.wizard"
    _description = "Ask ONE Lite"

    question = fields.Char(
        string="Ask ONE",
        required=True,
        help="Ask about company health, revenue, profit, unpaid invoices, liquidity, receivables, payables, inventory, employees, CRM, approvals, purchases, quotations or ZATCA.",
    )
    answer = fields.Text(string="Answer", readonly=True)

    def _is_arabic(self, text):
        return bool(ARABIC_RE.search(text or ""))

    def _contains_any(self, text, terms):
        return any(term in text for term in terms)

    def _money(self, amount):
        return format_amount(self.env, amount, self.env.company.currency_id)

    def _overdue_customers(self):
        today = fields.Date.context_today(self)
        rows = self.env["account.move.line"]._read_group(
            [
                ("company_id", "=", self.env.company.id),
                ("move_id.state", "=", "posted"),
                ("account_id.account_type", "=", "asset_receivable"),
                ("partner_id", "!=", False),
                ("date_maturity", "<", today),
                ("amount_residual", ">", 0),
            ],
            ["partner_id"],
            ["amount_residual:sum"],
            order="amount_residual:sum desc",
            limit=5,
        )
        return [(partner, amount or 0.0) for partner, amount in rows]

    def _unpaid_customer_invoices(self):
        moves = self.env["account.move"].search([
            ("company_id", "=", self.env.company.id),
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("payment_state", "in", ("not_paid", "partial")),
        ])
        return len(moves), sum(abs(move.amount_residual_signed) for move in moves)

    def _cash_bank_balance(self):
        rows = self.env["account.move.line"]._read_group(
            [
                ("company_id", "=", self.env.company.id),
                ("move_id.state", "=", "posted"),
                ("account_id.account_type", "=", "asset_cash"),
            ],
            aggregates=["balance:sum"],
        )
        return (rows[0][0] if rows else 0.0) or 0.0

    def _pending_stock(self, picking_type_code):
        return self.env["stock.picking"].search_count([
            ("company_id", "=", self.env.company.id),
            ("picking_type_code", "=", picking_type_code),
            ("state", "not in", ("done", "cancel")),
        ])

    def _decision_brief(self, is_ar):
        """Return a compact, action-first management brief using only current-company data."""
        radar = self.env.company.one_get_business_radar()
        summary = self.env.company.one_get_dashboard_summary()
        alerts = radar.get("alerts", [])

        actions = []
        action_map_ar = {
            "overdue": "ابدأ التحصيل اليوم من أكبر العملاء المتأخرين، ثم ثبّت مواعيد متابعة واضحة.",
            "margin": "راجع أوامر البيع ذات الهامش السلبي قبل أي تسعير أو اعتماد جديد.",
            "approvals": "احسم الموافقات المعلقة حتى لا تتحول إلى اختناق تشغيلي.",
            "receipts": "راجع الاستلامات المفتوحة وحدد ما يؤثر على التوريد أو التسليم.",
            "compliance_missing": "أكمل ملف ZATCA والعنوان الوطني قبل الاعتماد على الفوترة الإلكترونية.",
            "compliance_incomplete": "أغلق عناصر الجاهزية الناقصة في ملف الامتثال السعودي.",
        }
        action_map_en = {
            "overdue": "Start collections with the largest overdue customers and set clear follow-up dates.",
            "margin": "Review negative-margin sales before approving new pricing or orders.",
            "approvals": "Clear pending approvals so they do not become an operating bottleneck.",
            "receipts": "Review open receipts and identify anything blocking supply or delivery.",
            "compliance_missing": "Complete the ZATCA and National Address profile before relying on e-invoicing.",
            "compliance_incomplete": "Close the remaining Saudi compliance readiness gaps.",
        }

        action_map = action_map_ar if is_ar else action_map_en
        for alert in alerts:
            action = action_map.get(alert.get("key"))
            if action and action not in actions:
                actions.append(action)
            if len(actions) >= 3:
                break

        if len(actions) < 3 and summary.get("draftQuotations"):
            actions.append(
                "راجع عروض الأسعار المفتوحة وحدد ما يحتاج متابعة لإغلاق الصفقة."
                if is_ar else
                "Review open quotations and identify the ones that need follow-up to close."
            )
        if len(actions) < 3 and summary.get("pendingPurchases"):
            actions.append(
                "راجع أوامر الشراء المعلقة واربط كل طلب باحتياج تشغيلي واضح."
                if is_ar else
                "Review pending purchase orders and tie each one to a clear operational need."
            )
        if len(actions) < 3 and summary.get("unpaidInvoices"):
            actions.append(
                "رتب الفواتير غير المحصلة حسب القيمة والاستحقاق وابدأ بالأعلى أثرًا."
                if is_ar else
                "Rank unpaid invoices by value and due date, then start with the highest-impact items."
            )
        if len(actions) < 3:
            actions.append(
                "لا توجد إشارة حرجة الآن؛ ركّز على فرص CRM والعروض المفتوحة للحفاظ على الزخم."
                if is_ar else
                "There is no critical signal right now; focus on CRM opportunities and open quotations to maintain momentum."
            )

        actions = actions[:3]
        if is_ar:
            numbered = " ".join(f"{idx}) {action}" for idx, action in enumerate(actions, 1))
            return (
                f"موجز القرار اليومي: صحة الشركة {radar['score']}/100 ({radar['status']}). "
                f"أولوياتك الآن: {numbered}"
            )
        numbered = " ".join(f"{idx}) {action}" for idx, action in enumerate(actions, 1))
        return (
            f"Daily decision brief: company health {radar['score']}/100 ({radar['status']}). "
            f"Your priorities now: {numbered}"
        )

    def _answer_question(self, question):
        q = (question or "").strip().lower()
        is_ar = self._is_arabic(q)
        pulse = self.env.company.one_get_pulse()

        if self._contains_any(
            q,
            (
                "اعمل ايه",
                "اعمل إيه",
                "ابدأ بايه",
                "ابدأ بإيه",
                "الأولوية",
                "الاولويه",
                "الأولويات",
                "الاولويات",
                "موجز القرار",
                "قرار اليوم",
                "ملخص اليوم",
                "priorities",
                "priority",
                "what should i do",
                "decision brief",
                "daily brief",
                "today's focus",
                "todays focus",
            ),
        ):
            return self._decision_brief(is_ar)

        if self._contains_any(
            q,
            (
                "صحة الشركة",
                "صحه الشركه",
                "وضع الشركة",
                "وضع الشركه",
                "مؤشر الصحة",
                "مؤشر صحه",
                "business health",
                "company health",
                "health score",
                "business radar",
                "radar",
            ),
        ):
            radar = self.env.company.one_get_business_radar()
            alerts = radar.get("alerts", [])
            if is_ar:
                highlights = "، ".join(alert["title"] for alert in alerts[:3])
                if not highlights:
                    highlights = "لا توجد تنبيهات عاجلة"
                return (
                    f"مؤشر صحة الشركة: {radar['score']}/100 ({radar['status']}). "
                    f"أبرز الإشارات: {highlights}."
                )
            highlights = "; ".join(alert["title"] for alert in alerts[:3]) or "No urgent signals"
            return (
                f"Company health score: {radar['score']}/100 ({radar['status']}). "
                f"Top signals: {highlights}."
            )

        if self._contains_any(q, ("زاتكا", "zatca", "فاتورة إلكتر", "فاتوره الكتر", "e-invoice", "einvoice")):
            summary = self.env["one.saudi.profile"].one_get_readiness_summary()
            if is_ar:
                return (
                    f"ملفات الامتثال: {summary['profiles']}، الجاهز للربط: {summary['ready']}، "
                    f"والمربوط فعليًا: {summary['connected']}."
                )
            return (
                f"Compliance profiles: {summary['profiles']}; ready for onboarding: {summary['ready']}; "
                f"connected: {summary['connected']}."
            )

        invoice_terms = ("فاتور", "invoice")
        unpaid_terms = ("غير محصل", "غير مدفوع", "متبقي", "متبقى", "unpaid", "outstanding")
        if self._contains_any(q, invoice_terms) and self._contains_any(q, unpaid_terms):
            count, amount = self._unpaid_customer_invoices()
            return (
                f"الفواتير غير المحصلة: {count} فاتورة، بإجمالي متبقٍ {self._money(amount)}."
                if is_ar
                else f"Unpaid customer invoices: {count}; outstanding balance: {self._money(amount)}."
            )

        if self._contains_any(q, ("سيول", "نقد", "بنك", "بنوك", "cash", "bank balance", "liquidity")):
            amount = self._cash_bank_balance()
            return (
                f"رصيد حسابات النقد والبنوك المسجل محاسبيًا: {self._money(amount)}."
                if is_ar
                else f"Posted cash and bank ledger balance: {self._money(amount)}."
            )

        if self._contains_any(q, ("استلام", "وارد", "receipt", "incoming shipment")):
            pending = self._pending_stock("incoming")
            return (
                f"الاستلامات المعلقة حاليًا: {pending}."
                if is_ar
                else f"Pending receipts: {pending}."
            )

        if self._contains_any(q, ("تسليم", "شحنات خارجة", "delivery", "outgoing shipment")):
            pending = self._pending_stock("outgoing")
            return (
                f"التسليمات المعلقة حاليًا: {pending}."
                if is_ar
                else f"Pending deliveries: {pending}."
            )

        if self._contains_any(q, ("موظف", "موظفين", "employee", "employees", "headcount")):
            count = self.env["hr.employee"].search_count([
                ("company_id", "=", self.env.company.id),
            ])
            return (
                f"عدد الموظفين المسجلين في الشركة الحالية: {count}."
                if is_ar
                else f"Employees in the current company: {count}."
            )

        if self._contains_any(q, ("فرص", "فرصة", "crm", "opportunit", "pipeline")):
            count = self.env["crm.lead"].search_count([
                ("company_id", "=", self.env.company.id),
                ("type", "=", "opportunity"),
                ("active", "=", True),
                ("probability", "<", 100),
            ])
            return (
                f"الفرص المفتوحة في مسار المبيعات: {count}."
                if is_ar
                else f"Open CRM opportunities: {count}."
            )

        if self._contains_any(q, ("ربح", "خسار", "profit", "loss")):
            return (
                f"ربح / خسارة الشهر الحالي: {pulse['month_profit']}."
                if is_ar
                else f"Current month profit / loss: {pulse['month_profit']}."
            )

        if self._contains_any(q, ("إيراد", "ايراد", "مبيعات الشهر", "revenue", "month sales", "sales this month")):
            return (
                f"إيرادات الشهر الحالي: {pulse['month_revenue']}."
                if is_ar
                else f"Current month revenue: {pulse['month_revenue']}."
            )

        if self._contains_any(q, ("عملا", "تحصيل", "متأخر", "receivable", "customer", "collection", "overdue")):
            if self._contains_any(q, ("مين", "من ", "who", "top", "أكبر", "اكبر")):
                rows = self._overdue_customers()
                if not rows:
                    return "لا توجد ذمم عملاء متأخرة حاليًا." if is_ar else "There are no overdue customer receivables."
                if is_ar:
                    details = "، ".join(f"{partner.name}: {self._money(amount)}" for partner, amount in rows)
                    return f"أعلى العملاء المتأخرين: {details}."
                details = "; ".join(f"{partner.name}: {self._money(amount)}" for partner, amount in rows)
                return f"Top overdue customers: {details}."
            return (
                f"إجمالي ذمم العملاء: {pulse['receivables']}، والمتأخر منها: {pulse['overdue']}."
                if is_ar
                else f"Total receivables: {pulse['receivables']}; overdue: {pulse['overdue']}."
            )

        if self._contains_any(q, ("مورد", "دائن", "payable", "supplier", "vendor")):
            return (
                f"إجمالي ذمم الموردين الحالية: {pulse['payables']}."
                if is_ar
                else f"Current supplier payables: {pulse['payables']}."
            )

        if self._contains_any(q, ("موافق", "اعتماد", "approval", "approve")):
            pending = self.env["one.approval.request"].search_count([
                ("company_id", "=", self.env.company.id),
                ("state", "=", "submitted"),
            ])
            return (
                f"لديك {pending} طلب موافقة معلق ضمن صلاحياتك."
                if is_ar
                else f"You have {pending} pending approval request(s) within your access."
            )

        if self._contains_any(q, ("شراء", "مشتريات", "purchase", "purchases")):
            pending = self.env["purchase.order"].search_count([
                ("company_id", "=", self.env.company.id),
                ("state", "in", ("draft", "sent", "to approve")),
            ])
            return (
                f"أوامر الشراء المعلقة حاليًا: {pending}."
                if is_ar
                else f"Pending purchase orders: {pending}."
            )

        if self._contains_any(q, ("عرض سعر", "عروض", "quotation", "quote")):
            pending = self.env["sale.order"].search_count([
                ("company_id", "=", self.env.company.id),
                ("state", "in", ("draft", "sent")),
            ])
            return (
                f"عروض الأسعار المفتوحة حاليًا: {pending}."
                if is_ar
                else f"Open quotations: {pending}."
            )

        return (
            "أقدر حاليًا أجاوب عن: أولويات اليوم وموجز القرار، إيرادات وربح الشهر، الفواتير غير المحصلة، السيولة، ذمم العملاء والمتأخرين، "
            "الموردين، الاستلامات والتسليمات، الموظفين، فرص CRM، الموافقات، المشتريات، عروض الأسعار، "
            "وحالة ZATCA. مثال: «كام الفواتير غير المحصلة؟»"
            if is_ar
            else
            "I can currently answer about today's priorities and decision brief, monthly revenue/profit, unpaid invoices, cash/bank balance, receivables, "
            "overdue customers, payables, receipts, deliveries, employees, CRM opportunities, approvals, "
            "purchases, quotations, and ZATCA status. Example: “How much is still unpaid?”"
        )

    def action_ask(self):
        self.ensure_one()
        try:
            self.answer = self._answer_question(self.question)
        except AccessError:
            self.answer = (
                "ليس لديك صلاحية لعرض البيانات المطلوبة."
                if self._is_arabic(self.question)
                else "You do not have permission to view the requested data."
            )
        return {
            "type": "ir.actions.act_window",
            "res_model": "one.ask.wizard",
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
