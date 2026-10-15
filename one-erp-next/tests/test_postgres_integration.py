"""PostgreSQL 14 integration checks on a disposable CI database only.

Set ONE_TEST_PG_DSN for a dedicated empty test database owned by a superuser.
Never point these destructive fixture/setup checks at production.
"""
import os
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from one_erp_core.accounting import (
    Account, AccountKind, AccountingPeriod, JournalEntry, JournalLine,
)
from one_erp_core.postgresql import persist_and_post, close_accounting_period
from one_erp_core.reversals import make_reversal
from one_erp_core.invoicing import (
    DocumentType, TaxCategory, InvoiceLine, InvoiceDocument, InvoiceLedgerAccounts,
)
from one_erp_core.invoice_persistence import persist_invoice_with_journal
from one_erp_core.payments import (
    PaymentRequest, PaymentDirection, PaymentLedgerAccounts,
    PaymentAllocation, OpenInvoice,
)
from one_erp_core.payment_persistence import persist_payment_with_allocations
from one_erp_core.report_repository import load_finance_snapshot
from one_erp_core.finance_http_boundary import (
    AuthenticatedFinanceIdentity,render_private_dashboard_response,
)
from one_erp_core.statement_details import balance_sheet, general_ledger_detail
from one_erp_core.tenancy import Membership, Role

DSN = os.environ.get("ONE_TEST_PG_DSN")

