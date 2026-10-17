import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import (
    Account, AccountKind, AccountingError, AccountingPeriod, EntryStatus,
    JournalEntry, JournalLine, post_entry, validate_entry,
)
from one_erp_core.tenancy import Membership, Role
from one_erp_core.financial_reports import trial_balance, profit_and_loss


class AccountingFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tenant, self.other_tenant, self.user = uuid4(), uuid4(), uuid4()
        self.memberships = (Membership(self.user, self.tenant, Role.ACCOUNTANT),)
        self.chart = {
            name: Account(name, self.tenant, code, name, kind)
            for name, code, kind in [
                ("cash", "1000", AccountKind.ASSET),
                ("sales", "4000", AccountKind.REVENUE),
                ("rent", "5000", AccountKind.EXPENSE),
            ]
        }
        self.period = AccountingPeriod(self.tenant, date(2026, 1, 1), date(2027, 1, 1))
        self.sale = JournalEntry(uuid4(), self.tenant, "SALE-1", date(2026, 10, 15),
                                 "SAR", "sale-1", (JournalLine("cash", Decimal("115.00")),
                                                   JournalLine("sales", credit=Decimal("115.00"))))

    def post(self, entry=None, **changes):
        return post_entry(entry or self.sale, chart=self.chart,
                          period=changes.pop("period", self.period), actor_id=self.user,
                          memberships=changes.pop("memberships", self.memberships), **changes)

    def test_balanced_sale_can_post(self):
        entry = self.post()
        self.assertEqual(entry.status, EntryStatus.POSTED)
        self.assertEqual(self.sale.status, EntryStatus.DRAFT)

    def test_both_or_no_amount_side_rejected(self):
        for line in (JournalLine("cash", Decimal(1), Decimal(1)), JournalLine("cash")):
            with self.assertRaises(AccountingError):
                validate_entry(replace(self.sale, lines=(line, self.sale.lines[1])),
                               chart=self.chart, period=self.period)

    def test_imbalanced_journal_rejected(self):
        invalid = replace(self.sale, lines=(JournalLine("cash", Decimal("110")), self.sale.lines[1]))
        with self.assertRaises(AccountingError):
            self.post(invalid)

    def test_float_and_excess_precision_rejected(self):
        for amount in (115.0, Decimal("115.001")):
            invalid = replace(self.sale, lines=(JournalLine("cash", amount), self.sale.lines[1]))
            with self.assertRaises(AccountingError):
                self.post(invalid)

    def test_duplicate_idempotency_key_rejected(self):
        prior = self.post()
        another = replace(self.sale, id=uuid4())
        with self.assertRaises(AccountingError):
            self.post(another, existing_posted=(prior,))

    def test_no_cross_tenant_posting(self):
        with self.assertRaises(AccountingError):
            self.post(replace(self.sale, tenant_id=self.other_tenant))

    def test_read_only_role_cannot_post(self):
        memberships = (Membership(self.user, self.tenant, Role.VIEWER),)
        with self.assertRaises(AccountingError):
            self.post(memberships=memberships)

    def test_closed_and_outside_period_rejected(self):
        for period in (replace(self.period, closed=True),
                       AccountingPeriod(self.tenant, date(2026, 1, 1), date(2026, 3, 1))):
            with self.assertRaises(AccountingError):
                self.post(period=period)

    def test_inactive_or_other_tenant_account_rejected(self):
        for forbidden in (replace(self.chart["cash"], active=False),
                          replace(self.chart["cash"], tenant_id=self.other_tenant)):
            chart = dict(self.chart, cash=forbidden)
            with self.assertRaises(AccountingError):
                post_entry(self.sale, chart=chart, period=self.period, actor_id=self.user,
                           memberships=self.memberships)

    def test_trial_balance_and_profit_loss_with_traceability(self):
        sale = self.post()
        tb = trial_balance(tenant_id=self.tenant, as_of=date(2026, 10, 15),
                           currency="SAR", chart=self.chart, entries=[sale])
        self.assertTrue(tb.balanced)
        self.assertEqual((tb.total_debit, tb.total_credit), (Decimal(115), Decimal(115)))
        self.assertEqual({r.account_code for r in tb.rows}, {"1000", "4000"})
        self.assertTrue(all(sale.id in row.source_entry_ids for row in tb.rows))
        pnl = profit_and_loss(tenant_id=self.tenant, from_date=date(2026, 1, 1),
                               through_date=date(2026, 10, 15), currency="SAR",
                               chart=self.chart, entries=[sale])
        self.assertEqual(pnl.net_profit, Decimal(115))
        self.assertEqual(pnl.source_entry_ids, (sale.id,))

    def test_tenant_mix_rejected_in_reports(self):
        posted = self.post()
        with self.assertRaises(AccountingError):
            trial_balance(tenant_id=self.other_tenant, as_of=date(2026, 10, 15),
                          currency="SAR", chart=self.chart, entries=[posted])

    def test_nonposted_or_later_entries_excluded_from_reports(self):
        report = trial_balance(tenant_id=self.tenant, as_of=date(2026, 10, 14),
                               currency="SAR", chart=self.chart, entries=[self.sale, self.post()])
        self.assertEqual(report.total_debit, Decimal(0))
        self.assertEqual(report.rows, ())

    def test_fx_not_invented(self):
        posted = self.post()
        with self.assertRaises(AccountingError):
            profit_and_loss(tenant_id=self.tenant, from_date=date(2026, 1, 1),
                            through_date=date(2026, 10, 15), currency="USD",
                            chart=self.chart, entries=[posted])


if __name__ == "__main__":
    unittest.main()
