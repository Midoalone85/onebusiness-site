"""Focused transactional replay tests. PostgreSQL SQL/role gates need CI too."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import unittest
from unittest.mock import patch
from uuid import uuid4

from one_erp_core.accounting import AccountingError
from one_erp_core.finance_request_replay import (
    _run_once, _fingerprint, FinanceReplayReceipt,
    issue_invoice_once, apply_internal_payment_once,
)
from one_erp_core.tenancy import Membership, Role


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.answer = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.conn.commands.append(sql)
        self.answer = None
        if "SELECT 1 FROM public.one_memberships" in sql:
            self.answer = (1,) if self.conn.member_active else None
        elif sql.startswith("INSERT INTO public.one_finance_requests"):
            tenant, op, rid, actor, digest = params
            key = (tenant, op, rid)
            if key not in self.conn.rows:
                self.conn.rows[key] = (actor, digest, None)
                self.answer = (rid,)
        elif sql.startswith("SELECT actor_id,request_sha256,result_id"):
            self.answer = self.conn.rows.get(tuple(params))
        elif sql.startswith("UPDATE public.one_finance_requests"):
            result, tenant, op, rid, actor, digest = params
            key = (tenant, op, rid)
            if self.conn.rows.get(key) == (actor, digest, None):
                self.conn.rows[key] = (actor, digest, result)
                self.answer = (result,)

    def fetchone(self):
        return self.answer


class FakeConnection:
    """Transaction snapshot suffices to assert rollback; no concurrency claim."""
    def __init__(self):
        self.rows = {}
        self.member_active = True
        self.commands = []

    @contextmanager
    def transaction(self):
        prior = deepcopy(self.rows)
        try:
            yield
        except BaseException:
            self.rows = prior
            raise

    def cursor(self):
        return FakeCursor(self)


@dataclass(frozen=True)
class DummyDocument:
    id: object
    tenant_id: object
    value: Decimal


@dataclass(frozen=True)
class DummyProposal:
    document: DummyDocument


@dataclass(frozen=True)
class DummyPaymentProposal:
    request: DummyDocument


class FinanceSameRequestTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeConnection()
        self.tenant = uuid4()
        self.actor = uuid4()
        self.document = uuid4()
        self.memberships = (Membership(self.actor, self.tenant, Role.ACCOUNTANT),)
        self.writes = 0

    def operation(self, *, payload=None, actor=None, memberships=None,
                  tenant=None, op="internal_invoice", write=None):
        def actual_write():
            self.writes += 1
            return self.document
        return _run_once(
            self.db, operation=op, document_id=self.document,
            tenant_id=tenant or self.tenant, actor_id=actor or self.actor,
            memberships=memberships if memberships is not None else self.memberships,
            payload={"value": Decimal("115.00")} if payload is None else payload,
            write=write or actual_write,
        )

    def test_first_run_and_four_identical_retries_do_not_repeat_write(self):
        first = self.operation()
        self.assertEqual(first, FinanceReplayReceipt("internal_invoice",self.document,False))
        for _ in range(4):
            self.assertEqual(
                self.operation(),
                FinanceReplayReceipt("internal_invoice",self.document,True),
            )
        self.assertEqual(self.writes, 1)
        self.assertEqual(len(self.db.rows), 1)
        self.assertTrue(any("ON CONFLICT" in sql for sql in self.db.commands))
        self.assertTrue(any("FOR UPDATE" in sql for sql in self.db.commands))

    def test_payload_change_same_identity_is_refused(self):
        self.operation()
        with self.assertRaisesRegex(AccountingError,"Conflicting"):
            self.operation(payload={"value":Decimal("116.00")})
        self.assertEqual(self.writes, 1)

    def test_other_actor_cannot_claim_original_receipt(self):
        self.operation()
        other = uuid4()
        with self.assertRaisesRegex(AccountingError,"Conflicting"):
            self.operation(actor=other,memberships=(
                Membership(other,self.tenant,Role.ACCOUNTANT),))
        self.assertEqual(self.writes, 1)

    def test_viewer_and_cross_company_and_disabled_db_access(self):
        with self.assertRaises(PermissionError):
            self.operation(memberships=(Membership(self.actor,self.tenant,Role.VIEWER),))
        with self.assertRaises(PermissionError):
            self.operation(tenant=uuid4())
        self.db.member_active = False
        with self.assertRaises(PermissionError):
            self.operation()
        self.assertEqual(self.writes, 0)
        self.assertEqual(self.db.rows, {})

    def test_failed_write_rolls_back_reservation_and_retries_normally(self):
        def fail():
            raise RuntimeError("transaction failed")
        with self.assertRaisesRegex(RuntimeError,"transaction failed"):
            self.operation(write=fail)
        self.assertEqual(self.db.rows, {})
        self.assertFalse(self.operation().replayed)

    def test_result_id_must_match_request_and_rollback(self):
        with self.assertRaisesRegex(AccountingError,"identity differs"):
            self.operation(write=lambda: uuid4())
        self.assertEqual(self.db.rows, {})

    def test_pending_or_inconsistent_result_fails_closed(self):
        h = _fingerprint("internal_invoice", {"value":Decimal("115.00")})
        self.db.rows[(self.tenant,"internal_invoice",self.document)] = (
            self.actor,h,None)
        with self.assertRaisesRegex(AccountingError,"Incomplete"):
            self.operation()
        self.assertEqual(self.writes,0)

    def test_operation_and_tenant_are_independent_identities(self):
        self.operation()
        self.operation(op="internal_payment")
        self.assertEqual(self.writes,2)
        tenant2 = uuid4()
        self.operation(tenant=tenant2,memberships=(
            Membership(self.actor,tenant2,Role.ACCOUNTANT),))
        self.assertEqual(len(self.db.rows),3)

    def test_fingerprint_deterministic_and_rejects_float_and_nan(self):
        a={"money":Decimal("15.00"),"code":"SAR"}
        b={"code":"SAR","money":Decimal("15.00")}
        self.assertEqual(_fingerprint("internal_payment",a),
                         _fingerprint("internal_payment",b))
        with self.assertRaises(AccountingError):
            _fingerprint("internal_payment",{"amount":float("nan")})
        with self.assertRaises(AccountingError):
            _fingerprint("internal_payment",{"amount":Decimal("NaN")})

    def test_invoice_adapter_calls_persistence_once(self):
        doc = DummyDocument(self.document,self.tenant,Decimal("115.00"))
        with patch("one_erp_core.invoice_persistence.persist_invoice_with_journal",
                   return_value=DummyProposal(doc)) as saved:
            opts=dict(
                document=doc,accounts={"ar":"x"},chart={},period="fixture",
                period_id=uuid4(),actor_id=self.actor,memberships=self.memberships,
            )
            first=issue_invoice_once(self.db,**opts)
            later=issue_invoice_once(self.db,**opts)
        self.assertFalse(first.replayed)
        self.assertTrue(later.replayed)
        self.assertEqual(saved.call_count,1)

    def test_payment_adapter_ignores_changed_outstanding_snapshot_on_retry(self):
        doc = DummyDocument(self.document,self.tenant,Decimal("115.00"))
        with patch("one_erp_core.payment_persistence.persist_payment_with_allocations",
                   return_value=DummyPaymentProposal(doc)) as saved:
            opts=dict(
                payment=doc,invoices=("initial",),accounts={"bank":"x"},
                chart={},period="fixture",period_id=uuid4(),
                actor_id=self.actor,memberships=self.memberships,
            )
            first=apply_internal_payment_once(self.db,**opts)
            opts["invoices"]=("after_first_settlement",)
            later=apply_internal_payment_once(self.db,**opts)
        self.assertFalse(first.replayed)
        self.assertTrue(later.replayed)
        self.assertEqual(saved.call_count,1)


if __name__ == "__main__":
    unittest.main()
