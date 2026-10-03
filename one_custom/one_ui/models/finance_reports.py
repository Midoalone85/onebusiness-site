from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class OneFinanceReportWizard(models.TransientModel):
    _name = "one.finance.report.wizard"
    _description = "ONE ERP Financial Reports"

    date_from = fields.Date(
        string="From Date",
        required=True,
        default=lambda self: fields.Date.context_today(self).replace(day=1),
    )
    date_to = fields.Date(
        string="To Date",
        required=True,
        default=fields.Date.context_today,
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
    )
    target_move = fields.Selection(
        [
            ("posted", "Posted Entries"),
            ("all", "All Entries"),
        ],
        string="Entries",
        required=True,
        default="posted",
    )
    report_type = fields.Selection(
        [
            ("trial_balance", "Trial Balance"),
            ("general_ledger", "General Ledger"),
        ],
        string="Report",
        required=True,
        default="trial_balance",
    )

    @api.constrains("date_from", "date_to")
    def _check_dates(self):
        for wizard in self:
            if wizard.date_from and wizard.date_to and wizard.date_from > wizard.date_to:
                raise UserError(_("From Date cannot be later than To Date."))

    def action_print(self):
        self.ensure_one()
        data = {
            "form": {
                "date_from": fields.Date.to_string(self.date_from),
                "date_to": fields.Date.to_string(self.date_to),
                "company_id": self.company_id.id,
                "target_move": self.target_move,
            }
        }
        if self.report_type == "general_ledger":
            report = self.env.ref("one_ui.action_report_one_general_ledger")
        else:
            report = self.env.ref("one_ui.action_report_one_trial_balance")
        return report.report_action(self, data=data)


class OneFinanceReportMixin(models.AbstractModel):
    _name = "one.finance.report.mixin"
    _description = "ONE ERP Finance Report Helpers"

    def _one_form_values(self, data):
        form = (data or {}).get("form") or {}
        company = self.env["res.company"].browse(form.get("company_id")).exists()
        if not company:
            company = self.env.company

        date_from = fields.Date.to_date(form.get("date_from"))
        date_to = fields.Date.to_date(form.get("date_to"))
        if not date_from or not date_to:
            raise UserError(_("A valid report date range is required."))

        return {
            "company": company,
            "date_from": date_from,
            "date_to": date_to,
            "target_move": form.get("target_move") or "posted",
        }

    def _one_move_line_domain(self, values, date_from=None, date_to=None):
        domain = [("company_id", "=", values["company"].id)]
        if date_from:
            domain.append(("date", ">=", date_from))
        if date_to:
            domain.append(("date", "<=", date_to))
        if values["target_move"] == "posted":
            domain.append(("move_id.state", "=", "posted"))
        return domain

    def _one_opening_balances(self, values):
        opening_domain = self._one_move_line_domain(
            values,
            date_to=fields.Date.subtract(values["date_from"], days=1),
        )
        opening = defaultdict(float)
        for line in self.env["account.move.line"].search(opening_domain):
            opening[line.account_id.id] += line.debit - line.credit
        return opening


class ReportOneTrialBalance(models.AbstractModel):
    _name = "report.one_ui.report_one_trial_balance"
    _inherit = "one.finance.report.mixin"
    _description = "ONE ERP Trial Balance Report"

    @api.model
    def _get_report_values(self, docids, data=None):
        values = self._one_form_values(data)
        currency = values["company"].currency_id
        opening = self._one_opening_balances(values)

        movement_domain = self._one_move_line_domain(
            values,
            date_from=values["date_from"],
            date_to=values["date_to"],
        )
        accounts = {}
        for line in self.env["account.move.line"].search(
            movement_domain,
            order="account_id, date, id",
        ):
            account = line.account_id
            row = accounts.setdefault(
                account.id,
                {
                    "account": account,
                    "opening": opening.get(account.id, 0.0),
                    "debit": 0.0,
                    "credit": 0.0,
                },
            )
            row["debit"] += line.debit
            row["credit"] += line.credit

        for account_id, amount in opening.items():
            if account_id not in accounts and not currency.is_zero(amount):
                account = self.env["account.account"].browse(account_id)
                accounts[account_id] = {
                    "account": account,
                    "opening": amount,
                    "debit": 0.0,
                    "credit": 0.0,
                }

        rows = []
        for row in accounts.values():
            row["closing"] = row["opening"] + row["debit"] - row["credit"]
            if not (
                currency.is_zero(row["opening"])
                and currency.is_zero(row["debit"])
                and currency.is_zero(row["credit"])
                and currency.is_zero(row["closing"])
            ):
                rows.append(row)

        rows.sort(key=lambda item: (item["account"].code or "", item["account"].name or ""))

        totals = {
            "opening": sum(row["opening"] for row in rows),
            "debit": sum(row["debit"] for row in rows),
            "credit": sum(row["credit"] for row in rows),
            "closing": sum(row["closing"] for row in rows),
        }

        return {
            "doc_ids": docids,
            "doc_model": "one.finance.report.wizard",
            "docs": self.env["one.finance.report.wizard"].browse(docids),
            "rows": rows,
            "totals": totals,
            "company": values["company"],
            "currency": currency,
            "date_from": values["date_from"],
            "date_to": values["date_to"],
            "target_move": values["target_move"],
        }


class ReportOneGeneralLedger(models.AbstractModel):
    _name = "report.one_ui.report_one_general_ledger"
    _inherit = "one.finance.report.mixin"
    _description = "ONE ERP General Ledger Report"

    @api.model
    def _get_report_values(self, docids, data=None):
        values = self._one_form_values(data)
        currency = values["company"].currency_id
        opening = self._one_opening_balances(values)

        movement_domain = self._one_move_line_domain(
            values,
            date_from=values["date_from"],
            date_to=values["date_to"],
        )
        grouped = defaultdict(list)
        accounts = {}

        for line in self.env["account.move.line"].search(
            movement_domain,
            order="account_id, date, move_id, id",
        ):
            account = line.account_id
            accounts[account.id] = account
            grouped[account.id].append(line)

        for account_id, amount in opening.items():
            if not currency.is_zero(amount):
                accounts.setdefault(account_id, self.env["account.account"].browse(account_id))

        sections = []
        for account_id, account in accounts.items():
            running = opening.get(account_id, 0.0)
            rows = []
            debit_total = 0.0
            credit_total = 0.0
            for line in grouped.get(account_id, []):
                running += line.debit - line.credit
                debit_total += line.debit
                credit_total += line.credit
                rows.append(
                    {
                        "line": line,
                        "balance": running,
                    }
                )

            sections.append(
                {
                    "account": account,
                    "opening": opening.get(account_id, 0.0),
                    "rows": rows,
                    "debit": debit_total,
                    "credit": credit_total,
                    "closing": running,
                }
            )

        sections.sort(key=lambda item: (item["account"].code or "", item["account"].name or ""))

        return {
            "doc_ids": docids,
            "doc_model": "one.finance.report.wizard",
            "docs": self.env["one.finance.report.wizard"].browse(docids),
            "sections": sections,
            "company": values["company"],
            "currency": currency,
            "date_from": values["date_from"],
            "date_to": values["date_to"],
            "target_move": values["target_move"],
        }
