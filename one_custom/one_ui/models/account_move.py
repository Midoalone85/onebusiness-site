from odoo import models


class AccountMove(models.Model):
    _inherit = "account.move"

    def one_report_lines(self):
        self.ensure_one()
        return self.line_ids.filtered(lambda line: not line.display_type)

    def one_report_total_debit(self):
        self.ensure_one()
        return sum(self.one_report_lines().mapped("debit"))

    def one_report_total_credit(self):
        self.ensure_one()
        return sum(self.one_report_lines().mapped("credit"))

    def one_report_balance_difference(self):
        self.ensure_one()
        return self.one_report_total_debit() - self.one_report_total_credit()
