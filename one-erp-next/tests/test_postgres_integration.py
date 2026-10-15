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
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from one_erp_core.control_plane_identity import (
    ControlPlaneIdentityDirectory, ControlPlaneRevocationChecker,
)
from one_erp_core.oidc_identity import RS256VerifierConfig, VerifiedRS256FinanceAccess
from one_erp_core.finance_wsgi import OneFinanceWSGI, TenantFinanceConnections
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
            (migration_dir.parent / "control_plane" / "001_identity_registry.sql")
            .read_text(encoding="utf-8"), prepare=False,
        )
        cls.admin.execute(
            (migration_dir.parent / "control_plane" / "002_audited_provisioning.sql")
            .read_text(encoding="utf-8"), prepare=False,
        )
        cls.admin.execute(
            (migration_dir.parent / "control_plane" / "003_tenant_bootstrap_gate.sql")
            .read_text(encoding="utf-8"), prepare=False,
        )
        cls.admin.execute(
            "CREATE ROLE one_erp_auth_ci LOGIN PASSWORD 'ci_auth_only' "
            "NOSUPERUSER NOBYPASSRLS"
        )
        cls.admin.execute("GRANT one_erp_auth_lookup TO one_erp_auth_ci")
        cls.admin.execute(
            "CREATE ROLE one_erp_auth_provision_ci LOGIN PASSWORD 'ci_provision_only' "
            "NOSUPERUSER NOBYPASSRLS"
        )
        cls.admin.execute(
            "GRANT one_erp_auth_provisioner TO one_erp_auth_provision_ci"
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
        cls.admin.execute(
            """INSERT INTO one_auth.subject_links(issuer,subject,actor_id)
               VALUES (%s,%s,%s)""",
            ("https://identity.example.test","ci_subject",cls.actor),
        )
        cls.admin.execute(
            """INSERT INTO one_auth.tenant_entitlements(actor_id,tenant_id)
               VALUES (%s,%s)""",
            (cls.actor,cls.tenant_a),
        )
        cls.admin.execute(
            """INSERT INTO one_auth.tenant_registry(tenant_id,active)
               VALUES (%s,true),(%s,true)""",
            (cls.tenant_a,cls.tenant_b),
        )

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



    def auth_connection(self):
        return self.psycopg.connect(
            host=os.environ.get("ONE_TEST_PG_HOST", "localhost"),
            port=int(os.environ.get("ONE_TEST_PG_PORT", "5432")),
            dbname=os.environ.get("ONE_TEST_PG_DB", "one_erp_test"),
            user="one_erp_auth_ci", password="ci_auth_only", autocommit=True,
        )

    def test_verified_identity_resolves_server_side_tenant_grants(self):
        directory=ControlPlaneIdentityDirectory(self.auth_connection)
        identity=directory.resolve("https://identity.example.test","ci_subject")
        self.assertEqual(identity.actor_id,self.actor)
        self.assertEqual(identity.allowed_tenants,frozenset({self.tenant_a}))
        with self.assertRaises(Exception):
            directory.resolve("https://identity.example.test","invalid-subject")
        with self.assertRaises(Exception):
            directory.resolve("https://other-issuer.test","ci_subject")

    def test_identity_control_plane_role_is_read_only(self):
        db=self.auth_connection()
        try:
            with db.transaction():
                privileges=db.execute(
                    """SELECT has_table_privilege(current_user,
                         'one_auth.subject_links','SELECT'),
                              has_table_privilege(current_user,
                         'one_auth.subject_links','INSERT'),
                              has_table_privilege(current_user,
                         'one_journal_entries','SELECT')"""
                ).fetchone()
                self.assertEqual(privileges,(True,False,False))
            with self.assertRaises(self.psycopg.Error):
                with db.transaction():
                    db.execute(
                        """INSERT INTO one_auth.tenant_entitlements(actor_id,tenant_id)
                           VALUES (%s,%s)""",(uuid4(),uuid4()),
                    )
        finally:
            db.close()

    def test_hashed_revocation_flags_blocked_token_without_plaintext_storage(self):
        issuer="https://identity.example.test"
        blocked_id="revoked-ci-demo"
        digest=sha256((issuer+"\0"+blocked_id).encode("utf-8")).digest()
        self.admin.execute(
            """INSERT INTO one_auth.revoked_jti(token_digest,expires_at)
               VALUES (%s,%s)""",
            (digest, datetime.now(timezone.utc)+timedelta(hours=1)),
        )
        checker=ControlPlaneRevocationChecker(self.auth_connection)
        self.assertTrue(checker.is_revoked(issuer,blocked_id))
        self.assertFalse(checker.is_revoked(issuer,"other-jti"))
        self.assertFalse(checker.is_revoked("https://another.example.test",blocked_id))
        with self.assertRaises(Exception):
            checker.is_revoked(issuer,"")



    def test_signed_access_token_to_read_only_dashboard_end_to_end(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        # Ephemeral key exists only inside the integration test process.
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        private_key=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_key=key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        now=int(datetime.now(timezone.utc).timestamp())
        claims=dict(
            iss="https://identity.example.test",
            aud="one-erp-finance-api",
            sub="ci_subject",
            iat=now-5,
            nbf=now-5,
            exp=now+300,
            jti=str(uuid4()),
            token_use="access",
        )
        token=jwt.encode(claims,private_key,algorithm="RS256",headers={"kid":"ci-1"})
        verifier=VerifiedRS256FinanceAccess(
            RS256VerifierConfig(
                issuer="https://identity.example.test",
                audience="one-erp-finance-api",
                signing_keys_by_id={"ci-1":public_key},
            ),
            ControlPlaneIdentityDirectory(self.auth_connection),
            ControlPlaneRevocationChecker(self.auth_connection),
        )
        posted=self.post(self.fresh_entry())
        response=render_private_dashboard_response(
            self.reporting_db,
            raw_access_token=token,
            identity_verifier=verifier,
            requested_tenant=self.tenant_a,
            from_date=date(2026,10,1),
            as_of=date(2026,10,15),
            currency="SAR",
        )
        self.assertEqual(response.status,200)
        self.assertIn("no-store",response.headers["Cache-Control"])
        self.assertIn("Test Tenant",response.body)
        self.assertIn(str(posted.id),response.body)
        self.assertNotIn(token,response.body)
        with self.assertRaises(Exception):
            render_private_dashboard_response(
                self.reporting_db,
                raw_access_token=token,
                identity_verifier=verifier,
                requested_tenant=self.tenant_b,
                from_date=date(2026,10,1),
                as_of=date(2026,10,15),
                currency="SAR",
            )



    def test_wsgi_route_with_real_readonly_postgresql_report(self):
        # HTTP parsing/tenant routing + PostgreSQL role/membership/report stack.
        class TrustedTestVerifier:
            def __init__(self, actor, tenant):
                self.actor,self.tenant=actor,tenant
            def verify_access_token(self,raw_token):
                if raw_token != "header.payload.signature":
                    raise ValueError("Invalid CI-only token")
                return AuthenticatedFinanceIdentity(self.actor,frozenset({self.tenant}))
        reader_connections=0
        def report_connection():
            nonlocal reader_connections
            reader_connections += 1
            return self.psycopg.connect(
                host=os.environ.get("ONE_TEST_PG_HOST","localhost"),
                port=int(os.environ.get("ONE_TEST_PG_PORT","5432")),
                dbname=os.environ.get("ONE_TEST_PG_DB","one_erp_test"),
                user="one_erp_report_ci",password="ci_report_only",
                autocommit=True,
            )
        app=OneFinanceWSGI(
            verifier=TrustedTestVerifier(self.actor,self.tenant_a),
            connections=TenantFinanceConnections({self.tenant_a:report_connection}),
            allowed_hosts=frozenset({"finance.example.test"}),
        )
        posted=self.post(self.fresh_entry())
        def wsgi_call(tenant,*,scheme="https"):
            status={}
            html=b"".join(app({
                "REQUEST_METHOD":"GET",
                "wsgi.url_scheme":scheme,
                "HTTP_HOST":"finance.example.test",
                "PATH_INFO":"/api/v1/finance/dashboard",
                "QUERY_STRING":(
                    f"tenant={tenant}&from=2026-10-01"
                    "&as_of=2026-10-15&currency=SAR&locale=en"
                ),
                "HTTP_AUTHORIZATION":"Bearer header.payload.signature",
                "CONTENT_LENGTH":"",
            },lambda code,headers:status.update(
                code=code,headers=dict(headers)
            ))).decode("utf-8")
            return status,html
        response,html=wsgi_call(self.tenant_a)
        self.assertEqual(response["code"],"200 OK")
        self.assertEqual(reader_connections,1)
        self.assertIn("no-store",response["headers"]["Cache-Control"])
        self.assertIn("Test Tenant",html)
        self.assertIn(str(posted.id),html)
        self.assertNotIn("header.payload.signature",html)
        forbidden,blank=wsgi_call(self.tenant_b)
        self.assertEqual(forbidden["code"],"403 Forbidden")
        self.assertEqual(reader_connections,1)
        insecure,_=wsgi_call(self.tenant_a,scheme="http")
        self.assertEqual(insecure["code"],"403 Forbidden")
        self.assertEqual(reader_connections,1)


    def provisioning_connection(self):
        return self.psycopg.connect(
            host=os.environ.get("ONE_TEST_PG_HOST", "localhost"),
            port=int(os.environ.get("ONE_TEST_PG_PORT", "5432")),
            dbname=os.environ.get("ONE_TEST_PG_DB", "one_erp_test"),
            user="one_erp_auth_provision_ci", password="ci_provision_only",
            autocommit=True,
        )

    def _complete_ci_bootstrap(self, conn, tenant, operator):
        # Synthetic CI stage evidence only. In production the trusted backend
        # MUST independently verify each real infrastructure checkpoint;
        # this helper is not a production release approval or restore test.
        job = uuid4()
        conn.execute(
            "SELECT one_auth.begin_tenant_bootstrap(%s,%s,%s,%s,%s)",
            (tenant, job, operator, "CI-APPROVAL-ONLY", uuid4()),
        )
        for stage in (
            "database_created", "schema_migrated", "isolation_verified",
            "restore_verified", "route_verified", "ready",
        ):
            digest = sha256(("fictional-ci-" + stage).encode()).digest()
            conn.execute(
                "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                (job, operator, stage, digest, uuid4()),
            )
        return job

    def test_staged_bootstrap_denies_access_before_ready_and_blocks_skips(self):
        from one_erp_core.accounting import AccountingError
        tenant, actor, operator, job = uuid4(), uuid4(), uuid4(), uuid4()
        subject = "provision-" + uuid4().hex
        issuer = "https://identity.example.test"
        begin_event = uuid4()
        proof = sha256(b"ci-only-checkpoint").digest()
        with self.provisioning_connection() as conn:
            conn.execute(
                "SELECT one_auth.register_tenant(%s,%s,%s)",
                (tenant, operator, uuid4()),
            )
            conn.execute(
                "SELECT one_auth.link_subject(%s,%s,%s,%s,%s)",
                (issuer, subject, actor, operator, uuid4()),
            )
            self.assertFalse(self.admin.execute(
                "SELECT active FROM one_auth.tenant_registry WHERE tenant_id=%s",
                (tenant,),
            ).fetchone()[0])
            # Registered without a physical database cannot gain membership.
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT one_auth.set_entitlement(%s,%s,true,%s,%s)",
                    (actor, tenant, operator, uuid4()),
                )
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT one_auth.set_tenant_active(%s,true,%s,%s)",
                    (tenant, operator, uuid4()),
                )
            self.assertEqual(conn.execute(
                "SELECT one_auth.begin_tenant_bootstrap(%s,%s,%s,%s,%s)",
                (tenant, job, operator, "CI-CHANGE-1234", begin_event),
            ).fetchone()[0], "requested")
            self.assertEqual(conn.execute(
                "SELECT one_auth.begin_tenant_bootstrap(%s,%s,%s,%s,%s)",
                (tenant, job, operator, "CI-CHANGE-1234", begin_event),
            ).fetchone()[0], "requested")
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (job, operator, "ready", proof, uuid4()),
                )
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (job, operator, "database_created", b"short", uuid4()),
                )
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (job, uuid4(), "database_created", proof, uuid4()),
                )
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT * FROM one_auth.tenant_bootstrap_jobs"
                )
            stages = (
                "database_created", "schema_migrated", "isolation_verified",
                "restore_verified", "route_verified", "ready",
            )
            for stage in stages:
                event = uuid4()
                self.assertEqual(conn.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (job, operator, stage, proof, event),
                ).fetchone()[0], stage)
                self.assertEqual(conn.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (job, operator, stage, proof, event),
                ).fetchone()[0], stage)
                if stage != "ready":
                    self.assertFalse(self.admin.execute(
                        "SELECT active FROM one_auth.tenant_registry WHERE tenant_id=%s",
                        (tenant,),
                    ).fetchone()[0])
            self.assertTrue(self.admin.execute(
                "SELECT active FROM one_auth.tenant_registry WHERE tenant_id=%s",
                (tenant,),
            ).fetchone()[0])
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (job, operator, "ready", proof, uuid4()),
                )
            conn.execute(
                "SELECT one_auth.set_entitlement(%s,%s,true,%s,%s)",
                (actor, tenant, operator, uuid4()),
            )
        resolved = ControlPlaneIdentityDirectory(self.auth_connection).resolve(
            issuer, subject
        )
        self.assertIn(tenant, resolved.allowed_tenants)
        audit = self.admin.execute(
            "SELECT old_state,new_state FROM one_auth.tenant_bootstrap_events "
            "WHERE job_id=%s ORDER BY recorded_at, event_id", (job,),
        ).fetchall()
        self.assertEqual(len(audit), 7)
        self.assertEqual({row[1] for row in audit},
                         {"requested", *stages})

    def test_staged_bootstrap_failure_is_quarantined_and_audited(self):
        tenant, operator, job = uuid4(), uuid4(), uuid4()
        proof = sha256(b"ci-only-checkpoint").digest()
        failure = uuid4()
        with self.provisioning_connection() as conn:
            conn.execute(
                "SELECT one_auth.register_tenant(%s,%s,%s)",
                (tenant, operator, uuid4()),
            )
            conn.execute(
                "SELECT one_auth.begin_tenant_bootstrap(%s,%s,%s,%s,%s)",
                (tenant, job, operator, "CI-FAIL-12345", uuid4()),
            )
            conn.execute(
                "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                (job, operator, "database_created", proof, uuid4()),
            )
            self.assertEqual(conn.execute(
                "SELECT one_auth.fail_tenant_bootstrap(%s,%s,%s,%s)",
                (job, operator, "RESTORE_CHECK_FAILED", failure),
            ).fetchone()[0], "failed")
            self.assertEqual(conn.execute(
                "SELECT one_auth.fail_tenant_bootstrap(%s,%s,%s,%s)",
                (job, operator, "RESTORE_CHECK_FAILED", failure),
            ).fetchone()[0], "failed")
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (job, operator, "schema_migrated", proof, uuid4()),
                )
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT one_auth.begin_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (tenant, uuid4(), operator, "CI-FAIL-NEW", uuid4()),
                )
        state, enabled = self.admin.execute(
            "SELECT j.state,t.active FROM one_auth.tenant_bootstrap_jobs j "
            "JOIN one_auth.tenant_registry t USING (tenant_id) "
            "WHERE j.job_id=%s", (job,),
        ).fetchone()
        self.assertEqual(state, "failed")
        self.assertFalse(enabled)
        audit = self.admin.execute(
            "SELECT error_code FROM one_auth.tenant_bootstrap_events "
            "WHERE job_id=%s AND new_state='failed'", (job,),
        ).fetchall()
        self.assertEqual(audit, [("RESTORE_CHECK_FAILED",)])

    def test_audited_provisioning_enforces_tenant_disable_and_least_privilege(self):
        # Run every operation as the unprivileged provisioner (not test superuser).
        from one_erp_core.accounting import AccountingError
        operator, actor, tenant = uuid4(), uuid4(), uuid4()
        issuer = "https://identity.example.test"
        subject = "new-ci-" + uuid4().hex
        request_ids = [uuid4() for _ in range(4)]
        with self.provisioning_connection() as conn:
            conn.execute(
                "SELECT one_auth.register_tenant(%s,%s,%s)",
                (tenant, operator, request_ids[0]),
            )
            conn.execute(
                "SELECT one_auth.link_subject(%s,%s,%s,%s,%s)",
                (issuer, subject, actor, operator, request_ids[1]),
            )
            self._complete_ci_bootstrap(conn, tenant, operator)
            conn.execute(
                "SELECT one_auth.set_entitlement(%s,%s,%s,%s,%s)",
                (actor, tenant, True, operator, request_ids[2]),
            )
            # The provisioning role has EXECUTE-only access to approved
            # procedures. It must not edit grants or audit rows directly.
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "UPDATE one_auth.tenant_entitlements SET active=false "
                    "WHERE actor_id=%s AND tenant_id=%s",
                    (actor, tenant),
                )
            with self.assertRaises(self.psycopg.Error):
                conn.execute("SELECT * FROM one_auth.provisioning_audit")
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT one_auth.register_tenant(%s,%s,%s)",
                    (tenant, operator, request_ids[0]),
                )

        directory = ControlPlaneIdentityDirectory(self.auth_connection)
        identity = directory.resolve(issuer, subject)
        self.assertEqual(identity.actor_id, actor)
        self.assertEqual(identity.allowed_tenants, frozenset({tenant}))
        rows = self.admin.execute(
            """SELECT action FROM one_auth.provisioning_audit
               WHERE request_id = ANY(%s) ORDER BY created_at""",
            (request_ids[:3],),
        ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(set(r[0] for r in rows),
                         {"register_tenant", "link_subject", "set_entitlement"})
        with self.provisioning_connection() as conn:
            conn.execute(
                "SELECT one_auth.set_tenant_active(%s,%s,%s,%s)",
                (tenant, False, operator, request_ids[3]),
            )
            # Granting a disabled company should fail with zero new audit rows.
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT one_auth.set_entitlement(%s,%s,%s,%s,%s)",
                    (actor, tenant, True, operator, uuid4()),
                )
        with self.assertRaises(AccountingError):
            directory.resolve(issuer, subject)
        count = self.admin.execute(
            "SELECT count(*) FROM one_auth.provisioning_audit "
            "WHERE request_id = ANY(%s)", (request_ids,),
        ).fetchone()[0]
        self.assertEqual(count, 4)

    def test_control_plane_rejects_subject_rebinding_and_audits_revocation(self):
        from one_erp_core.accounting import AccountingError
        issuer = "https://identity.example.test"
        operator, actor, other_actor, tenant = uuid4(), uuid4(), uuid4(), uuid4()
        subject = "no-rebinding-" + uuid4().hex
        jti = "revoked-from-admin-" + uuid4().hex
        digest = sha256((issuer + "\0" + jti).encode("utf-8")).digest()
        with self.provisioning_connection() as conn:
            conn.execute(
                "SELECT one_auth.register_tenant(%s,%s,%s)",
                (tenant, operator, uuid4()),
            )
            conn.execute(
                "SELECT one_auth.link_subject(%s,%s,%s,%s,%s)",
                (issuer, subject, actor, operator, uuid4()),
            )
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT one_auth.link_subject(%s,%s,%s,%s,%s)",
                    (issuer, subject, other_actor, operator, uuid4()),
                )
            conn.execute(
                "SELECT one_auth.revoke_jti_digest(%s,%s,%s,%s)",
                (digest, datetime.now(timezone.utc)+timedelta(minutes=10),
                 operator, uuid4()),
            )
            with self.assertRaises(self.psycopg.Error):
                conn.execute(
                    "SELECT one_auth.revoke_jti_digest(%s,%s,%s,%s)",
                    (digest, datetime.now(timezone.utc)-timedelta(seconds=1),
                     operator, uuid4()),
                )
            conn.execute(
                "SELECT one_auth.disable_subject(%s,%s,%s,%s)",
                (issuer, subject, operator, uuid4()),
            )
        stored = self.admin.execute(
            "SELECT actor_id,active FROM one_auth.subject_links "
            "WHERE issuer=%s AND subject=%s",
            (issuer, subject),
        ).fetchone()
        self.assertEqual(stored, (actor, False))
        with self.assertRaises(AccountingError):
            ControlPlaneIdentityDirectory(self.auth_connection).resolve(issuer, subject)
        checker = ControlPlaneRevocationChecker(self.auth_connection)
        self.assertTrue(checker.is_revoked(issuer, jti))
        self.assertFalse(checker.is_revoked(issuer, jti+"-other"))
        audit = self.admin.execute(
            "SELECT action,details::text FROM one_auth.provisioning_audit "
            "WHERE operator_id=%s", (operator,),
        ).fetchall()
        self.assertEqual(len(audit), 4)
        self.assertIn("revoke_jti", {r[0] for r in audit})
        self.assertTrue(all(jti not in row[1] for row in audit))




if __name__ == "__main__":
    unittest.main()
