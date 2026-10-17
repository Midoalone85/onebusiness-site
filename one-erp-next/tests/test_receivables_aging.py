"""Receivables aging contracts, separate from actual PostgreSQL acceptance.

All amounts are exact decimal strings; no payment gateway, banking or
legal ZATCA e-invoice is contacted. This only REPORTS approved GL sources.
"""
import unittest
from datetime import date,timedelta
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.operations_read_repository import _aging_row

BASE=date(2026,10,20)


def item(due, gross="200.00", paid="60.00", credit="10.00"):
    return (
        uuid4(),"INV-001","customer-ci","SAR",
        date(2026,10,17),due,Decimal(gross),Decimal(paid),Decimal(credit),
    )


class ReceivablesAgingPolicyTests(unittest.TestCase):
    def test_exact_decimal_balance_and_private_identifiers(self):
        x=_aging_row(item(date(2026,11,17)),BASE)
        self.assertEqual(x["gross"],"200.00")
        self.assertEqual(x["applied_receipts"],"60.00")
        self.assertEqual(x["issued_credits"],"10.00")
        self.assertEqual(x["outstanding"],"130.00")
        self.assertEqual(x["aging_bucket"],"current")
        self.assertEqual(x["days_overdue"],0)
        self.assertEqual(x["status"],"open")
        self.assertEqual(x["currency"],"SAR")

    def test_all_bucket_boundaries_as_of_date(self):
        for days,expected in (
            (-1,"current"),(0,"current"),
            (1,"1_30"),(30,"1_30"),
            (31,"31_60"),(60,"31_60"),
            (61,"61_90"),(90,"61_90"),(91,"over_90"),
        ):
            with self.subTest(days=days):
                x=_aging_row(item(BASE-timedelta(days=days)),BASE)
                self.assertEqual(x["aging_bucket"],expected)
                self.assertEqual(x["days_overdue"],max(0,days))

    def test_fully_paid_invoices_are_reported_as_settled(self):
        x=_aging_row(item(BASE-timedelta(days=120),"200.00","200.00","0"),BASE)
        self.assertEqual(x["outstanding"],"0.00")
        self.assertEqual(x["days_overdue"],0)
        self.assertEqual(x["status"],"settled")
        self.assertEqual(x["aging_bucket"],"settled")

    def test_overallocated_payments_and_credits_are_not_silently_hidden(self):
        for gross,paid,credit in (
            ("200.00","200.01","0"),("200.00","150.00","51.00"),
            ("200.00","0","201"),
        ):
            with self.subTest(gross=gross,paid=paid,credit=credit):
                with self.assertRaises(AccountingError):
                    _aging_row(item(BASE,gross,paid,credit),BASE)

    def test_nan_negative_and_float_values_fail_closed(self):
        for n in ("NaN","Infinity","-1.00"):
            with self.subTest(number=n):
                for pos in (6,7,8):
                    row=list(item(BASE))
                    row[pos]=Decimal(n)
                    with self.assertRaises(AccountingError):
                        _aging_row(tuple(row),BASE)
        row=list(item(BASE))
        row[6]=200.0
        with self.assertRaises(AccountingError):
            _aging_row(tuple(row),BASE)

    def test_different_currencies_are_not_converted_or_aggregated(self):
        row=list(item(BASE))
        row[3]="USD"
        result=_aging_row(tuple(row),BASE)
        self.assertEqual(result["currency"],"USD")
        self.assertEqual(result["outstanding"],"130.00")

if __name__=="__main__":
    unittest.main()
