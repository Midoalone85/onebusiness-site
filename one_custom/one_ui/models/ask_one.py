import re
from datetime import timedelta

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
        help="Ask about revenue, profit, unpaid invoices, liquidity, receivables, payables, inventory, employees, CRM, approvals, purchases, quotations or ZATCA.",
    )
    answer = fields.Text(string="Answer", readonly=True)

    def _is_arabic(self, text):
        return bool(ARABIC_RE.search(text or ""))

    def _contains_any(self, text, terms):
        return any(term in text for term in terms)

    def _money(self, amount):
        return format_amount(self.env, amount, self.env.company.currency_id)

    def _require_finance_access(self):
        # Prevent company-wide financial summaries leaking to non-accounting users.
        # Both the UI and underlying ORM access rules must authorize the request.
        if not (
            self.env.user.has_group("account.group_account_user")
            or self.env.user.has_group("base.group_system")
        ):
            raise AccessError(_("Accounting access is required for this answer."))

    def _ledger_total(self, account_types, date_from, date_to):
        rows = self.env["account.move.line"]._read_group(
            [
                ("company_id", "=", self.env.company.id),
                ("move_id.state", "=", "posted"),
                ("date", ">=", date_from),
                ("date", "<=", date_to),
                ("account_id.account_type", "in", account_types),
            ],
            aggregates=["balance:sum"],
        )
        return (rows[0][0] if rows else 0.0) or 0.0

    def _period_metrics(self, date_from, date_to):
        income_types = ("income", "income_other")
        expense_types = (
            "expense", "expense_other", "expense_direct_cost",
            "expense_depreciation",
        )
        revenue = -self._ledger_total(income_types, date_from, date_to)
        net_profit = -self._ledger_total(
            income_types + expense_types, date_from, date_to
        )
        return {"revenue": revenue, "profit": net_profit}

    def _financial_period_answer(self, question, metric, is_ar):
        today = fields.Date.context_today(self)
        this_start = today.replace(day=1)
        prior_end = this_start - timedelta(days=1)
        prior_start = prior_end.replace(day=1)
        compare = self._contains_any(
            question, ("قارن", "مقارن", "مقارنة", "مقارنه", "الفرق", "compare", "comparison", "versus", " vs ")
        )
        previous = self._contains_any(
            question,
            ("الشهر اللي فات", "الشهر الماضي", "الشهر السابق", "last month", "previous month"),
        )
        title_ar = "صافي الربح/الخسارة" if metric == "profit" else "الإيرادات المحاسبية"
        title_en = "Net profit/loss" if metric == "profit" else "Posted accounting revenue"
        label = title_ar if is_ar else title_en

        if compare:
            # A month-to-date comparison against the same numbered days last month,
            # rather than comparing a partial month against a full month.
            prior_comparable_end = prior_end.replace(day=min(today.day, prior_end.day))
            current = self._period_metrics(this_start, today)[metric]
            earlier = self._period_metrics(prior_start, prior_comparable_end)[metric]
            delta = current - earlier
            change = (
                f"{delta / abs(earlier) * 100:+.1f}%"
                if earlier else ("غير متاحة" if is_ar else "n/a")
            )
            if is_ar:
                return (
                    f"{label} من {this_start} إلى {today}: {self._money(current)}. "
                    f"الفترة المقارنة من {prior_start} إلى {prior_comparable_end}: "
                    f"{self._money(earlier)}. الفرق: {self._money(delta)} ({change}). "
                    "المصدر: القيود المحاسبية المرحلة للشركة الحالية فقط."
                )
            return (
                f"{label} from {this_start} to {today}: {self._money(current)}. "
                f"Comparable prior period {prior_start} to {prior_comparable_end}: "
                f"{self._money(earlier)}. Difference: {self._money(delta)} ({change}). "
                "Source: posted journal entries in the current company."
            )

        date_from, date_to = (prior_start, prior_end) if previous else (this_start, today)
        value = self._period_metrics(date_from, date_to)[metric]
        if is_ar:
            return (
                f"{label} من {date_from} إلى {date_to}: {self._money(value)}. "
                "المصدر: القيود المحاسبية المرحلة للشركة الحالية فقط."
            )
        return (
            f"{label} from {date_from} to {date_to}: {self._money(value)}. "
            "Source: posted journal entries in the current company."
        )

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

    def _answer_question(self, question):
        q = (question or "").strip().lower()
        is_ar = self._is_arabic(q)

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
            self._require_finance_access()
            count, amount = self._unpaid_customer_invoices()
            return (
                f"الفواتير غير المحصلة: {count} فاتورة، بإجمالي متبقٍ {self._money(amount)}."
                if is_ar
                else f"Unpaid customer invoices: {count}; outstanding balance: {self._money(amount)}."
            )

        if self._contains_any(q, ("سيول", "نقد", "بنك", "بنوك", "cash", "bank balance", "liquidity")):
            self._require_finance_access()
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
            self._require_finance_access()
            return self._financial_period_answer(q, "profit", is_ar)

        if self._contains_any(q, ("إيراد", "ايراد", "مبيعات", "المبيعات", "revenue", "sales", "income")):
            self._require_finance_access()
            return self._financial_period_answer(q, "revenue", is_ar)

        if self._contains_any(q, ("عملا", "تحصيل", "متأخر", "receivable", "customer", "collection", "overdue")):
            self._require_finance_access()
            if self._contains_any(q, ("مين", "من ", "who", "top", "أكبر", "اكبر")):
                rows = self._overdue_customers()
                if not rows:
                    return "لا توجد ذمم عملاء متأخرة حاليًا." if is_ar else "There are no overdue customer receivables."
                if is_ar:
                    details = "، ".join(f"{partner.name}: {self._money(amount)}" for partner, amount in rows)
                    return f"أعلى العملاء المتأخرين: {details}."
                details = "; ".join(f"{partner.name}: {self._money(amount)}" for partner, amount in rows)
                return f"Top overdue customers: {details}."
            pulse = self.env.company.one_get_pulse()
            return (
                f"إجمالي ذمم العملاء: {pulse['receivables']}، والمتأخر منها: {pulse['overdue']}."
                if is_ar
                else f"Total receivables: {pulse['receivables']}; overdue: {pulse['overdue']}."
            )

        if self._contains_any(q, ("مورد", "دائن", "payable", "supplier", "vendor")):
            self._require_finance_access()
            pulse = self.env.company.one_get_pulse()
            return (
                f"إجمالي ذمم الموردين الحالية: {pulse['payables']}."
                if is_ar
                else f"Current supplier payables: {pulse['payables']}."
            )

        if self._contains_any(q, ("موافق", "اعتماد", "approval", "approve")):
            pending = self.env["one.approval.request"].search_count([("state", "=", "submitted")])
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
            "أقدر حاليًا أجاوب عن: إيرادات وربح الشهر، الفواتير غير المحصلة، السيولة، ذمم العملاء والمتأخرين، "
            "الموردين، الاستلامات والتسليمات، الموظفين، فرص CRM، الموافقات، المشتريات، عروض الأسعار، "
            "وحالة ZATCA. مثال: «كام الفواتير غير المحصلة؟»"
            if is_ar
            else
            "I can currently answer about monthly revenue/profit, unpaid invoices, cash/bank balance, receivables, "
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
