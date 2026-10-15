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
from one_erp_core.sales_invoice_bridge import persist_dispatch_backed_sales_invoice
from one_erp_core.return_credit_bridge import persist_return_backed_customer_credit
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
            (migration_dir.parent / "control_plane" / "004_attested_transition_role.sql")
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
            "CREATE ROLE one_erp_auth_attestor_ci LOGIN PASSWORD 'ci_attestor_only' "
            "NOSUPERUSER NOBYPASSRLS"
        )
        cls.admin.execute(
            "GRANT one_erp_auth_attestor TO one_erp_auth_attestor_ci"
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
        # ONE ERP commercial test permissions are broad only in disposable CI.
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_confirm_commercial_order(uuid,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_fulfill_commercial_line(uuid,uuid,integer,numeric,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_reserve_sale_line(uuid,uuid,integer,numeric,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_release_sale_line(uuid,uuid,integer,numeric,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_dispatch_reserved_sale_line(uuid,uuid,integer,numeric,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        # Source linkage is accessible only in the disposable CI DB.
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_link_sales_invoice(uuid,uuid,uuid,uuid,integer[]) "
            "TO one_erp_runtime_ci"
        )
        # Fulfillment routines: disposable CI only; never use these broad grants live.
        for func in (
            "one_create_delivery(uuid,uuid,uuid,integer,numeric,uuid)",
            "one_advance_delivery(uuid,uuid,text,uuid,uuid,text)",
            "one_start_receiving_inspection(uuid,uuid,uuid,integer,numeric,uuid)",
            "one_complete_receiving_inspection(uuid,uuid,numeric,numeric,uuid,uuid)",
        ):
            cls.admin.execute("GRANT EXECUTE ON FUNCTION "+func+
                              " TO one_erp_runtime_ci")
        # Returns are reachable only from the disposable CI runtime role.
        for func in (
            "one_request_return(uuid,uuid,uuid,numeric,uuid,uuid,text)",
            "one_finish_return(uuid,uuid,numeric,uuid,uuid)",
        ):
            cls.admin.execute("GRANT EXECUTE ON FUNCTION "+func+
                              " TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_link_customer_return_credit(uuid,uuid,uuid,uuid,uuid) "
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

    def attestor_connection(self):
        # Separate credential from the ordinary control-plane provisioner.
        return self.psycopg.connect(
            host=os.environ.get("ONE_TEST_PG_HOST", "localhost"),
            port=int(os.environ.get("ONE_TEST_PG_PORT", "5432")),
            dbname=os.environ.get("ONE_TEST_PG_DB", "one_erp_test"),
            user="one_erp_auth_attestor_ci", password="ci_attestor_only",
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
        with self.attestor_connection() as verified_conn:
            for stage in (
                "database_created", "schema_migrated", "isolation_verified",
                "restore_verified", "route_verified", "ready",
            ):
                digest = sha256(("fictional-ci-" + stage).encode()).digest()
                verified_conn.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (job, operator, stage, digest, uuid4()),
                )
        return job

    def test_signed_checkpoint_bridge_requires_independent_valid_attestation(self):
        import base64
        import json
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from one_erp_core.attested_bootstrap_service import (
            record_signed_checkpoint,
        )
        from one_erp_core.bootstrap_attestation import InvalidBootstrapEvidence

        key = Ed25519PrivateKey.generate()  # Ephemeral CI signing identity.
        public = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        tenant, operator, job, event = uuid4(), uuid4(), uuid4(), uuid4()
        now = datetime(2026, 10, 15, 2, tzinfo=timezone.utc)
        claims = {
            "type": "ONE_BOOTSTRAP_STAGE_V1",
            "issuer": "one-ci-evidence",
            "job_id": str(job),
            "tenant_id": str(tenant),
            "operator_id": str(operator),
            "event_id": str(event),
            "stage": "database_created",
            "evidence_sha256": sha256(b"fictional isolated CI checkpoint").hexdigest(),
            "issued_at": (now-timedelta(minutes=1)).isoformat(),
            "expires_at": (now+timedelta(minutes=1)).isoformat(),
        }
        raw = json.dumps(
            claims, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        base = dict(
            claims_b64=base64.urlsafe_b64encode(raw).rstrip(b"=").decode(),
            signature_b64=base64.urlsafe_b64encode(
                key.sign(raw)
            ).rstrip(b"=").decode(),
            pinned_public_key=public, pinned_issuer="one-ci-evidence",
            trusted_job=job, trusted_tenant=tenant,
            trusted_operator=operator, trusted_event=event,
            trusted_stage="database_created", checked_at=now,
        )
        with self.provisioning_connection() as conn:
            conn.execute(
                "SELECT one_auth.register_tenant(%s,%s,%s)",
                (tenant, operator, uuid4()),
            )
            conn.execute(
                "SELECT one_auth.begin_tenant_bootstrap(%s,%s,%s,%s,%s)",
                (tenant, job, operator, "CI-EVIDENCE-123", uuid4()),
            )
            bad = dict(base, trusted_tenant=uuid4())
            with self.assertRaises(InvalidBootstrapEvidence):
                record_signed_checkpoint(connection=conn, **bad)
            bad = dict(base, signature_b64=base["signature_b64"][:-3] + "AAA")
            with self.assertRaises(InvalidBootstrapEvidence):
                record_signed_checkpoint(connection=conn, **bad)
            self.assertEqual(
                self.admin.execute(
                    "SELECT state FROM one_auth.tenant_bootstrap_jobs "
                    "WHERE job_id=%s", (job,),
                ).fetchone()[0],
                "requested",
            )
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (job, operator, "database_created",
                     bytes.fromhex(claims["evidence_sha256"]), uuid4()),
                )
            with self.attestor_connection() as verified_conn:
                status, proof = record_signed_checkpoint(
                    connection=verified_conn, **base
                )
                self.assertEqual(status, "database_created")
                self.assertEqual(proof.tenant_id, tenant)
                self.assertEqual(
                    proof.evidence_digest,
                    bytes.fromhex(claims["evidence_sha256"]),
                )
                # An exact resend is a no-op, without a duplicate audit row.
                repeat, _ = record_signed_checkpoint(
                    connection=verified_conn, **base
                )
                self.assertEqual(repeat, "database_created")
        self.assertFalse(self.admin.execute(
            "SELECT active FROM one_auth.tenant_registry WHERE tenant_id=%s",
            (tenant,),
        ).fetchone()[0])
        rows = self.admin.execute(
            "SELECT new_state FROM one_auth.tenant_bootstrap_events "
            "WHERE job_id=%s", (job,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({x[0] for x in rows}, {"requested", "database_created"})

    def test_stage_attestor_and_general_admin_roles_are_separated(self):
        # No ordinary provisioning login may send synthetic READY proofs.
        with self.provisioning_connection() as ordinary, self.attestor_connection() as attestor:
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                ordinary.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (uuid4(), uuid4(), "ready", sha256(b"fake").digest(), uuid4()),
                )
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                attestor.execute(
                    "SELECT one_auth.register_tenant(%s,%s,%s)",
                    (uuid4(), uuid4(), uuid4()),
                )
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                attestor.execute(
                    "SELECT one_auth.begin_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (uuid4(), uuid4(), uuid4(), "CI-APPROVAL-NEW", uuid4()),
                )
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                attestor.execute(
                    "SELECT * FROM one_auth.tenant_bootstrap_events"
                )
            # Confirm each CI role has only the expected, separate inherited role.
            self.assertEqual(
                ordinary.execute("SELECT current_user").fetchone()[0],
                "one_erp_auth_provision_ci",
            )
            self.assertEqual(
                attestor.execute("SELECT current_user").fetchone()[0],
                "one_erp_auth_attestor_ci",
            )

    def test_staged_bootstrap_denies_access_before_ready_and_blocks_skips(self):
        from one_erp_core.accounting import AccountingError
        tenant, actor, operator, job = uuid4(), uuid4(), uuid4(), uuid4()
        subject = "provision-" + uuid4().hex
        issuer = "https://identity.example.test"
        begin_event = uuid4()
        proof = sha256(b"ci-only-checkpoint").digest()
        with self.provisioning_connection() as conn, self.attestor_connection() as verified_conn:
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
                verified_conn.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (job, operator, "ready", proof, uuid4()),
                )
            with self.assertRaises(self.psycopg.Error):
                verified_conn.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (job, operator, "database_created", b"short", uuid4()),
                )
            with self.assertRaises(self.psycopg.Error):
                verified_conn.execute(
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
                self.assertEqual(verified_conn.execute(
                    "SELECT one_auth.advance_tenant_bootstrap(%s,%s,%s,%s,%s)",
                    (job, operator, stage, proof, event),
                ).fetchone()[0], stage)
                self.assertEqual(verified_conn.execute(
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
                verified_conn.execute(
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
        with self.provisioning_connection() as conn, self.attestor_connection() as verified_conn:
            conn.execute(
                "SELECT one_auth.register_tenant(%s,%s,%s)",
                (tenant, operator, uuid4()),
            )
            conn.execute(
                "SELECT one_auth.begin_tenant_bootstrap(%s,%s,%s,%s,%s)",
                (tenant, job, operator, "CI-FAIL-12345", uuid4()),
            )
            verified_conn.execute(
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
                verified_conn.execute(
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


    def commerce_fixture(self):
        # Synthetic orders, stock and a separate admin: never live records.
        tenant, actor = self.tenant_a, uuid4()
        product, warehouse, incoming, outgoing = (uuid4() for _ in range(4))
        self.admin.execute(
            "INSERT INTO one_memberships(tenant_id,actor_id,role) "
            "VALUES (%s,%s,'company_admin')", (tenant,actor),
        )
        self.admin.execute(
            "INSERT INTO one_products(tenant_id,id,sku,name) "
            "VALUES (%s,%s,%s,%s)",
            (tenant,product,"SKU-"+product.hex,"Test commercial stock"),
        )
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name) "
            "VALUES (%s,%s,%s,%s)",
            (tenant,warehouse,"W-"+warehouse.hex,"Test warehouse"),
        )
        for order,kind in ((incoming,"purchase"),(outgoing,"sale")):
            self.admin.execute(
                "INSERT INTO one_commercial_orders "
                "(tenant_id,id,kind,number,partner_id,currency) "
                "VALUES (%s,%s,%s,%s,%s,'SAR')",
                (tenant,order,kind,"CI-"+order.hex,"TEST-PARTNER"),
            )
            self.admin.execute(
                "INSERT INTO one_commercial_order_lines "
                "(tenant_id,order_id,line_no,product_id,warehouse_id,"
                "ordered_qty,unit_price) VALUES (%s,%s,1,%s,%s,5,12.50)",
                (tenant,order,product,warehouse),
            )
        return tenant,actor,product,warehouse,incoming,outgoing

    def commerce_execute(self, tenant, actor, statement, params):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(statement,params).fetchone()[0]

    def test_commerce_postgres_purchase_then_sale_with_event_ledger(self):
        tenant,actor,product,warehouse,incoming,outgoing = self.commerce_fixture()
        for order in (incoming,outgoing):
            self.assertEqual(self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor)), order)
        receipt,dispatch = uuid4(),uuid4()
        self.assertEqual(self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,incoming,receipt,actor)), receipt)
        self.assertEqual(self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
            (tenant,outgoing,dispatch,actor)), dispatch)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (5-3,0))
        self.assertEqual(self.admin.execute(
            "SELECT movement_kind,quantity FROM one_stock_events "
            "WHERE tenant_id=%s AND id IN (%s,%s) ORDER BY movement_kind",
            (tenant,receipt,dispatch)).fetchall(),
            [("purchase_receipt",5),("sale_dispatch",3)])
        self.assertEqual(self.admin.execute(
            "SELECT fulfilled_qty FROM one_commercial_order_lines "
            "WHERE tenant_id=%s AND order_id=%s AND line_no=1",
            (tenant,outgoing)).fetchone()[0],3)

    def test_commerce_postgres_rejects_overselling_and_duplicate_events(self):
        tenant,actor,product,warehouse,incoming,outgoing = self.commerce_fixture()
        for order in (incoming,outgoing):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        receipt = uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,incoming,receipt,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
                (tenant,outgoing,uuid4(),actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,1,%s,%s)",
                (tenant,incoming,receipt,actor))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],2)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events WHERE tenant_id=%s "
            "AND order_id=%s",(tenant,incoming)).fetchone()[0],1)

    def test_commerce_postgres_rejects_wrong_actor_tenant_and_draft(self):
        tenant,actor,product,warehouse,incoming,outgoing = self.commerce_fixture()
        other_actor = uuid4()
        for who,tid in ((other_actor,tenant),(actor,self.tenant_b)):
            with self.assertRaises(self.psycopg.Error):
                self.commerce_execute(
                    tid,who,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                    (tid,incoming,who))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,1,%s,%s)",
                (tenant,incoming,uuid4(),actor))

    def test_commerce_postgres_rejects_stock_tampering_and_event_mutation(self):
        tenant,actor,product,warehouse,incoming,outgoing = self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,incoming,actor))
        receipt=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,incoming,receipt,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"UPDATE one_stock_balances SET on_hand=999 "
                "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s "
                "RETURNING on_hand", (tenant,warehouse,product))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"DELETE FROM one_stock_events WHERE "
                "tenant_id=%s AND id=%s RETURNING id", (tenant,receipt))

    def test_commerce_postgres_partial_receipt_limit_and_immutable_order(self):
        tenant,actor,product,warehouse,incoming,outgoing = self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,incoming,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
            (tenant,incoming,uuid4(),actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
                (tenant,incoming,uuid4(),actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"UPDATE one_commercial_orders SET "
                "partner_id='TAMPER' WHERE tenant_id=%s AND id=%s "
                "RETURNING id",(tenant,incoming))
        self.assertEqual(self.admin.execute(
            "SELECT fulfilled_qty FROM one_commercial_order_lines "
            "WHERE tenant_id=%s AND order_id=%s AND line_no=1",
            (tenant,incoming)).fetchone()[0],3)





    def test_reserved_sale_persists_hold_dispatch_and_release(self):
        tenant,actor,product,warehouse,incoming,outgoing = self.commerce_fixture()
        for order in (incoming,outgoing):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,incoming,uuid4(),actor))
        hold_id = uuid4()
        self.assertEqual(self.commerce_execute(
            tenant,actor,"SELECT one_reserve_sale_line(%s,%s,1,3,%s,%s)",
            (tenant,outgoing,hold_id,actor)),hold_id)
        row = self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()
        self.assertEqual(tuple(map(Decimal,row)), (Decimal("5"),Decimal("3")))
        dispatch_id=uuid4()
        self.assertEqual(self.commerce_execute(
            tenant,actor,
            "SELECT one_dispatch_reserved_sale_line(%s,%s,1,2,%s,%s)",
            (tenant,outgoing,dispatch_id,actor)),dispatch_id)
        row = self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()
        self.assertEqual(tuple(map(Decimal,row)),(Decimal("3"),Decimal("1")))
        line = self.admin.execute(
            "SELECT fulfilled_qty,reserved_qty FROM one_commercial_order_lines "
            "WHERE tenant_id=%s AND order_id=%s AND line_no=1",
            (tenant,outgoing)).fetchone()
        self.assertEqual(tuple(map(Decimal,line)),(Decimal("2"),Decimal("1")))
        release_id=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_release_sale_line(%s,%s,1,1,%s,%s)",
            (tenant,outgoing,release_id,actor))
        self.assertEqual(self.admin.execute(
            "SELECT reserved FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0], 0)
        self.assertEqual(self.admin.execute(
            "SELECT action FROM one_reservation_events WHERE tenant_id=%s "
            "AND id IN (%s,%s) ORDER BY action",
            (tenant,hold_id,release_id)).fetchall(),
            [("hold",),("release",)])

    def test_reservations_block_competing_holds_and_unreserved_oversell(self):
        tenant,actor,product,warehouse,incoming,outgoing=self.commerce_fixture()
        for order in (incoming,outgoing):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,4,%s,%s)",
            (tenant,incoming,uuid4(),actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_reserve_sale_line(%s,%s,1,3,%s,%s)",
            (tenant,outgoing,uuid4(),actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,
                "SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
                (tenant,outgoing,uuid4(),actor))
        competitor=uuid4()
        self.admin.execute(
            "INSERT INTO one_commercial_orders "
            "(tenant_id,id,kind,number,partner_id,currency) "
            "VALUES (%s,%s,'sale',%s,'CUSTOMER-2','SAR')",
            (tenant,competitor,"CI-"+competitor.hex))
        self.admin.execute(
            "INSERT INTO one_commercial_order_lines "
            "(tenant_id,order_id,line_no,product_id,warehouse_id,ordered_qty,unit_price) "
            "VALUES (%s,%s,1,%s,%s,5,10)",
            (tenant,competitor,product,warehouse))
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,competitor,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_reserve_sale_line(%s,%s,1,2,%s,%s)",
                (tenant,competitor,uuid4(),actor))
        self.assertEqual(self.admin.execute(
            "SELECT reserved FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],3)

    def test_reservation_event_unique_and_immutable(self):
        tenant,actor,product,warehouse,incoming,outgoing=self.commerce_fixture()
        for order in (incoming,outgoing):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,incoming,uuid4(),actor))
        event=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_reserve_sale_line(%s,%s,1,1,%s,%s)",
            (tenant,outgoing,event,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_reserve_sale_line(%s,%s,1,1,%s,%s)",
                (tenant,outgoing,event,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"DELETE FROM one_reservation_events WHERE "
                "tenant_id=%s AND id=%s RETURNING id",(tenant,event))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"UPDATE one_reservation_events SET quantity=2 "
                "WHERE tenant_id=%s AND id=%s RETURNING id",(tenant,event))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_reservation_events "
            "WHERE tenant_id=%s AND id=%s",(tenant,event)).fetchone()[0],1)

    def test_reserved_dispatch_and_release_reject_wrong_actor_or_tenant(self):
        tenant,actor,product,warehouse,incoming,outgoing=self.commerce_fixture()
        for order in (incoming,outgoing):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
            (tenant,incoming,uuid4(),actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_reserve_sale_line(%s,%s,1,1,%s,%s)",
            (tenant,outgoing,uuid4(),actor))
        for fn in ("one_reserve_sale_line", "one_release_sale_line",
                   "one_dispatch_reserved_sale_line"):
            for context_tenant,who in ((self.tenant_b,actor),(tenant,uuid4())):
                with self.subTest(fn=fn,who=who),self.assertRaises(self.psycopg.Error):
                    self.commerce_execute(
                        context_tenant,who,
                        "SELECT "+fn+"(%s,%s,1,1,%s,%s)",
                        (tenant,outgoing,uuid4(),who))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_release_sale_line(%s,%s,1,2,%s,%s)",
                (tenant,outgoing,uuid4(),actor))

    def test_reservation_rollback_on_failed_stock_and_no_phantom_events(self):
        tenant,actor,product,warehouse,incoming,outgoing=self.commerce_fixture()
        for order in (incoming,outgoing):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,1,%s,%s)",
            (tenant,incoming,uuid4(),actor))
        bad_event=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_reserve_sale_line(%s,%s,1,2,%s,%s)",
                (tenant,outgoing,bad_event,actor))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_reservation_events "
            "WHERE tenant_id=%s AND id=%s",(tenant,bad_event)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT reserved FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],0)



    def test_competing_reservations_cannot_double_allocate_same_stock(self):
        """Two real PostgreSQL connections race for the last five units."""
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,actor,product,warehouse,incoming,outgoing=self.commerce_fixture()
        competitor=uuid4()
        self.admin.execute(
            "INSERT INTO one_commercial_orders "
            "(tenant_id,id,kind,number,partner_id,currency) "
            "VALUES (%s,%s,'sale',%s,'CUSTOMER-B','SAR')",
            (tenant,competitor,"CI-"+competitor.hex))
        self.admin.execute(
            "INSERT INTO one_commercial_order_lines "
            "(tenant_id,order_id,line_no,product_id,warehouse_id,ordered_qty,unit_price) "
            "VALUES (%s,%s,1,%s,%s,5,10)",
            (tenant,competitor,product,warehouse))
        for order in (incoming,outgoing,competitor):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,incoming,uuid4(),actor))
        gate=Barrier(2)

        def compete(order):
            with self.psycopg.connect(
                host=os.environ.get("ONE_TEST_PG_HOST", "localhost"),
                port=int(os.environ.get("ONE_TEST_PG_PORT", "5432")),
                dbname=os.environ.get("ONE_TEST_PG_DB", "one_erp_test"),
                user="one_erp_runtime_ci", password="ci_only_password",
                autocommit=True,
            ) as conn:
                try:
                    with conn.transaction():
                        conn.execute(
                            "SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
                        conn.execute(
                            "SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
                        gate.wait(timeout=10)
                        conn.execute(
                            "SELECT one_reserve_sale_line(%s,%s,1,3,%s,%s)",
                            (tenant,order,uuid4(),actor))
                    return True
                except self.psycopg.Error:
                    return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            left=pool.submit(compete,outgoing)
            right=pool.submit(compete,competitor)
            results=(left.result(timeout=20),right.result(timeout=20))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(self.admin.execute(
            "SELECT reserved FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],3)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_reservation_events "
            "WHERE tenant_id=%s AND order_id IN (%s,%s)",
            (tenant,outgoing,competitor)).fetchone()[0],1)



    def sale_source_invoice(self, tenant, quantity, *,
                            partner="TEST-PARTNER", price=Decimal("12.50")):
        return InvoiceDocument(
            uuid4(), tenant, "SI-"+uuid4().hex, partner,
            DocumentType.CUSTOMER_INVOICE, date(2026,10,15),
            date(2026,11,15), "SAR",
            (InvoiceLine("Stockable item", "sales", quantity, price),),
        )

    def post_dispatched_sale_invoice(self, order, actor, doc,
                                    line_numbers=(1,)):
        return persist_dispatch_backed_sales_invoice(
            self.db, order_id=order, order_line_numbers=line_numbers,
            document=doc, accounts=self.invoice_accounts, chart=self.chart,
            period=self.period, period_id=self.period_id, actor_id=actor,
            memberships=(Membership(actor,self.tenant_a,Role.COMPANY_ADMIN),),
        )

    def test_sales_dispatch_to_invoice_journal_has_traceable_source(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,4,%s,%s)",
            (tenant,purchase,uuid4(),actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
            (tenant,sale,uuid4(),actor))
        doc=self.sale_source_invoice(tenant,Decimal("2"))
        proposal=self.post_dispatched_sale_invoice(sale,actor,doc)
        self.assertEqual(proposal.totals.net,Decimal("25.00"))
        self.assertEqual(self.admin.execute(
            "SELECT d.status,j.status FROM one_invoice_docs d "
            "JOIN one_journal_entries j ON j.tenant_id=d.tenant_id AND j.id=d.journal_id "
            "WHERE d.tenant_id=%s AND d.id=%s",(tenant,doc.id)
        ).fetchone(),("issued","posted"))
        self.assertEqual(self.admin.execute(
            "SELECT order_id,order_line_no,billed_qty "
            "FROM one_sales_invoice_allocations "
            "WHERE tenant_id=%s AND invoice_id=%s",(tenant,doc.id)
        ).fetchone(),(sale,1,Decimal("2")))
        second=self.sale_source_invoice(tenant,Decimal("1"))
        self.post_dispatched_sale_invoice(sale,actor,second)
        self.assertEqual(self.admin.execute(
            "SELECT sum(billed_qty) FROM one_sales_invoice_allocations "
            "WHERE tenant_id=%s AND order_id=%s",(tenant,sale)
        ).fetchone()[0],Decimal("3"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_invoice_audit "
            "WHERE tenant_id=%s AND doc_id IN (%s,%s)",
            (tenant,doc.id,second.id)).fetchone()[0],2)

    def test_sales_invoice_cannot_bill_undelivered_quantity_or_leave_partial_gl(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,purchase,uuid4(),actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,sale,uuid4(),actor))
        self.post_dispatched_sale_invoice(
            sale,actor,self.sale_source_invoice(tenant,Decimal("1")))
        bad=self.sale_source_invoice(tenant,Decimal("2"))
        with self.assertRaises(self.psycopg.Error):
            self.post_dispatched_sale_invoice(sale,actor,bad)
        for table,where in (
            ("one_invoice_docs","id"),
            ("one_journal_entries","id"),
            ("one_invoice_audit","doc_id"),
            ("one_sales_invoice_allocations","invoice_id"),
        ):
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM "+table+" WHERE tenant_id=%s AND "
                +where+"=%s",(tenant,bad.id)).fetchone()[0],0)

    def test_sales_invoice_rejects_wrong_partner_and_price_atomically(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
            (tenant,purchase,uuid4(),actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,sale,uuid4(),actor))
        docs=(
            self.sale_source_invoice(tenant,Decimal("1"),partner="OTHER-BUYER"),
            self.sale_source_invoice(tenant,Decimal("1"),price=Decimal("14.00")),
        )
        for doc in docs:
            with self.subTest(id=doc.id), self.assertRaises(self.psycopg.Error):
                self.post_dispatched_sale_invoice(sale,actor,doc)
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM one_journal_entries "
                "WHERE tenant_id=%s AND id=%s",
                (tenant,doc.id)).fetchone()[0],0)

    def test_sales_invoice_requires_dispatched_and_confirmed_sale(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        first=self.sale_source_invoice(tenant,Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.post_dispatched_sale_invoice(sale,actor,first)
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,sale,actor))
        second=self.sale_source_invoice(tenant,Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.post_dispatched_sale_invoice(sale,actor,second)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_invoice_docs "
            "WHERE tenant_id=%s AND id IN (%s,%s)",
            (tenant,first.id,second.id)).fetchone()[0],0)

    def test_sales_invoice_rejects_invalid_source_map_before_mutation(self):
        from one_erp_core.accounting import AccountingError
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        doc=self.sale_source_invoice(tenant,Decimal("1"))
        for mapping in ((),(0,),(True,),(1,1),[1]):
            with self.subTest(mapping=mapping),self.assertRaises(AccountingError):
                self.post_dispatched_sale_invoice(sale,actor,doc,mapping)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_invoice_docs WHERE tenant_id=%s AND id=%s",
            (tenant,doc.id)).fetchone()[0],0)



    def test_two_competing_invoices_cannot_bill_same_dispatched_stock_twice(self):
        """Only one of two invoice writers may bill the remaining two units."""
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,purchase,uuid4(),actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,sale,uuid4(),actor))
        docs=[self.sale_source_invoice(tenant,Decimal("2")) for _ in range(2)]
        barrier=Barrier(2)
        def attempt(doc):
            with self.psycopg.connect(
                host=os.environ.get("ONE_TEST_PG_HOST", "localhost"),
                port=int(os.environ.get("ONE_TEST_PG_PORT", "5432")),
                dbname=os.environ.get("ONE_TEST_PG_DB", "one_erp_test"),
                user="one_erp_runtime_ci",password="ci_only_password",
                autocommit=True,
            ) as conn:
                try:
                    barrier.wait(timeout=10)
                    persist_dispatch_backed_sales_invoice(
                        conn,order_id=sale,order_line_numbers=(1,),
                        document=doc,accounts=self.invoice_accounts,
                        chart=self.chart,period=self.period,
                        period_id=self.period_id,actor_id=actor,
                        memberships=(
                            Membership(actor,tenant,Role.COMPANY_ADMIN),
                        ),
                    )
                    return True
                except self.psycopg.Error:
                    return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(attempt,docs[0])
            second=pool.submit(attempt,docs[1])
            outcomes=(first.result(timeout=20),second.result(timeout=20))
        self.assertEqual(sorted(outcomes),[False,True])
        ids=tuple(d.id for d in docs)
        self.assertEqual(self.admin.execute(
            "SELECT coalesce(sum(billed_qty),0) "
            "FROM one_sales_invoice_allocations WHERE tenant_id=%s "
            "AND order_id=%s",(tenant,sale)).fetchone()[0],Decimal("2"))
        for table,ident in (
            ("one_invoice_docs","id"),
            ("one_journal_entries","id"),
            ("one_invoice_audit","doc_id"),
        ):
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM "+table+" WHERE tenant_id=%s AND "
                +ident+" IN (%s,%s)",(tenant,*ids)).fetchone()[0],1)



    def prepare_outbound_delivery(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,purchase,uuid4(),actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_reserve_sale_line(%s,%s,1,4,%s,%s)",
            (tenant,sale,uuid4(),actor))
        return tenant,actor,product,warehouse,purchase,sale

    def delivery_stage(self,tenant,actor,job,status,event=None,proof=None):
        return self.commerce_execute(
            tenant,actor,
            "SELECT one_advance_delivery(%s,%s,%s,%s,%s,%s)",
            (tenant,job,status,actor,event,proof))

    def test_hub_delivery_pick_pack_dispatch_and_customer_proof(self):
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        job=uuid4()
        self.assertEqual(self.commerce_execute(
            tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,3,%s)",
            (tenant,job,sale,actor)),job)
        for stage in ("picked","packed"):
            self.assertEqual(self.delivery_stage(tenant,actor,job,stage),job)
        ship_id=uuid4()
        self.assertEqual(self.delivery_stage(
            tenant,actor,job,"dispatched",ship_id),job)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),(Decimal("2"),Decimal("1")))
        self.assertEqual(self.delivery_stage(
            tenant,actor,job,"delivered",proof="CI-POD-REFERENCE"),job)
        row=self.admin.execute(
            "SELECT status,dispatch_event_id,proof_reference FROM one_delivery_jobs "
            "WHERE tenant_id=%s AND id=%s",(tenant,job)).fetchone()
        self.assertEqual(row,("delivered",ship_id,"CI-POD-REFERENCE"))
        self.assertEqual(self.admin.execute(
            "SELECT event FROM one_fulfillment_audit WHERE tenant_id=%s "
            "AND delivery_id=%s ORDER BY happened_at,event",
            (tenant,job)).fetchall(),
            [("created",),("picked",),("packed",),("dispatched",),("delivered",)])
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events WHERE tenant_id=%s "
            "AND id=%s AND movement_kind='sale_dispatch'",
            (tenant,ship_id)).fetchone()[0],1)

    def test_delivery_guards_reject_stage_skips_and_fake_proof(self):
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        job=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,2,%s)",
            (tenant,job,sale,actor))
        with self.assertRaises(self.psycopg.Error):
            self.delivery_stage(tenant,actor,job,"dispatched",uuid4())
        self.delivery_stage(tenant,actor,job,"picked")
        with self.assertRaises(self.psycopg.Error):
            self.delivery_stage(tenant,actor,job,"delivered",proof="FAKE")
        self.delivery_stage(tenant,actor,job,"packed")
        with self.assertRaises(self.psycopg.Error):
            self.delivery_stage(tenant,actor,job,"dispatched")
        event=uuid4()
        self.delivery_stage(tenant,actor,job,"dispatched",event)
        with self.assertRaises(self.psycopg.Error):
            self.delivery_stage(tenant,actor,job,"delivered",proof="  ")
        self.delivery_stage(tenant,actor,job,"delivered",proof="EXTERNAL-REF")
        with self.assertRaises(self.psycopg.Error):
            self.delivery_stage(tenant,actor,job,"delivered",proof="REPEATED")

    def test_delivery_allocations_protect_picked_stock_from_raw_release(self):
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        job=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,3,%s)",
            (tenant,job,sale,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,
                "SELECT one_release_sale_line(%s,%s,1,2,%s,%s)",
                (tenant,sale,uuid4(),actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,
                "SELECT one_dispatch_reserved_sale_line(%s,%s,1,2,%s,%s)",
                (tenant,sale,uuid4(),actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,2,%s)",
                (tenant,uuid4(),sale,actor))
        self.delivery_stage(tenant,actor,job,"picked")
        self.delivery_stage(tenant,actor,job,"packed")
        self.delivery_stage(tenant,actor,job,"dispatched",uuid4())
        self.assertEqual(self.admin.execute(
            "SELECT reserved_qty FROM one_commercial_order_lines WHERE "
            "tenant_id=%s AND order_id=%s AND line_no=1",(tenant,sale)
        ).fetchone()[0],1)

    def test_delivery_job_and_audit_are_write_protected(self):
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        job=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,1,%s)",
            (tenant,job,sale,actor))
        for query,args in (
            ("UPDATE one_delivery_jobs SET quantity=3 WHERE tenant_id=%s "
             "AND id=%s RETURNING id",(tenant,job)),
            ("DELETE FROM one_delivery_jobs WHERE tenant_id=%s AND id=%s "
             "RETURNING id",(tenant,job)),
            ("DELETE FROM one_fulfillment_audit WHERE tenant_id=%s "
             "AND delivery_id=%s RETURNING id",(tenant,job)),
        ):
            with self.assertRaises(self.psycopg.Error):
                self.commerce_execute(tenant,actor,query,args)

    def test_delivery_rejects_wrong_actor_tenant_and_direct_overallocation(self):
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        for tid,person in ((tenant,uuid4()),(self.tenant_b,actor)):
            with self.assertRaises(self.psycopg.Error):
                self.commerce_execute(
                    tid,person,"SELECT one_create_delivery(%s,%s,%s,1,2,%s)",
                    (tenant,uuid4(),sale,person))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,5,%s)",
                (tenant,uuid4(),sale,actor))

    def test_receiving_inspection_accepts_partial_and_rejects_bad_units(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        inspection=uuid4()
        self.assertEqual(self.commerce_execute(
            tenant,actor,"SELECT one_start_receiving_inspection(%s,%s,%s,1,5,%s)",
            (tenant,inspection,purchase,actor)),inspection)
        stock_event=uuid4()
        self.assertEqual(self.commerce_execute(
            tenant,actor,
            "SELECT one_complete_receiving_inspection(%s,%s,3,2,%s,%s)",
            (tenant,inspection,actor,stock_event)),inspection)
        self.assertEqual(self.admin.execute(
            "SELECT accepted_qty,rejected_qty,status FROM one_receiving_inspections "
            "WHERE tenant_id=%s AND id=%s",(tenant,inspection)).fetchone(),
            (Decimal("3"),Decimal("2"),"completed"))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("3"))
        self.assertEqual(self.admin.execute(
            "SELECT fulfilled_qty FROM one_commercial_order_lines WHERE "
            "tenant_id=%s AND order_id=%s AND line_no=1",
            (tenant,purchase)).fetchone()[0],Decimal("3"))
        second=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_start_receiving_inspection(%s,%s,%s,1,2,%s)",
            (tenant,second,purchase,actor))
        self.commerce_execute(
            tenant,actor,
            "SELECT one_complete_receiving_inspection(%s,%s,0,2,%s,%s)",
            (tenant,second,actor,None))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events WHERE tenant_id=%s "
            "AND order_id=%s",(tenant,purchase)).fetchone()[0],1)

    def test_pending_inspection_prevents_bypass_receipt_and_overinspection(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        inspection=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_start_receiving_inspection(%s,%s,%s,1,4,%s)",
            (tenant,inspection,purchase,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,
                "SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
                (tenant,purchase,uuid4(),actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_start_receiving_inspection(%s,%s,%s,1,2,%s)",
                (tenant,uuid4(),purchase,actor))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events WHERE tenant_id=%s "
            "AND order_id=%s",(tenant,purchase)).fetchone()[0],0)
        self.commerce_execute(
            tenant,actor,
            "SELECT one_complete_receiving_inspection(%s,%s,4,0,%s,%s)",
            (tenant,inspection,actor,uuid4()))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("4"))

    def test_inspection_rejects_wrong_order_actor_and_unauthorized_changes(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_start_receiving_inspection(%s,%s,%s,1,1,%s)",
                (tenant,uuid4(),sale,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,uuid4(),
                "SELECT one_start_receiving_inspection(%s,%s,%s,1,1,%s)",
                (tenant,uuid4(),purchase,uuid4()))
        ins=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_start_receiving_inspection(%s,%s,%s,1,1,%s)",
            (tenant,ins,purchase,actor))
        for statement in (
            "UPDATE one_receiving_inspections SET status='completed' "
            "WHERE tenant_id=%s AND id=%s RETURNING id",
            "DELETE FROM one_receiving_inspections "
            "WHERE tenant_id=%s AND id=%s RETURNING id",
        ):
            with self.assertRaises(self.psycopg.Error):
                self.commerce_execute(tenant,actor,statement,(tenant,ins))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,
                "SELECT one_complete_receiving_inspection(%s,%s,0,1,%s,%s)",
                (tenant,ins,actor,uuid4()))
        self.commerce_execute(
            tenant,actor,
            "SELECT one_complete_receiving_inspection(%s,%s,0,1,%s,%s)",
            (tenant,ins,actor,None))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,
                "SELECT one_complete_receiving_inspection(%s,%s,0,1,%s,%s)",
                (tenant,ins,actor,None))



    def test_parallel_delivery_allocations_never_overbook_reserved_stock(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        gate=Barrier(2)
        def runner(job):
            with self.psycopg.connect(
                host=os.environ.get("ONE_TEST_PG_HOST","localhost"),
                port=int(os.environ.get("ONE_TEST_PG_PORT","5432")),
                dbname=os.environ.get("ONE_TEST_PG_DB","one_erp_test"),
                user="one_erp_runtime_ci",password="ci_only_password",
                autocommit=True,
            ) as conn:
                try:
                    with conn.transaction():
                        conn.execute("SELECT set_config('one.tenant_id',%s,true)",
                                     (str(tenant),))
                        conn.execute("SELECT set_config('one.actor_id',%s,true)",
                                     (str(actor),))
                        gate.wait(timeout=10)
                        conn.execute(
                            "SELECT one_create_delivery(%s,%s,%s,1,3,%s)",
                            (tenant,job,sale,actor))
                    return True
                except self.psycopg.Error:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(runner,uuid4())
            b=pool.submit(runner,uuid4())
            results=(a.result(timeout=25),b.result(timeout=25))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(self.admin.execute(
            "SELECT sum(quantity) FROM one_delivery_jobs "
            "WHERE tenant_id=%s AND order_id=%s AND status='draft'",
            (tenant,sale)).fetchone()[0],Decimal("3"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_fulfillment_audit WHERE tenant_id=%s "
            "AND event='created' AND delivery_id IN (SELECT id FROM one_delivery_jobs "
            "WHERE tenant_id=%s AND order_id=%s)",
            (tenant,tenant,sale)).fetchone()[0],1)

    def test_parallel_inspections_prevent_double_booking_purchase_remaining(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        gate=Barrier(2)
        def runner(inspection_id):
            with self.psycopg.connect(
                host=os.environ.get("ONE_TEST_PG_HOST","localhost"),
                port=int(os.environ.get("ONE_TEST_PG_PORT","5432")),
                dbname=os.environ.get("ONE_TEST_PG_DB","one_erp_test"),
                user="one_erp_runtime_ci",password="ci_only_password",
                autocommit=True,
            ) as conn:
                try:
                    with conn.transaction():
                        conn.execute("SELECT set_config('one.tenant_id',%s,true)",
                                     (str(tenant),))
                        conn.execute("SELECT set_config('one.actor_id',%s,true)",
                                     (str(actor),))
                        gate.wait(timeout=10)
                        conn.execute(
                            "SELECT one_start_receiving_inspection(%s,%s,%s,1,3,%s)",
                            (tenant,inspection_id,purchase,actor))
                    return True
                except self.psycopg.Error:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(runner,uuid4())
            b=pool.submit(runner,uuid4())
            results=(a.result(timeout=25),b.result(timeout=25))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(self.admin.execute(
            "SELECT sum(expected_qty) FROM one_receiving_inspections "
            "WHERE tenant_id=%s AND order_id=%s AND status='pending'",
            (tenant,purchase)).fetchone()[0],Decimal("3"))

    def test_delivery_stock_event_conflict_rolls_back_stage_and_audit(self):
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        job=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,2,%s)",
            (tenant,job,sale,actor))
        self.delivery_stage(tenant,actor,job,"picked")
        self.delivery_stage(tenant,actor,job,"packed")
        existing=self.admin.execute(
            "SELECT id FROM one_stock_events WHERE tenant_id=%s "
            "AND order_id=%s ORDER BY created_at LIMIT 1",
            (tenant,purchase)).fetchone()[0]
        with self.assertRaises(self.psycopg.Error):
            self.delivery_stage(tenant,actor,job,"dispatched",existing)
        self.assertEqual(self.admin.execute(
            "SELECT status,dispatch_event_id FROM one_delivery_jobs "
            "WHERE tenant_id=%s AND id=%s",(tenant,job)).fetchone(),
            ("packed",None))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_fulfillment_audit "
            "WHERE tenant_id=%s AND delivery_id=%s AND event='dispatched'",
            (tenant,job)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT reserved FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("4"))



    def test_one_operation_outbox_connects_sales_warehouse_invoice_and_gl(self):
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        job=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,2,%s)",
            (tenant,job,sale,actor))
        self.delivery_stage(tenant,actor,job,"picked")
        self.delivery_stage(tenant,actor,job,"packed")
        shipment=uuid4()
        self.delivery_stage(tenant,actor,job,"dispatched",shipment)
        self.delivery_stage(tenant,actor,job,"delivered",proof="TEST-EXTERNAL-REF")
        doc=self.sale_source_invoice(tenant,Decimal("2"))
        self.post_dispatched_sale_invoice(sale,actor,doc)
        rows=self.admin.execute(
            "SELECT event_type,source_id FROM one_operation_outbox "
            "WHERE tenant_id=%s AND source_id IN (%s,%s,%s) "
            "ORDER BY event_type",
            (tenant,job,shipment,doc.id)).fetchall()
        events=set(rows)
        self.assertTrue({
            ("delivery.created",job),
            ("delivery.picked",job),
            ("delivery.packed",job),
            ("delivery.dispatched",job),
            ("delivery.delivered",job),
            ("stock.sale_dispatched",shipment),
            ("finance.journal_posted",doc.id),
            ("finance.invoice_issued",doc.id),
        }.issubset(events))
        self.assertEqual(len(rows),len(events))

    def test_one_operation_outbox_receiving_posts_only_accepted_stock(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        inspection,event=uuid4(),uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_start_receiving_inspection(%s,%s,%s,1,4,%s)",
            (tenant,inspection,purchase,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_complete_receiving_inspection(%s,%s,3,1,%s,%s)",
            (tenant,inspection,actor,event))
        rows=set(self.admin.execute(
            "SELECT event_type,source_id FROM one_operation_outbox "
            "WHERE tenant_id=%s AND source_id IN (%s,%s)",
            (tenant,inspection,event)).fetchall())
        self.assertEqual(rows,{
            ("receiving.inspection_started",inspection),
            ("receiving.inspection_completed",inspection),
            ("stock.purchase_received",event),
        })

    def test_one_operation_outbox_immutable_scoped_and_no_failed_event(self):
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        job=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,2,%s)",
            (tenant,job,sale,actor))
        self.delivery_stage(tenant,actor,job,"picked")
        self.delivery_stage(tenant,actor,job,"packed")
        bogus=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.delivery_stage(tenant,actor,job,"dispatched",bogus,
                                proof="should roll back")
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_operation_outbox WHERE tenant_id=%s "
            "AND event_type='delivery.dispatched' AND source_id=%s",
            (tenant,job)).fetchone()[0],0)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(self.tenant_b),))
            self.assertEqual(self.db.execute(
                "SELECT count(*) FROM one_operation_outbox WHERE source_id=%s",
                (job,)).fetchone()[0],0)
        for stmt in (
            "UPDATE one_operation_outbox SET event_type='delivery.delivered' "
            "WHERE tenant_id=%s AND source_id=%s RETURNING id",
            "DELETE FROM one_operation_outbox "
            "WHERE tenant_id=%s AND source_id=%s RETURNING id",
        ):
            with self.assertRaises(self.psycopg.Error):
                self.commerce_execute(tenant,actor,stmt,(tenant,job))



    def test_operation_event_cursor_monotonic_and_read_role_cannot_access_queue(self):
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        job=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,1,%s)",
            (tenant,job,sale,actor))
        self.delivery_stage(tenant,actor,job,"picked")
        rows=self.admin.execute(
            "SELECT stream_position,event_type FROM one_operation_outbox "
            "WHERE tenant_id=%s AND source_id=%s ORDER BY stream_position",
            (tenant,job)).fetchall()
        self.assertEqual([r[1] for r in rows],
                         ["delivery.created","delivery.picked"])
        self.assertLess(rows[0][0],rows[1][0])
        with self.assertRaises(self.psycopg.Error):
            with self.reporting_db.transaction():
                self.reporting_db.execute(
                    "SELECT set_config('one.tenant_id',%s,true)",
                    (str(tenant),))
                self.reporting_db.execute(
                    "SELECT stream_position FROM one_operation_outbox "
                    "WHERE tenant_id=%s",(tenant,)).fetchall()



    def test_customer_return_quarantines_only_inspected_accepted_quantity(self):
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        job,dispatch_event,quarantine=uuid4(),uuid4(),uuid4()
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name,warehouse_type) "
            "VALUES (%s,%s,%s,'Quarantine','quarantine')",
            (tenant,quarantine,"Q-"+quarantine.hex))
        self.commerce_execute(
            tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,2,%s)",
            (tenant,job,sale,actor))
        for stage in ("picked","packed"):
            self.delivery_stage(tenant,actor,job,stage)
        self.delivery_stage(tenant,actor,job,"dispatched",dispatch_event)
        return_id,event_id=uuid4(),uuid4()
        self.assertEqual(self.commerce_execute(
            tenant,actor,
            "SELECT one_request_return(%s,%s,%s,2,%s,%s,'Product inspected')",
            (tenant,return_id,dispatch_event,quarantine,actor)),return_id)
        self.assertEqual(self.commerce_execute(
            tenant,actor,"SELECT one_finish_return(%s,%s,1,%s,%s)",
            (tenant,return_id,event_id,actor)),return_id)
        self.assertEqual(self.admin.execute(
            "SELECT status,accepted_qty,processed_event_id FROM one_return_jobs "
            "WHERE tenant_id=%s AND id=%s",(tenant,return_id)).fetchone(),
            ("completed",Decimal("1"),event_id))
        q_qty=self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone()[0]
        self.assertEqual(q_qty,Decimal("1"))
        original=self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()
        self.assertEqual(original,(Decimal("3"),Decimal("2")))
        events=set(self.admin.execute(
            "SELECT event_type,source_id FROM one_operation_outbox "
            "WHERE tenant_id=%s AND source_id IN (%s,%s)",
            (tenant,return_id,event_id)).fetchall())
        self.assertEqual(events,{
            ("returns.requested",return_id),("returns.completed",return_id),
            ("stock.customer_returned",event_id),
        })
        self.assertEqual(self.admin.execute(
            "SELECT movement_kind,warehouse_id,quantity FROM one_stock_events "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,event_id)).fetchone(),
            ("customer_return",quarantine,Decimal("1")))

    def test_return_quantity_guard_counts_pending_and_prior_accepted_only(self):
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        job,src,quarantine=uuid4(),uuid4(),uuid4()
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name,warehouse_type) "
            "VALUES (%s,%s,%s,'Quarantine','quarantine')",
            (tenant,quarantine,"Q-"+quarantine.hex))
        self.commerce_execute(
            tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,2,%s)",
            (tenant,job,sale,actor))
        self.delivery_stage(tenant,actor,job,"picked")
        self.delivery_stage(tenant,actor,job,"packed")
        self.delivery_stage(tenant,actor,job,"dispatched",src)
        r1=uuid4()
        self.commerce_execute(
            tenant,actor,
            "SELECT one_request_return(%s,%s,%s,2,%s,%s,'Returned shipment')",
            (tenant,r1,src,quarantine,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,
                "SELECT one_request_return(%s,%s,%s,1,%s,%s,'Oversubscribed')",
                (tenant,uuid4(),src,quarantine,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_finish_return(%s,%s,1,%s,%s)",
            (tenant,r1,uuid4(),actor))
        r2=uuid4()
        self.commerce_execute(
            tenant,actor,
            "SELECT one_request_return(%s,%s,%s,1,%s,%s,'Second return')",
            (tenant,r2,src,quarantine,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_finish_return(%s,%s,0,%s,%s)",
            (tenant,r2,None,actor))
        self.assertEqual(self.admin.execute(
            "SELECT status,accepted_qty FROM one_return_jobs WHERE "
            "tenant_id=%s AND id=%s",(tenant,r2)).fetchone(),
            ("rejected",Decimal("0")))
        r3=uuid4()
        self.commerce_execute(
            tenant,actor,
            "SELECT one_request_return(%s,%s,%s,1,%s,%s,'Reinspection allowed')",
            (tenant,r3,src,quarantine,actor))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_return_jobs WHERE tenant_id=%s "
            "AND source_event_id=%s",(tenant,src)).fetchone()[0],3)

    def test_vendor_return_refuses_reserved_stock_and_then_dispatches_unreserved(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        original=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,purchase,original,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_reserve_sale_line(%s,%s,1,4,%s,%s)",
            (tenant,sale,uuid4(),actor))
        return_job,event=uuid4(),uuid4()
        self.commerce_execute(
            tenant,actor,
            "SELECT one_request_return(%s,%s,%s,2,%s,%s,'Supplier recall')",
            (tenant,return_job,original,None,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_finish_return(%s,%s,2,%s,%s)",
                (tenant,return_job,event,actor))
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_return_jobs WHERE tenant_id=%s AND id=%s",
            (tenant,return_job)).fetchone()[0],"requested")
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events WHERE tenant_id=%s AND id=%s",
            (tenant,event)).fetchone()[0],0)
        self.commerce_execute(
            tenant,actor,"SELECT one_release_sale_line(%s,%s,1,2,%s,%s)",
            (tenant,sale,uuid4(),actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_finish_return(%s,%s,2,%s,%s)",
            (tenant,return_job,event,actor))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("3"),Decimal("2")))
        self.assertEqual(self.admin.execute(
            "SELECT movement_kind,quantity FROM one_stock_events "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,event)).fetchone(),("vendor_return",Decimal("2")))
        self.assertEqual(self.admin.execute(
            "SELECT event_type FROM one_operation_outbox WHERE tenant_id=%s "
            "AND source_id=%s AND event_type='stock.vendor_returned'",
            (tenant,event)).fetchone(),("stock.vendor_returned",))

    def test_return_mutations_not_repeatable_and_invalid_quarantine_refused(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        src=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,purchase,src,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_request_return(%s,%s,%s,1,%s,%s,'Bad company')",
                (tenant,uuid4(),src,uuid4(),actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,uuid4(),
                "SELECT one_request_return(%s,%s,%s,1,%s,%s,'Actor denied')",
                (tenant,uuid4(),src,None,uuid4()))
        rjob=uuid4()
        self.commerce_execute(
            tenant,actor,
            "SELECT one_request_return(%s,%s,%s,1,%s,%s,'Good supplier return')",
            (tenant,rjob,src,None,actor))
        event=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_finish_return(%s,%s,1,%s,%s)",
            (tenant,rjob,event,actor))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,actor,"SELECT one_finish_return(%s,%s,1,%s,%s)",
                (tenant,rjob,uuid4(),actor))
        for stmt in (
            "DELETE FROM one_return_jobs WHERE tenant_id=%s AND id=%s RETURNING id",
            "UPDATE one_return_jobs SET reason='tampered' "
            "WHERE tenant_id=%s AND id=%s RETURNING id",
            "DELETE FROM one_return_audit WHERE tenant_id=%s "
            "AND job_id=%s RETURNING id",
        ):
            with self.assertRaises(self.psycopg.Error):
                self.commerce_execute(tenant,actor,stmt,(tenant,rjob))

    def test_concurrent_return_requests_cannot_exceed_original_movement(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        source=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
            (tenant,purchase,source,actor))
        gate=Barrier(2)
        def attempt(job_id):
            with self.psycopg.connect(
                host=os.environ.get("ONE_TEST_PG_HOST","localhost"),
                port=int(os.environ.get("ONE_TEST_PG_PORT","5432")),
                dbname=os.environ.get("ONE_TEST_PG_DB","one_erp_test"),
                user="one_erp_runtime_ci",password="ci_only_password",
                autocommit=True
            ) as conn:
                try:
                    with conn.transaction():
                        conn.execute("SELECT set_config('one.tenant_id',%s,true)",
                                     (str(tenant),))
                        conn.execute("SELECT set_config('one.actor_id',%s,true)",
                                     (str(actor),))
                        gate.wait(timeout=10)
                        conn.execute(
                            "SELECT one_request_return(%s,%s,%s,2,%s,%s,'Supplier returns')",
                            (tenant,job_id,source,None,actor))
                    return True
                except self.psycopg.Error:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(attempt,uuid4())
            b=pool.submit(attempt,uuid4())
            results=(a.result(timeout=25),b.result(timeout=25))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(self.admin.execute(
            "SELECT sum(quantity) FROM one_return_jobs "
            "WHERE tenant_id=%s AND source_event_id=%s AND status='requested'",
            (tenant,source)).fetchone()[0],Decimal("2"))



    def complete_customer_return_for_invoice(self, *,
                                             acceptance=Decimal("2")):
        tenant,actor,product,warehouse,purchase,sale=self.prepare_outbound_delivery()
        quarantine,job,dispatch,return_job = (uuid4() for _ in range(4))
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name,warehouse_type) "
            "VALUES (%s,%s,%s,'Return quarantine','quarantine')",
            (tenant,quarantine,"QR-"+quarantine.hex))
        self.commerce_execute(
            tenant,actor,"SELECT one_create_delivery(%s,%s,%s,1,2,%s)",
            (tenant,job,sale,actor))
        self.delivery_stage(tenant,actor,job,"picked")
        self.delivery_stage(tenant,actor,job,"packed")
        self.delivery_stage(tenant,actor,job,"dispatched",dispatch)
        invoice=self.sale_source_invoice(tenant,Decimal("2"))
        self.post_dispatched_sale_invoice(sale,actor,invoice)
        self.commerce_execute(
            tenant,actor,"SELECT one_request_return(%s,%s,%s,2,%s,%s,'Customer returned')",
            (tenant,return_job,dispatch,quarantine,actor))
        if acceptance is not None:
            self.commerce_execute(
                tenant,actor,"SELECT one_finish_return(%s,%s,%s,%s,%s)",
                (tenant,return_job,acceptance,
                 uuid4() if acceptance>0 else None,actor))
        return tenant,actor,invoice,return_job

    def credit_for_source_return(self, source, quantity, *, price=Decimal("12.50")):
        from dataclasses import replace
        return replace(
            source,id=uuid4(),number="CN-"+uuid4().hex,
            kind=DocumentType.CUSTOMER_CREDIT,source_invoice_id=source.id,
            lines=(replace(source.lines[0],
                           quantity=quantity,unit_price=price),),
        )

    def issue_accepted_return_credit(self, credit, job, actor):
        return persist_return_backed_customer_credit(
            self.db,return_job_id=job,document=credit,
            accounts=self.invoice_accounts,chart=self.chart,
            period=self.period,period_id=self.period_id,
            actor_id=actor,
            memberships=(Membership(actor,self.tenant_a,Role.COMPANY_ADMIN),),
        )

    def test_return_credit_journal_and_source_are_atomic_traceable(self):
        tenant,actor,original,return_job=self.complete_customer_return_for_invoice()
        first=self.credit_for_source_return(original,Decimal("1"))
        second=self.credit_for_source_return(original,Decimal("1"))
        for credit in (first,second):
            result=self.issue_accepted_return_credit(credit,return_job,actor)
            self.assertEqual(result.totals.net,Decimal("12.50"))
            self.assertEqual(self.admin.execute(
                "SELECT d.status,j.status FROM one_invoice_docs d "
                "JOIN one_journal_entries j ON j.tenant_id=d.tenant_id "
                "AND j.id=d.journal_id WHERE d.tenant_id=%s AND d.id=%s",
                (tenant,credit.id)).fetchone(),("issued","posted"))
        self.assertEqual(self.admin.execute(
            "SELECT sum(quantity) FROM one_customer_return_credits "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,return_job)).fetchone()[0],Decimal("2"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_operation_outbox "
            "WHERE tenant_id=%s AND event_type='finance.customer_return_credited' "
            "AND source_id IN (%s,%s)",
            (tenant,first.id,second.id)).fetchone()[0],2)

    def test_return_credit_above_accepted_qty_rolls_back_every_finance_record(self):
        tenant,actor,original,return_job=self.complete_customer_return_for_invoice(
            acceptance=Decimal("1"))
        too_large=self.credit_for_source_return(original,Decimal("2"))
        with self.assertRaises(self.psycopg.Error):
            self.issue_accepted_return_credit(too_large,return_job,actor)
        for table,col in (
            ("one_invoice_docs","id"),
            ("one_journal_entries","id"),
            ("one_invoice_audit","doc_id"),
            ("one_customer_return_credits","credit_invoice_id"),
        ):
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM "+table+" WHERE tenant_id=%s AND "
                +col+"=%s",(tenant,too_large.id)).fetchone()[0],0)
        valid=self.credit_for_source_return(original,Decimal("1"))
        self.issue_accepted_return_credit(valid,return_job,actor)
        self.assertEqual(self.admin.execute(
            "SELECT sum(quantity) FROM one_customer_return_credits "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,return_job)).fetchone()[0],Decimal("1"))

    def test_customer_credit_cannot_be_issued_on_pending_return_or_changed_price(self):
        tenant,actor,source,job=self.complete_customer_return_for_invoice(
            acceptance=None)
        early=self.credit_for_source_return(source,Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.issue_accepted_return_credit(early,job,actor)
        self.commerce_execute(
            tenant,actor,"SELECT one_finish_return(%s,%s,2,%s,%s)",
            (tenant,job,uuid4(),actor))
        mismatched=self.credit_for_source_return(source,Decimal("1"),
                                                 price=Decimal("14.00"))
        with self.assertRaises(self.psycopg.Error):
            self.issue_accepted_return_credit(mismatched,job,actor)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_journal_entries "
            "WHERE tenant_id=%s AND id IN (%s,%s)",
            (tenant,early.id,mismatched.id)).fetchone()[0],0)

    def test_return_credit_rejects_wrong_source_and_unauthorized_actor(self):
        tenant,actor,source,job=self.complete_customer_return_for_invoice()
        wrong=self.invoice()
        self.post_invoice(wrong)
        credit=self.credit_for_source_return(wrong,Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.issue_accepted_return_credit(credit,job,actor)
        another=self.credit_for_source_return(source,Decimal("1"))
        # Policy fixture claims an admin membership, but PostgreSQL independently
        # rechecks the real role and rejects the absent actor.
        with self.assertRaises(self.psycopg.Error):
            self.issue_accepted_return_credit(another,job,uuid4())
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_customer_return_credits WHERE "
            "tenant_id=%s AND return_job_id=%s",
            (tenant,job)).fetchone()[0],0)



    def test_authorized_accountant_can_credit_admin_inspected_customer_return(self):
        tenant,actor,source,job=self.complete_customer_return_for_invoice(
            acceptance=Decimal("1"))
        credit=self.credit_for_source_return(source,Decimal("1"))
        approved=persist_return_backed_customer_credit(
            self.db,return_job_id=job,document=credit,
            accounts=self.invoice_accounts,chart=self.chart,
            period=self.period,period_id=self.period_id,
            actor_id=self.actor,memberships=self.memberships,
        )
        self.assertEqual(approved.totals.net,Decimal("12.50"))
        self.assertEqual(self.admin.execute(
            "SELECT actor_id FROM one_customer_return_credits "
            "WHERE tenant_id=%s AND credit_invoice_id=%s",
            (tenant,credit.id)).fetchone()[0],self.actor)

    def test_parallel_credits_cannot_use_same_accepted_return_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,actor,source,job=self.complete_customer_return_for_invoice(
            acceptance=Decimal("1"))
        docs=[self.credit_for_source_return(source,Decimal("1")) for _ in range(2)]
        gate=Barrier(2)
        def try_credit(doc):
            with self.psycopg.connect(
                host=os.environ.get("ONE_TEST_PG_HOST","localhost"),
                port=int(os.environ.get("ONE_TEST_PG_PORT","5432")),
                dbname=os.environ.get("ONE_TEST_PG_DB","one_erp_test"),
                user="one_erp_runtime_ci",password="ci_only_password",
                autocommit=True,
            ) as conn:
                try:
                    gate.wait(timeout=10)
                    persist_return_backed_customer_credit(
                        conn,return_job_id=job,document=doc,
                        accounts=self.invoice_accounts,chart=self.chart,
                        period=self.period,period_id=self.period_id,
                        actor_id=actor,memberships=(
                            Membership(actor,tenant,Role.COMPANY_ADMIN),
                        ),
                    )
                    return True
                except self.psycopg.Error:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(try_credit,docs[0])
            b=pool.submit(try_credit,docs[1])
            success=(a.result(timeout=30),b.result(timeout=30))
        self.assertEqual(sorted(success),[False,True])
        self.assertEqual(self.admin.execute(
            "SELECT sum(quantity) FROM one_customer_return_credits "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,job)).fetchone()[0],Decimal("1"))
        for table,column in (
            ("one_customer_return_credits","credit_invoice_id"),
            ("one_invoice_docs","id"),
            ("one_journal_entries","id"),
        ):
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM "+table+" WHERE tenant_id=%s AND "
                +column+" IN (%s,%s)",
                (tenant,docs[0].id,docs[1].id)).fetchone()[0],1)


if __name__ == "__main__":
    unittest.main()