@unittest.skipUnless(DSN, "PostgreSQL integration suite requires ONE_TEST_PG_DSN")
class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.psycopg = psycopg
        cls.admin = psycopg.connect(DSN, autocommit=True)
        migration_dir = Path(__file__).resolve().parents[1] / "db"
        for migration in sorted(migration_dir.glob("[0-9][0-9][0-9]_*.sql")):
            cls.admin.execute(migration.read_text(encoding="utf-8"), prepare=False)
        cls.admin.execute(
            (migration_dir.parent / "ops" / "report_reader_privileges.sql").read_text(encoding="utf-8"),
            prepare=False,
        )
        cls.admin.execute(
            "CREATE ROLE one_erp_report_ci LOGIN PASSWORD 'ci_report_only' "
            "NOSUPERUSER NOBYPASSRLS"
        )
        cls.admin.execute(
            "GRANT one_erp_finance_report_reader TO one_erp_report_ci"
        )
        # Ephemeral CI-only role. The broad DML grants below allow tests to
        # verify RLS and triggers; never copy them into production grants.
        cls.admin.execute(
            "CREATE ROLE one_erp_runtime_ci LOGIN PASSWORD 'ci_only_password' "
            "NOSUPERUSER NOBYPASSRLS"
        )
        cls.admin.execute("GRANT USAGE ON SCHEMA public TO one_erp_runtime_ci")
        cls.admin.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO one_erp_runtime_ci")
        cls.admin.execute("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_post_journal(uuid,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_close_period(uuid,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_issue_invoice(uuid,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_apply_payment(uuid,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.db = psycopg.connect(
            host=os.environ.get("ONE_TEST_PG_HOST", "localhost"),
            port=int(os.environ.get("ONE_TEST_PG_PORT", "5432")),
            dbname=os.environ.get("ONE_TEST_PG_DB", "one_erp_test"),
            user="one_erp_runtime_ci", password="ci_only_password", autocommit=True,
        )
        cls.reporting_db = psycopg.connect(
            host=os.environ.get("ONE_TEST_PG_HOST", "localhost"),
            port=int(os.environ.get("ONE_TEST_PG_PORT", "5432")),
            dbname=os.environ.get("ONE_TEST_PG_DB", "one_erp_test"),
            user="one_erp_report_ci", password="ci_report_only", autocommit=True,
        )
        cls.tenant_a, cls.tenant_b, cls.actor = uuid4(), uuid4(), uuid4()
        cls.period_id = uuid4()
        for tid in (cls.tenant_a, cls.tenant_b):
            cls.admin.execute(
                "INSERT INTO one_tenants(id,company_name) VALUES (%s,%s)",
                (tid, "Test Tenant"),
            )
        cls.admin.execute(
            "INSERT INTO one_memberships(tenant_id,actor_id,role) VALUES (%s,%s,%s)",
            (cls.tenant_a, cls.actor, "accountant"),
        )
        cls.admin.execute(
            """INSERT INTO one_periods(tenant_id,id,begins,ends_exclusive)
               VALUES (%s,%s,%s,%s)""",
            (cls.tenant_a, cls.period_id, date(2026,1,1), date(2027,1,1)),
        )
        cls.chart = {
            "cash": Account("cash", cls.tenant_a, "100", "Cash", AccountKind.ASSET),
            "sales": Account("sales", cls.tenant_a, "400", "Sales", AccountKind.REVENUE),
        }
        for item in cls.chart.values():
            cls.admin.execute(
                """INSERT INTO one_accounts
                   (tenant_id,id,code,name,kind) VALUES (%s,%s,%s,%s,%s)""",
                (item.tenant_id,item.id,item.code,item.name,item.kind.value),
            )
        for name, code, kind in (
            ("ar","120",AccountKind.ASSET),
            ("ap","220",AccountKind.LIABILITY),
            ("expense","500",AccountKind.EXPENSE),
            ("vatout","230",AccountKind.LIABILITY),
            ("vatin","130",AccountKind.ASSET),
        ):
            item = Account(name,cls.tenant_a,code,name,kind)
            cls.chart[name] = item
            cls.admin.execute(
                """INSERT INTO one_accounts(tenant_id,id,code,name,kind)
                   VALUES (%s,%s,%s,%s,%s)""",
                (item.tenant_id,item.id,item.code,item.name,item.kind.value),
            )
        cls.invoice_accounts = InvoiceLedgerAccounts("ar","ap","vatout","vatin")
        cls.period = AccountingPeriod(cls.tenant_a, date(2026,1,1), date(2027,1,1))
        cls.memberships = (Membership(cls.actor, cls.tenant_a, Role.ACCOUNTANT),)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "reporting_db"):
            cls.reporting_db.close()
        if hasattr(cls, "db"):
            cls.db.close()
        if hasattr(cls, "admin"):
            cls.admin.close()

    def fresh_entry(self, *, idempotency_key=None):
        return JournalEntry(
            uuid4(), self.tenant_a, "S001", date(2026,10,15), "SAR",
            idempotency_key or str(uuid4()),
            (JournalLine("cash", Decimal("10.00")),
             JournalLine("sales", credit=Decimal("10.00"))),
        )

    def post(self, entry):
        return persist_and_post(
            self.db, entry=entry, period_id=self.period_id, period=self.period,
            chart=self.chart, actor_id=self.actor, memberships=self.memberships,
        )

    def test_a_commit_with_audit_and_immutable_posted_lines(self):
        entry = self.fresh_entry()
        self.post(entry)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)", (str(self.tenant_a),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)", (str(self.actor),))
            status = self.db.execute(
                "SELECT status FROM one_journal_entries WHERE tenant_id=%s AND id=%s",
                (self.tenant_a,entry.id),
            ).fetchone()[0]
            count = self.db.execute(
                "SELECT count(*) FROM one_ledger_audit WHERE tenant_id=%s AND entry_id=%s",
                (self.tenant_a,entry.id),
            ).fetchone()[0]
        self.assertEqual(status, "posted")
        self.assertEqual(count, 1)
        with self.assertRaises(self.psycopg.Error):
            with self.db.transaction():
                self.db.execute("SELECT set_config('one.tenant_id',%s,true)", (str(self.tenant_a),))
                self.db.execute(
                    """UPDATE one_journal_lines SET debit=11
                       WHERE tenant_id=%s AND entry_id=%s AND line_number=1""",
                    (self.tenant_a,entry.id),
                )

    def test_duplicate_key_rolls_back_cleanly(self):
        entry = self.fresh_entry(idempotency_key=str(uuid4()))
        self.post(entry)
        clone = self.fresh_entry(idempotency_key=entry.idempotency_key)
        with self.assertRaises(self.psycopg.Error):
            self.post(clone)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)", (str(self.tenant_a),))
            n = self.db.execute(
                "SELECT count(*) FROM one_journal_entries WHERE idempotency_key=%s",
                (entry.idempotency_key,),
            ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_cross_company_visibility_is_denied(self):
        entry = self.fresh_entry()
        self.post(entry)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)", (str(self.tenant_b),))
            n = self.db.execute("SELECT count(*) FROM one_journal_entries").fetchone()[0]
        self.assertEqual(n, 0)

    def test_server_side_currency_precision(self):
        entry = self.fresh_entry()
        self.post(entry)
        bad = self.fresh_entry()
        with self.assertRaises(self.psycopg.Error):
            with self.db.transaction():
                self.db.execute("SELECT set_config('one.tenant_id',%s,true)", (str(self.tenant_a),))
                self.db.execute("SELECT set_config('one.actor_id',%s,true)", (str(self.actor),))
                self.db.execute(
                    """INSERT INTO one_journal_entries
                       (tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (bad.tenant_id,bad.id,self.period_id,bad.reference,bad.idempotency_key,
                     bad.occurred_on,bad.currency),
                )
                self.db.execute(
                    """INSERT INTO one_journal_lines
                       (tenant_id,entry_id,line_number,account_id,debit,credit)
                       VALUES (%s,%s,1,'cash',10.001,0),(%s,%s,2,'sales',0,10.001)""",
                    (bad.tenant_id,bad.id,bad.tenant_id,bad.id),
                )
                self.db.execute(
                    "SELECT one_post_journal(%s,%s,%s)",
                    (bad.tenant_id,bad.id,self.actor),
                )


    def test_post_source_and_exact_reversal(self):
        source = self.post(self.fresh_entry())
        reversal = make_reversal(source, reversal_on=date(2026,10,16),reason="Invoice correction")
        result = self.post(reversal)
        self.assertEqual(result.reversal_of, source.id)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)", (str(self.tenant_a),))
            src_count = self.db.execute(
                "SELECT count(*) FROM one_journal_entries WHERE id=%s AND status='posted'",
                (source.id,),
            ).fetchone()[0]
            rev_count = self.db.execute(
                "SELECT count(*) FROM one_journal_entries WHERE reversal_of=%s AND status='posted'",
                (source.id,),
            ).fetchone()[0]
        self.assertEqual((src_count,rev_count), (1,1))
        with self.assertRaises(self.psycopg.Error):
            self.post(make_reversal(source,reversal_on=date(2026,10,17),reason="Again"))

    def test_db_refuses_balanced_but_inexact_reversal(self):
        source = self.post(self.fresh_entry())
        reversal = make_reversal(source, reversal_on=date(2026,10,16),reason="Correct")
        wrong_lines = (
            JournalLine("cash",credit=Decimal("9.00")),
            JournalLine("sales",debit=Decimal("9.00")),
        )
        with self.assertRaises(self.psycopg.Error):
            self.post(replace(reversal,lines=wrong_lines))

    def test_admin_closes_period_with_audit_and_future_post_denied(self):
        period_id, admin_actor = uuid4(), uuid4()
        self.admin.execute(
            """INSERT INTO one_periods(tenant_id,id,begins,ends_exclusive)
               VALUES (%s,%s,%s,%s)""",
            (self.tenant_a,period_id,date(2027,1,1),date(2028,1,1)),
        )
        self.admin.execute(
            "INSERT INTO one_memberships(tenant_id,actor_id,role) VALUES (%s,%s,%s)",
            (self.tenant_a,admin_actor,"company_admin"),
        )
        self.assertEqual(close_accounting_period(
            self.db,tenant_id=self.tenant_a,period_id=period_id,actor_id=admin_actor,
        ),period_id)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)", (str(self.tenant_a),))
            audit=self.db.execute(
                "SELECT count(*) FROM one_period_close_audit WHERE tenant_id=%s AND period_id=%s",
                (self.tenant_a,period_id),
            ).fetchone()[0]
        self.assertEqual(audit,1)
        with self.assertRaises(self.psycopg.Error):
            close_accounting_period(self.db,tenant_id=self.tenant_a,
                                    period_id=period_id,actor_id=admin_actor)
        late=replace(self.fresh_entry(),occurred_on=date(2027,1,2))
        with self.assertRaises(self.psycopg.Error):
            persist_and_post(
                self.db,entry=late,period_id=period_id,
                period=AccountingPeriod(self.tenant_a,date(2027,1,1),date(2028,1,1)),
                chart=self.chart,actor_id=self.actor,memberships=self.memberships,
            )

    def test_accountant_cannot_close_period(self):
        with self.assertRaises(self.psycopg.Error):
            close_accounting_period(self.db,tenant_id=self.tenant_a,
                                    period_id=self.period_id,actor_id=self.actor)



    def invoice(self, *, kind=DocumentType.CUSTOMER_INVOICE,
                number=None, source_invoice_id=None,
                price=Decimal("100.00")):
        line=InvoiceLine(
            "Example item", "sales" if kind in (
                DocumentType.CUSTOMER_INVOICE,DocumentType.CUSTOMER_CREDIT
            ) else "expense",
            Decimal("1"),price,
            tax_category=TaxCategory.STANDARD,tax_rate=Decimal("0.15"),
            tax_code="DEMO-STANDARD",input_tax_recoverable=kind in (
                DocumentType.VENDOR_BILL,DocumentType.VENDOR_CREDIT
            ),
        )
        return InvoiceDocument(
            uuid4(),self.tenant_a,number or str(uuid4()),"partner-ci",
            kind,date(2026,10,15),date(2026,11,15),"SAR",(line,),
            source_invoice_id=source_invoice_id,
        )

    def post_invoice(self, document):
        return persist_invoice_with_journal(
            self.db,document=document,accounts=self.invoice_accounts,
            chart=self.chart,period=self.period,period_id=self.period_id,
            actor_id=self.actor,memberships=self.memberships,
        )

    def test_internal_invoice_issuance_is_atomic_and_audited(self):
        doc=self.invoice()
        proposal=self.post_invoice(doc)
        self.assertEqual(proposal.totals.gross,Decimal("115.00"))
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",(str(self.tenant_a),))
            status=self.db.execute(
                "SELECT status FROM one_invoice_docs WHERE tenant_id=%s AND id=%s",
                (self.tenant_a,doc.id),
            ).fetchone()[0]
            gl=self.db.execute(
                "SELECT status FROM one_journal_entries WHERE tenant_id=%s AND id=%s",
                (self.tenant_a,doc.id),
            ).fetchone()[0]
            audits=self.db.execute(
                "SELECT count(*) FROM one_invoice_audit WHERE tenant_id=%s AND doc_id=%s",
                (self.tenant_a,doc.id),
            ).fetchone()[0]
        self.assertEqual((status,gl,audits),("issued","posted",1))

    def test_issued_invoice_and_lines_are_immutable(self):
        doc=self.invoice()
        self.post_invoice(doc)
        for sql in (
            "UPDATE one_invoice_docs SET partner_id='tampered' WHERE tenant_id=%s AND id=%s",
            "DELETE FROM one_invoice_lines WHERE tenant_id=%s AND doc_id=%s",
        ):
            with self.assertRaises(self.psycopg.Error):
                with self.db.transaction():
                    self.db.execute("SELECT set_config('one.tenant_id',%s,true)",(str(self.tenant_a),))
                    self.db.execute(sql,(self.tenant_a,doc.id))

    def test_duplicate_invoice_number_rolls_back_new_gl(self):
        number=str(uuid4())
        doc=self.invoice(number=number)
        self.post_invoice(doc)
        other=self.invoice(number=number)
        with self.assertRaises(self.psycopg.Error):
            self.post_invoice(other)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",(str(self.tenant_a),))
            count=self.db.execute(
                "SELECT count(*) FROM one_journal_entries WHERE tenant_id=%s AND id=%s",
                (self.tenant_a,other.id),
            ).fetchone()[0]
        self.assertEqual(count,0)

    def test_credit_note_is_bounded_by_issued_source(self):
        doc=self.invoice()
        self.post_invoice(doc)
        partial=self.invoice(kind=DocumentType.CUSTOMER_CREDIT,
                             source_invoice_id=doc.id,price=Decimal("40.00"))
        self.post_invoice(partial)
        too_much=self.invoice(kind=DocumentType.CUSTOMER_CREDIT,
                              source_invoice_id=doc.id,price=Decimal("75.00"))
        with self.assertRaises(self.psycopg.Error):
            self.post_invoice(too_much)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",(str(self.tenant_a),))
            count=self.db.execute(
                "SELECT count(*) FROM one_invoice_docs WHERE source_invoice_id=%s AND status='issued'",
                (doc.id,),
            ).fetchone()[0]
        self.assertEqual(count,1)

    def test_vendor_bill_and_vendor_credit_note_internal_issue(self):
        bill=self.invoice(kind=DocumentType.VENDOR_BILL)
        self.post_invoice(bill)
        credit=self.invoice(kind=DocumentType.VENDOR_CREDIT,
                             source_invoice_id=bill.id,price=Decimal("20.00"))
        self.post_invoice(credit)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",(str(self.tenant_a),))
            count=self.db.execute(
                "SELECT count(*) FROM one_invoice_docs WHERE id IN (%s,%s) AND status='issued'",
                (bill.id,credit.id),
            ).fetchone()[0]
        self.assertEqual(count,2)



    def make_payment(self, source, amount, *, bank_reference=None,
                     occurred_on=date(2026,10,16)):
        return PaymentRequest(
            uuid4(),self.tenant_a,source.partner_id,occurred_on,"SAR",
            PaymentDirection.CUSTOMER_RECEIPT,amount,
            (PaymentAllocation(source.id,amount),),
            bank_reference or str(uuid4()),
        )

    def post_payment(self, payment, source, *, outstanding=None):
        remaining=source.lines[0].unit_price * source.lines[0].quantity
        remaining=(remaining * Decimal("1.15")).quantize(Decimal("0.01"))
        prior=OpenInvoice(
            source.id,self.tenant_a,source.partner_id,"SAR",source.issued_on,
            remaining if outstanding is None else outstanding,True,
        )
        return persist_payment_with_allocations(
            self.db,payment=payment,invoices=(prior,),
            accounts=PaymentLedgerAccounts("cash","ar","ap"),chart=self.chart,
            period=self.period,period_id=self.period_id,actor_id=self.actor,
            memberships=self.memberships,
        )

    def test_partial_payments_use_db_outstanding_not_stale_python_data(self):
        doc=self.invoice(price=Decimal("100.00"))
        self.post_invoice(doc)
        p1=self.make_payment(doc,Decimal("60.00"))
        p2=self.make_payment(doc,Decimal("55.00"))
        self.post_payment(p1,doc)
        self.post_payment(p2,doc)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",(str(self.tenant_a),))
            count=self.db.execute(
                "SELECT count(*) FROM one_payment_docs WHERE id IN (%s,%s) AND status='applied'",
                (p1.id,p2.id),
            ).fetchone()[0]
        self.assertEqual(count,2)
        # Fake stale Python snapshot still claims 115 due. DB catches overpay.
        p3=self.make_payment(doc,Decimal("1.00"))
        with self.assertRaises(self.psycopg.Error):
            self.post_payment(p3,doc)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",(str(self.tenant_a),))
            count=self.db.execute(
                "SELECT count(*) FROM one_journal_entries WHERE id=%s",
                (p3.id,),
            ).fetchone()[0]
        self.assertEqual(count,0)

    def test_payment_and_credit_note_cannot_exceed_same_invoice(self):
        doc=self.invoice()
        self.post_invoice(doc)
        credit=self.invoice(kind=DocumentType.CUSTOMER_CREDIT,
                            source_invoice_id=doc.id,price=Decimal("50.00"))
        self.post_invoice(credit)  # 57.50 credited, 57.50 remains due
        payment=self.make_payment(doc,Decimal("57.50"))
        self.post_payment(payment,doc)
        excess_credit=self.invoice(kind=DocumentType.CUSTOMER_CREDIT,
                            source_invoice_id=doc.id,price=Decimal("1.00"))
        with self.assertRaises(self.psycopg.Error):
            self.post_invoice(excess_credit)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",(str(self.tenant_a),))
            count=self.db.execute(
                "SELECT count(*) FROM one_invoice_docs WHERE id=%s",
                (excess_credit.id,),
            ).fetchone()[0]
        self.assertEqual(count,0)

    def test_payment_is_immutable_after_internal_application(self):
        doc=self.invoice()
        self.post_invoice(doc)
        payment=self.make_payment(doc,Decimal("50.00"))
        self.post_payment(payment,doc)
        with self.assertRaises(self.psycopg.Error):
            with self.db.transaction():
                self.db.execute("SELECT set_config('one.tenant_id',%s,true)",(str(self.tenant_a),))
                self.db.execute(
                    """UPDATE one_payment_allocations SET amount=49
                       WHERE tenant_id=%s AND payment_id=%s""",
                    (self.tenant_a,payment.id),
                )



    def test_real_db_reporting_snapshot_is_consistent_and_traceable(self):
        posted=self.post(self.fresh_entry())
        snapshot=load_finance_snapshot(
            self.db,tenant_id=self.tenant_a,actor_id=self.actor,
            as_of=date(2026,10,15),
        )
        self.assertIn(posted.id,{entry.id for entry in snapshot.entries})
        self.assertEqual(snapshot.tenant_id,self.tenant_a)
        self.assertEqual(snapshot.actor_id,self.actor)
        self.assertEqual(snapshot.memberships[0].role,Role.ACCOUNTANT)
        self.assertEqual(snapshot.chart["cash"].kind,AccountKind.ASSET)
        for e in snapshot.entries:
            self.assertEqual(e.status.value,"posted")
            self.assertGreaterEqual(len(e.lines),2)
            self.assertEqual(sum(l.debit for l in e.lines),
                             sum(l.credit for l in e.lines))
        report=balance_sheet(
            tenant_id=self.tenant_a,as_of=date(2026,10,15),currency="SAR",
            chart=snapshot.chart,entries=snapshot.entries,
            actor_id=self.actor,memberships=snapshot.memberships,
        )
        self.assertTrue(report.balanced)
        self.assertIn(posted.id,report.source_entry_ids)

    def test_db_snapshot_denies_actor_without_membership(self):
        with self.assertRaises(Exception):
            load_finance_snapshot(
                self.db,tenant_id=self.tenant_a,actor_id=uuid4(),
                as_of=date(2026,10,15),
            )

    def test_db_snapshot_enforces_tenant_scope(self):
        with self.assertRaises(Exception):
            load_finance_snapshot(
                self.db,tenant_id=self.tenant_b,actor_id=self.actor,
                as_of=date(2026,10,15),
            )

    def test_db_snapshot_deny_superuser_role(self):
        with self.assertRaises(Exception):
            load_finance_snapshot(
                self.admin,tenant_id=self.tenant_a,actor_id=self.actor,
                as_of=date(2026,10,15),
            )

    def test_db_snapshot_respects_asof_cutoff(self):
        later=self.post(self.fresh_entry())
        old=load_finance_snapshot(
            self.db,tenant_id=self.tenant_a,actor_id=self.actor,
            as_of=date(2026,10,14),
        )
        today=load_finance_snapshot(
            self.db,tenant_id=self.tenant_a,actor_id=self.actor,
            as_of=date(2026,10,15),
        )
        self.assertNotIn(later.id,{e.id for e in old.entries})
        self.assertIn(later.id,{e.id for e in today.entries})



    def test_authenticated_private_dashboard_uses_tenant_db_name(self):
        report_entry=self.post(self.fresh_entry())
        class DemoVerifier:
            def __init__(self,actor,tenant):
                self.actor=actor
                self.tenant=tenant
            def verify_access_token(self,raw_token):
                if raw_token != "test-session":
                    raise ValueError("invalid test token")
                return AuthenticatedFinanceIdentity(
                    self.actor,frozenset({self.tenant})
                )

        response=render_private_dashboard_response(
            self.db,
            raw_access_token="test-session",
            identity_verifier=DemoVerifier(self.actor,self.tenant_a),
            requested_tenant=self.tenant_a,
            from_date=date(2026,10,1),
            as_of=date(2026,10,15),
            currency="SAR",
            locale="ar",
        )
        self.assertEqual(response.status,200)
        self.assertIn("no-store",response.headers["Cache-Control"])
        self.assertIn("frame-ancestors 'none'",response.headers["Content-Security-Policy"])
        self.assertIn('lang="ar" dir="rtl"',response.body)
        self.assertIn("Test Tenant",response.body)
        self.assertIn("ONE ERP",response.body)
        self.assertIn(str(report_entry.id),response.body)
        self.assertNotIn("test-session",response.body)



    def test_restricted_reporting_role_reads_and_never_writes(self):
        self.post(self.fresh_entry())
        snapshot = load_finance_snapshot(
            self.reporting_db, tenant_id=self.tenant_a, actor_id=self.actor,
            as_of=date(2026,10,15),
        )
        self.assertTrue(snapshot.entries)
        self.assertEqual(snapshot.company_name,"Test Tenant")
        with self.reporting_db.transaction():
            self.reporting_db.execute(
                "SELECT set_config('one.tenant_id',%s,true)",
                (str(self.tenant_a),),
            )
            privileges = self.reporting_db.execute(
                """SELECT has_table_privilege(current_user,'one_accounts','SELECT'),
                          has_table_privilege(current_user,'one_accounts','UPDATE'),
                          has_table_privilege(current_user,'one_invoice_docs','SELECT'),
                          has_function_privilege(current_user,
                            'one_post_journal(uuid,uuid,uuid)','EXECUTE')"""
            ).fetchone()
            self.assertEqual(privileges,(True,False,False,False))
        with self.assertRaises(self.psycopg.Error):
            with self.reporting_db.transaction():
                self.reporting_db.execute(
                    "SELECT set_config('one.tenant_id',%s,true)",
                    (str(self.tenant_a),),
                )
                self.reporting_db.execute(
                    "UPDATE one_accounts SET name='Illegal write' WHERE tenant_id=%s",
                    (self.tenant_a,),
                )

    def test_report_role_still_requires_active_db_membership(self):
        with self.assertRaises(Exception):
            load_finance_snapshot(
                self.reporting_db, tenant_id=self.tenant_b,
                actor_id=self.actor, as_of=date(2026,10,15),
            )



if __name__ == "__main__":
    unittest.main()
