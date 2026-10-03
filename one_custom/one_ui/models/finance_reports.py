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
    partner_id = fields.Many2one(
        "res.partner",
        string="Partner",
        help="Optional for aging reports and required for the partner statement.",
    )
    report_type = fields.Selection(
        [
            ("trial_balance", "Trial Balance"),
            ("general_ledger", "General Ledger"),
            ("aged_receivable", "Aged Receivables"),
            ("aged_payable", "Aged Payables"),
            ("partner_statement", "Partner Statement"),
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
        if self.report_type == "partner_statement" and not self.partner_id:
            raise UserError(_("Select a partner to print the partner statement."))

        data = {
            "form": {
                "date_from": fields.Date.to_string(self.date_from),
                "date_to": fields.Date.to_string(self.date_to),
                "company_id": self.company_id.id,
                "target_move": self.target_move,
                "partner_id": self.partner_id.id or False,
                "report_type": self.report_type,
            }
        }

        report_xmlids = {
            "trial_balance": "one_ui.action_report_one_trial_balance",
            "general_ledger": "one_ui.action_report_one_general_ledger",
            "aged_receivable": "one_ui.action_report_one_aged_partner",
            "aged_payable": "one_ui.action_report_one_aged_partner",
            "partner_statement": "one_ui.action_report_one_partner_statement",
        }
        return self.env.ref(report_xmlids[self.report_type]).report_action(self, data=data)


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
            "partner": self.env["res.partner"].browse(form.get("partner_id")).exists(),
            "report_type": form.get("report_type"),
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
        grouped = self.env["account.move.line"]._read_group(
            opening_domain,
            ["account_id"],
            ["debit:sum", "credit:sum"],
        )
        for account, debit, credit in grouped:
            opening[account.id] = (debit or 0.0) - (credit or 0.0)
        return opening

    def _one_historical_residuals(self, lines, as_of_date):
        """Return company-currency residuals as they stood on a historical date."""
        if not lines:
            return {}

        debit_reconciles = self.env["account.partial.reconcile"]._read_group(
            [
                ("debit_move_id", "in", lines.ids),
                ("max_date", "<=", as_of_date),
            ],
            ["debit_move_id"],
            ["amount:sum"],
        )
        credit_reconciles = self.env["account.partial.reconcile"]._read_group(
            [
                ("credit_move_id", "in", lines.ids),
                ("max_date", "<=", as_of_date),
            ],
            ["credit_move_id"],
            ["amount:sum"],
        )
        debit_matched = {line.id: amount or 0.0 for line, amount in debit_reconciles}
        credit_matched = {line.id: amount or 0.0 for line, amount in credit_reconciles}

        return {
            line.id: line.balance
            - debit_matched.get(line.id, 0.0)
            + credit_matched.get(line.id, 0.0)
            for line in lines
        }


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
        grouped = self.env["account.move.line"]._read_group(
            movement_domain,
            ["account_id"],
            ["debit:sum", "credit:sum"],
        )
        for account, debit, credit in grouped:
            accounts[account.id] = {
                "account": account,
                "opening": opening.get(account.id, 0.0),
                "debit": debit or 0.0,
                "credit": credit or 0.0,
            }

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
                rows.append({"line": line, "balance": running})

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


class ReportOneAgedPartner(models.AbstractModel):
    _name = "report.one_ui.report_one_aged_partner"
    _inherit = "one.finance.report.mixin"
    _description = "ONE ERP Aged Receivable Payable Report"

    @api.model
    def _get_report_values(self, docids, data=None):
        values = self._one_form_values(data)
        currency = values["company"].currency_id
        receivable = values["report_type"] == "aged_receivable"
        account_type = "asset_receivable" if receivable else "liability_payable"

        domain = self._one_move_line_domain(values, date_to=values["date_to"])
        domain += [
            ("account_id.account_type", "=", account_type),
            ("partner_id", "!=", False),
        ]
        if values["partner"]:
            domain.append(("partner_id", "=", values["partner"].id))

        lines = self.env["account.move.line"].search(
            domain,
            order="partner_id, date_maturity, date, id",
        )
        historical = self._one_historical_residuals(lines, values["date_to"])
        sign = 1.0 if receivable else -1.0

        partners = {}
        for line in lines:
            outstanding = historical.get(line.id, 0.0) * sign
            if currency.is_zero(outstanding):
                continue

            due_date = line.date_maturity or line.date
            days_overdue = (values["date_to"] - due_date).days
            row = partners.setdefault(
                line.partner_id.id,
                {
                    "partner": line.partner_id,
                    "current": 0.0,
                    "days_1_30": 0.0,
                    "days_31_60": 0.0,
                    "days_61_90": 0.0,
                    "days_91_plus": 0.0,
                    "total": 0.0,
                },
            )
            if days_overdue <= 0:
                row["current"] += outstanding
            elif days_overdue <= 30:
                row["days_1_30"] += outstanding
            elif days_overdue <= 60:
                row["days_31_60"] += outstanding
            elif days_overdue <= 90:
                row["days_61_90"] += outstanding
            else:
                row["days_91_plus"] += outstanding
            row["total"] += outstanding

        rows = sorted(
            partners.values(),
            key=lambda row: (row["partner"].name or "").lower(),
        )
        totals = {
            key: sum(row[key] for row in rows)
            for key in (
                "current",
                "days_1_30",
                "days_31_60",
                "days_61_90",
                "days_91_plus",
                "total",
            )
        }

        return {
            "doc_ids": docids,
            "doc_model": "one.finance.report.wizard",
            "docs": self.env["one.finance.report.wizard"].browse(docids),
            "rows": rows,
            "totals": totals,
            "company": values["company"],
            "currency": currency,
            "date_to": values["date_to"],
            "partner": values["partner"],
            "report_title": _("Aged Receivables") if receivable else _("Aged Payables"),
        }


class ReportOnePartnerStatement(models.AbstractModel):
    _name = "report.one_ui.report_one_partner_statement"
    _inherit = "one.finance.report.mixin"
    _description = "ONE ERP Partner Statement"

    @api.model
    def _get_report_values(self, docids, data=None):
        values = self._one_form_values(data)
        partner = values["partner"]
        if not partner:
            raise UserError(_("Select a partner to print the partner statement."))

        base_domain = [
            ("company_id", "=", values["company"].id),
            ("partner_id", "=", partner.id),
            ("account_id.account_type", "in", ("asset_receivable", "liability_payable")),
        ]
        if values["target_move"] == "posted":
            base_domain.append(("move_id.state", "=", "posted"))

        opening_grouped = self.env["account.move.line"]._read_group(
            base_domain + [("date", "<", values["date_from"])],
            aggregates=["debit:sum", "credit:sum"],
        )
        if opening_grouped:
            opening_debit, opening_credit = opening_grouped[0]
        else:
            opening_debit = opening_credit = 0.0
        opening = (opening_debit or 0.0) - (opening_credit or 0.0)

        lines = self.env["account.move.line"].search(
            base_domain
            + [
                ("date", ">=", values["date_from"]),
                ("date", "<=", values["date_to"]),
            ],
            order="date, move_id, id",
        )

        running = opening
        rows = []
        debit_total = 0.0
        credit_total = 0.0
        for line in lines:
            running += line.debit - line.credit
            debit_total += line.debit
            credit_total += line.credit
            rows.append({"line": line, "balance": running})

        return {
            "doc_ids": docids,
            "doc_model": "one.finance.report.wizard",
            "docs": self.env["one.finance.report.wizard"].browse(docids),
            "company": values["company"],
            "currency": values["company"].currency_id,
            "partner": partner,
            "date_from": values["date_from"],
            "date_to": values["date_to"],
            "opening": opening,
            "rows": rows,
            "debit_total": debit_total,
            "credit_total": credit_total,
            "closing": running,
        }
