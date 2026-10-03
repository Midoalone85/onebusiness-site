from odoo import api, fields, models
from odoo.tools.misc import format_amount


class ResCompany(models.Model):
    _inherit = "res.company"

    @api.model
    def one_get_pulse(self):
        company = self.env.company
        currency = company.currency_id
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)

        MoveLine = self.env["account.move.line"]
        receivable_domain = [
            ("company_id", "=", company.id),
            ("move_id.state", "=", "posted"),
            ("account_id.account_type", "=", "asset_receivable"),
            ("amount_residual", "!=", 0),
        ]
        payable_domain = [
            ("company_id", "=", company.id),
            ("move_id.state", "=", "posted"),
            ("account_id.account_type", "=", "liability_payable"),
            ("amount_residual", "!=", 0),
        ]

        receivable_group = MoveLine._read_group(
            receivable_domain,
            aggregates=["amount_residual:sum"],
        )
        payable_group = MoveLine._read_group(
            payable_domain,
            aggregates=["amount_residual:sum"],
        )
        overdue_group = MoveLine._read_group(
            receivable_domain + [("date_maturity", "<", today)],
            aggregates=["amount_residual:sum"],
        )

        receivables = (receivable_group[0][0] if receivable_group else 0.0) or 0.0
        payables = -((payable_group[0][0] if payable_group else 0.0) or 0.0)
        overdue = (overdue_group[0][0] if overdue_group else 0.0) or 0.0

        sales_group = self.env["sale.order"]._read_group(
            [
                ("company_id", "=", company.id),
                ("state", "in", ("sale", "done")),
                ("date_order", ">=", fields.Datetime.to_datetime(month_start)),
            ],
            aggregates=["amount_total:sum"],
        )
        month_sales = (sales_group[0][0] if sales_group else 0.0) or 0.0

        profit_group = MoveLine._read_group(
            [
                ("company_id", "=", company.id),
                ("move_id.state", "=", "posted"),
                ("date", ">=", month_start),
                ("date", "<=", today),
                (
                    "account_id.account_type",
                    "in",
                    (
                        "income",
                        "income_other",
                        "expense_direct_cost",
                        "expense",
                        "expense_other",
                        "expense_depreciation",
                    ),
                ),
            ],
            aggregates=["balance:sum"],
        )
        month_profit = -((profit_group[0][0] if profit_group else 0.0) or 0.0)

        return {
            "receivables": format_amount(self.env, receivables, currency),
            "overdue": format_amount(self.env, overdue, currency),
            "payables": format_amount(self.env, payables, currency),
            "month_sales": format_amount(self.env, month_sales, currency),
            "month_profit": format_amount(self.env, month_profit, currency),
            "currency": currency.name,
        }
