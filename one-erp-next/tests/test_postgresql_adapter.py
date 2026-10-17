"""Unit contract tests: mocks verify adapter call order and transaction semantics.
These are NOT substitutes for PostgreSQL/RLS integration tests.
"""
import unittest
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from uuid import uuid4

from one_erp_core.accounting import (
    Account, AccountKind, AccountingPeriod, EntryStatus, JournalEntry,
    JournalLine, AccountingError,
)
from one_erp_core.postgresql import persist_and_post
from one_erp_core.tenancy import Membership, Role


class FakeDatabase:
    def __init__(self, *, error_sql=None, return_posted=True):
        self.queries = []
        self.error_sql = error_sql
        self.return_posted = return_posted
        self.entered = 0
        self.committed = 0
        self.rolled_back = 0
        self.last_entry_id = None

    @contextmanager
    def transaction(self):
        self.entered += 1
        try:
            yield
        except Exception:
            self.rolled_back += 1
            raise
        else:
            self.committed += 1

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, params):
        self.queries.append((sql, params))
        if self.error_sql and self.error_sql in sql:
            raise RuntimeError("database operation failed")
        if "SELECT one_post_journal" in sql:
            self.last_entry_id = params[1]

    def fetchone(self):
        return (self.last_entry_id,) if self.return_posted else None


class PostgresAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tenant, self.actor, self.period_id = uuid4(), uuid4(), uuid4()
        self.period = AccountingPeriod(self.tenant, date(2026, 1, 1), date(2027, 1, 1))
        self.accounts = {
            "cash": Account("cash", self.tenant, "100", "Cash", AccountKind.ASSET),
            "revenue": Account("revenue", self.tenant, "400", "Revenue", AccountKind.REVENUE),
        }
        self.entry = JournalEntry(
            uuid4(), self.tenant, "S001", date(2026, 10, 15), "SAR", "sale-1",
            (JournalLine("cash", Decimal("10.00")),
             JournalLine("revenue", credit=Decimal("10.00"))),
        )
        self.memberships = (Membership(self.actor, self.tenant, Role.ACCOUNTANT),)

    def call(self, db, **overrides):
        kw = dict(entry=self.entry, period_id=self.period_id,
                  period=self.period, chart=self.accounts, actor_id=self.actor,
                  memberships=self.memberships)
        kw.update(overrides)
        return persist_and_post(db, **kw)

    def test_one_transaction_contains_all_writes_and_audit_routine(self):
        db = FakeDatabase()
        result = self.call(db)
        self.assertEqual(result.status, EntryStatus.POSTED)
        self.assertEqual((db.entered, db.committed, db.rolled_back), (1, 1, 0))
        sql = " ".join(q for q, _ in db.queries)
        self.assertIn("one.tenant_id", sql)
        self.assertIn("one.actor_id", sql)
        self.assertIn("INSERT INTO one_journal_entries", sql)
        self.assertEqual(sum("INSERT INTO one_journal_lines" in q for q, _ in db.queries), 2)
        self.assertIn("SELECT one_post_journal", sql)

    def test_failure_rolls_back_including_header_and_lines(self):
        db = FakeDatabase(error_sql="SELECT one_post_journal")
        with self.assertRaises(RuntimeError):
            self.call(db)
        self.assertEqual((db.committed, db.rolled_back), (0, 1))

    def test_missing_post_confirmation_rolls_back(self):
        db = FakeDatabase(return_posted=False)
        with self.assertRaises(AccountingError):
            self.call(db)
        self.assertEqual((db.committed, db.rolled_back), (0, 1))

    def test_permission_denied_before_any_db_write(self):
        db = FakeDatabase()
        with self.assertRaises(AccountingError):
            self.call(db, memberships=(Membership(self.actor, self.tenant, Role.VIEWER),))
        self.assertEqual(db.entered, 0)
        self.assertEqual(db.queries, [])

    def test_bad_journal_denied_before_db(self):
        db = FakeDatabase()
        bad_entry = JournalEntry(
            uuid4(), self.tenant, "bad", date(2026, 10, 15), "SAR", "bad",
            (JournalLine("cash", Decimal("10.00")),
             JournalLine("revenue", credit=Decimal("9.00"))),
        )
        with self.assertRaises(AccountingError):
            self.call(db, entry=bad_entry)
        self.assertEqual(db.entered, 0)


if __name__ == "__main__":
    unittest.main()
