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
from one_erp_core.vendor_goods_bridge import persist_valued_goods_vendor_bill
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
        # 053: explicitly opt-in separately reviewed restricted command role.
        cls.admin.execute(
            (migration_dir.parent / "ops" / "finance_command_privileges.sql")
            .read_text(encoding="utf-8"), prepare=False,
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
        # 053: dedicated CI LOGIN inherits ONLY vetted NOLOGIN command grants.
        # Never share this password outside the disposable PG14 CI database.
        cls.admin.execute(
            "CREATE ROLE one_erp_command_ci LOGIN "
            "PASSWORD 'ci_command_only' NOSUPERUSER NOBYPASSRLS "
            "NOCREATEDB NOCREATEROLE"
        )
        cls.admin.execute(
            "GRANT one_erp_finance_command_executor TO one_erp_command_ci"
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
        # 051: period comparison is exposed only to the disposable CI runtime.
        for func in (
            "one_soft_adjustment_statement_comparison(uuid,uuid,uuid)",
            "one_soft_adjustment_statement_drift(uuid,uuid,uuid)",
        ):
            cls.admin.execute("GRANT EXECUTE ON FUNCTION "+func+
                              " TO one_erp_runtime_ci")
        # 050: limits and multi-line preview functions granted ONLY in disposable CI.
        for func in (
            "one_actor_soft_adjust_limit(uuid,uuid,text)",
            "one_request_soft_adjust_limit("
            "uuid,uuid,uuid,text,numeric,date,date,uuid)",
            "one_approve_soft_adjust_limit(uuid,uuid,uuid)",
            "one_request_soft_adjustment_lines("
            "uuid,uuid,uuid,uuid,date,text,jsonb,text,uuid)",
            "one_soft_adjustment_impact_preview(uuid,uuid,uuid)",
        ):
            cls.admin.execute("GRANT EXECUTE ON FUNCTION "+func+
                              " TO one_erp_runtime_ci")
        # 049: disposable CI-only soft review and two-person adjustment calls.
        for func in (
            "one_request_period_soft_close(uuid,uuid,uuid)",
            "one_approve_period_soft_close(uuid,uuid,uuid)",
            "one_request_soft_adjustment("
            "uuid,uuid,uuid,uuid,date,text,text,text,numeric,text,uuid)",
            "one_approve_soft_adjustment(uuid,uuid,uuid)",
            "one_finance_period_state(uuid,uuid,uuid)",
        ):
            cls.admin.execute("GRANT EXECUTE ON FUNCTION "+func+
                              " TO one_erp_runtime_ci")
        # 048: ONLY disposable PG14 CI role; production grant audit required.
        for func in (
            "one_actor_has_permission(uuid,uuid,text)",
            "one_request_permission_change(uuid,uuid,uuid,text,text,text,date,date,uuid)",
            "one_approve_permission_change(uuid,uuid,uuid)",
        ):
            cls.admin.execute("GRANT EXECUTE ON FUNCTION "+func+
                              " TO one_erp_runtime_ci")
        # 047: disposable PostgreSQL CI service grants, NOT production roles.
        for func in (
            "one_request_historical_inventory_close(uuid,uuid)",
            "one_approve_historical_inventory_close(uuid,uuid)",
            "one_inventory_historical_net_asof(uuid,date,uuid)",
            "one_inventory_historical_close_details(uuid,uuid,uuid)",
        ):
            cls.admin.execute("GRANT EXECUTE ON FUNCTION "+func+
                              " TO one_erp_runtime_ci")
        # 046: permit two-step stock-date workflows in disposable CI only.
        for func in (
            "one_request_inventory_effective_date(uuid,uuid,date,uuid)",
            "one_approve_inventory_effective_date(uuid,uuid,uuid)",
            "one_inventory_gross_history_asof(uuid,date,uuid)",
        ):
            cls.admin.execute("GRANT EXECUTE ON FUNCTION "+func+
                              " TO one_erp_runtime_ci")
        # 045: grant the read-only report function to the disposable CI role.
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_inventory_period_seal_details(uuid,uuid,uuid) "
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
        # Unified return case reads and QA dispositions: throwaway CI DB only.
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_resolve_return_quarantine("
            "uuid,uuid,uuid,text,numeric,uuid,uuid,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_return_case_overview(uuid,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_return_case_timeline(uuid,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_value_physical_stock_event(uuid,uuid,uuid,text,text,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_link_goods_vendor_bill(uuid,uuid,uuid,text,integer[],uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_inventory_finance_reconciliation(uuid,uuid,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        # Paired movement+valuation functions are CI-only executable.
        for fn in (
          "one_receive_and_value_goods(uuid,uuid,integer,numeric,uuid,uuid,uuid,text,text,uuid)",
          "one_ship_and_value_goods(uuid,uuid,integer,numeric,uuid,uuid,uuid,text,text,uuid)",
          "one_dispatch_reserved_and_value_goods(uuid,uuid,integer,numeric,uuid,uuid,uuid,text,text,uuid)",
          "one_dispatch_delivery_and_value(uuid,uuid,uuid,uuid,uuid,text,text,uuid)",
        ):
            cls.admin.execute("GRANT EXECUTE ON FUNCTION "+fn+
                              " TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_accept_and_value_customer_return("
            "uuid,uuid,numeric,uuid,uuid,uuid,text,text,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_resolve_and_value_customer_quarantine("
            "uuid,uuid,uuid,text,numeric,uuid,uuid,uuid,uuid,text,text,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_complete_unbilled_vendor_return(uuid,uuid,uuid,uuid,uuid,text,text,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_complete_billed_vendor_return("
            "uuid,uuid,uuid,integer,uuid,text,uuid,uuid,text,text,uuid,date) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_post_inventory_nrv_adjustment("
            "uuid,uuid,uuid,uuid,uuid,uuid,text,text,uuid,"
            "integer,numeric,numeric,numeric,numeric,numeric,text,date) "
            "TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_inventory_nrv_reconciliation("
            "uuid,uuid,uuid,uuid) TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_inventory_nrv_company_snapshot(uuid,uuid) "
            "TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_inventory_nrv_period_activity(uuid,uuid,date,date) "
            "TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_ship_and_allocate_nrv("
            "uuid,uuid,integer,numeric,uuid,uuid,uuid,uuid,text,text,text,uuid) "
            "TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_dispatch_reserved_or_delivery_with_nrv("
            "uuid,uuid,integer,uuid,numeric,uuid,uuid,uuid,uuid,"
            "text,text,text,uuid) TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_nrv_adjusted_sales_activity("
            "uuid,uuid,date,date) TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION one_accept_reassessed_nrv_customer_return("
            "uuid,uuid,numeric,uuid,uuid,uuid,uuid,uuid,"
            "text,text,text,text,uuid,numeric,numeric,numeric,text,date) "
            "TO one_erp_runtime_ci")
        cls.admin.execute("GRANT EXECUTE ON FUNCTION "
          "one_nrv_qa_return_check(uuid,uuid,uuid) TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_issue_reviewed_nrv_customer_credit("
            "uuid,uuid,uuid,uuid,text,uuid,text,text,uuid,date) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_nrv_customer_credit_case(uuid,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_nrv_return_settlement_exposure(uuid,uuid,uuid,uuid) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_recognize_nrv_customer_refund("
            "uuid,uuid,uuid,uuid,uuid,text,text,text,uuid,date,text) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_nrv_customer_refund_liability_register(uuid,uuid,date,date) "
            "TO one_erp_runtime_ci"
        )
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_request_customer_refund_order(uuid,uuid,uuid,uuid,text,text) "
            "TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_approve_customer_refund_order(uuid,uuid,uuid,text) "
            "TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_customer_refund_approval_overview(uuid,uuid,date,date) "
            "TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_refund_approved_instruction_preview(uuid,uuid,uuid) "
            "TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_register_refund_bank_candidate("
            "uuid,uuid,uuid,uuid,text,text,text,text,text,text,numeric,date) "
            "TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_refund_bank_candidate_company_overview(uuid,uuid) "
            "TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_refund_control_pulse(uuid,uuid) TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_refund_period_close_preflight(uuid,uuid,uuid) "
            "TO one_erp_runtime_ci")
        cls.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "one_inventory_period_close_preflight(uuid,uuid,uuid) "
            "TO one_erp_runtime_ci")
        # 054: CI-only execution after all previous role function grants:
        # globally remove PUBLIC EXECUTE on public routines, then explicitly
        # restore old broad test-runtime rights ONLY in disposable CI.
        cls.admin.execute(
            (migration_dir.parent / "ops" /
             "finance_command_execute_lockdown.sql")
            .read_text(encoding="utf-8"), prepare=False,
        )
        cls.admin.execute(
            "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public "
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
            ("inventory","140",AccountKind.ASSET),
            ("grni","240",AccountKind.LIABILITY),
            ("cogs","510",AccountKind.EXPENSE),
            ("vatout","230",AccountKind.LIABILITY),
            ("vatin","130",AccountKind.ASSET),
            ("nrvallow","145",AccountKind.ASSET),
            ("nrvexpense","525",AccountKind.EXPENSE),
            ("refunddue","250",AccountKind.LIABILITY),
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



    def read_unified_return_case(self,tenant,actor,job):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT return_kind,warehouse_status,finance_status,"
                "requested_qty,accepted_qty,disposition_qty,in_quarantine_qty,"
                "credited_qty,original_order_id "
                "FROM one_return_case_overview(%s,%s,%s)",
                (tenant,job,actor)).fetchone()

    def unified_return_disposition(self,tenant,actor,job,action,qty,target,
                                   outbound,inbound=None,disposition=None):
        key=disposition or uuid4()
        result=self.commerce_execute(
            tenant,actor,
            "SELECT one_resolve_return_quarantine(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant,job,key,action,qty,target,outbound,inbound,actor))
        return key,result

    def test_one_return_case_tracks_both_qa_and_finance_independently(self):
        tenant,actor,invoice,job=self.complete_customer_return_for_invoice()
        meta=self.admin.execute(
            "SELECT r.destination_warehouse_id,s.product_id,s.warehouse_id "
            "FROM one_return_jobs r JOIN one_stock_events s "
            "ON s.tenant_id=r.tenant_id AND s.id=r.source_event_id "
            "WHERE r.tenant_id=%s AND r.id=%s",(tenant,job)).fetchone()
        quarantine,product,origin=meta
        before=self.read_unified_return_case(tenant,actor,job)
        self.assertEqual(before[0:3],
                         ("customer","quarantine","credit_review_pending"))
        self.assertEqual(before[4:8],
                         (Decimal("2"),Decimal("0"),Decimal("2"),Decimal("0")))
        target=uuid4()
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name) "
            "VALUES (%s,%s,%s,'Saleable inspection released')",
            (tenant,target,"SALE-"+target.hex))
        outbound,inbound=uuid4(),uuid4()
        disposition,returned=self.unified_return_disposition(
            tenant,actor,job,"restock",Decimal("1"),target,outbound,inbound)
        self.assertEqual(returned,disposition)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone(),
            (Decimal("1"),Decimal("0")))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,target,product)).fetchone(),
            (Decimal("1"),Decimal("0")))
        self.assertEqual(self.read_unified_return_case(tenant,actor,job)[1:3],
                         ("qa_partially_resolved","credit_review_pending"))
        credit=self.credit_for_source_return(invoice,Decimal("1"))
        self.issue_accepted_return_credit(credit,job,actor)
        mid=self.read_unified_return_case(tenant,actor,job)
        self.assertEqual(mid[1:3],
                         ("qa_partially_resolved","partially_credited"))
        self.assertEqual(mid[5:8],
                         (Decimal("1"),Decimal("1"),Decimal("1")))
        other=uuid4()
        self.unified_return_disposition(
            tenant,actor,job,"dispose",Decimal("1"),None,other)
        final=self.read_unified_return_case(tenant,actor,job)
        self.assertEqual(final[1:3],("qa_resolved","partially_credited"))
        self.assertEqual(final[5:8],
                         (Decimal("2"),Decimal("0"),Decimal("1")))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone()[0],0)
        self.assertEqual(set(self.admin.execute(
            "SELECT event_type FROM one_operation_outbox "
            "WHERE tenant_id=%s AND source_id IN (%s,%s,%s)",
            (tenant,outbound,inbound,other)).fetchall()),{
            ("stock.return_restock_out",),
            ("stock.return_restock_in",),
            ("stock.return_disposal",),
        })
        self.assertEqual(self.admin.execute(
            "SELECT action FROM one_return_dispositions "
            "WHERE tenant_id=%s AND return_job_id=%s ORDER BY action",
            (tenant,job)).fetchall(),[("dispose",),("restock",)])

    def test_qa_disposition_rejects_overprocess_bad_target_and_replay(self):
        tenant,actor,invoice,job=self.complete_customer_return_for_invoice(
            acceptance=Decimal("1"))
        source=self.admin.execute(
            "SELECT destination_warehouse_id FROM one_return_jobs "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,job)).fetchone()[0]
        for kwargs in (
            ("restock",Decimal("1"),None,uuid4(),uuid4()),
            ("restock",Decimal("1"),source,uuid4(),uuid4()),
            ("dispose",Decimal("1"),source,uuid4(),None),
            ("dispose",Decimal("2"),None,uuid4(),None),
            ("restock",Decimal("1"),uuid4(),uuid4(),uuid4()),
        ):
            with self.subTest(kwargs=kwargs),self.assertRaises(self.psycopg.Error):
                self.unified_return_disposition(
                    tenant,actor,job,*kwargs)
        event=uuid4()
        first,_=self.unified_return_disposition(
            tenant,actor,job,"dispose",Decimal("1"),None,event)
        with self.assertRaises(self.psycopg.Error):
            self.unified_return_disposition(
                tenant,actor,job,"dispose",Decimal("1"),None,uuid4(),
                disposition=first)
        with self.assertRaises(self.psycopg.Error):
            self.unified_return_disposition(
                tenant,actor,job,"dispose",Decimal("1"),None,event)
        self.assertEqual(self.read_unified_return_case(tenant,actor,job)[1:3],
                         ("qa_resolved","credit_review_pending"))

    def test_unified_return_case_scoped_to_membership_and_db_tenant(self):
        tenant,actor,invoice,job=self.complete_customer_return_for_invoice()
        with self.assertRaises(self.psycopg.Error):
            self.read_unified_return_case(tenant,uuid4(),job)
        with self.assertRaises(self.psycopg.Error):
            self.read_unified_return_case(self.tenant_b,actor,job)
        with self.assertRaises(self.psycopg.Error):
            self.unified_return_disposition(
                tenant,uuid4(),job,"dispose",Decimal("1"),None,uuid4())
        with self.assertRaises(self.psycopg.Error):
            self.unified_return_disposition(
                self.tenant_b,actor,job,"dispose",Decimal("1"),None,uuid4())

    def test_unified_return_case_pending_rejected_and_vendor_paths(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        receipt=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
            (tenant,purchase,receipt,actor))
        vendor_job=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_request_return(%s,%s,%s,1,%s,%s,'Vendor return')",
            (tenant,vendor_job,receipt,None,actor))
        self.assertEqual(self.read_unified_return_case(
            tenant,actor,vendor_job)[1:3],
            ("inspection_pending","supplier_bill_matching_not_ready"))
        self.commerce_execute(
            tenant,actor,"SELECT one_finish_return(%s,%s,1,%s,%s)",
            (tenant,vendor_job,uuid4(),actor))
        self.assertEqual(self.read_unified_return_case(
            tenant,actor,vendor_job)[1:3],
            ("vendor_stock_returned","supplier_bill_matching_not_ready"))
        rejected=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_request_return(%s,%s,%s,1,%s,%s,'Rejected claim')",
            (tenant,rejected,receipt,None,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_finish_return(%s,%s,0,%s,%s)",
            (tenant,rejected,None,actor))
        self.assertEqual(self.read_unified_return_case(
            tenant,actor,rejected)[1:3],
            ("rejected","supplier_bill_matching_not_ready"))

    def test_disposition_event_conflict_rolls_back_both_stock_buckets(self):
        tenant,actor,invoice,job=self.complete_customer_return_for_invoice()
        row=self.admin.execute(
            "SELECT r.destination_warehouse_id,s.product_id,s.id "
            "FROM one_return_jobs r JOIN one_stock_events s "
            "ON s.tenant_id=r.tenant_id AND s.id=r.source_event_id "
            "WHERE r.tenant_id=%s AND r.id=%s",(tenant,job)).fetchone()
        quarantine,product,existing_event=row
        target=uuid4()
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name) VALUES "
            "(%s,%s,%s,'Return target')",
            (tenant,target,"TG-"+target.hex))
        with self.assertRaises(self.psycopg.Error):
            self.unified_return_disposition(
                tenant,actor,job,"restock",Decimal("1"),target,
                existing_event,uuid4())
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone()[0],Decimal("2"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,target,product)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_return_dispositions "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,job)).fetchone()[0],0)



    def test_quarantine_warehouse_cannot_supply_customer_sales(self):
        tenant,actor,invoice,job=self.complete_customer_return_for_invoice(
            acceptance=Decimal("1"))
        quarantine=self.admin.execute(
            "SELECT destination_warehouse_id FROM one_return_jobs "
            "WHERE tenant_id=%s AND id=%s",(tenant,job)).fetchone()[0]
        product=self.admin.execute(
            "SELECT product_id FROM one_stock_events e "
            "JOIN one_return_jobs r ON r.tenant_id=e.tenant_id "
            "AND r.source_event_id=e.id "
            "WHERE r.tenant_id=%s AND r.id=%s",
            (tenant,job)).fetchone()[0]
        new_order=uuid4()
        self.admin.execute(
            "INSERT INTO one_commercial_orders "
            "(tenant_id,id,kind,number,partner_id,currency) "
            "VALUES (%s,%s,'sale',%s,'DEMO-CUSTOMER','SAR')",
            (tenant,new_order,"QUAR-"+new_order.hex))
        with self.assertRaises(self.psycopg.Error):
            self.admin.execute(
                "INSERT INTO one_commercial_order_lines "
                "(tenant_id,order_id,line_no,product_id,warehouse_id,"
                "ordered_qty,unit_price) VALUES (%s,%s,1,%s,%s,1,10)",
                (tenant,new_order,product,quarantine))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone()[0],Decimal("1"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_commercial_order_lines "
            "WHERE tenant_id=%s AND order_id=%s",
            (tenant,new_order)).fetchone()[0],0)

    def test_warehouse_classification_is_immutable_after_creation(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        with self.assertRaises(self.psycopg.Error):
            self.admin.execute(
                "UPDATE one_warehouses SET warehouse_type='quarantine' "
                "WHERE tenant_id=%s AND id=%s",(tenant,warehouse))
        self.assertEqual(self.admin.execute(
            "SELECT warehouse_type FROM one_warehouses "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,warehouse)).fetchone()[0],"sellable")



    def fetch_return_timeline(self,tenant,actor,job):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT stream_position,event_type,source_id "
                "FROM one_return_case_timeline(%s,%s,%s)",
                (tenant,job,actor)).fetchall()

    def test_return_timeline_connects_physical_decisions_and_credit(self):
        tenant,actor,invoice,job=self.complete_customer_return_for_invoice()
        q=self.admin.execute(
            "SELECT s.product_id FROM one_return_jobs j JOIN one_stock_events s "
            "ON s.tenant_id=j.tenant_id AND s.id=j.source_event_id "
            "WHERE j.tenant_id=%s AND j.id=%s",(tenant,job)).fetchone()[0]
        target=uuid4()
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name) "
            "VALUES (%s,%s,%s,'QC Released')",(tenant,target,"RL-"+target.hex))
        d1=uuid4()
        self.unified_return_disposition(
            tenant,actor,job,"restock",Decimal("1"),target,uuid4(),uuid4(),
            disposition=d1)
        d2=uuid4()
        self.unified_return_disposition(
            tenant,actor,job,"dispose",Decimal("1"),None,uuid4(),
            disposition=d2)
        credit=self.credit_for_source_return(invoice,Decimal("1"))
        self.issue_accepted_return_credit(credit,job,actor)
        trace=self.fetch_return_timeline(tenant,actor,job)
        events={ev for _,ev,_ in trace}
        self.assertTrue({
            "returns.requested","returns.completed",
            "stock.customer_returned","returns.restocked","returns.disposed",
            "stock.return_restock_in","stock.return_restock_out",
            "stock.return_disposal","finance.customer_return_credited",
            "finance.invoice_issued","finance.journal_posted"
        }.issubset(events))
        self.assertEqual({sid for _,ev,sid in trace
                          if ev=="returns.restocked"},{d1})
        self.assertEqual({sid for _,ev,sid in trace
                          if ev=="returns.disposed"},{d2})
        self.assertEqual(len(trace),len(set((e,sid) for _,e,sid in trace)))
        positions=[x[0] for x in trace]
        self.assertEqual(positions,sorted(positions))
        self.assertTrue(all(pos>0 for pos in positions))

    def test_return_case_timeline_denies_wrong_user_tenant_and_unknown_case(self):
        tenant,actor,invoice,job=self.complete_customer_return_for_invoice()
        for tid,who,case in (
            (tenant,uuid4(),job),
            (self.tenant_b,actor,job),
            (tenant,actor,uuid4()),
        ):
            with self.subTest(tid=tid,case=case),self.assertRaises(self.psycopg.Error):
                self.fetch_return_timeline(tid,who,case)
        self.assertIn("returns.requested",
                      [e for _,e,_ in self.fetch_return_timeline(tenant,actor,job)])

    def test_concurrent_quarantine_qa_resolution_never_processes_same_return_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,actor,invoice,job=self.complete_customer_return_for_invoice(
            acceptance=Decimal("1"))
        gate=Barrier(2)
        def settle(disposition,event):
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
                        gate.wait(timeout=12)
                        conn.execute(
                            "SELECT one_resolve_return_quarantine("
                            "%s,%s,%s,'dispose',1,NULL,%s,NULL,%s)",
                            (tenant,job,disposition,event,actor))
                    return True
                except self.psycopg.Error:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(settle,uuid4(),uuid4())
            second=pool.submit(settle,uuid4(),uuid4())
            outcomes=(first.result(timeout=28),second.result(timeout=28))
        self.assertEqual(sorted(outcomes),[False,True])
        self.assertEqual(self.admin.execute(
            "SELECT sum(quantity) FROM one_return_dispositions "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,job)).fetchone()[0],Decimal("1"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_operation_outbox o "
            "WHERE o.tenant_id=%s AND o.event_type='returns.disposed' "
            "AND o.source_id IN (SELECT id FROM one_return_dispositions "
            "WHERE tenant_id=%s AND return_job_id=%s)",
            (tenant,tenant,job)).fetchone()[0],1)
        self.assertEqual(self.read_unified_return_case(tenant,actor,job)[1],
                         "qa_resolved")

    def test_failed_qa_disposition_writes_no_success_event_or_movement(self):
        tenant,actor,invoice,job=self.complete_customer_return_for_invoice(
            acceptance=Decimal("1"))
        d=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.unified_return_disposition(
                tenant,actor,job,"dispose",Decimal("2"),None,uuid4(),
                disposition=d)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_operation_outbox "
            "WHERE tenant_id=%s AND event_type='returns.disposed' "
            "AND source_id=%s",(tenant,d)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_return_dispositions "
            "WHERE tenant_id=%s AND id=%s",(tenant,d)).fetchone()[0],0)



    def test_vendor_return_trace_includes_original_receipt_and_supplier_dispatch(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        received=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
            (tenant,purchase,received,actor))
        rma,returned=uuid4(),uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_request_return(%s,%s,%s,1,%s,%s,'Supplier reject')",
            (tenant,rma,received,None,actor))
        self.commerce_execute(
            tenant,actor,"SELECT one_finish_return(%s,%s,1,%s,%s)",
            (tenant,rma,returned,actor))
        log=self.fetch_return_timeline(tenant,actor,rma)
        self.assertTrue({
            ("stock.purchase_received",received),
            ("returns.requested",rma),("returns.completed",rma),
            ("stock.vendor_returned",returned),
        }.issubset(set((event,source) for _,event,source in log)))
        self.assertEqual(len(log),4)
        self.assertEqual([pos for pos,_,_ in log],
                         sorted(pos for pos,_,_ in log))
        self.assertEqual(self.read_unified_return_case(tenant,actor,rma)[1],
                         "vendor_stock_returned")



    def value_stock_event(self,tenant,actor,stock_event,offset,journal=None,
                          inventory="inventory",period_id=None):
        journal=journal or uuid4()
        return journal,self.commerce_execute(
            tenant,actor,"SELECT one_value_physical_stock_event("
            "%s,%s,%s,%s,%s,%s,%s)",
            (tenant,stock_event,period_id or self.period_id,
             inventory,offset,journal,actor))

    def test_real_valuation_receipt_then_sale_posts_grni_cogs_and_source_link(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        receipt=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,purchase,receipt,actor))
        j1,result=self.value_stock_event(tenant,actor,receipt,"grni")
        self.assertEqual(j1,result)
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value,currency FROM one_stock_value_positions "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("5"),Decimal("62.50"),"SAR"))
        ship=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,sale,ship,actor))
        j2,result=self.value_stock_event(tenant,actor,ship,"cogs")
        self.assertEqual(j2,result)
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("3"),Decimal("37.50")))
        self.assertEqual(self.admin.execute(
            "SELECT movement_kind,qty,amount,journal_id FROM one_stock_value_events "
            "WHERE tenant_id=%s AND stock_event_id=%s",
            (tenant,ship)).fetchone(),
            ("sale_dispatch",Decimal("2"),Decimal("25.00"),j2))
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_journal_entries WHERE tenant_id=%s "
            "AND id=%s",(tenant,j2)).fetchone()[0],"posted")
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines "
            "WHERE tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,j2)).fetchall(),
            [("cogs",Decimal("25"),Decimal("0")),
             ("inventory",Decimal("0"),Decimal("25"))])
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines "
            "WHERE tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,j1)).fetchall(),
            [("inventory",Decimal("62.50"),Decimal("0")),
             ("grni",Decimal("0"),Decimal("62.50"))])
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_ledger_audit WHERE tenant_id=%s "
            "AND entry_id IN (%s,%s)",
            (tenant,j1,j2)).fetchone()[0],2)

    def test_full_sale_empties_book_value_and_is_not_double_costed(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        first=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,purchase,first,actor))
        self.value_stock_event(tenant,actor,first,"grni")
        ship=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,sale,ship,actor))
        journal,_=self.value_stock_event(tenant,actor,ship,"cogs")
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),(0,0))
        with self.assertRaises(self.psycopg.Error):
            self.value_stock_event(tenant,actor,ship,"cogs")
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_value_events WHERE "
            "tenant_id=%s AND stock_event_id=%s",
            (tenant,ship)).fetchone()[0],1)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_journal_entries "
            "WHERE tenant_id=%s AND id=%s",(tenant,journal)).fetchone()[0],1)

    def test_value_ledger_refuses_old_stock_event_when_multiple_unvalued(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,actor))
        receipt=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,purchase,receipt,actor))
        ship=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,1,%s,%s)",
            (tenant,sale,ship,actor))
        for evt,offset in ((receipt,"grni"),(ship,"cogs")):
            with self.assertRaises(self.psycopg.Error):
                self.value_stock_event(tenant,actor,evt,offset)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_value_positions WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_value_events "
            "WHERE tenant_id=%s AND stock_event_id IN (%s,%s)",
            (tenant,receipt,ship)).fetchone()[0],0)

    def test_valuation_fail_closed_wrong_account_role_or_closed_period(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        evt=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,purchase,evt,actor))
        # An unknown/unauthorized period must fail without partial valuations.
        invalid_period=uuid4()
        for args in (
            {"offset":"cogs"},
            {"offset":"grni","inventory":"sales"},
            {"offset":"grni","period_id":invalid_period},
        ):
            with self.subTest(args=args),self.assertRaises(self.psycopg.Error):
                self.value_stock_event(tenant,actor,evt,**args)
        with self.assertRaises(self.psycopg.Error):
            self.value_stock_event(tenant,self.actor,evt,"grni")
        with self.assertRaises(self.psycopg.Error):
            self.value_stock_event(self.tenant_b,actor,evt,"grni")
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_value_positions WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],0)
        self.value_stock_event(tenant,actor,evt,"grni")
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_value_events WHERE "
            "tenant_id=%s AND stock_event_id=%s",
            (tenant,evt)).fetchone()[0],1)

    def test_valuation_position_and_source_events_are_append_only(self):
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        evt=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,1,%s,%s)",
            (tenant,purchase,evt,actor))
        self.value_stock_event(tenant,actor,evt,"grni")
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(tenant,actor,
                "UPDATE one_stock_value_positions SET book_value=999 "
                "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s "
                "RETURNING qty",(tenant,warehouse,product))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(tenant,actor,
                "DELETE FROM one_stock_value_events "
                "WHERE tenant_id=%s AND stock_event_id=%s RETURNING journal_id",
                (tenant,evt))
        self.assertEqual(self.admin.execute(
            "SELECT book_value FROM one_stock_value_positions "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("12.50"))

    def test_two_transactions_cannot_value_the_same_receipt_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,actor,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        receipt=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
            (tenant,purchase,receipt,actor))
        gate=Barrier(2)
        def attempt(journal):
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
                            "SELECT one_value_physical_stock_event("
                            "%s,%s,%s,'inventory','grni',%s,%s)",
                            (tenant,receipt,self.period_id,journal,actor))
                    return True
                except self.psycopg.Error:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(attempt,uuid4())
            two=pool.submit(attempt,uuid4())
            outcomes=(one.result(timeout=25),two.result(timeout=25))
        self.assertEqual(sorted(outcomes),[False,True])
        self.assertEqual(self.admin.execute(
            "SELECT book_value,qty FROM one_stock_value_positions "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("37.50"),Decimal("3")))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_value_events WHERE tenant_id=%s "
            "AND stock_event_id=%s",(tenant,receipt)).fetchone()[0],1)



    def valued_receipt_for_vendor_bill(self,qty=Decimal("5")):
        tenant,admin,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,admin))
        physical=uuid4()
        self.commerce_execute(
            tenant,admin,"SELECT one_fulfill_commercial_line(%s,%s,1,%s,%s,%s)",
            (tenant,purchase,qty,physical,admin))
        self.value_stock_event(tenant,admin,physical,"grni")
        return tenant,admin,product,warehouse,purchase,sale,physical

    def sample_goods_vendor_bill(self,tenant,quantity,*,partner="TEST-PARTNER",
                                 price=Decimal("12.50"),grni="grni"):
        return InvoiceDocument(
            uuid4(),tenant,"VB-"+uuid4().hex,partner,
            DocumentType.VENDOR_BILL,date(2026,10,15),date(2026,11,15),
            "SAR",(InvoiceLine("Accepted stock / GRNI",grni,quantity,price),),
        )

    def post_matched_vendor_goods(self,tenant,actor,purchase,doc,lines=(1,)):
        membership=(Membership(actor,tenant,Role.ACCOUNTANT),)
        return persist_valued_goods_vendor_bill(
            self.db,order_id=purchase,order_line_numbers=lines,
            grni_account_id="grni",document=doc,
            accounts=self.invoice_accounts,chart=self.chart,
            period=self.period,period_id=self.period_id,
            actor_id=actor,memberships=membership,
        )

    def test_valued_receipt_3_way_match_clears_grni_to_vendor_ap(self):
        tenant,admin,product,warehouse,purchase,sale,physical=(
            self.valued_receipt_for_vendor_bill())
        doc=self.sample_goods_vendor_bill(tenant,Decimal("3"))
        proposal=self.post_matched_vendor_goods(tenant,self.actor,purchase,doc)
        self.assertEqual(proposal.totals.net,Decimal("37.50"))
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_invoice_docs WHERE tenant_id=%s AND id=%s",
            (tenant,doc.id)).fetchone()[0],"issued")
        self.assertEqual(self.admin.execute(
            "SELECT order_id,order_line_no,billed_qty "
            "FROM one_goods_bill_allocations WHERE tenant_id=%s AND bill_id=%s",
            (tenant,doc.id)).fetchone(),
            (purchase,1,Decimal("3")))
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines "
            "WHERE tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,doc.id)).fetchall(),
            [("grni",Decimal("37.50"),Decimal("0")),
             ("ap",Decimal("0"),Decimal("37.50"))])
        next_doc=self.sample_goods_vendor_bill(tenant,Decimal("2"))
        self.post_matched_vendor_goods(tenant,self.actor,purchase,next_doc)
        self.assertEqual(self.admin.execute(
            "SELECT sum(billed_qty) FROM one_goods_bill_allocations "
            "WHERE tenant_id=%s AND order_id=%s",
            (tenant,purchase)).fetchone()[0],Decimal("5"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_invoice_audit "
            "WHERE tenant_id=%s AND doc_id IN (%s,%s)",
            (tenant,doc.id,next_doc.id)).fetchone()[0],2)

    def test_goods_vendor_bill_overmatched_qty_rolls_back_document_gl_and_audit(self):
        tenant,admin,product,warehouse,purchase,sale,physical=(
            self.valued_receipt_for_vendor_bill(Decimal("2")))
        first=self.sample_goods_vendor_bill(tenant,Decimal("1"))
        self.post_matched_vendor_goods(tenant,self.actor,purchase,first)
        excess=self.sample_goods_vendor_bill(tenant,Decimal("2"))
        with self.assertRaises(self.psycopg.Error):
            self.post_matched_vendor_goods(tenant,self.actor,purchase,excess)
        for table,col in (
            ("one_invoice_docs","id"),
            ("one_journal_entries","id"),
            ("one_invoice_audit","doc_id"),
            ("one_goods_bill_allocations","bill_id"),
        ):
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM "+table+" WHERE tenant_id=%s AND "+col+
                "=%s",(tenant,excess.id)).fetchone()[0],0)

    def test_goods_vendor_bill_rejects_wrong_partner_price_and_receipt_absence(self):
        tenant,admin,product,warehouse,purchase,sale,physical=(
            self.valued_receipt_for_vendor_bill(Decimal("3")))
        bad_docs=[
            self.sample_goods_vendor_bill(tenant,Decimal("1"),partner="OTHER"),
            self.sample_goods_vendor_bill(tenant,Decimal("1"),
                                          price=Decimal("13.00")),
        ]
        for doc in bad_docs:
            with self.subTest(doc=doc.id),self.assertRaises(self.psycopg.Error):
                self.post_matched_vendor_goods(tenant,self.actor,purchase,doc)
        other_tenant,other_admin,_,_,other_purchase,_,_=(
            self.valued_receipt_for_vendor_bill(Decimal("1")))
        # A separate purchase order is not covered by this goods bill's PO.
        early=self.sample_goods_vendor_bill(tenant,Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.post_matched_vendor_goods(tenant,self.actor,uuid4(),early)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_invoice_docs WHERE tenant_id=%s "
            "AND id IN (%s,%s,%s)",(tenant,*[d.id for d in bad_docs],early.id)
        ).fetchone()[0],0)

    def test_goods_vendor_bill_wont_post_before_valued_physical_receipt(self):
        tenant,admin,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,admin))
        # First not even received; then physically received but unvalued.
        doc=self.sample_goods_vendor_bill(tenant,Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.post_matched_vendor_goods(tenant,self.actor,purchase,doc)
        physical=uuid4()
        self.commerce_execute(
            tenant,admin,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,purchase,physical,admin))
        with self.assertRaises(self.psycopg.Error):
            self.post_matched_vendor_goods(tenant,self.actor,purchase,doc)
        self.value_stock_event(tenant,admin,physical,"grni")
        self.post_matched_vendor_goods(tenant,self.actor,purchase,doc)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_goods_bill_allocations "
            "WHERE tenant_id=%s AND bill_id=%s",
            (tenant,doc.id)).fetchone()[0],1)

    def test_vendor_bill_grni_clearing_cannot_bypass_dedicated_bridge(self):
        from one_erp_core.accounting import AccountingError
        tenant,admin,product,warehouse,purchase,sale,physical=(
            self.valued_receipt_for_vendor_bill(Decimal("1")))
        doc=self.sample_goods_vendor_bill(tenant,Decimal("1"))
        with self.assertRaises(AccountingError):
            persist_invoice_with_journal(
                self.db,document=doc,accounts=self.invoice_accounts,
                chart=self.chart,period=self.period,
                period_id=self.period_id,actor_id=self.actor,
                memberships=self.memberships)
        with self.assertRaises(AccountingError):
            self.post_matched_vendor_goods(
                tenant,self.actor,purchase,doc,lines=(1,1))
        with self.assertRaises(AccountingError):
            self.post_matched_vendor_goods(
                tenant,self.actor,purchase,doc,lines=(True,))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_invoice_docs WHERE tenant_id=%s AND id=%s",
            (tenant,doc.id)).fetchone()[0],0)

    def test_concurrent_valued_vendor_bills_do_not_double_claim_same_receipt(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,admin,product,warehouse,purchase,sale,physical=(
            self.valued_receipt_for_vendor_bill(Decimal("2")))
        docs=[self.sample_goods_vendor_bill(tenant,Decimal("2")) for _ in range(2)]
        barrier=Barrier(2)
        def run(doc):
            with self.psycopg.connect(
                host=os.environ.get("ONE_TEST_PG_HOST","localhost"),
                port=int(os.environ.get("ONE_TEST_PG_PORT","5432")),
                dbname=os.environ.get("ONE_TEST_PG_DB","one_erp_test"),
                user="one_erp_runtime_ci",password="ci_only_password",
                autocommit=True
            ) as conn:
                try:
                    barrier.wait(timeout=10)
                    persist_valued_goods_vendor_bill(
                        conn,order_id=purchase,order_line_numbers=(1,),
                        grni_account_id="grni",document=doc,
                        accounts=self.invoice_accounts,chart=self.chart,
                        period=self.period,period_id=self.period_id,
                        actor_id=self.actor,memberships=self.memberships,
                    )
                    return True
                except self.psycopg.Error:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            left=pool.submit(run,docs[0])
            right=pool.submit(run,docs[1])
            results=(left.result(timeout=30),right.result(timeout=30))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(self.admin.execute(
            "SELECT sum(billed_qty) FROM one_goods_bill_allocations "
            "WHERE tenant_id=%s AND order_id=%s",
            (tenant,purchase)).fetchone()[0],Decimal("2"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_invoice_docs WHERE tenant_id=%s "
            "AND id IN (%s,%s)",
            (tenant,docs[0].id,docs[1].id)).fetchone()[0],1)



    def inventory_pulse_check(self,tenant,actor,warehouse,product):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT physical_qty,reserved_qty,available_qty,valued_qty,"
                "valued_amount,unvalued_events,grni_received_value,"
                "grni_billed_value,grni_outstanding_value,reconciliation_ok "
                "FROM one_inventory_finance_reconciliation(%s,%s,%s,%s)",
                (tenant,warehouse,product,actor)).fetchone()

    def test_inventory_finance_pulse_reconciles_stock_cogs_and_vendor_grni(self):
        tenant,admin,product,warehouse,purchase,sale,received=(
            self.valued_receipt_for_vendor_bill(Decimal("5")))
        bill=self.sample_goods_vendor_bill(tenant,Decimal("3"))
        self.post_matched_vendor_goods(tenant,self.actor,purchase,bill)
        self.commerce_execute(
            tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,sale,admin))
        sent=uuid4()
        self.commerce_execute(
            tenant,admin,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,sale,sent,admin))
        self.value_stock_event(tenant,admin,sent,"cogs")
        row=self.inventory_pulse_check(tenant,self.actor,warehouse,product)
        self.assertEqual(row,(
            Decimal("3"),Decimal("0"),Decimal("3"),
            Decimal("3"),Decimal("37.50"),0,Decimal("62.50"),
            Decimal("37.50"),Decimal("25.00"),True
        ))

    def test_unvalued_stock_detected_and_not_reported_as_book_value(self):
        tenant,admin,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,admin))
        evt=uuid4()
        self.commerce_execute(
            tenant,admin,"SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,purchase,evt,admin))
        before=self.inventory_pulse_check(tenant,admin,warehouse,product)
        self.assertEqual(before[0:6],(
            Decimal("2"),Decimal("0"),Decimal("2"),
            Decimal("0"),Decimal("0"),1))
        self.assertFalse(before[-1])
        self.value_stock_event(tenant,admin,evt,"grni")
        after=self.inventory_pulse_check(tenant,admin,warehouse,product)
        self.assertEqual(after[0:6],(
            Decimal("2"),Decimal("0"),Decimal("2"),
            Decimal("2"),Decimal("25.00"),0))
        self.assertEqual(after[6:9],(
            Decimal("25.00"),Decimal("0"),Decimal("25.00")))
        self.assertTrue(after[-1])

    def test_inventory_pulse_denies_tenant_and_actor_impersonation(self):
        tenant,admin,product,warehouse,purchase,sale,received=(
            self.valued_receipt_for_vendor_bill(Decimal("1")))
        with self.assertRaises(self.psycopg.Error):
            self.inventory_pulse_check(tenant,uuid4(),warehouse,product)
        with self.assertRaises(self.psycopg.Error):
            self.inventory_pulse_check(self.tenant_b,admin,warehouse,product)
        self.assertTrue(self.inventory_pulse_check(
            tenant,self.actor,warehouse,product)[-1])

    def test_weighted_average_across_two_purchase_prices_is_booked_not_guessed(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill(Decimal("2")))
        other_order=uuid4()
        self.admin.execute(
            "INSERT INTO one_commercial_orders"
            "(tenant_id,id,kind,number,partner_id,currency) "
            "VALUES (%s,%s,'purchase',%s,'TEST-PARTNER','SAR')",
            (tenant,other_order,"PO-"+other_order.hex))
        self.admin.execute(
            "INSERT INTO one_commercial_order_lines "
            "(tenant_id,order_id,line_no,product_id,warehouse_id,"
            "ordered_qty,unit_price) VALUES (%s,%s,1,%s,%s,3,20)",
            (tenant,other_order,product,warehouse))
        self.commerce_execute(
            tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,other_order,admin))
        r2=uuid4()
        self.commerce_execute(
            tenant,admin,"SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
            (tenant,other_order,r2,admin))
        self.value_stock_event(tenant,admin,r2,"grni")
        self.commerce_execute(
            tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,sale,admin))
        shipment=uuid4()
        self.commerce_execute(
            tenant,admin,"SELECT one_fulfill_commercial_line(%s,%s,1,1,%s,%s)",
            (tenant,sale,shipment,admin))
        self.value_stock_event(tenant,admin,shipment,"cogs")
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("4"),Decimal("68.00")))
        self.assertEqual(self.admin.execute(
            "SELECT amount FROM one_stock_value_events "
            "WHERE tenant_id=%s AND stock_event_id=%s",
            (tenant,shipment)).fetchone()[0],Decimal("17.00"))
        report=self.inventory_pulse_check(tenant,admin,warehouse,product)
        self.assertEqual(report[6:9],(
            Decimal("85.00"),Decimal("0"),Decimal("85.00")))
        self.assertTrue(report[-1])



    def atomic_stock_post(self,tenant,admin,order,qty,kind,*,event=None,
                          journal=None,period=None,offset=None):
        event=event or uuid4()
        journal=journal or uuid4()
        fn="one_receive_and_value_goods" if kind=="purchase" else "one_ship_and_value_goods"
        val=offset or ("grni" if kind=="purchase" else "cogs")
        result=self.commerce_execute(
            tenant,admin,
            "SELECT "+fn+"(%s,%s,1,%s,%s,%s,%s,'inventory',%s,%s)",
            (tenant,order,qty,event,journal,period or self.period_id,val,admin))
        return event,journal,result

    def test_atomic_goods_receipt_sale_value_and_audit_are_single_transaction(self):
        tenant,admin,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,admin))
        incoming,in_journal,value=self.atomic_stock_post(
            tenant,admin,purchase,Decimal("5"),"purchase")
        self.assertEqual(incoming,value)
        outgoing,out_journal,value=self.atomic_stock_post(
            tenant,admin,sale,Decimal("2"),"sale")
        self.assertEqual(outgoing,value)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("3"))
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("3"),Decimal("37.50")))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_value_events WHERE tenant_id=%s "
            "AND stock_event_id IN (%s,%s)",(tenant,incoming,outgoing)).fetchone()[0],2)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_ledger_audit WHERE tenant_id=%s "
            "AND entry_id IN (%s,%s)",(tenant,in_journal,out_journal)).fetchone()[0],2)

    def test_atomic_receipt_with_unpostable_period_does_not_receive_stock(self):
        tenant,admin,product,warehouse,purchase,sale=self.commerce_fixture()
        self.commerce_execute(
            tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,admin))
        event,journal=uuid4(),uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.atomic_stock_post(
                tenant,admin,purchase,Decimal("3"),"purchase",
                event=event,journal=journal,period=uuid4())
        self.assertEqual(self.admin.execute(
            "SELECT fulfilled_qty FROM one_commercial_order_lines WHERE "
            "tenant_id=%s AND order_id=%s AND line_no=1",
            (tenant,purchase)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],0)
        for table,col in (
            ("one_stock_events","id"),
            ("one_stock_value_events","stock_event_id"),
            ("one_journal_entries","id"),
        ):
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM "+table+" WHERE tenant_id=%s AND "
                +col+"=%s",(tenant,event if table!="one_journal_entries" else journal)
            ).fetchone()[0],0)
        self.atomic_stock_post(tenant,admin,purchase,Decimal("3"),"purchase",
                               event=event,journal=journal)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("3"))

    def test_atomic_sale_wrong_cogs_account_rolls_back_dispatched_units(self):
        tenant,admin,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,admin))
        self.atomic_stock_post(tenant,admin,purchase,Decimal("4"),"purchase")
        bad=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.atomic_stock_post(
                tenant,admin,sale,Decimal("2"),"sale",
                event=bad,offset="grni")
        self.assertEqual(self.admin.execute(
            "SELECT fulfilled_qty FROM one_commercial_order_lines "
            "WHERE tenant_id=%s AND order_id=%s AND line_no=1",
            (tenant,sale)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("4"))
        self.assertEqual(self.admin.execute(
            "SELECT book_value FROM one_stock_value_positions WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("50"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events "
            "WHERE tenant_id=%s AND id=%s",(tenant,bad)).fetchone()[0],0)

    def test_atomic_delivery_pick_pack_ship_also_posts_cogs(self):
        tenant,admin,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,admin))
        self.atomic_stock_post(tenant,admin,purchase,Decimal("5"),"purchase")
        self.commerce_execute(
            tenant,admin,"SELECT one_reserve_sale_line(%s,%s,1,2,%s,%s)",
            (tenant,sale,uuid4(),admin))
        job=uuid4()
        self.commerce_execute(
            tenant,admin,"SELECT one_create_delivery(%s,%s,%s,1,2,%s)",
            (tenant,job,sale,admin))
        self.delivery_stage(tenant,admin,job,"picked")
        self.delivery_stage(tenant,admin,job,"packed")
        shipment,journal=uuid4(),uuid4()
        result=self.commerce_execute(
            tenant,admin,
            "SELECT one_dispatch_delivery_and_value("
            "%s,%s,%s,%s,%s,'inventory','cogs',%s)",
            (tenant,job,shipment,journal,self.period_id,admin))
        self.assertEqual(result,job)
        self.assertEqual(self.admin.execute(
            "SELECT status,dispatch_event_id FROM one_delivery_jobs "
            "WHERE tenant_id=%s AND id=%s",(tenant,job)).fetchone(),
            ("dispatched",shipment))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("3"),Decimal("0")))
        self.assertEqual(self.admin.execute(
            "SELECT amount,journal_id FROM one_stock_value_events "
            "WHERE tenant_id=%s AND stock_event_id=%s",
            (tenant,shipment)).fetchone(),
            (Decimal("25"),journal))

    def test_atomic_reserved_dispatch_failure_does_not_consume_allocation(self):
        tenant,admin,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,admin))
        self.atomic_stock_post(tenant,admin,purchase,Decimal("5"),"purchase")
        self.commerce_execute(
            tenant,admin,"SELECT one_reserve_sale_line(%s,%s,1,2,%s,%s)",
            (tenant,sale,uuid4(),admin))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,admin,
                "SELECT one_dispatch_reserved_and_value_goods("
                "%s,%s,1,2,%s,%s,%s,'inventory','grni',%s)",
                (tenant,sale,uuid4(),uuid4(),self.period_id,admin))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("5"),Decimal("2")))
        self.assertEqual(self.admin.execute(
            "SELECT reserved_qty,fulfilled_qty FROM one_commercial_order_lines "
            "WHERE tenant_id=%s AND order_id=%s AND line_no=1",
            (tenant,sale)).fetchone(),
            (Decimal("2"),Decimal("0")))
        self.commerce_execute(
            tenant,admin,
            "SELECT one_dispatch_reserved_and_value_goods("
            "%s,%s,1,2,%s,%s,%s,'inventory','cogs',%s)",
            (tenant,sale,uuid4(),uuid4(),self.period_id,admin))
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("3"),Decimal("37.50")))



    def prepare_costed_customer_return(self, accepted=Decimal("2")):
        tenant,admin,product,warehouse,purchase,sale=self.commerce_fixture()
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,admin))
        self.atomic_stock_post(tenant,admin,purchase,Decimal("4"),"purchase")
        source,journal,_=self.atomic_stock_post(
            tenant,admin,sale,Decimal("2"),"sale")
        quarantine=uuid4()
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name,warehouse_type) "
            "VALUES (%s,%s,%s,'Return cost quarantine','quarantine')",
            (tenant,quarantine,"QC-"+quarantine.hex))
        return_job=uuid4()
        self.commerce_execute(
            tenant,admin,
            "SELECT one_request_return(%s,%s,%s,%s,%s,%s,'Accepted for QC')",
            (tenant,return_job,source,accepted,quarantine,admin))
        return tenant,admin,product,warehouse,sale,source,quarantine,return_job

    def post_costed_customer_return(self,tenant,admin,job,qty,
                                    *,event=None,journal=None,period=None):
        event=event or uuid4()
        journal=journal or uuid4()
        result=self.commerce_execute(
            tenant,admin,
            "SELECT one_accept_and_value_customer_return("
            "%s,%s,%s,%s,%s,%s,'inventory','cogs',%s)",
            (tenant,job,qty,event,journal,period or self.period_id,admin))
        return event,journal,result

    def test_customer_return_restores_source_cogs_at_original_cost_not_sale_price(self):
        tenant,admin,product,warehouse,sale,source,quarantine,job=(
            self.prepare_costed_customer_return())
        event,journal,result=self.post_costed_customer_return(
            tenant,admin,job,Decimal("2"))
        self.assertEqual(result,job)
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value,currency FROM one_stock_value_positions "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone(),
            (Decimal("2"),Decimal("25.00"),"SAR"))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone()[0],Decimal("2"))
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines "
            "WHERE tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,journal)).fetchall(),
            [("inventory",Decimal("25.00"),Decimal("0")),
             ("cogs",Decimal("0"),Decimal("25.00"))])
        self.assertEqual(self.admin.execute(
            "SELECT source_dispatch_event_id,quantity,amount "
            "FROM one_customer_return_cost_links "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,job)).fetchone(),
            (source,Decimal("2"),Decimal("25.00")))
        self.assertEqual(self.admin.execute(
            "SELECT movement_kind,qty,amount FROM one_stock_value_events "
            "WHERE tenant_id=%s AND stock_event_id=%s",
            (tenant,event)).fetchone(),
            ("customer_return",Decimal("2"),Decimal("25.00")))
        self.assertTrue(self.inventory_pulse_check(
            tenant,admin,quarantine,product)[-1])
        self.assertEqual(self.read_unified_return_case(tenant,admin,job)[1],
                         "quarantine")

    def test_accepted_return_costing_rolls_back_stock_on_invalid_gl_period(self):
        tenant,admin,product,warehouse,sale,source,quarantine,job=(
            self.prepare_costed_customer_return())
        event,journal=uuid4(),uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.post_costed_customer_return(
                tenant,admin,job,Decimal("2"),
                event=event,journal=journal,period=uuid4())
        self.assertEqual(self.admin.execute(
            "SELECT status,processed_event_id FROM one_return_jobs "
            "WHERE tenant_id=%s AND id=%s",(tenant,job)).fetchone(),
            ("requested",None))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone()[0],0)
        for table,column,key in (
            ("one_stock_events","id",event),
            ("one_stock_value_events","stock_event_id",event),
            ("one_journal_entries","id",journal),
            ("one_customer_return_cost_links","return_job_id",job),
        ):
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM "+table+" WHERE tenant_id=%s AND "+
                column+"=%s",(tenant,key)).fetchone()[0],0)
        self.post_costed_customer_return(
            tenant,admin,job,Decimal("2"),
            event=event,journal=journal)
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_return_jobs WHERE tenant_id=%s AND id=%s",
            (tenant,job)).fetchone()[0],"completed")

    def test_customer_return_cost_repeated_acceptance_is_rejected(self):
        tenant,admin,product,warehouse,sale,source,quarantine,job=(
            self.prepare_costed_customer_return())
        self.post_costed_customer_return(tenant,admin,job,Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.post_costed_customer_return(tenant,admin,job,Decimal("1"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_customer_return_cost_links "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,job)).fetchone()[0],1)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone()[0],Decimal("1"))

    def test_customer_return_cost_denies_forged_tenant_and_wrong_role(self):
        tenant,admin,product,warehouse,sale,source,quarantine,job=(
            self.prepare_costed_customer_return())
        with self.assertRaises(self.psycopg.Error):
            self.post_costed_customer_return(tenant,self.actor,job,Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.post_costed_customer_return(
                self.tenant_b,admin,job,Decimal("1"))
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_return_jobs WHERE tenant_id=%s AND id=%s",
            (tenant,job)).fetchone()[0],"requested")



    def costed_qa_disposition(self,tenant,actor,job,action,quantity,target=None,
                              *,disposition=None,stock_out=None,stock_in=None,
                              journal=None,period=None):
        disposition=disposition or uuid4()
        stock_out=stock_out or uuid4()
        stock_in=(stock_in or uuid4()) if action=="restock" else None
        journal=(journal or uuid4()) if action=="dispose" else None
        result=self.commerce_execute(
            tenant,actor,
            "SELECT one_resolve_and_value_customer_quarantine("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,'inventory','expense',%s,%s)",
            (tenant,job,disposition,action,quantity,target,stock_out,stock_in,
             period or self.period_id,journal,actor))
        return disposition,stock_out,stock_in,journal,result

    def test_costed_customer_restock_and_disposal_complete_full_qa_and_valuation(self):
        tenant,admin,product,warehouse,sale,source,quarantine,job=(
            self.prepare_costed_customer_return())
        self.post_costed_customer_return(tenant,admin,job,Decimal("2"))
        target=uuid4()
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name) "
            "VALUES(%s,%s,%s,'Restock destination')",
            (tenant,target,"RS-"+target.hex))
        restock,*_=self.costed_qa_disposition(
            tenant,admin,job,"restock",Decimal("1"),target)
        dispose,_,_,expense_journal,_=self.costed_qa_disposition(
            tenant,admin,job,"dispose",Decimal("1"))
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone(),(0,0))
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,target,product)).fetchone(),
            (Decimal("1"),Decimal("12.50")))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,target,product)).fetchone()[0],Decimal("1"))
        self.assertEqual(self.admin.execute(
            "SELECT action,amount FROM one_return_disposition_costs "
            "WHERE tenant_id=%s AND return_job_id=%s ORDER BY action",
            (tenant,job)).fetchall(),[
            ("dispose",Decimal("12.50")),("restock",Decimal("12.50"))])
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines "
            "WHERE tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,expense_journal)).fetchall(),[
            ("expense",Decimal("12.50"),Decimal("0")),
            ("inventory",Decimal("0"),Decimal("12.50"))])
        self.assertEqual(self.read_unified_return_case(tenant,admin,job)[1],
                         "qa_resolved")
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_ledger_audit WHERE tenant_id=%s "
            "AND entry_id=%s",(tenant,expense_journal)).fetchone()[0],1)

    def test_costed_qa_disposal_failure_rolls_back_physical_disposition(self):
        tenant,admin,product,warehouse,sale,source,quarantine,job=(
            self.prepare_costed_customer_return())
        self.post_costed_customer_return(tenant,admin,job,Decimal("2"))
        disposition,out=uuid4(),uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.costed_qa_disposition(
                tenant,admin,job,"dispose",Decimal("1"),
                disposition=disposition,stock_out=out,period=uuid4())
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone()[0],Decimal("2"))
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone(),
            (Decimal("2"),Decimal("25")))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_return_dispositions "
            "WHERE tenant_id=%s AND id=%s",(tenant,disposition)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events "
            "WHERE tenant_id=%s AND id=%s",(tenant,out)).fetchone()[0],0)
        self.costed_qa_disposition(
            tenant,admin,job,"dispose",Decimal("1"),
            disposition=disposition,stock_out=out)
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone(),
            (Decimal("1"),Decimal("12.50")))

    def test_costed_qa_refuses_double_disposal_and_unvalued_legacy_returns(self):
        tenant,admin,product,warehouse,sale,source,quarantine,job=(
            self.prepare_costed_customer_return())
        self.post_costed_customer_return(tenant,admin,job,Decimal("1"))
        self.costed_qa_disposition(tenant,admin,job,"dispose",Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.costed_qa_disposition(tenant,admin,job,"dispose",Decimal("1"))
        self.assertEqual(self.admin.execute(
            "SELECT sum(amount) FROM one_return_disposition_costs WHERE "
            "tenant_id=%s AND return_job_id=%s",
            (tenant,job)).fetchone()[0],Decimal("12.50"))
        legacy=uuid4()
        self.commerce_execute(
            tenant,admin,
            "SELECT one_request_return(%s,%s,%s,1,%s,%s,'Old uncoded return')",
            (tenant,legacy,source,quarantine,admin))
        self.commerce_execute(
            tenant,admin,"SELECT one_finish_return(%s,%s,1,%s,%s)",
            (tenant,legacy,uuid4(),admin))
        with self.assertRaises(self.psycopg.Error):
            self.costed_qa_disposition(
                tenant,admin,legacy,"dispose",Decimal("1"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_return_disposition_costs WHERE "
            "tenant_id=%s AND return_job_id=%s",
            (tenant,legacy)).fetchone()[0],0)



    def test_one_pulse_reports_valued_quarantine_restock_and_disposal(self):
        tenant,admin,product,warehouse,sale,source,quarantine,job=(
            self.prepare_costed_customer_return())
        stock_event,accepted_journal,_=self.post_costed_customer_return(
            tenant,admin,job,Decimal("2"))
        target=uuid4()
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name) "
            "VALUES (%s,%s,%s,'QC restocked value')",
            (tenant,target,"QCVAL-"+target.hex))
        self.costed_qa_disposition(
            tenant,admin,job,"restock",Decimal("1"),target)
        _,_,_,disposal_journal,_=self.costed_qa_disposition(
            tenant,admin,job,"dispose",Decimal("1"))
        for wh,qty,val in (
            (quarantine,Decimal("0"),Decimal("0")),
            (target,Decimal("1"),Decimal("12.50")),
            (warehouse,Decimal("2"),Decimal("25.00"))
        ):
            report=self.inventory_pulse_check(tenant,admin,wh,product)
            self.assertEqual(report[0],qty)
            self.assertEqual(report[3],qty)
            self.assertEqual(report[4],val)
            self.assertEqual(report[5],0)
            self.assertTrue(report[-1])
        event_timeline=self.fetch_return_timeline(tenant,admin,job)
        posted={sid for _,event,sid in event_timeline
                if event=="finance.journal_posted"}
        self.assertIn(accepted_journal,posted)
        self.assertIn(disposal_journal,posted)
        self.assertIn(("stock.customer_returned",stock_event),
                      {(event,sid) for _,event,sid in event_timeline})

    def test_one_pulse_warns_unvalued_manual_return_into_quarantine(self):
        tenant,admin,product,warehouse,sale,source,quarantine,job=(
            self.prepare_costed_customer_return())
        event=uuid4()
        self.commerce_execute(
            tenant,admin,"SELECT one_finish_return(%s,%s,1,%s,%s)",
            (tenant,job,event,admin))
        report=self.inventory_pulse_check(tenant,admin,quarantine,product)
        self.assertEqual(report[0],Decimal("1"))
        self.assertEqual(report[3],Decimal("0"))
        self.assertEqual(report[5],1)
        self.assertFalse(report[-1])



    def request_costed_vendor_return(self,tenant,admin,source,quantity):
        job=uuid4()
        self.commerce_execute(
            tenant,admin,"SELECT one_request_return(%s,%s,%s,%s,NULL,%s,'Vendor RMA')",
            (tenant,job,source,quantity,admin))
        return job

    def complete_costed_vendor_return(self,tenant,admin,job,*,event=None,
                                       journal=None,period=None):
        event=event or uuid4()
        journal=journal or uuid4()
        result=self.commerce_execute(
            tenant,admin,"SELECT one_complete_unbilled_vendor_return("
            "%s,%s,%s,%s,%s,'inventory','grni',%s)",
            (tenant,job,event,journal,period or self.period_id,admin))
        return event,journal,result

    def test_unbilled_vendor_return_restores_grni_and_reduces_stock_value(self):
        tenant,admin,product,warehouse,purchase,sale,source=(
            self.valued_receipt_for_vendor_bill(Decimal("5")))
        job=self.request_costed_vendor_return(
            tenant,admin,source,Decimal("2"))
        event,journal,result=self.complete_costed_vendor_return(
            tenant,admin,job)
        self.assertEqual(result,job)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("3"),Decimal("0")))
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("3"),Decimal("37.50")))
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines "
            "WHERE tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,journal)).fetchall(),[
                ("grni",Decimal("25"),Decimal("0")),
                ("inventory",Decimal("0"),Decimal("25"))
            ])
        self.assertEqual(self.admin.execute(
            "SELECT qty,amount FROM one_vendor_return_cost_links "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,job)).fetchone(),(Decimal("2"),Decimal("25")))
        self.assertEqual(self.admin.execute(
            "SELECT movement_kind,qty,amount FROM one_stock_value_events "
            "WHERE tenant_id=%s AND stock_event_id=%s",
            (tenant,event)).fetchone(),
            ("vendor_return",Decimal("2"),Decimal("25")))
        self.assertTrue(self.inventory_pulse_check(
            tenant,admin,warehouse,product)[-1])

    def test_vendor_bill_cannot_rebill_units_already_returned_to_supplier(self):
        tenant,admin,product,warehouse,purchase,sale,source=(
            self.valued_receipt_for_vendor_bill(Decimal("5")))
        job=self.request_costed_vendor_return(tenant,admin,source,Decimal("2"))
        self.complete_costed_vendor_return(tenant,admin,job)
        over=self.sample_goods_vendor_bill(tenant,Decimal("4"))
        with self.assertRaises(self.psycopg.Error):
            self.post_matched_vendor_goods(tenant,self.actor,purchase,over)
        acceptable=self.sample_goods_vendor_bill(tenant,Decimal("3"))
        self.post_matched_vendor_goods(
            tenant,self.actor,purchase,acceptable)
        self.assertEqual(self.admin.execute(
            "SELECT coalesce(sum(billed_qty),0) FROM one_goods_bill_allocations "
            "WHERE tenant_id=%s AND order_id=%s",
            (tenant,purchase)).fetchone()[0],Decimal("3"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_invoice_docs "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,over.id)).fetchone()[0],0)

    def test_vendor_return_already_billed_part_rejected_and_kept_requested(self):
        tenant,admin,product,warehouse,purchase,sale,source=(
            self.valued_receipt_for_vendor_bill(Decimal("5")))
        bill=self.sample_goods_vendor_bill(tenant,Decimal("4"))
        self.post_matched_vendor_goods(tenant,self.actor,purchase,bill)
        job=self.request_costed_vendor_return(tenant,admin,source,Decimal("2"))
        event,journal=uuid4(),uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.complete_costed_vendor_return(
                tenant,admin,job,event=event,journal=journal)
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_return_jobs WHERE tenant_id=%s "
            "AND id=%s",(tenant,job)).fetchone()[0],"requested")
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("5"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events WHERE tenant_id=%s "
            "AND id=%s",(tenant,event)).fetchone()[0],0)

    def test_vendor_return_wrong_accounting_period_fails_atomically(self):
        tenant,admin,product,warehouse,purchase,sale,source=(
            self.valued_receipt_for_vendor_bill(Decimal("5")))
        job=self.request_costed_vendor_return(tenant,admin,source,Decimal("1"))
        event,journal=uuid4(),uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.complete_costed_vendor_return(
                tenant,admin,job,event=event,journal=journal,period=uuid4())
        self.assertEqual(self.admin.execute(
            "SELECT status,processed_event_id FROM one_return_jobs "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,job)).fetchone(),("requested",None))
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("5"),Decimal("62.50")))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_vendor_return_cost_links WHERE "
            "tenant_id=%s AND return_job_id=%s",
            (tenant,job)).fetchone()[0],0)
        self.complete_costed_vendor_return(
            tenant,admin,job,event=event,journal=journal)
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_return_jobs WHERE tenant_id=%s "
            "AND id=%s",(tenant,job)).fetchone()[0],"completed")

    def test_vendor_return_mixed_cost_cannot_guess_po_source_revaluation(self):
        tenant,admin,product,warehouse,purchase,sale,source=(
            self.valued_receipt_for_vendor_bill(Decimal("2")))
        other=uuid4()
        self.admin.execute(
            "INSERT INTO one_commercial_orders "
            "(tenant_id,id,kind,number,partner_id,currency) "
            "VALUES(%s,%s,'purchase',%s,'TEST-PARTNER','SAR')",
            (tenant,other,"PO-MIX-"+other.hex))
        self.admin.execute(
            "INSERT INTO one_commercial_order_lines "
            "(tenant_id,order_id,line_no,product_id,warehouse_id,"
            "ordered_qty,unit_price) VALUES(%s,%s,1,%s,%s,3,20)",
            (tenant,other,product,warehouse))
        self.commerce_execute(
            tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,other,admin))
        self.atomic_stock_post(tenant,admin,other,Decimal("3"),"purchase")
        job=self.request_costed_vendor_return(tenant,admin,source,Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.complete_costed_vendor_return(tenant,admin,job)
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("5"),Decimal("85")))



    def test_grni_report_nets_costed_supplier_returns_and_matched_bills(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill(Decimal("5")))
        job=self.request_costed_vendor_return(
            tenant,admin,receipt,Decimal("2"))
        returned,journal,_=self.complete_costed_vendor_return(
            tenant,admin,job)
        pending=self.inventory_pulse_check(tenant,admin,warehouse,product)
        self.assertEqual(pending[0:6],(
            Decimal("3"),Decimal("0"),Decimal("3"),Decimal("3"),
            Decimal("37.50"),0
        ))
        self.assertEqual(pending[6:9],(
            Decimal("62.50"),Decimal("0"),Decimal("37.50")
        ))
        self.assertTrue(pending[-1])
        bill=self.sample_goods_vendor_bill(tenant,Decimal("3"))
        self.post_matched_vendor_goods(
            tenant,self.actor,purchase,bill)
        posted=self.inventory_pulse_check(tenant,admin,warehouse,product)
        self.assertEqual(posted[6:9],(
            Decimal("62.50"),Decimal("37.50"),Decimal("0")
        ))
        self.assertTrue(posted[-1])
        trace=self.fetch_return_timeline(tenant,admin,job)
        self.assertIn(
            ("finance.journal_posted",journal),
            {(event,source) for _,event,source in trace}
        )
        self.assertIn(
            ("stock.vendor_returned",returned),
            {(event,source) for _,event,source in trace}
        )

    def test_vendor_return_legacy_unvalued_is_flagged_as_reconciliation_error(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill(Decimal("5")))
        job=self.request_costed_vendor_return(
            tenant,admin,receipt,Decimal("1"))
        self.commerce_execute(
            tenant,admin,"SELECT one_finish_return(%s,%s,1,%s,%s)",
            (tenant,job,uuid4(),admin))
        report=self.inventory_pulse_check(tenant,admin,warehouse,product)
        self.assertEqual(report[0],Decimal("4"))
        self.assertEqual(report[3],Decimal("5"))
        self.assertEqual(report[5],1)
        self.assertFalse(report[-1])



    def test_concurrent_supplier_bill_and_unbilled_return_cannot_claim_same_units(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,admin,product,warehouse,purchase,sale,source=(
            self.valued_receipt_for_vendor_bill(Decimal("5")))
        job=self.request_costed_vendor_return(
            tenant,admin,source,Decimal("3"))
        bill=self.sample_goods_vendor_bill(tenant,Decimal("3"))
        gate=Barrier(2)
        def try_vendor_return():
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
                                     (str(admin),))
                        gate.wait(timeout=10)
                        conn.execute(
                            "SELECT one_complete_unbilled_vendor_return("
                            "%s,%s,%s,%s,%s,'inventory','grni',%s)",
                            (tenant,job,uuid4(),uuid4(),self.period_id,admin))
                    return True
                except self.psycopg.Error:
                    return False
        def try_vendor_bill():
            with self.psycopg.connect(
                host=os.environ.get("ONE_TEST_PG_HOST","localhost"),
                port=int(os.environ.get("ONE_TEST_PG_PORT","5432")),
                dbname=os.environ.get("ONE_TEST_PG_DB","one_erp_test"),
                user="one_erp_runtime_ci",password="ci_only_password",
                autocommit=True
            ) as conn:
                try:
                    gate.wait(timeout=10)
                    persist_valued_goods_vendor_bill(
                        conn,order_id=purchase,order_line_numbers=(1,),
                        grni_account_id="grni",document=bill,
                        accounts=self.invoice_accounts,chart=self.chart,
                        period=self.period,period_id=self.period_id,
                        actor_id=self.actor,memberships=self.memberships,
                    )
                    return True
                except self.psycopg.Error:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(try_vendor_return)
            two=pool.submit(try_vendor_bill)
            outcomes=(one.result(timeout=35),two.result(timeout=35))
        self.assertEqual(sorted(outcomes),[False,True])
        return_qty=self.admin.execute(
            "SELECT coalesce(sum(quantity),0) FROM one_stock_events "
            "WHERE tenant_id=%s AND order_id=%s AND "
            "movement_kind='vendor_return'",
            (tenant,purchase)).fetchone()[0]
        bill_qty=self.admin.execute(
            "SELECT coalesce(sum(billed_qty),0) FROM one_goods_bill_allocations "
            "WHERE tenant_id=%s AND order_id=%s",
            (tenant,purchase)).fetchone()[0]
        self.assertLessEqual(return_qty+bill_qty,Decimal("5"))
        self.assertEqual(return_qty+bill_qty,Decimal("3"))


    # 024 | Source-linked internal vendor credit on ALREADY-BILLED purchased
    # goods. None of these test fixtures may be run on customer databases.
    def billed_vendor_return_setup(self,*,received=Decimal("5"),
                                    billed=Decimal("5"),returned=Decimal("2")):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill(received))
        bill=self.sample_goods_vendor_bill(tenant,billed)
        self.post_matched_vendor_goods(tenant,self.actor,purchase,bill)
        job=self.request_costed_vendor_return(tenant,admin,receipt,returned)
        return tenant,admin,product,warehouse,purchase,receipt,bill,job

    def complete_billed_vendor_return(self,tenant,admin,job,bill,*,
                                     credit=None,event=None,period=None,
                                     account="inventory",ap="ap",
                                     number=None,day=date(2026,10,15)):
        credit=credit or uuid4()
        event=event or uuid4()
        number=number or ("VC-"+credit.hex)
        result=self.commerce_execute(
            tenant,admin,"SELECT one_complete_billed_vendor_return("
            "%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant,job,bill.id,credit,number,event,
             period or self.period_id,account,ap,admin,day))
        return credit,event,result

    def test_billed_vendor_return_atomic_ap_credit_and_rma_timeline(self):
        tenant,admin,product,warehouse,purchase,receipt,bill,job=(
            self.billed_vendor_return_setup())
        credit,event,result=self.complete_billed_vendor_return(
            tenant,admin,job,bill)
        self.assertEqual(result,credit)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("3"),Decimal("0")))
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("3"),Decimal("37.50")))
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines WHERE "
            "tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,credit)).fetchall(),[
                ("ap",Decimal("25"),Decimal("0")),
                ("inventory",Decimal("0"),Decimal("25"))])
        self.assertEqual(self.admin.execute(
            "SELECT kind,status,source_invoice_id,net_total,tax_total "
            "FROM one_invoice_docs WHERE tenant_id=%s AND id=%s",
            (tenant,credit)).fetchone(),
            ("vendor_credit","issued",bill.id,Decimal("25"),Decimal("0")))
        self.assertEqual(self.admin.execute(
            "SELECT quantity,amount FROM one_billed_vendor_return_credit_links "
            "WHERE tenant_id=%s AND credit_invoice_id=%s",
            (tenant,credit)).fetchone(),
            (Decimal("2"),Decimal("25")))
        self.assertEqual(self.admin.execute(
            "SELECT movement_kind,qty,amount FROM one_stock_value_events "
            "WHERE tenant_id=%s AND stock_event_id=%s",
            (tenant,event)).fetchone(),
            ("vendor_return",Decimal("2"),Decimal("25")))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_invoice_audit WHERE tenant_id=%s AND "
            "doc_id=%s",(tenant,credit)).fetchone()[0],1)
        pulse=self.inventory_pulse_check(tenant,admin,warehouse,product)
        self.assertEqual(pulse[6],Decimal("62.50"))
        self.assertEqual(pulse[7],Decimal("62.50"))
        self.assertEqual(pulse[8],Decimal("0"))
        self.assertTrue(pulse[-1])
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(admin),))
            events=self.db.execute(
                "SELECT event_type,source_id FROM "
                "one_return_case_timeline(%s,%s,%s)",
                (tenant,job,admin)).fetchall()
        self.assertIn(("finance.invoice_issued",credit),events)
        self.assertIn(("finance.journal_posted",credit),events)

    def test_billed_return_does_not_falsely_reverse_outstanding_grni(self):
        tenant,admin,product,warehouse,purchase,receipt,bill,job=(
            self.billed_vendor_return_setup(billed=Decimal("4")))
        self.complete_billed_vendor_return(tenant,admin,job,bill)
        pulse=self.inventory_pulse_check(tenant,admin,warehouse,product)
        self.assertEqual(pulse[6],Decimal("62.50"))
        self.assertEqual(pulse[7],Decimal("50.00"))
        self.assertEqual(pulse[8],Decimal("12.50"))
        self.assertTrue(pulse[-1])

    def test_billed_return_invalid_period_undoes_all_physical_and_invoice_rows(self):
        tenant,admin,product,warehouse,purchase,receipt,bill,job=(
            self.billed_vendor_return_setup())
        credit,event=uuid4(),uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.complete_billed_vendor_return(
                tenant,admin,job,bill,credit=credit,event=event,period=uuid4())
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_return_jobs WHERE tenant_id=%s AND id=%s",
            (tenant,job)).fetchone()[0],"requested")
        for table,col in (
            ("one_invoice_docs","id"),("one_journal_entries","id"),
            ("one_stock_events","id")
        ):
            source_id=event if table=="one_stock_events" else credit
            self.assertEqual(self.admin.execute(
                f"SELECT count(*) FROM {table} WHERE tenant_id=%s AND {col}=%s",
                (tenant,source_id)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("5"))
        self.complete_billed_vendor_return(
            tenant,admin,job,bill,credit=credit,event=event)

    def test_billed_vendor_return_is_at_most_once_despite_retry(self):
        tenant,admin,product,warehouse,purchase,receipt,bill,job=(
            self.billed_vendor_return_setup())
        self.complete_billed_vendor_return(tenant,admin,job,bill)
        with self.assertRaises(self.psycopg.Error):
            self.complete_billed_vendor_return(tenant,admin,job,bill)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_billed_vendor_return_credit_links "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,job)).fetchone()[0],1)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("3"))

    def test_billed_vendor_return_larger_than_issued_bill_is_rejected(self):
        tenant,admin,product,warehouse,purchase,receipt,bill,job=(
            self.billed_vendor_return_setup(
                billed=Decimal("1"),returned=Decimal("2")))
        with self.assertRaises(self.psycopg.Error):
            self.complete_billed_vendor_return(tenant,admin,job,bill)
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_return_jobs WHERE tenant_id=%s AND id=%s",
            (tenant,job)).fetchone()[0],"requested")

    def test_billed_vendor_return_cross_company_or_wrong_account_refused(self):
        tenant,admin,product,warehouse,purchase,receipt,bill,job=(
            self.billed_vendor_return_setup())
        with self.assertRaises(self.psycopg.Error):
            self.complete_billed_vendor_return(
                self.tenant_b,admin,job,bill)
        with self.assertRaises(self.psycopg.Error):
            self.complete_billed_vendor_return(
                tenant,admin,job,bill,account="expense")
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_return_jobs WHERE tenant_id=%s AND id=%s",
            (tenant,job)).fetchone()[0],"requested")

    def test_billed_vendor_return_rejects_unmatched_second_receipt(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill(Decimal("2")))
        self.atomic_stock_post(tenant,admin,purchase,Decimal("3"),"purchase")
        bill=self.sample_goods_vendor_bill(tenant,Decimal("5"))
        self.post_matched_vendor_goods(tenant,self.actor,purchase,bill)
        job=self.request_costed_vendor_return(
            tenant,admin,receipt,Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.complete_billed_vendor_return(tenant,admin,job,bill)

    def test_billed_rma_rejects_credit_if_other_vendor_credit_consumed_bill_ceiling(self):
        tenant,admin,product,warehouse,purchase,receipt,bill,job=(
            self.billed_vendor_return_setup())
        # Simulates an earlier separately issued supplier price adjustment.
        # The cash/AP credit has already used 50 of the original 62.50 bill.
        prior=InvoiceDocument(
            uuid4(),tenant,"VCPREV-"+uuid4().hex,"TEST-PARTNER",
            DocumentType.VENDOR_CREDIT,date(2026,10,15),date(2026,10,15),
            "SAR",(InvoiceLine("Prior supplier adjustment","expense",
                Decimal("1"),Decimal("50.00")),),
            source_invoice_id=bill.id)
        self.post_invoice(prior)
        with self.assertRaises(self.psycopg.Error):
            self.complete_billed_vendor_return(tenant,admin,job,bill)
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_return_jobs WHERE tenant_id=%s AND id=%s",
            (tenant,job)).fetchone()[0],"requested")
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("5"))

    def test_concurrent_billed_vendor_returns_cannot_overcredit_same_bill(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,admin,product,warehouse,purchase,receipt,bill,first=(
            self.billed_vendor_return_setup(billed=Decimal("3")))
        second=self.request_costed_vendor_return(
            tenant,admin,receipt,Decimal("2"))
        gate=Barrier(2)
        def attempt(job):
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
                                     (str(admin),))
                        gate.wait(timeout=10)
                        cid=uuid4()
                        conn.execute(
                            "SELECT one_complete_billed_vendor_return("
                            "%s,%s,%s,1,%s,%s,%s,%s,'inventory','ap',%s,%s)",
                            (tenant,job,bill.id,cid,"VC-"+cid.hex,
                             uuid4(),self.period_id,admin,date(2026,10,15)))
                    return True
                except self.psycopg.Error:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(attempt,first)
            two=pool.submit(attempt,second)
            outcomes=(one.result(timeout=35),two.result(timeout=35))
        self.assertEqual(sorted(outcomes),[False,True])
        self.assertEqual(self.admin.execute(
            "SELECT coalesce(sum(quantity),0) FROM "
            "one_billed_vendor_return_credit_links "
            "WHERE tenant_id=%s AND source_bill_id=%s",
            (tenant,bill.id)).fetchone()[0],Decimal("2"))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("3"))

    # 025 | IAS 2 separate contra-asset position and immutable approved event.
    def nrv_post(self,tenant,admin,warehouse,product,*,revision=0,
                 cost=Decimal("62.50"),qty=Decimal("5"),
                 selling=Decimal("10"),completion=Decimal("0"),
                 selling_cost=Decimal("1"),event=None,journal=None,
                 period=None,allowance="nrvallow",expense="nrvexpense",
                 evidence=None):
        event=event or uuid4()
        journal=journal or uuid4()
        evidence=evidence or ("IAS2-"+event.hex)
        return event,journal,self.commerce_execute(
            tenant,admin,
            "SELECT one_post_inventory_nrv_adjustment("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant,warehouse,product,event,journal,
             period or self.period_id,allowance,expense,admin,
             revision,qty,cost,selling,completion,selling_cost,
             evidence,date(2026,10,15)))

    def nrv_report(self,tenant,admin,warehouse,product):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(admin),))
            return self.db.execute(
                "SELECT * FROM one_inventory_nrv_reconciliation(%s,%s,%s,%s)",
                (tenant,warehouse,product,admin)).fetchone()

    def test_nrv_posting_reconciles_inventory_contra_asset_and_ifrs_net_value(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        event,journal,result=self.nrv_post(tenant,admin,warehouse,product)
        self.assertEqual(event,result)
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines "
            "WHERE tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,journal)).fetchall(),[
                ("nrvexpense",Decimal("17.50"),Decimal("0")),
                ("nrvallow",Decimal("0"),Decimal("17.50"))
            ])
        self.assertEqual(self.nrv_report(tenant,admin,warehouse,product),(
            Decimal("5"),Decimal("5"),Decimal("62.50"),
            Decimal("17.50"),Decimal("45.00"),
            Decimal("17.50"),True))
        self.assertEqual(self.admin.execute(
            "SELECT adjustment,new_allowance,net_value_after "
            "FROM one_inventory_nrv_events WHERE tenant_id=%s AND id=%s",
            (tenant,event)).fetchone(),
            (Decimal("17.50"),Decimal("17.50"),Decimal("45.00")))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_operation_outbox WHERE tenant_id=%s "
            "AND event_type='finance.inventory_nrv_adjusted' AND source_id=%s",
            (tenant,event)).fetchone()[0],1)
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("5"),Decimal("62.50")))

    def test_nrv_recovery_reverses_prior_loss_but_never_above_cost(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        self.nrv_post(tenant,admin,warehouse,product)
        rev_event,rev_journal,_=self.nrv_post(
            tenant,admin,warehouse,product,revision=1,selling=Decimal("20"),
            selling_cost=Decimal("0"))
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines "
            "WHERE tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,rev_journal)).fetchall(),[
               ("nrvallow",Decimal("17.50"),Decimal("0")),
               ("nrvexpense",Decimal("0"),Decimal("17.50"))])
        self.assertEqual(self.nrv_report(tenant,admin,warehouse,product),(
            Decimal("5"),Decimal("5"),Decimal("62.50"),
            Decimal("0"),Decimal("62.50"),Decimal("0"),True))
        self.commerce_execute(tenant,admin,
            "SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,sale,admin))
        self.atomic_stock_post(tenant,admin,sale,Decimal("1"),"sale")
        self.assertEqual(self.nrv_report(tenant,admin,warehouse,product)[:3],(
             Decimal("4"),Decimal("4"),Decimal("50.00")))

    def test_nrv_active_allowance_blocks_costed_sales_and_additional_receipts(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill(Decimal("2")))
        self.nrv_post(tenant,admin,warehouse,product,
                      cost=Decimal("25"),qty=Decimal("2"))
        self.commerce_execute(tenant,admin,
             "SELECT one_confirm_commercial_order(%s,%s,%s)",
             (tenant,sale,admin))
        for kind,order in (("sale",sale),("purchase",purchase)):
            with self.assertRaises(self.psycopg.Error):
                self.atomic_stock_post(tenant,admin,order,Decimal("1"),kind)
        self.assertEqual(self.admin.execute(
            "SELECT qty,book_value FROM one_stock_value_positions WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("2"),Decimal("25")))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("2"))

    def test_nrv_missing_period_cannot_create_persistent_position_or_gl(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        event,journal=uuid4(),uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.nrv_post(tenant,admin,warehouse,product,
                event=event,journal=journal,period=uuid4())
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_inventory_nrv_positions WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],0)
        for table,col in (
           ("one_inventory_nrv_events","id"),
           ("one_journal_entries","id"),
        ):
            token=journal if table=="one_journal_entries" else event
            self.assertEqual(self.admin.execute(
                f"SELECT count(*) FROM {table} WHERE tenant_id=%s AND {col}=%s",
                (tenant,token)).fetchone()[0],0)
        self.nrv_post(tenant,admin,warehouse,product,
                      event=event,journal=journal)

    def test_nrv_stale_revision_and_duplicate_evidence_rejected(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        event,journal,_=self.nrv_post(tenant,admin,warehouse,product)
        with self.assertRaises(self.psycopg.Error):
            self.nrv_post(tenant,admin,warehouse,product,revision=0,
                          selling=Decimal("9"))
        with self.assertRaises(self.psycopg.Error):
            self.nrv_post(tenant,admin,warehouse,product,revision=1,
                          selling=Decimal("9"),
                          evidence="IAS2-"+event.hex)
        self.assertEqual(self.admin.execute(
            "SELECT revision,allowance FROM one_inventory_nrv_positions "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (1,Decimal("17.50")))

    def test_nrv_inactive_wrong_account_wrong_company_and_negative_nrv_fail_closed(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        for overrides in (
            {"allowance":"expense"},
            {"expense":"inventory"},
            {"selling":Decimal("0"),"selling_cost":Decimal("2")},
            {"qty":Decimal("7")},
        ):
            with self.assertRaises(self.psycopg.Error):
                self.nrv_post(tenant,admin,warehouse,product,**overrides)
        with self.assertRaises(self.psycopg.Error):
            self.nrv_post(self.tenant_b,admin,warehouse,product)
        with self.assertRaises(self.psycopg.Error):
            self.nrv_post(tenant,self.actor,warehouse,product)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_inventory_nrv_events WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],0)

    def test_nrv_direct_mutation_and_duplicate_reversal_are_forbidden(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        self.nrv_post(tenant,admin,warehouse,product)
        for statement in (
            "UPDATE one_inventory_nrv_positions SET allowance=0 "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            "DELETE FROM one_inventory_nrv_events WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
        ):
            with self.assertRaises(self.psycopg.Error):
                self.commerce_execute(
                    tenant,admin,statement+" RETURNING product_id",
                    (tenant,warehouse,product))
        self.nrv_post(tenant,admin,warehouse,product,
                      revision=1,selling=Decimal("50"),
                      selling_cost=Decimal("0"))
        with self.assertRaises(self.psycopg.Error):
            self.nrv_post(tenant,admin,warehouse,product,
                          revision=2,selling=Decimal("50"),
                          selling_cost=Decimal("0"))

    def test_nrv_partial_reversal_and_source_audit_history(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        self.nrv_post(tenant,admin,warehouse,product)
        event,journal,_=self.nrv_post(
            tenant,admin,warehouse,product,revision=1,
            selling=Decimal("12"),selling_cost=Decimal("1"))
        row=self.nrv_report(tenant,admin,warehouse,product)
        self.assertEqual(row[3],Decimal("7.50"))
        self.assertEqual(row[4],Decimal("55.00"))
        self.assertEqual(row[5],Decimal("7.50"))
        self.assertTrue(row[6])
        self.assertEqual(self.admin.execute(
            "SELECT adjustment FROM one_inventory_nrv_events "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,event)).fetchone()[0],Decimal("-10"))

    # 026 | Auditable company valuation schedule and period NRV movements.
    def inventory_nrv_company(self,tenant,actor):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_inventory_nrv_company_snapshot(%s,%s)",
                (tenant,actor)).fetchone()

    def inventory_nrv_activity(self,tenant,actor,start=date(2026,1,1),
                               end=date(2027,1,1)):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_inventory_nrv_period_activity(%s,%s,%s,%s)",
                (tenant,actor,start,end)).fetchone()

    def test_nrv_company_reconciliation_uses_net_ias2_value(self):
        tenant=self.tenant_a
        before=self.inventory_nrv_company(tenant,self.actor)
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        self.nrv_post(tenant,admin,warehouse,product)
        after=self.inventory_nrv_company(tenant,self.actor)
        self.assertEqual(after[0]-before[0],1)
        self.assertEqual(after[1]-before[1],Decimal("5"))
        self.assertEqual(after[2]-before[2],Decimal("5"))
        self.assertEqual(after[3]-before[3],Decimal("62.50"))
        self.assertEqual(after[4]-before[4],Decimal("17.50"))
        self.assertEqual(after[5]-before[5],Decimal("45.00"))
        self.assertEqual(after[6]-before[6],Decimal("17.50"))
        self.assertEqual(after[7]-before[7],0)

    def test_nrv_period_report_recognizes_expense_and_reversal_once(self):
        before=self.inventory_nrv_activity(self.tenant_a,self.actor)
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        self.nrv_post(tenant,admin,warehouse,product)
        self.nrv_post(tenant,admin,warehouse,product,revision=1,
                      selling=Decimal("12"),selling_cost=Decimal("1"))
        after=self.inventory_nrv_activity(tenant,self.actor)
        self.assertEqual(after[0]-before[0],2)
        self.assertEqual(after[1]-before[1],Decimal("17.50"))
        self.assertEqual(after[2]-before[2],Decimal("10.00"))
        self.assertEqual(after[3]-before[3],Decimal("7.50"))
        self.assertEqual(after[4],date(2026,10,15))
        day=self.inventory_nrv_activity(
            tenant,self.actor,start=date(2026,10,16),end=date(2026,11,1))
        self.assertEqual(day[:4],(0,Decimal("0"),Decimal("0"),Decimal("0")))

    def test_nrv_company_reader_refuses_wrong_actor_tenant_and_period(self):
        tenant=self.tenant_a
        for statement,values in (
            ("SELECT * FROM one_inventory_nrv_company_snapshot(%s,%s)",
             (tenant,uuid4())),
            ("SELECT * FROM one_inventory_nrv_period_activity(%s,%s,%s,%s)",
             (tenant,self.actor,date(2027,1,1),date(2026,1,1))),
            ("SELECT * FROM one_inventory_nrv_company_snapshot(%s,%s)",
             (self.tenant_b,self.actor)),
        ):
            with self.assertRaises(self.psycopg.Error):
                self.commerce_execute(tenant,self.actor,statement,values)

    # 027 | Separate gross COGS and NRV contra-asset release in one sale.
    def sell_nrv_adjusted(self,tenant,admin,sale,quantity,*,event=None,
                          gross_journal=None,release_journal=None,period=None):
        event=event or uuid4()
        gross_journal=gross_journal or uuid4()
        release_journal=release_journal or uuid4()
        return event,gross_journal,release_journal,self.commerce_execute(
            tenant,admin,"SELECT one_ship_and_allocate_nrv("
            "%s,%s,1,%s,%s,%s,%s,%s,'inventory','cogs','nrvallow',%s)",
            (tenant,sale,quantity,event,gross_journal,release_journal,
             period or self.period_id,admin))

    def test_nrv_partial_sale_releases_allowance_and_reports_net_cogs(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        self.nrv_post(tenant,admin,warehouse,product)
        self.commerce_execute(tenant,admin,
             "SELECT one_confirm_commercial_order(%s,%s,%s)",
             (tenant,sale,admin))
        event,gross,release,res=self.sell_nrv_adjusted(
            tenant,admin,sale,Decimal("2"))
        self.assertEqual(res,event)
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines WHERE "
            "tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,gross)).fetchall(),[
            ("cogs",Decimal("25"),Decimal("0")),
            ("inventory",Decimal("0"),Decimal("25"))])
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines WHERE "
            "tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,release)).fetchall(),[
            ("nrvallow",Decimal("7"),Decimal("0")),
            ("cogs",Decimal("0"),Decimal("7"))])
        self.assertEqual(self.admin.execute(
            "SELECT quantity,gross_cogs,released_allowance,net_cogs,allowance_after "
            "FROM one_inventory_nrv_sale_releases WHERE tenant_id=%s "
            "AND stock_event_id=%s",
            (tenant,event)).fetchone(),
            (Decimal("2"),Decimal("25"),Decimal("7"),
             Decimal("18"),Decimal("10.50")))
        self.assertEqual(self.nrv_report(tenant,admin,warehouse,product),(
            Decimal("3"),Decimal("3"),Decimal("37.50"),
            Decimal("10.50"),Decimal("27"),Decimal("10.50"),True))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_operation_outbox WHERE tenant_id=%s "
            "AND event_type='finance.inventory_nrv_released' AND source_id=%s",
            (tenant,event)).fetchone()[0],1)

    def test_nrv_full_sale_exhausts_gross_cost_and_allowance(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        self.nrv_post(tenant,admin,warehouse,product)
        self.commerce_execute(tenant,admin,
             "SELECT one_confirm_commercial_order(%s,%s,%s)",
             (tenant,sale,admin))
        event,gross,release,result=self.sell_nrv_adjusted(
            tenant,admin,sale,Decimal("5"))
        self.assertEqual(result,event)
        self.assertEqual(self.admin.execute(
            "SELECT cost_qty,gross_cost,allowance FROM one_inventory_nrv_positions "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("0"),Decimal("0"),Decimal("0")))
        self.assertEqual(self.nrv_report(tenant,admin,warehouse,product),(
            Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),
            Decimal("0"),Decimal("0"),True))
        self.assertEqual(self.admin.execute(
            "SELECT net_cogs FROM one_inventory_nrv_sale_releases "
            "WHERE tenant_id=%s AND stock_event_id=%s",
            (tenant,event)).fetchone()[0],Decimal("45"))

    def test_nrv_sale_invalid_period_rolls_back_both_journals_and_stock(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        self.nrv_post(tenant,admin,warehouse,product)
        self.commerce_execute(tenant,admin,
            "SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,sale,admin))
        event,gross,release=uuid4(),uuid4(),uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.sell_nrv_adjusted(
                tenant,admin,sale,Decimal("2"),event=event,
                gross_journal=gross,release_journal=release,
                period=uuid4())
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events WHERE tenant_id=%s AND id=%s",
            (tenant,event)).fetchone()[0],0)
        for j in (gross,release):
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM one_journal_entries WHERE tenant_id=%s AND id=%s",
                (tenant,j)).fetchone()[0],0)
        self.assertEqual(self.nrv_report(tenant,admin,warehouse,product),(
            Decimal("5"),Decimal("5"),Decimal("62.50"),
            Decimal("17.50"),Decimal("45"),Decimal("17.50"),True))
        self.sell_nrv_adjusted(
            tenant,admin,sale,Decimal("2"),event=event,
            gross_journal=gross,release_journal=release)

    def test_nrv_sale_retries_or_unsupported_quantity_cannot_double_release(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        self.nrv_post(tenant,admin,warehouse,product)
        self.commerce_execute(tenant,admin,
            "SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,sale,admin))
        event,gross,release,_=self.sell_nrv_adjusted(
            tenant,admin,sale,Decimal("2"))
        with self.assertRaises(self.psycopg.Error):
            self.sell_nrv_adjusted(
                tenant,admin,sale,Decimal("2"),event=event,
                gross_journal=gross,release_journal=release)
        with self.assertRaises(self.psycopg.Error):
            self.sell_nrv_adjusted(tenant,admin,sale,Decimal("4"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_inventory_nrv_sale_releases WHERE "
            "tenant_id=%s AND stock_event_id=%s",
            (tenant,event)).fetchone()[0],1)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("3"))

    # 028 | NRV adjusted dispatch for reserved orders and packed delivery jobs.
    def prepare_nrv_reserved_sale(self, reserve=Decimal("3")):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        self.nrv_post(tenant,admin,warehouse,product)
        self.commerce_execute(
            tenant,admin,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,sale,admin))
        self.commerce_execute(
            tenant,admin,"SELECT one_reserve_sale_line(%s,%s,1,%s,%s,%s)",
            (tenant,sale,reserve,uuid4(),admin))
        return tenant,admin,product,warehouse,sale

    def dispatch_nrv_reserved(self,tenant,admin,sale,quantity, *,
                              job=None,period=None,stock=None,gross=None,
                              released=None,inv="inventory",cost="cogs",
                              allowance="nrvallow"):
        stock=stock or uuid4()
        gross=gross or uuid4()
        released=released or uuid4()
        result=self.commerce_execute(
            tenant,admin,"SELECT one_dispatch_reserved_or_delivery_with_nrv("
            "%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant,sale,job,quantity,stock,gross,released,
             period or self.period_id,inv,cost,allowance,admin))
        return stock,gross,released,result

    def test_reserved_ias2_dispatch_adjusts_net_cogs_without_using_other_reservations(self):
        tenant,admin,product,warehouse,sale=self.prepare_nrv_reserved_sale()
        stock,gross,released,result=self.dispatch_nrv_reserved(
            tenant,admin,sale,Decimal("2"))
        self.assertEqual(result,stock)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("3"),Decimal("1")))
        self.assertEqual(self.admin.execute(
            "SELECT gross_cogs,released_allowance,net_cogs,allowance_after "
            "FROM one_inventory_nrv_sale_releases WHERE tenant_id=%s "
            "AND stock_event_id=%s",(tenant,stock)).fetchone(),
            (Decimal("25"),Decimal("7"),Decimal("18"),Decimal("10.50")))
        self.assertEqual(self.nrv_report(tenant,admin,warehouse,product),(
            Decimal("3"),Decimal("3"),Decimal("37.50"),
            Decimal("10.50"),Decimal("27"),Decimal("10.50"),True))
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines WHERE "
            "tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,released)).fetchall(),[
            ("nrvallow",Decimal("7"),Decimal("0")),
            ("cogs",Decimal("0"),Decimal("7"))])

    def test_ias2_packed_delivery_atomic_finance_then_customer_proof(self):
        tenant,admin,product,warehouse,sale=self.prepare_nrv_reserved_sale()
        job=uuid4()
        self.commerce_execute(
            tenant,admin,"SELECT one_create_delivery(%s,%s,%s,1,3,%s)",
            (tenant,job,sale,admin))
        for stage in ("picked","packed"):
            self.delivery_stage(tenant,admin,job,stage)
        stock,gross,released,result=self.dispatch_nrv_reserved(
            tenant,admin,sale,Decimal("3"),job=job)
        self.assertEqual(result,job)
        self.assertEqual(self.admin.execute(
            "SELECT status,dispatch_event_id FROM one_delivery_jobs "
            "WHERE tenant_id=%s AND id=%s",(tenant,job)).fetchone(),
            ("dispatched",stock))
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines WHERE "
            "tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,released)).fetchall(),[
            ("nrvallow",Decimal("10.50"),Decimal("0")),
            ("cogs",Decimal("0"),Decimal("10.50"))])
        self.assertEqual(self.nrv_report(tenant,admin,warehouse,product),(
            Decimal("2"),Decimal("2"),Decimal("25"),
            Decimal("7"),Decimal("18"),Decimal("7"),True))
        self.assertEqual(self.delivery_stage(
            tenant,admin,job,"delivered",proof="CONFIRMED-TEST-POD"),job)
        self.assertEqual(self.admin.execute(
            "SELECT event FROM one_fulfillment_audit WHERE tenant_id=%s "
            "AND delivery_id=%s ORDER BY happened_at,event",
            (tenant,job)).fetchall(),[
             ("created",),("picked",),("packed",),("dispatched",),("delivered",)])

    def test_ias2_packed_delivery_invalid_period_rolls_back_every_stage(self):
        tenant,admin,product,warehouse,sale=self.prepare_nrv_reserved_sale()
        job=uuid4()
        self.commerce_execute(tenant,admin,
            "SELECT one_create_delivery(%s,%s,%s,1,3,%s)",
            (tenant,job,sale,admin))
        for step in ("picked","packed"):
            self.delivery_stage(tenant,admin,job,step)
        stock,gross,released=uuid4(),uuid4(),uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.dispatch_nrv_reserved(
                tenant,admin,sale,Decimal("3"),job=job,
                period=uuid4(),stock=stock,gross=gross,released=released)
        self.assertEqual(self.admin.execute(
            "SELECT status,dispatch_event_id FROM one_delivery_jobs "
            "WHERE tenant_id=%s AND id=%s",(tenant,job)).fetchone(),
            ("packed",None))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events WHERE tenant_id=%s AND id=%s",
            (tenant,stock)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_inventory_nrv_sale_releases "
            "WHERE tenant_id=%s AND stock_event_id=%s",
            (tenant,stock)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand,reserved FROM one_stock_balances WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone(),
            (Decimal("5"),Decimal("3")))
        self.dispatch_nrv_reserved(
            tenant,admin,sale,Decimal("3"),job=job,
            stock=stock,gross=gross,released=released)

    def test_ias2_reserved_route_must_not_bypass_existing_delivery_job(self):
        tenant,admin,product,warehouse,sale=self.prepare_nrv_reserved_sale()
        job=uuid4()
        self.commerce_execute(tenant,admin,
             "SELECT one_create_delivery(%s,%s,%s,1,2,%s)",
             (tenant,job,sale,admin))
        with self.assertRaises(self.psycopg.Error):
            self.dispatch_nrv_reserved(tenant,admin,sale,Decimal("2"))
        with self.assertRaises(self.psycopg.Error):
            self.dispatch_nrv_reserved(tenant,admin,sale,Decimal("2"),job=job)
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_delivery_jobs WHERE tenant_id=%s AND id=%s",
            (tenant,job)).fetchone()[0],"draft")
        self.delivery_stage(tenant,admin,job,"picked")
        self.delivery_stage(tenant,admin,job,"packed")
        self.dispatch_nrv_reserved(tenant,admin,sale,Decimal("2"),job=job)

    def test_ias2_reserved_protects_against_repeat_cross_tenant_and_wrong_accounts(self):
        tenant,admin,product,warehouse,sale=self.prepare_nrv_reserved_sale()
        for tenant_arg,who,account in (
            (self.tenant_b,admin,"nrvallow"),
            (tenant,self.actor,"nrvallow"),
            (tenant,admin,"inventory"),
        ):
            with self.assertRaises(self.psycopg.Error):
                self.dispatch_nrv_reserved(
                    tenant_arg,who,sale,Decimal("1"),allowance=account)
        stock,gross,released,_=self.dispatch_nrv_reserved(
            tenant,admin,sale,Decimal("2"))
        with self.assertRaises(self.psycopg.Error):
            self.dispatch_nrv_reserved(
                tenant,admin,sale,Decimal("2"),
                stock=stock,gross=gross,released=released)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_inventory_nrv_sale_releases "
            "WHERE tenant_id=%s AND stock_event_id=%s",
            (tenant,stock)).fetchone()[0],1)
        self.assertEqual(self.nrv_report(tenant,admin,warehouse,product)[3],
                         Decimal("10.50"))

    # 029 | IAS 2 NRV sale cost audit and source-aware customer-return gate.
    def nrv_sales_activity(self,tenant,actor,*,start=date(2026,1,1),
                           end=date(2027,1,1)):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_nrv_adjusted_sales_activity(%s,%s,%s,%s)",
                (tenant,actor,start,end)).fetchone()

    def test_ias2_sale_cost_activity_reconciles_dispatch_and_net_cogs(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        before=self.nrv_sales_activity(tenant,self.actor)
        self.nrv_post(tenant,admin,warehouse,product)
        self.commerce_execute(tenant,admin,
            "SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,sale,admin))
        self.sell_nrv_adjusted(tenant,admin,sale,Decimal("2"))
        after=self.nrv_sales_activity(tenant,self.actor)
        self.assertEqual(tuple(x-y for x,y in zip(after[:5],before[:5])),(
            1,Decimal("2"),Decimal("25"),Decimal("7"),Decimal("18")))
        self.assertEqual(after[5]-before[5],0)
        self.assertTrue(after[6])
        later=self.nrv_sales_activity(tenant,self.actor,
            start=date(2026,10,16),end=date(2026,11,1))
        self.assertEqual(later[:6],(
            0,Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),0))
        self.assertTrue(later[6])

    def test_ias2_cost_activity_includes_packed_delivery_without_double_count(self):
        tenant,admin,product,warehouse,sale=self.prepare_nrv_reserved_sale()
        job=uuid4()
        self.commerce_execute(tenant,admin,
            "SELECT one_create_delivery(%s,%s,%s,1,3,%s)",
            (tenant,job,sale,admin))
        self.delivery_stage(tenant,admin,job,"picked")
        self.delivery_stage(tenant,admin,job,"packed")
        before=self.nrv_sales_activity(tenant,self.actor)
        self.dispatch_nrv_reserved(tenant,admin,sale,Decimal("3"),job=job)
        after=self.nrv_sales_activity(tenant,self.actor)
        self.assertEqual(tuple(x-y for x,y in zip(after[:5],before[:5])),(
            1,Decimal("3"),Decimal("37.50"),Decimal("10.50"),Decimal("27")))
        self.assertTrue(after[6])

    def test_ias2_adjusted_sale_return_stays_requested_if_accounted_without_policy(self):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        self.nrv_post(tenant,admin,warehouse,product)
        self.commerce_execute(tenant,admin,
            "SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,sale,admin))
        sold_event,gross,release,_=self.sell_nrv_adjusted(
            tenant,admin,sale,Decimal("2"))
        quarantine=uuid4()
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name,warehouse_type) "
            "VALUES(%s,%s,%s,'NRV return test quarantine','quarantine')",
            (tenant,quarantine,"Q-NRV-"+quarantine.hex))
        job=uuid4()
        self.commerce_execute(tenant,admin,
            "SELECT one_request_return(%s,%s,%s,2,%s,%s,'NRV sale refund')",
            (tenant,job,sold_event,quarantine,admin))
        attempt_event,attempt_journal=uuid4(),uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(tenant,admin,
                "SELECT one_finish_return(%s,%s,2,%s,%s)",
                (tenant,job,attempt_event,admin))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(tenant,admin,
                "SELECT one_accept_and_value_customer_return("
                "%s,%s,2,%s,%s,%s,'inventory','cogs',%s)",
                (tenant,job,attempt_event,attempt_journal,self.period_id,admin))
        self.assertEqual(self.admin.execute(
            "SELECT status,processed_event_id FROM one_return_jobs "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,job)).fetchone(),("requested",None))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,attempt_event)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_journal_entries "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,attempt_journal)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("3"))
        # Rejecting an RMA remains a permitted non-stock/no-credit outcome.
        self.commerce_execute(tenant,admin,
            "SELECT one_finish_return(%s,%s,0,NULL,%s)",
            (tenant,job,admin))
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_return_jobs WHERE tenant_id=%s AND id=%s",
            (tenant,job)).fetchone()[0],"rejected")

    def test_ias2_sale_cost_activity_requires_authorized_company_period(self):
        for tid,person,begin,end in (
            (self.tenant_a,uuid4(),date(2026,1,1),date(2027,1,1)),
            (self.tenant_b,self.actor,date(2026,1,1),date(2027,1,1)),
            (self.tenant_a,self.actor,date(2027,1,1),date(2026,1,1)),
        ):
            with self.assertRaises(self.psycopg.Error):
                self.commerce_execute(tid,person,
                    "SELECT * FROM one_nrv_adjusted_sales_activity(%s,%s,%s,%s)",
                    (tid,person,begin,end))

    # 030 | A newly reviewed impaired customer return restores gross source
    # cost into isolated QA and posts a *fresh*, evidenced NRV assessment.
    def make_nrv_customer_return(self, qty=Decimal("2")):
        tenant,admin,product,warehouse,purchase,sale,receipt=(
            self.valued_receipt_for_vendor_bill())
        self.nrv_post(tenant,admin,warehouse,product)
        self.commerce_execute(tenant,admin,
            "SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,sale,admin))
        shipped,gross,allowance,_=self.sell_nrv_adjusted(
            tenant,admin,sale,Decimal("2"))
        quarantine=uuid4()
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name,warehouse_type) "
            "VALUES(%s,%s,%s,'Isolated RMA NRV QA','quarantine')",
            (tenant,quarantine,"NRV-QC-"+quarantine.hex))
        rma=uuid4()
        self.commerce_execute(tenant,admin,
            "SELECT one_request_return(%s,%s,%s,%s,%s,%s,'NRV inspection RMA')",
            (tenant,rma,shipped,qty,quarantine,admin))
        return tenant,admin,product,warehouse,quarantine,shipped,rma

    def reassess_nrv_return(self,tenant,admin,rma,accepted, *,
                            stock=None,cogs_gl=None,nrv_evt=None,nrv_gl=None,
                            period=None,sale_price=Decimal("9"),
                            completion=Decimal("0"),sell_cost=Decimal("1"),
                            evidence=None):
        stock=stock or uuid4()
        cogs_gl=cogs_gl or uuid4()
        nrv_evt=nrv_evt or uuid4()
        nrv_gl=nrv_gl or uuid4()
        evidence=evidence or ("REVIEW-NRV-"+stock.hex)
        result=self.commerce_execute(
            tenant,admin,
            "SELECT one_accept_reassessed_nrv_customer_return("
            "%s,%s,%s,%s,%s,%s,%s,%s,"
            "'inventory','cogs','nrvallow','nrvexpense',"
            "%s,%s,%s,%s,%s,%s)",
            (tenant,rma,accepted,stock,cogs_gl,nrv_evt,nrv_gl,
             period or self.period_id,admin,sale_price,completion,sell_cost,
             evidence,date(2026,10,15)))
        return stock,cogs_gl,nrv_evt,nrv_gl,result

    def test_nrv_customer_return_new_inspection_books_original_cost_and_fresh_nrv(self):
        tenant,admin,product,warehouse,qa,shipped,rma=(
            self.make_nrv_customer_return())
        stock,cogs_gl,nrv_evt,nrv_gl,result=self.reassess_nrv_return(
            tenant,admin,rma,Decimal("2"))
        self.assertEqual(result,rma)
        self.assertEqual(self.admin.execute(
            "SELECT status,accepted_qty,processed_event_id "
            "FROM one_return_jobs WHERE tenant_id=%s AND id=%s",
            (tenant,rma)).fetchone(),("completed",Decimal("2"),stock))
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines "
            "WHERE tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,cogs_gl)).fetchall(),[
                ("inventory",Decimal("25"),Decimal("0")),
                ("cogs",Decimal("0"),Decimal("25"))])
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines "
            "WHERE tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,nrv_gl)).fetchall(),[
                ("nrvexpense",Decimal("9"),Decimal("0")),
                ("nrvallow",Decimal("0"),Decimal("9"))])
        self.assertEqual(self.nrv_report(tenant,admin,qa,product),(
            Decimal("2"),Decimal("2"),Decimal("25"),Decimal("9"),
            Decimal("16"),Decimal("9"),True))
        self.assertEqual(self.admin.execute(
            "SELECT original_gross_return_cost,reassessed_nrv,new_allowance "
            "FROM one_nrv_customer_return_reviews WHERE tenant_id=%s "
            "AND return_job_id=%s",(tenant,rma)).fetchone(),(
             Decimal("25"),Decimal("16"),Decimal("9")))
        self.assertEqual(self.admin.execute(
            "SELECT event_type FROM one_operation_outbox WHERE tenant_id=%s "
            "AND source_id=%s AND event_type='returns.nrv_reassessed'",
            (tenant,rma)).fetchone(),("returns.nrv_reassessed",))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_customer_return_credits WHERE tenant_id=%s "
            "AND return_job_id=%s",(tenant,rma)).fetchone()[0],0)

    def test_nrv_customer_partial_return_is_valued_only_at_accepted_quantity(self):
        tenant,admin,product,warehouse,qa,shipped,rma=(
            self.make_nrv_customer_return())
        stock,cogs_gl,nrv_evt,nrv_gl,res=self.reassess_nrv_return(
            tenant,admin,rma,Decimal("1"))
        self.assertEqual(res,rma)
        self.assertEqual(self.admin.execute(
            "SELECT accepted_qty FROM one_return_jobs "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,rma)).fetchone()[0],Decimal("1"))
        self.assertEqual(self.nrv_report(tenant,admin,qa,product),(
            Decimal("1"),Decimal("1"),Decimal("12.50"),Decimal("4.50"),
            Decimal("8"),Decimal("4.50"),True))

    def test_nrv_customer_return_invalid_period_reverts_whole_warehouse_and_gl(self):
        tenant,admin,product,warehouse,qa,shipped,rma=(
            self.make_nrv_customer_return())
        stock,gl,nrv_event,nrv_journal=(uuid4() for _ in range(4))
        with self.assertRaises(self.psycopg.Error):
            self.reassess_nrv_return(
                tenant,admin,rma,Decimal("2"),
                stock=stock,cogs_gl=gl,nrv_evt=nrv_event,nrv_gl=nrv_journal,
                period=uuid4())
        self.assertEqual(self.admin.execute(
            "SELECT status,processed_event_id FROM one_return_jobs "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,rma)).fetchone(),("requested",None))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events "
            "WHERE tenant_id=%s AND id=%s",(tenant,stock)).fetchone()[0],0)
        for journal in (gl,nrv_journal):
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM one_journal_entries "
                "WHERE tenant_id=%s AND id=%s",(tenant,journal)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_nrv_customer_return_reviews "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,rma)).fetchone()[0],0)
        self.reassess_nrv_return(tenant,admin,rma,Decimal("2"),
            stock=stock,cogs_gl=gl,nrv_evt=nrv_event,nrv_gl=nrv_journal)

    def test_nrv_customer_return_rejects_unreviewed_no_write_down(self):
        tenant,admin,product,warehouse,qa,shipped,rma=(
            self.make_nrv_customer_return())
        with self.assertRaises(self.psycopg.Error):
            self.reassess_nrv_return(tenant,admin,rma,Decimal("2"),
                sale_price=Decimal("50"),sell_cost=Decimal("0"))
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_return_jobs WHERE tenant_id=%s AND id=%s",
            (tenant,rma)).fetchone()[0],"requested")
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s AND on_hand>0",
            (tenant,qa,product)).fetchone()[0],0)

    def test_nrv_customer_return_prevents_wrong_tenant_and_double_accepted_rma(self):
        tenant,admin,product,warehouse,qa,shipped,rma=(
            self.make_nrv_customer_return())
        with self.assertRaises(self.psycopg.Error):
            self.reassess_nrv_return(
                self.tenant_b,admin,rma,Decimal("2"))
        with self.assertRaises(self.psycopg.Error):
            self.reassess_nrv_return(
                tenant,self.actor,rma,Decimal("2"))
        self.reassess_nrv_return(tenant,admin,rma,Decimal("2"))
        with self.assertRaises(self.psycopg.Error):
            self.reassess_nrv_return(tenant,admin,rma,Decimal("2"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_nrv_customer_return_reviews "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,rma)).fetchone()[0],1)

    def test_nrv_qa_source_report_links_original_sale_and_posted_qa(self):
        tenant,admin,product,warehouse,qa,shipped,rma=(
            self.make_nrv_customer_return())
        self.reassess_nrv_return(tenant,admin,rma,Decimal("2"))
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",(str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",(str(admin),))
            report=self.db.execute(
                "SELECT * FROM one_nrv_qa_return_check(%s,%s,%s)",
                (tenant,admin,rma)).fetchone()
        self.assertEqual(report,(Decimal("2"),Decimal("25"),
                                 Decimal("9"),Decimal("16"),True))

    def test_nrv_qa_source_report_rejects_wrong_tenant_and_unknown_actor(self):
        tenant,admin,product,warehouse,qa,shipped,rma=(
            self.make_nrv_customer_return())
        self.reassess_nrv_return(tenant,admin,rma,Decimal("2"))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(tenant,uuid4(),
                "SELECT * FROM one_nrv_qa_return_check(%s,%s,%s)",
                (tenant,uuid4(),rma))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(self.tenant_b,admin,
                "SELECT * FROM one_nrv_qa_return_check(%s,%s,%s)",
                (self.tenant_b,admin,rma))

    # 032 | Internal, non-taxable customer refund liability/AR credit pilot.
    # The 030 acceptance, gross recovery and fresh IAS 2 review are separate
    # already committed transactions. This action atomically credits AR,
    # debits original revenue and links the immutable reviewed RMA.
    def nrv_credit_fixture(self,qty=Decimal("2")):
        tenant,admin,product,warehouse,qa,sold,rma=(
            self.make_nrv_customer_return(qty))
        order=self.admin.execute(
            "SELECT order_id FROM one_stock_events "
            "WHERE tenant_id=%s AND id=%s",(tenant,sold)).fetchone()[0]
        invoice=self.sale_source_invoice(tenant,Decimal("2"))
        self.post_dispatched_sale_invoice(order,admin,invoice)
        self.reassess_nrv_return(tenant,admin,rma,qty)
        return tenant,admin,product,warehouse,qa,rma,invoice

    def issue_reviewed_nrv_credit(self,tenant,actor,rma,invoice,*,
                                  credit=None,period=None,ar="ar",revenue="sales",
                                  number=None,issued_on=date(2026,10,15)):
        credit=credit or uuid4()
        number=number or ("NRC-"+credit.hex)
        return credit,self.commerce_execute(
          tenant,actor,"SELECT one_issue_reviewed_nrv_customer_credit("
          "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
          (tenant,rma,invoice.id,credit,number,period or self.period_id,
           ar,revenue,actor,issued_on))

    def test_nrv_reviewed_customer_credit_posts_ar_and_source_revenue_once(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        credit,posted=self.issue_reviewed_nrv_credit(tenant,admin,rma,invoice)
        self.assertEqual(credit,posted)
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines "
            "WHERE tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,credit)).fetchall(),[
               ("sales",Decimal("25"),Decimal("0")),
               ("ar",Decimal("0"),Decimal("25"))])
        self.assertEqual(self.admin.execute(
            "SELECT kind,status,source_invoice_id,net_total,tax_total "
            "FROM one_invoice_docs WHERE tenant_id=%s AND id=%s",
            (tenant,credit)).fetchone(),(
                "customer_credit","issued",invoice.id,Decimal("25"),Decimal("0")))
        self.assertEqual(self.admin.execute(
            "SELECT approved_qty,issued_net FROM "
            "one_nrv_customer_return_credit_links "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,rma)).fetchone(),(Decimal("2"),Decimal("25")))
        self.assertEqual(self.admin.execute(
            "SELECT quantity FROM one_customer_return_credits "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,rma)).fetchone()[0],Decimal("2"))
        self.assertEqual(self.nrv_report(tenant,admin,qa,product),(
            Decimal("2"),Decimal("2"),Decimal("25"),Decimal("9"),
            Decimal("16"),Decimal("9"),True))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_invoice_audit "
            "WHERE tenant_id=%s AND doc_id=%s",(tenant,credit)).fetchone()[0],1)
        for event_type in ("finance.invoice_issued","finance.customer_return_credited"):
            self.assertEqual(self.admin.execute(
               "SELECT count(*) FROM one_operation_outbox "
               "WHERE tenant_id=%s AND event_type=%s AND source_id=%s",
               (tenant,event_type,credit)).fetchone()[0],1)

    def test_nrv_customer_credit_one_partially_accepted_unit(self):
        tenant,admin,product,warehouse,qa,rma,invoice=(
            self.nrv_credit_fixture(Decimal("1")))
        credit,posted=self.issue_reviewed_nrv_credit(tenant,admin,rma,invoice)
        self.assertEqual(credit,posted)
        self.assertEqual(self.admin.execute(
            "SELECT gross_total FROM one_invoice_docs "
            "WHERE tenant_id=%s AND id=%s",(tenant,credit)).fetchone()[0],
            Decimal("12.50"))

    def test_nrv_customer_credit_duplicate_cannot_move_revenue_twice(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        credit,_=self.issue_reviewed_nrv_credit(tenant,admin,rma,invoice)
        attempted=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.issue_reviewed_nrv_credit(
                tenant,admin,rma,invoice,credit=attempted)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_journal_entries "
            "WHERE tenant_id=%s AND id=%s",(tenant,attempted)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_nrv_customer_return_credit_links "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,rma)).fetchone()[0],1)

    def test_nrv_customer_credit_invalid_period_and_gl_roll_back_every_row(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        for overrides in ({"period":uuid4()},{"revenue":"expense"},{"ar":"grni"}):
            attempt=uuid4()
            with self.assertRaises(self.psycopg.Error):
                self.issue_reviewed_nrv_credit(
                    tenant,admin,rma,invoice,credit=attempt,**overrides)
            for table,column in (
                ("one_invoice_docs","id"),("one_journal_entries","id"),
                ("one_invoice_audit","doc_id"),
                ("one_customer_return_credits","credit_invoice_id"),
                ("one_nrv_customer_return_credit_links","credit_invoice_id")
            ):
                self.assertEqual(self.admin.execute(
                    f"SELECT count(*) FROM {table} "
                    f"WHERE tenant_id=%s AND {column}=%s",
                    (tenant,attempt)).fetchone()[0],0)
        good,_=self.issue_reviewed_nrv_credit(tenant,admin,rma,invoice)
        self.assertIsNotNone(good)

    def test_nrv_customer_credit_denies_wrong_company_actor_or_source(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        another=self.invoice()
        self.post_invoice(another)
        for dest,actor,doc in (
            (self.tenant_b,admin,invoice),
            (tenant,uuid4(),invoice),
            (tenant,admin,another)
        ):
            with self.assertRaises(self.psycopg.Error):
                self.issue_reviewed_nrv_credit(dest,actor,rma,doc)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_nrv_customer_return_credit_links "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,rma)).fetchone()[0],0)

    # 033 | Distinguish physically accepted RMA from actual AR credit.
    def nrv_customer_credit_status(self,tenant,actor,rma):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_nrv_customer_credit_case(%s,%s,%s)",
                (tenant,actor,rma)).fetchone()

    def test_nrv_credit_case_exposes_qa_then_financially_posted_states(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        before=self.nrv_customer_credit_status(tenant,admin,rma)
        self.assertEqual(before[0:5],(
            Decimal("2"),Decimal("25"),Decimal("9"),Decimal("16"),True))
        self.assertEqual(before[5:9],(
            None,None,Decimal("0"),Decimal("0")))
        self.assertEqual(before[-2:],(False,"awaiting_internal_credit"))
        credit,_=self.issue_reviewed_nrv_credit(tenant,admin,rma,invoice)
        after=self.nrv_customer_credit_status(tenant,admin,rma)
        self.assertEqual(after[5:9],(
            credit,invoice.id,Decimal("2"),Decimal("25")))
        self.assertEqual(after[-2:],(True,"internal_credit_posted"))

    def test_nrv_credit_case_rejects_wrong_actor_and_cross_company_case(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        for as_tenant,person in (
            (tenant,uuid4()),(self.tenant_b,admin)
        ):
            with self.assertRaises(self.psycopg.Error):
                self.nrv_customer_credit_status(as_tenant,person,rma)

    def test_nrv_customer_credit_rejects_paid_source_until_refund_workflow(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        payment=self.make_payment(invoice,Decimal("10"))
        self.post_payment(payment,invoice,outstanding=Decimal("25"))
        attempted=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.issue_reviewed_nrv_credit(
                tenant,admin,rma,invoice,credit=attempted,
                issued_on=date(2026,10,17))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_invoice_docs WHERE tenant_id=%s AND id=%s",
            (tenant,attempted)).fetchone()[0],0)
        self.assertEqual(
            self.nrv_customer_credit_status(tenant,admin,rma)[-1],
            "awaiting_internal_credit")

    def test_nrv_customer_credit_rejects_vat_source_without_tax_pack(self):
        tenant,admin,product,warehouse,qa,shipped,rma=(
            self.make_nrv_customer_return())
        order=self.admin.execute(
            "SELECT order_id FROM one_stock_events WHERE tenant_id=%s AND id=%s",
            (tenant,shipped)).fetchone()[0]
        base=self.sale_source_invoice(tenant,Decimal("2"))
        original=replace(base,lines=(
            replace(base.lines[0],tax_category=TaxCategory.STANDARD,
                    tax_code="DEMO-STANDARD",tax_rate=Decimal("0.15")),))
        self.post_dispatched_sale_invoice(order,admin,original)
        self.reassess_nrv_return(tenant,admin,rma,Decimal("2"))
        attempted=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.issue_reviewed_nrv_credit(
                tenant,admin,rma,original,credit=attempted)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_journal_entries WHERE tenant_id=%s AND id=%s",
            (tenant,attempted)).fetchone()[0],0)

    # 034 | Informational AR/refund allocation, never a bank action.
    def nrv_refund_exposure(self,tenant,actor,rma,invoice):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_nrv_return_settlement_exposure(%s,%s,%s,%s)",
                (tenant,actor,rma,invoice.id)).fetchone()

    def test_nrv_unpaid_sale_return_has_no_refund_liability(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        a=self.nrv_refund_exposure(tenant,admin,rma,invoice)
        self.assertEqual(a,(
            invoice.id,Decimal("2"),Decimal("25"),Decimal("0"),
            Decimal("0"),Decimal("25"),Decimal("25"),Decimal("0"),
            Decimal("25"),Decimal("25"),Decimal("0"),"internal_credit_ready"))

    def test_nrv_partially_paid_return_requires_split_settlement(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.post_payment(self.make_payment(invoice,Decimal("10")),
                          invoice,outstanding=Decimal("25"))
        a=self.nrv_refund_exposure(tenant,admin,rma,invoice)
        self.assertEqual(a[3:12],(
            Decimal("10"),Decimal("0"),Decimal("15"),Decimal("25"),
            Decimal("0"),Decimal("25"),Decimal("15"),Decimal("10"),
            "requires_refund_liability_workflow"))

    def test_nrv_fully_paid_return_is_not_misreported_as_ar_credit(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.post_payment(self.make_payment(invoice,Decimal("25")),
                          invoice,outstanding=Decimal("25"))
        a=self.nrv_refund_exposure(tenant,admin,rma,invoice)
        self.assertEqual(a[-3:],(
            Decimal("0"),Decimal("25"),"requires_refund_liability_workflow"))

    def test_nrv_issued_internal_credit_cannot_create_new_refund_proposal(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.issue_reviewed_nrv_credit(tenant,admin,rma,invoice)
        a=self.nrv_refund_exposure(tenant,admin,rma,invoice)
        self.assertEqual(a[3:6],(
            Decimal("0"),Decimal("25"),Decimal("0")))
        self.assertEqual(a[-3:],(
            Decimal("0"),Decimal("0"),"already_internally_credited"))

    def test_nrv_vat_source_exposure_requires_zatca_pack(self):
        tenant,admin,product,warehouse,qa,shipped,rma=(
            self.make_nrv_customer_return())
        order=self.admin.execute(
            "SELECT order_id FROM one_stock_events "
            "WHERE tenant_id=%s AND id=%s",(tenant,shipped)).fetchone()[0]
        original=self.sale_source_invoice(tenant,Decimal("2"))
        original=replace(original,lines=(
            replace(original.lines[0],tax_category=TaxCategory.STANDARD,
                    tax_code="DEMO-STANDARD",tax_rate=Decimal("0.15")),))
        self.post_dispatched_sale_invoice(order,admin,original)
        self.reassess_nrv_return(tenant,admin,rma,Decimal("2"))
        a=self.nrv_refund_exposure(tenant,admin,rma,original)
        self.assertEqual(a[6:9],(
            Decimal("25"),Decimal("3.75"),Decimal("28.75")))
        self.assertEqual(a[-1],"requires_zatca_tax_pack")

    def test_nrv_refund_report_rejects_wrong_tenant_actor_and_invoice(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        wrong=self.invoice()
        self.post_invoice(wrong)
        for tid,actor,original in (
            (self.tenant_b,admin,invoice),
            (tenant,uuid4(),invoice),
            (tenant,admin,wrong),
        ):
            with self.assertRaises(self.psycopg.Error):
                self.nrv_refund_exposure(tid,actor,rma,original)

    # 036 | Recognized refund liability, still NO bank transfer or legal VAT note.
    def nrv_recognize_refund(self,tenant,admin,rma,invoice,*,journal=None,
                             period=None,ar="ar",revenue="sales",
                             liability="refunddue",as_of=date(2026,10,17)):
        journal=journal or uuid4()
        result=self.commerce_execute(
           tenant,admin,"SELECT one_recognize_nrv_customer_refund("
           "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
           (tenant,rma,invoice.id,journal,period or self.period_id,
            ar,revenue,liability,admin,as_of,
            "Reviewed return cash refund obligation"))
        return journal,result

    def test_nrv_part_paid_refund_creates_exact_ar_and_liability_journal(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.post_payment(self.make_payment(invoice,Decimal("10")),
                          invoice,outstanding=Decimal("25"))
        journal,posted=self.nrv_recognize_refund(tenant,admin,rma,invoice)
        self.assertEqual(journal,posted)
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines WHERE "
            "tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,journal)).fetchall(),[
             ("sales",Decimal("25"),Decimal("0")),
             ("ar",Decimal("0"),Decimal("15")),
             ("refunddue",Decimal("0"),Decimal("10"))])
        self.assertEqual(self.admin.execute(
            "SELECT gross_return_amount,ar_reduction,refund_liability "
            "FROM one_nrv_refund_claims WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,rma)).fetchone(),
            (Decimal("25"),Decimal("15"),Decimal("10")))
        exposure=self.nrv_refund_exposure(tenant,admin,rma,invoice)
        self.assertEqual(exposure[5],Decimal("0"))
        self.assertEqual(exposure[-3:],(
            Decimal("0"),Decimal("0"),"refund_liability_recognized"))
        self.assertEqual(self.nrv_report(tenant,admin,qa,product),(
             Decimal("2"),Decimal("2"),Decimal("25"),
             Decimal("9"),Decimal("16"),Decimal("9"),True))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_payment_allocations a "
            "JOIN one_payment_docs d ON d.tenant_id=a.tenant_id "
            "AND d.id=a.payment_id "
            "WHERE a.tenant_id=%s AND a.invoice_id=%s "
            "AND d.direction='customer_receipt' AND d.status='applied'",
            (tenant,invoice.id)).fetchone()[0],1)

    def test_nrv_refund_recognized_ar_cannot_collect_same_customer_balance_again(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.post_payment(self.make_payment(invoice,Decimal("10")),
                          invoice,outstanding=Decimal("25"))
        self.nrv_recognize_refund(tenant,admin,rma,invoice)
        attempted=self.make_payment(invoice,Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.post_payment(attempted,invoice,outstanding=Decimal("15"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_payment_docs WHERE tenant_id=%s AND id=%s",
            (tenant,attempted.id)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT gross_return_amount,ar_reduction,refund_liability "
            "FROM one_nrv_refund_claims WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,rma)).fetchone(),
            (Decimal("25"),Decimal("15"),Decimal("10")))

    def test_nrv_concurrent_refund_claim_has_exactly_one_posted_journal(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.post_payment(self.make_payment(invoice,Decimal("10")),
                          invoice,outstanding=Decimal("25"))
        barrier=Barrier(2)
        def recognize():
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
                                     (str(admin),))
                        barrier.wait(timeout=10)
                        conn.execute(
                            "SELECT one_recognize_nrv_customer_refund("
                            "%s,%s,%s,%s,%s,'ar','sales','refunddue',%s,%s,%s)",
                            (tenant,rma,invoice.id,uuid4(),self.period_id,admin,
                             date(2026,10,17),"RMA payment refund claim review"))
                    return True
                except self.psycopg.Error:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(recognize)
            second=pool.submit(recognize)
            results=(first.result(timeout=35),second.result(timeout=35))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(self.admin.execute(
            "SELECT count(*),coalesce(sum(refund_liability),0) "
            "FROM one_nrv_refund_claims WHERE tenant_id=%s "
            "AND return_job_id=%s",(tenant,rma)).fetchone(),
            (1,Decimal("10")))
        self.assertEqual(self.nrv_refund_exposure(
            tenant,admin,rma,invoice)[-1],"refund_liability_recognized")

    def test_nrv_full_paid_refund_does_not_make_receivable_negative(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.post_payment(self.make_payment(invoice,Decimal("25")),
                          invoice,outstanding=Decimal("25"))
        journal,_=self.nrv_recognize_refund(tenant,admin,rma,invoice)
        self.assertEqual(self.admin.execute(
            "SELECT account_id,debit,credit FROM one_journal_lines WHERE "
            "tenant_id=%s AND entry_id=%s ORDER BY line_number",
            (tenant,journal)).fetchall(),[
             ("sales",Decimal("25"),Decimal("0")),
             ("refunddue",Decimal("0"),Decimal("25"))])
        self.assertEqual(self.nrv_refund_exposure(
           tenant,admin,rma,invoice)[-3:],
           (Decimal("0"),Decimal("0"),"refund_liability_recognized"))

    def test_nrv_refund_claim_unpaid_invoice_refused_without_payment(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        attempted=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.nrv_recognize_refund(tenant,admin,rma,invoice,journal=attempted)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_journal_entries WHERE "
            "tenant_id=%s AND id=%s",(tenant,attempted)).fetchone()[0],0)
        self.assertEqual(self.nrv_refund_exposure(
           tenant,admin,rma,invoice)[-1],"internal_credit_ready")

    def test_nrv_refund_duplicate_or_new_customer_credit_does_not_double_reverse(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.post_payment(self.make_payment(invoice,Decimal("10")),
                          invoice,outstanding=Decimal("25"))
        journal,_=self.nrv_recognize_refund(tenant,admin,rma,invoice)
        with self.assertRaises(self.psycopg.Error):
            self.nrv_recognize_refund(tenant,admin,rma,invoice)
        additional=self.credit_for_source_return(invoice,Decimal("1"))
        with self.assertRaises(self.psycopg.Error):
            self.issue_accepted_return_credit(additional,rma,admin)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_nrv_refund_claims "
            "WHERE tenant_id=%s AND return_job_id=%s",(tenant,rma)).fetchone()[0],1)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_invoice_docs WHERE tenant_id=%s AND id=%s",
            (tenant,additional.id)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_journal_entries "
            "WHERE tenant_id=%s AND id=%s",(tenant,journal)).fetchone()[0],1)

    def test_nrv_refund_bad_period_and_accounts_fail_without_residual(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.post_payment(self.make_payment(invoice,Decimal("10")),
                          invoice,outstanding=Decimal("25"))
        for changes in (
            {"period":uuid4()},{"ar":"ap"},{"revenue":"expense"},
            {"liability":"inventory"},{"as_of":date(2026,10,1)}
        ):
            journal=uuid4()
            with self.assertRaises(self.psycopg.Error):
                self.nrv_recognize_refund(
                    tenant,admin,rma,invoice,journal=journal,**changes)
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM one_journal_entries "
                "WHERE tenant_id=%s AND id=%s",
                (tenant,journal)).fetchone()[0],0)
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM one_nrv_refund_claims "
                "WHERE tenant_id=%s AND return_job_id=%s",
                (tenant,rma)).fetchone()[0],0)
        self.nrv_recognize_refund(tenant,admin,rma,invoice)

    def test_nrv_refund_refuses_other_company_and_inactive_identity(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.post_payment(self.make_payment(invoice,Decimal("10")),
                          invoice,outstanding=Decimal("25"))
        for target,actor in ((self.tenant_b,admin),(tenant,uuid4())):
            with self.assertRaises(self.psycopg.Error):
                self.nrv_recognize_refund(target,actor,rma,invoice)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_nrv_refund_claims "
            "WHERE tenant_id=%s AND return_job_id=%s",
            (tenant,rma)).fetchone()[0],0)

    def test_nrv_refund_taxed_original_is_rejected_until_approved_ksa_pack(self):
        tenant,admin,product,warehouse,qa,shipped,rma=(
            self.make_nrv_customer_return())
        order=self.admin.execute(
            "SELECT order_id FROM one_stock_events WHERE tenant_id=%s AND id=%s",
            (tenant,shipped)).fetchone()[0]
        invoice=self.sale_source_invoice(tenant,Decimal("2"))
        invoice=replace(invoice,lines=(replace(
            invoice.lines[0],tax_category=TaxCategory.STANDARD,
            tax_code="VAT15",tax_rate=Decimal("0.15")),))
        self.post_dispatched_sale_invoice(order,admin,invoice)
        self.reassess_nrv_return(tenant,admin,rma,Decimal("2"))
        self.post_payment(self.make_payment(invoice,Decimal("10")),
                          invoice,outstanding=Decimal("28.75"))
        with self.assertRaises(self.psycopg.Error):
            self.nrv_recognize_refund(tenant,admin,rma,invoice)

    # 037 | Audited, read-only company return-refund liability register.
    def nrv_refund_register(self,tenant,actor,*,start=date(2026,1,1),
                            end=date(2027,1,1)):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_nrv_customer_refund_liability_register("
                "%s,%s,%s,%s)",(tenant,actor,start,end)).fetchone()

    def test_nrv_refund_register_links_original_qa_and_paid_invoice_gl(self):
        before=self.nrv_refund_register(self.tenant_a,self.actor)
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.post_payment(self.make_payment(invoice,Decimal("10")),
                          invoice,outstanding=Decimal("25"))
        self.nrv_recognize_refund(tenant,admin,rma,invoice)
        after=self.nrv_refund_register(tenant,self.actor)
        self.assertEqual(after[0]-before[0],1)
        self.assertEqual(after[1]-before[1],Decimal("25"))
        self.assertEqual(after[2]-before[2],Decimal("15"))
        self.assertEqual(after[3]-before[3],Decimal("10"))
        self.assertEqual(after[4]-before[4],0)
        self.assertTrue(after[5])

    def test_nrv_full_paid_refund_register_tracks_entire_liability(self):
        before=self.nrv_refund_register(self.tenant_a,self.actor)
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.post_payment(self.make_payment(invoice,Decimal("25")),
                          invoice,outstanding=Decimal("25"))
        self.nrv_recognize_refund(tenant,admin,rma,invoice)
        after=self.nrv_refund_register(tenant,self.actor)
        self.assertEqual(after[0]-before[0],1)
        self.assertEqual(after[2]-before[2],Decimal("0"))
        self.assertEqual(after[3]-before[3],Decimal("25"))
        self.assertTrue(after[5])

    def test_nrv_refund_register_denies_other_company_unknown_actor_and_bad_period(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        for company,who,start,end in (
             (self.tenant_b,admin,date(2026,1,1),date(2027,1,1)),
             (tenant,uuid4(),date(2026,1,1),date(2027,1,1)),
             (tenant,self.actor,date(2027,1,1),date(2026,1,1)),
        ):
            with self.assertRaises(self.psycopg.Error):
                self.nrv_refund_register(company,who,start=start,end=end)

    # 038 | Maker-checker customer refund orders without any bank payout.
    def prepare_refund_approval(self,amount=Decimal("10")):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        self.post_payment(self.make_payment(invoice,amount),
                          invoice,outstanding=Decimal("25"))
        self.nrv_recognize_refund(tenant,admin,rma,invoice)
        approver=uuid4()
        self.admin.execute(
            "INSERT INTO one_memberships(tenant_id,actor_id,role) "
            "VALUES(%s,%s,'company_admin')",(tenant,approver))
        return tenant,admin,rma,invoice,approver

    def nrv_request_approval(self,tenant,actor,rma,*,order=None,
                             evidence="BENEFICIARY-REVIEW-CI",
                             reason="Customer returned accepted faulty units"):
        order=order or uuid4()
        return order,self.commerce_execute(
            tenant,actor,"SELECT one_request_customer_refund_order("
            "%s,%s,%s,%s,%s,%s)",
            (tenant,rma,order,actor,evidence,reason))

    def nrv_approve_request(self,tenant,actor,order,*,evidence="INDEPENDENT-REVIEW-CI"):
        return self.commerce_execute(
            tenant,actor,"SELECT one_approve_customer_refund_order("
            "%s,%s,%s,%s)",(tenant,order,actor,evidence))

    def test_refund_maker_checker_requires_distinct_admin_and_immutable_audit(self):
        tenant,admin,rma,invoice,reviewer=self.prepare_refund_approval()
        order,request=self.nrv_request_approval(tenant,admin,rma)
        self.assertEqual(request,order)
        self.assertEqual(self.admin.execute(
            "SELECT amount,currency,status,requested_by,approved_by "
            "FROM one_customer_refund_orders WHERE tenant_id=%s AND id=%s",
            (tenant,order)).fetchone(),
            (Decimal("10"),"SAR","requested",admin,None))
        with self.assertRaises(self.psycopg.Error):
            self.nrv_approve_request(tenant,admin,order)
        self.assertEqual(self.nrv_approve_request(tenant,reviewer,order),order)
        self.assertEqual(self.admin.execute(
            "SELECT status,approved_by FROM one_customer_refund_orders "
            "WHERE tenant_id=%s AND id=%s",(tenant,order)).fetchone(),
            ("approved",reviewer))
        self.assertEqual(self.admin.execute(
            "SELECT event,actor_id FROM one_customer_refund_approval_audit "
            "WHERE tenant_id=%s AND order_id=%s ORDER BY id",
            (tenant,order)).fetchall(),[
               ("refund.requested",admin),("refund.approved",reviewer)])
        with self.assertRaises(self.psycopg.Error):
            self.nrv_approve_request(tenant,reviewer,order)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(tenant,admin,
                "DELETE FROM one_customer_refund_orders "
                "WHERE tenant_id=%s AND id=%s RETURNING id",(tenant,order))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_journal_entries WHERE tenant_id=%s "
            "AND reference LIKE 'refund-payout:%%'",
            (tenant,)).fetchone()[0],0)

    def test_refund_request_rejects_duplicate_and_other_tenant(self):
        tenant,admin,rma,invoice,reviewer=self.prepare_refund_approval()
        order,_=self.nrv_request_approval(tenant,admin,rma)
        with self.assertRaises(self.psycopg.Error):
            self.nrv_request_approval(tenant,admin,rma)
        with self.assertRaises(self.psycopg.Error):
            self.nrv_request_approval(self.tenant_b,admin,rma)
        with self.assertRaises(self.psycopg.Error):
            self.nrv_approve_request(tenant,uuid4(),order)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_customer_refund_approval_audit "
            "WHERE tenant_id=%s AND order_id=%s",
            (tenant,order)).fetchone()[0],1)

    def test_refund_request_requires_posted_claim_and_supporting_evidence(self):
        tenant,admin,product,warehouse,qa,rma,invoice=self.nrv_credit_fixture()
        with self.assertRaises(self.psycopg.Error):
            self.nrv_request_approval(tenant,admin,rma)
        self.post_payment(self.make_payment(invoice,Decimal("10")),
                          invoice,outstanding=Decimal("25"))
        self.nrv_recognize_refund(tenant,admin,rma,invoice)
        for evidence in ("", "short"):
            with self.assertRaises(self.psycopg.Error):
                self.nrv_request_approval(tenant,admin,rma,evidence=evidence)
        order,_=self.nrv_request_approval(tenant,admin,rma)
        self.assertIsNotNone(order)

    def test_refund_order_cannot_change_protected_identity_or_amount(self):
        tenant,admin,rma,invoice,reviewer=self.prepare_refund_approval()
        order,_=self.nrv_request_approval(tenant,admin,rma)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(tenant,admin,
                "UPDATE one_customer_refund_orders SET amount=999 "
                "WHERE tenant_id=%s AND id=%s RETURNING amount",(tenant,order))
        self.assertEqual(self.admin.execute(
            "SELECT amount,status FROM one_customer_refund_orders "
            "WHERE tenant_id=%s AND id=%s",(tenant,order)).fetchone(),
            (Decimal("10"),"requested"))
        self.nrv_approve_request(tenant,reviewer,order)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(tenant,admin,
                "UPDATE one_customer_refund_orders SET status='requested' "
                "WHERE tenant_id=%s AND id=%s RETURNING status",(tenant,order))

    # 039 | Finance dashboard: approved means UNPAID, not cash settled.
    def refund_approvals_overview(self,tenant,actor, *,
                                  start=date(2026,1,1),
                                  end=date(2027,1,1)):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_customer_refund_approval_overview("
                "%s,%s,%s,%s)",(tenant,actor,start,end)).fetchone()

    def refund_preview(self,tenant,actor,order):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_refund_approved_instruction_preview(%s,%s,%s)",
                (tenant,actor,order)).fetchone()

    def test_refund_approval_report_separates_unpaid_instructions_from_claims(self):
        before=self.refund_approvals_overview(self.tenant_a,self.actor)
        tenant,admin,rma,invoice,reviewer=self.prepare_refund_approval()
        recognized=self.refund_approvals_overview(tenant,self.actor)
        self.assertEqual(recognized[4]-before[4],1)
        order,_=self.nrv_request_approval(tenant,admin,rma)
        pending=self.refund_approvals_overview(tenant,self.actor)
        self.assertEqual(pending[0]-before[0],1)
        self.assertEqual(pending[2]-before[2],Decimal("10"))
        self.assertEqual(pending[4],before[4])
        self.assertEqual(pending[5],before[5])
        self.assertIsNone(self.refund_preview(tenant,admin,order))
        self.nrv_approve_request(tenant,reviewer,order)
        approved=self.refund_approvals_overview(tenant,self.actor)
        self.assertEqual(approved[0],before[0])
        self.assertEqual(approved[1]-before[1],1)
        self.assertEqual(approved[3]-before[3],Decimal("10"))
        self.assertEqual(approved[5],before[5])
        self.assertTrue(approved[6])
        self.assertEqual(self.refund_preview(tenant,admin,order),(
            order,invoice.id,invoice.partner_id,"SAR",Decimal("10"),
            "BENEFICIARY-REVIEW-CI","INDEPENDENT-REVIEW-CI",
            "approved_not_disbursed"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_payment_docs WHERE tenant_id=%s "
            "AND direction='vendor_disbursement'",(tenant,)).fetchone()[0],0)

    def test_refund_approval_reports_refuse_cross_tenant_and_untrusted_actor(self):
        tenant,admin,rma,invoice,reviewer=self.prepare_refund_approval()
        order,_=self.nrv_request_approval(tenant,admin,rma)
        self.nrv_approve_request(tenant,reviewer,order)
        for tid,who in ((self.tenant_b,admin),(tenant,uuid4())):
            with self.assertRaises(self.psycopg.Error):
                self.refund_approvals_overview(tid,who)
            with self.assertRaises(self.psycopg.Error):
                self.refund_preview(tid,who,order)
        with self.assertRaises(self.psycopg.Error):
            self.refund_approvals_overview(
                tenant,self.actor,start=date(2027,1,1),
                end=date(2026,1,1))

    # 041 | Manually entered statement candidates, NEVER bank-paid evidence.
    def prepare_bank_candidate_order(self):
        tenant,preparer,rma,invoice,reviewer=self.prepare_refund_approval()
        order,_=self.nrv_request_approval(tenant,preparer,rma)
        self.nrv_approve_request(tenant,reviewer,order)
        bank_clerk=uuid4()
        self.admin.execute(
            "INSERT INTO one_memberships(tenant_id,actor_id,role) "
            "VALUES(%s,%s,'accountant')",(tenant,bank_clerk))
        return tenant,preparer,reviewer,bank_clerk,order

    def register_bank_candidate(self,tenant,actor,order,*,bank_line=None,
                amount=Decimal("10"),currency="SAR",
                beneficiary="BENEFICIARY-REVIEW-CI",
                payment_reference=None,
                transaction_ref=None,
                statement_ref="STATEMENT-TEST-20261015",
                value_date=date(2026,10,16)):
        bank_line=bank_line or uuid4()
        return bank_line,self.commerce_execute(
            tenant,actor,"SELECT one_register_refund_bank_candidate("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant,order,bank_line,actor,"BANK-ACCOUNT-DEMO",
             statement_ref,transaction_ref or "TX-"+bank_line.hex,
             payment_reference or "ORDER-"+str(order),beneficiary,
             currency,amount,value_date))

    def bank_candidate_company_report(self,tenant,actor):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_refund_bank_candidate_company_overview(%s,%s)",
                (tenant,actor)).fetchone()

    def test_bank_candidate_is_immutable_unverified_and_never_posts_cash(self):
        before=self.bank_candidate_company_report(self.tenant_a,self.actor)
        tenant,preparer,reviewer,bank_clerk,order=self.prepare_bank_candidate_order()
        pending=self.bank_candidate_company_report(tenant,self.actor)
        self.assertEqual(pending[0]-before[0],1)
        self.assertEqual(pending[3]-before[3],1)
        bank_line,created=self.register_bank_candidate(tenant,bank_clerk,order)
        self.assertEqual(created,bank_line)
        self.assertEqual(self.admin.execute(
            "SELECT verification_status,debit_amount,currency,order_id "
            "FROM one_refund_bank_debit_candidates WHERE tenant_id=%s AND id=%s",
            (tenant,bank_line)).fetchone(),
            ("unverified",Decimal("10"),"SAR",order))
        after=self.bank_candidate_company_report(tenant,self.actor)
        self.assertEqual(after[0]-before[0],1)
        self.assertEqual(after[1]-before[1],1)
        self.assertEqual(after[2]-before[2],Decimal("10"))
        self.assertEqual(after[3],before[3])
        self.assertEqual(after[4],before[4])
        self.assertEqual(after[5],"candidates_unverified_no_disbursement")
        for action in (
            "UPDATE one_refund_bank_debit_candidates SET "
            "verification_status='verified' WHERE tenant_id=%s AND id=%s",
            "DELETE FROM one_refund_bank_debit_candidates "
            "WHERE tenant_id=%s AND id=%s",
        ):
            with self.assertRaises(self.psycopg.Error):
                self.commerce_execute(tenant,bank_clerk,action,
                                     (tenant,bank_line))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_journal_entries "
            "WHERE tenant_id=%s AND reference LIKE 'refund-payout:%%'",
            (tenant,)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_customer_refund_orders "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,order)).fetchone()[0],"approved")

    def test_bank_candidate_rejects_duplicate_order_and_reference(self):
        tenant,preparer,reviewer,bank_clerk,order=self.prepare_bank_candidate_order()
        bank_line,_=self.register_bank_candidate(tenant,bank_clerk,order)
        with self.assertRaises(self.psycopg.Error):
            self.register_bank_candidate(tenant,bank_clerk,order)
        with self.assertRaises(self.psycopg.Error):
            self.register_bank_candidate(tenant,bank_clerk,order,bank_line=bank_line)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_refund_bank_debit_candidates "
            "WHERE tenant_id=%s AND order_id=%s",
            (tenant,order)).fetchone()[0],1)

    def test_bank_candidate_duplicate_transaction_cannot_cover_two_refunds(self):
        tenant,preparer,reviewer,clerk1,order1=self.prepare_bank_candidate_order()
        tenant2,preparer2,reviewer2,clerk2,order2=self.prepare_bank_candidate_order()
        self.assertEqual(tenant,tenant2)
        self.register_bank_candidate(
            tenant,clerk1,order1,transaction_ref="TX-REUSED-BANK-001")
        with self.assertRaises(self.psycopg.Error):
            self.register_bank_candidate(
                tenant,clerk2,order2,transaction_ref="TX-REUSED-BANK-001")
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_refund_bank_debit_candidates "
            "WHERE tenant_id=%s AND bank_transaction_ref=%s",
            (tenant,"TX-REUSED-BANK-001")).fetchone()[0],1)

    def test_parallel_bank_candidate_submission_has_one_winner_and_no_duplicate(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tenant,preparer,reviewer,clerk,order=self.prepare_bank_candidate_order()
        start=Barrier(2)
        def attempt():
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
                                     (str(clerk),))
                        start.wait(timeout=10)
                        line_id=uuid4()
                        conn.execute(
                            "SELECT one_register_refund_bank_candidate("
                            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (tenant,order,line_id,clerk,"BANK-ACCOUNT-DEMO",
                             "STATEMENT-TEST-20261015","TX-"+line_id.hex,
                             "ORDER-"+str(order),"BENEFICIARY-REVIEW-CI",
                             "SAR",Decimal("10"),date(2026,10,16)))
                    return True
                except self.psycopg.Error:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(attempt)
            second=pool.submit(attempt)
            outcomes=(first.result(timeout=35),second.result(timeout=35))
        self.assertEqual(sorted(outcomes),[False,True])
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_refund_bank_debit_candidates "
            "WHERE tenant_id=%s AND order_id=%s",
            (tenant,order)).fetchone()[0],1)
        self.assertEqual(self.bank_candidate_company_report(
            tenant,self.actor)[-1],"candidates_unverified_no_disbursement")

    def test_bank_candidate_rejects_incorrect_amount_currency_and_evidence(self):
        tenant,preparer,reviewer,bank_clerk,order=self.prepare_bank_candidate_order()
        for options in (
            {"amount":Decimal("11")},{"amount":Decimal("-10")},
            {"currency":"USD"},{"beneficiary":"WRONG-BENEFICIARY"},
            {"payment_reference":"ANOTHER-ORDER"},
            {"value_date":date(2020,1,1)},
        ):
            with self.assertRaises(self.psycopg.Error):
                self.register_bank_candidate(tenant,bank_clerk,order,**options)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_refund_bank_debit_candidates "
            "WHERE tenant_id=%s AND order_id=%s",
            (tenant,order)).fetchone()[0],0)

    def test_bank_candidate_enforces_separate_roles_and_tenant(self):
        tenant,preparer,reviewer,bank_clerk,order=self.prepare_bank_candidate_order()
        for who in (preparer,reviewer,uuid4()):
            with self.assertRaises(self.psycopg.Error):
                self.register_bank_candidate(tenant,who,order)
        with self.assertRaises(self.psycopg.Error):
            self.register_bank_candidate(self.tenant_b,bank_clerk,order)
        self.register_bank_candidate(tenant,bank_clerk,order)
        with self.assertRaises(self.psycopg.Error):
            self.bank_candidate_company_report(self.tenant_b,bank_clerk)
        with self.assertRaises(self.psycopg.Error):
            self.bank_candidate_company_report(tenant,uuid4())

    # 042 | ONE PULSE reconciles all non-disbursed internal refund stages.
    def refund_control_pulse(self,tenant,actor):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_refund_control_pulse(%s,%s)",
                (tenant,actor)).fetchone()

    def test_refund_control_pulse_tracks_every_unpaid_stage_without_false_cash_claim(self):
        before=self.refund_control_pulse(self.tenant_a,self.actor)
        tenant,preparer,rma,invoice,reviewer=self.prepare_refund_approval()
        recognized=self.refund_control_pulse(tenant,self.actor)
        self.assertEqual(recognized[0]-before[0],Decimal("10"))
        self.assertEqual(recognized[1]-before[1],Decimal("10"))
        self.assertEqual(recognized[5],before[5])
        order,_=self.nrv_request_approval(tenant,preparer,rma)
        pending=self.refund_control_pulse(tenant,self.actor)
        self.assertEqual(pending[1],before[1])
        self.assertEqual(pending[2]-before[2],Decimal("10"))
        self.nrv_approve_request(tenant,reviewer,order)
        approved=self.refund_control_pulse(tenant,self.actor)
        self.assertEqual(approved[2],before[2])
        self.assertEqual(approved[3]-before[3],Decimal("10"))
        bank_clerk=uuid4()
        self.admin.execute(
            "INSERT INTO one_memberships(tenant_id,actor_id,role) "
            "VALUES(%s,%s,'accountant')",(tenant,bank_clerk))
        self.register_bank_candidate(tenant,bank_clerk,order)
        bank=self.refund_control_pulse(tenant,self.actor)
        self.assertEqual(bank[3],before[3])
        self.assertEqual(bank[4]-before[4],Decimal("10"))
        self.assertEqual(bank[0]-before[0],Decimal("10"))
        self.assertEqual(bank[5],before[5])
        self.assertTrue(bank[6])
        self.assertEqual(bank[7],
                         "bank_settlement_unknown_no_authenticated_feed")
        self.assertEqual(bank[0],sum(bank[1:5]))

    def test_refund_control_pulse_tenant_reader_roles_are_scoped(self):
        tenant,preparer,reviewer,bank_clerk,order=self.prepare_bank_candidate_order()
        baseline=self.refund_control_pulse(tenant,self.actor)
        self.assertTrue(baseline[6])
        for company,actor in ((self.tenant_b,self.actor),
                              (tenant,uuid4())):
            with self.assertRaises(self.psycopg.Error):
                self.refund_control_pulse(company,actor)

    # 043 | Accounting-period close checks finance refund reconciliation.
    def new_period_for_close(self,start=date(2031,1,1),end=date(2032,1,1)):
        pid,admin=uuid4(),uuid4()
        self.admin.execute(
            "INSERT INTO one_periods(tenant_id,id,begins,ends_exclusive) "
            "VALUES(%s,%s,%s,%s)",(self.tenant_a,pid,start,end))
        self.admin.execute(
            "INSERT INTO one_memberships(tenant_id,actor_id,role) "
            "VALUES(%s,%s,'company_admin')",(self.tenant_a,admin))
        return pid,admin

    def refund_close_preflight(self,tenant,admin,period):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(admin),))
            return self.db.execute(
                "SELECT * FROM one_refund_period_close_preflight(%s,%s,%s)",
                (tenant,period,admin)).fetchall()

    def test_close_period_refuses_stock_gap_even_if_refunds_are_reconciled(self):
        # Migration 044 strengthens 043: clean refund GL is not sufficient
        # if there is an unvalued source receipt elsewhere in the company.
        pid,admin=self.new_period_for_close()
        overview=self.refund_control_pulse(self.tenant_a,self.actor)
        ready=self.refund_close_preflight(self.tenant_a,admin,pid)[0]
        self.assertEqual(ready[0],0)
        self.assertEqual(ready[1],0)
        self.assertEqual(ready[4],overview[0])
        self.assertTrue(ready[7])
        tenant,actor,product,warehouse,purchase,_=self.commerce_fixture()
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        receipt=uuid4()
        self.commerce_execute(
            tenant,actor,"SELECT one_fulfill_commercial_line(%s,%s,1,1,%s,%s)",
            (tenant,purchase,receipt,actor))
        stock=self.inventory_close_preflight(self.tenant_a,admin,pid)
        self.assertGreaterEqual(stock[2],1)
        self.assertFalse(stock[3])
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                self.tenant_a,admin,"SELECT one_close_period(%s,%s,%s)",
                (self.tenant_a,pid,admin))
        self.assertFalse(self.admin.execute(
            "SELECT closed FROM one_periods WHERE tenant_id=%s AND id=%s",
            (self.tenant_a,pid)).fetchone()[0])
        for table in ("one_period_close_audit",
                      "one_period_refund_close_evidence",
                      "one_period_inventory_close_evidence"):
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM "+table+
                " WHERE tenant_id=%s AND period_id=%s",
                (self.tenant_a,pid)).fetchone()[0],0)
        self.value_stock_event(tenant,actor,receipt,"grni")

    def test_close_period_rejects_unposted_journal_without_sealing(self):
        pid,admin=self.new_period_for_close(
            date(2032,1,1),date(2033,1,1))
        draft=uuid4()
        self.admin.execute(
            "INSERT INTO one_journal_entries("
            "tenant_id,id,period_id,reference,idempotency_key,occurred_on,currency)"
            " VALUES(%s,%s,%s,%s,%s,%s,'SAR')",
            (self.tenant_a,draft,pid,"unposted-close-gate-"+str(draft),
             "close-gate:"+str(draft),date(2032,2,1)))
        summary=self.refund_close_preflight(self.tenant_a,admin,pid)[0]
        self.assertEqual(summary[0],1)
        self.assertFalse(summary[7])
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                self.tenant_a,admin,"SELECT one_close_period(%s,%s,%s)",
                (self.tenant_a,pid,admin))
        self.assertEqual(self.admin.execute(
            "SELECT closed FROM one_periods WHERE tenant_id=%s AND id=%s",
            (self.tenant_a,pid)).fetchone()[0],False)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_period_refund_close_evidence "
            "WHERE tenant_id=%s AND period_id=%s",
            (self.tenant_a,pid)).fetchone()[0],0)

    def test_refund_close_gate_rejects_wrong_company_or_non_admin(self):
        pid,admin=self.new_period_for_close(
            date(2033,1,1),date(2034,1,1))
        for tid,actor in (
            (self.tenant_b,admin),
            (self.tenant_a,self.actor),
            (self.tenant_a,uuid4())
        ):
            with self.assertRaises(self.psycopg.Error):
                self.refund_close_preflight(tid,actor,pid)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_period_close_audit "
            "WHERE tenant_id=%s AND period_id=%s",
            (self.tenant_a,pid)).fetchone()[0],0)

    # 044 | Stock/valuation preflight and independently immutable close facts.
    def inventory_close_preflight(self, tenant, actor, period):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_inventory_period_close_preflight(%s,%s,%s)",
                (tenant,period,actor)).fetchone()

    def test_close_inventory_empty_company_seals_both_audit_evidences(self):
        period, admin = uuid4(), uuid4()
        self.admin.execute(
            "INSERT INTO one_periods(tenant_id,id,begins,ends_exclusive) "
            "VALUES(%s,%s,%s,%s)",
            (self.tenant_b,period,date(2034,1,1),date(2035,1,1)))
        self.admin.execute(
            "INSERT INTO one_memberships(tenant_id,actor_id,role) "
            "VALUES(%s,%s,'company_admin')",
            (self.tenant_b,admin))
        check=self.inventory_close_preflight(self.tenant_b,admin,period)
        self.assertEqual(check[0:3],(0,0,0))
        self.assertTrue(check[3])
        self.assertEqual(self.commerce_execute(
            self.tenant_b,admin,"SELECT one_close_period(%s,%s,%s)",
            (self.tenant_b,period,admin)),period)
        evidence=self.admin.execute(
            "SELECT inspected_positions,unreconciled_positions,"
            "unvalued_movements FROM one_period_inventory_close_evidence "
            "WHERE tenant_id=%s AND period_id=%s",
            (self.tenant_b,period)).fetchone()
        self.assertEqual(evidence,(0,0,0))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_period_refund_close_evidence "
            "WHERE tenant_id=%s AND period_id=%s",
            (self.tenant_b,period)).fetchone()[0],1)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                self.tenant_b,admin,
                "UPDATE one_period_inventory_close_evidence "
                "SET inspected_positions=1 WHERE tenant_id=%s AND period_id=%s",
                (self.tenant_b,period))

    def test_inventory_close_unvalued_receipt_blocks_atomic_period_close(self):
        tenant,actor,product,warehouse,purchase,_=self.commerce_fixture()
        pid,admin=self.new_period_for_close(
            date(2035,1,1),date(2036,1,1))
        before=self.inventory_close_preflight(tenant,admin,pid)
        self.commerce_execute(
            tenant,actor,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,actor))
        event=uuid4()
        self.commerce_execute(
            tenant,actor,
            "SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,purchase,event,actor))
        inspection=self.inventory_close_preflight(tenant,admin,pid)
        self.assertEqual(inspection[0],before[0]+1)
        self.assertGreaterEqual(inspection[1],before[1]+1)
        self.assertGreaterEqual(inspection[2],before[2]+1)
        self.assertFalse(inspection[3])
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,admin,"SELECT one_close_period(%s,%s,%s)",
                (tenant,pid,admin))
        self.assertFalse(self.admin.execute(
            "SELECT closed FROM one_periods WHERE tenant_id=%s AND id=%s",
            (tenant,pid)).fetchone()[0])
        for table in ("one_period_close_audit",
                      "one_period_inventory_close_evidence",
                      "one_period_refund_close_evidence"):
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM "+table+
                " WHERE tenant_id=%s AND period_id=%s",
                (tenant,pid)).fetchone()[0],0)
        # Rectify this test's receipt before continuing other tests.
        self.value_stock_event(tenant,actor,event,"grni")
        repaired=self.inventory_close_preflight(tenant,admin,pid)
        self.assertEqual(repaired[2],before[2])

    def test_inventory_close_preflight_refuses_cross_tenant_or_non_admin(self):
        pid,admin=self.new_period_for_close(
            date(2036,1,1),date(2037,1,1))
        for tenant,actor in ((self.tenant_b,admin),
                             (self.tenant_a,self.actor),
                             (self.tenant_a,uuid4())):
            with self.assertRaises(self.psycopg.Error):
                self.inventory_close_preflight(tenant,actor,pid)


    # 045 | Captured close-time positions stay frozen after subsequent activity.
    def test_inventory_seal_045_empty_company_and_access_control(self):
        period, admin, viewer = uuid4(), uuid4(), uuid4()
        self.admin.execute(
            "INSERT INTO one_periods(tenant_id,id,begins,ends_exclusive) "
            "VALUES(%s,%s,%s,%s)",
            (self.tenant_b,period,date(2040,1,1),date(2041,1,1)))
        for actor,role in ((admin,"company_admin"),(viewer,"viewer")):
            self.admin.execute(
                "INSERT INTO one_memberships(tenant_id,actor_id,role) "
                "VALUES(%s,%s,%s)",(self.tenant_b,actor,role))
        self.assertEqual(self.commerce_execute(
            self.tenant_b,admin,"SELECT one_close_period(%s,%s,%s)",
            (self.tenant_b,period,admin)),period)
        seal=self.admin.execute(
            "SELECT snapshot_kind,position_count,physical_movement_count,"
            "valued_movement_count FROM one_period_inventory_seals "
            "WHERE tenant_id=%s AND period_id=%s",
            (self.tenant_b,period)).fetchone()
        self.assertEqual(seal,("OBSERVED_AT_CLOSE",0,0,0))
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(self.tenant_b),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(viewer),))
            self.assertEqual(self.db.execute(
                "SELECT * FROM one_inventory_period_seal_details(%s,%s,%s)",
                (self.tenant_b,period,viewer)).fetchall(),[])
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                self.tenant_b,uuid4(),
                "SELECT count(*) FROM one_inventory_period_seal_details(%s,%s,%s)",
                (self.tenant_b,period,uuid4()))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                self.tenant_b,admin,
                "UPDATE one_period_inventory_seals SET position_count=1 "
                "WHERE tenant_id=%s AND period_id=%s RETURNING position_count",
                (self.tenant_b,period))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                self.tenant_b,admin,"SELECT one_close_period(%s,%s,%s)",
                (self.tenant_b,period,admin))

    def test_inventory_seal_045_nonempty_snapshot_is_not_live_balance(self):
        # Synthetic fixtures only: administrative inserts are never supported
        # production stock movements. They isolate close-time capture behavior.
        tenant,admin,warehouse,product,period = (uuid4() for _ in range(5))
        self.admin.execute(
            "INSERT INTO one_tenants(id,company_name) VALUES(%s,%s)",
            (tenant,"Seal 045 isolated CI"))
        self.admin.execute(
            "INSERT INTO one_memberships(tenant_id,actor_id,role) "
            "VALUES(%s,%s,'company_admin')",(tenant,admin))
        self.admin.execute(
            "INSERT INTO one_periods(tenant_id,id,begins,ends_exclusive) "
            "VALUES(%s,%s,%s,%s)",
            (tenant,period,date(2042,1,1),date(2043,1,1)))
        self.admin.execute(
            "INSERT INTO one_products(tenant_id,id,sku,name) "
            "VALUES(%s,%s,%s,'Seal-only product')",
            (tenant,product,product.hex))
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name) "
            "VALUES(%s,%s,%s,'Seal-only warehouse')",
            (tenant,warehouse,warehouse.hex[:15]))
        with self.admin.transaction():
            self.admin.execute("SELECT set_config('one.tenant_id',%s,true)",
                               (str(tenant),))
            self.admin.execute("SELECT set_config('one.commerce_context','yes',true)")
            self.admin.execute("SELECT set_config('one.valuation_context','yes',true)")
            self.admin.execute(
                "INSERT INTO one_stock_balances(tenant_id,warehouse_id,"
                "product_id,on_hand,reserved) VALUES(%s,%s,%s,3,1)",
                (tenant,warehouse,product))
            self.admin.execute(
                "INSERT INTO one_stock_value_positions(tenant_id,warehouse_id,"
                "product_id,currency,qty,book_value) VALUES(%s,%s,%s,'SAR',3,18)",
                (tenant,warehouse,product))
        check=self.inventory_close_preflight(tenant,admin,period)
        self.assertTrue(check[3])
        self.assertEqual(check[0],1)
        self.assertEqual(self.commerce_execute(
            tenant,admin,"SELECT one_close_period(%s,%s,%s)",
            (tenant,period,admin)),period)
        observed=self.admin.execute(
            "SELECT physical_qty,reserved_qty,gross_book_value,net_book_value "
            "FROM one_period_inventory_seal_lines WHERE tenant_id=%s "
            "AND period_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,period,warehouse,product)).fetchone()
        self.assertEqual(observed,(3,1,18,18))
        header=self.admin.execute(
            "SELECT position_count,snapshot_kind FROM one_period_inventory_seals "
            "WHERE tenant_id=%s AND period_id=%s",
            (tenant,period)).fetchone()
        self.assertEqual(header,(1,"OBSERVED_AT_CLOSE"))
        with self.admin.transaction():
            self.admin.execute("SELECT set_config('one.tenant_id',%s,true)",
                               (str(tenant),))
            self.admin.execute("SELECT set_config('one.commerce_context','yes',true)")
            self.admin.execute("SELECT set_config('one.valuation_context','yes',true)")
            self.admin.execute(
                "UPDATE one_stock_balances SET on_hand=4 "
                "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
                (tenant,warehouse,product))
            self.admin.execute(
                "UPDATE one_stock_value_positions SET qty=4,book_value=24 "
                "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
                (tenant,warehouse,product))
        still=self.admin.execute(
            "SELECT physical_qty,reserved_qty,gross_book_value,net_book_value "
            "FROM one_period_inventory_seal_lines WHERE tenant_id=%s "
            "AND period_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,period,warehouse,product)).fetchone()
        self.assertEqual(still,observed)
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(admin),))
            detail=self.db.execute(
                "SELECT physical_qty,gross_book_value,snapshot_kind "
                "FROM one_inventory_period_seal_details(%s,%s,%s)",
                (tenant,period,admin)).fetchone()
        self.assertEqual(detail,(3,18,"OBSERVED_AT_CLOSE"))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,admin,"DELETE FROM one_period_inventory_seal_lines "
                "WHERE tenant_id=%s AND period_id=%s RETURNING product_id",
                (tenant,period))


    # 046 | No guessed event dates: each event needs an independent approval.
    def inventory_history_046_fixture(self, *, period_end=None):
        tenant,owner,reviewer,product,warehouse,period = (uuid4() for _ in range(6))
        purchase,sale=uuid4(),uuid4()
        start=date.today().replace(month=1,day=1)
        end=period_end or start.replace(year=start.year+1)
        self.admin.execute(
            "INSERT INTO one_tenants(id,company_name) VALUES(%s,'Dated inventory CI')",
            (tenant,))
        for actor,role in ((owner,"company_admin"),(reviewer,"accountant")):
            self.admin.execute(
                "INSERT INTO one_memberships(tenant_id,actor_id,role) "
                "VALUES(%s,%s,%s)",(tenant,actor,role))
        self.admin.execute(
            "INSERT INTO one_periods(tenant_id,id,begins,ends_exclusive) "
            "VALUES(%s,%s,%s,%s)",(tenant,period,start,end))
        for acct,code,kind in (
            ("inventory","1540","asset"),
            ("grni","2350","liability"),
            ("cogs","5350","expense"),
        ):
            self.admin.execute(
                "INSERT INTO one_accounts(tenant_id,id,code,name,kind) "
                "VALUES(%s,%s,%s,%s,%s)",
                (tenant,acct,code,acct,kind))
        self.admin.execute(
            "INSERT INTO one_products(tenant_id,id,sku,name) "
            "VALUES(%s,%s,%s,'Effective dated item')",
            (tenant,product,product.hex))
        self.admin.execute(
            "INSERT INTO one_warehouses(tenant_id,id,code,name) "
            "VALUES(%s,%s,%s,'History warehouse')",
            (tenant,warehouse,warehouse.hex[:15]))
        for order,kind in ((purchase,"purchase"),(sale,"sale")):
            self.admin.execute(
                "INSERT INTO one_commercial_orders "
                "(tenant_id,id,kind,number,partner_id,currency) "
                "VALUES(%s,%s,%s,%s,'TEMPORAL-CI','SAR')",
                (tenant,order,kind,order.hex))
            self.admin.execute(
                "INSERT INTO one_commercial_order_lines(tenant_id,order_id,"
                "line_no,product_id,warehouse_id,ordered_qty,unit_price) "
                "VALUES(%s,%s,1,%s,%s,%s,12.50)",
                (tenant,order,product,warehouse,6 if kind=="purchase" else 5))
            self.commerce_execute(
                tenant,owner,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,owner))
        return tenant,owner,reviewer,product,warehouse,period,purchase,sale

    def history_asof_046(self,tenant,actor,cutoff):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_inventory_gross_history_asof(%s,%s,%s)",
                (tenant,cutoff,actor)).fetchall()

    def request_date_046(self,tenant,actor,event,on):
        return self.commerce_execute(
            tenant,actor,
            "SELECT one_request_inventory_effective_date(%s,%s,%s,%s)",
            (tenant,event,on,actor))

    def approve_date_046(self,tenant,actor,event):
        return self.commerce_execute(
            tenant,actor,
            "SELECT one_approve_inventory_effective_date(%s,%s,%s)",
            (tenant,event,actor))

    def test_046_stock_history_rejects_missing_dates_and_preserves_asof(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        receipt,dispatch=uuid4(),uuid4()
        self.commerce_execute(
            tenant,owner,
            "SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,purchase,receipt,owner))
        self.value_stock_event(tenant,owner,receipt,"grni",period_id=period)
        self.commerce_execute(
            tenant,owner,
            "SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,sale,dispatch,owner))
        self.value_stock_event(tenant,owner,dispatch,"cogs",period_id=period)
        with self.assertRaises(self.psycopg.Error):
            self.history_asof_046(tenant,owner,date.today())
        with self.assertRaises(self.psycopg.Error):
            self.request_date_046(tenant,reviewer,receipt,date.today())
        self.assertEqual(self.request_date_046(
            tenant,owner,receipt,date.today()),receipt)
        with self.assertRaises(self.psycopg.Error):
            self.approve_date_046(tenant,owner,receipt)
        with self.assertRaises(self.psycopg.Error):
            self.approve_date_046(self.tenant_b,reviewer,receipt)
        self.assertEqual(self.approve_date_046(
            tenant,reviewer,receipt),receipt)
        with self.assertRaises(self.psycopg.Error):
            self.history_asof_046(tenant,owner,date.today())
        self.request_date_046(tenant,owner,dispatch,date.today())
        self.approve_date_046(tenant,reviewer,dispatch)
        self.assertEqual(self.history_asof_046(
            tenant,owner,date.today()-timedelta(days=1)),[])
        rows=self.history_asof_046(tenant,reviewer,date.today())
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][0:5],
                         (warehouse,product,"SAR",Decimal("3"),Decimal("37.50")))
        self.assertEqual(rows[0][5],2)
        self.assertEqual(rows[0][7],"DUAL_ATTESTED_BUSINESS_DATE_GROSS_ONLY")
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "UPDATE one_inventory_effective_dates "
                "SET effective_on=%s WHERE tenant_id=%s AND stock_event_id=%s "
                "RETURNING stock_event_id",
                (date.today()-timedelta(days=1),tenant,receipt))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "DELETE FROM one_inventory_date_requests "
                "WHERE tenant_id=%s AND stock_event_id=%s RETURNING stock_event_id",
                (tenant,receipt))
        # New unregistered source invalidates even previously complete reports.
        extra=uuid4()
        self.commerce_execute(
            tenant,owner,
            "SELECT one_fulfill_commercial_line(%s,%s,1,1,%s,%s)",
            (tenant,purchase,extra,owner))
        self.value_stock_event(tenant,owner,extra,"grni",period_id=period)
        with self.assertRaises(self.psycopg.Error):
            self.history_asof_046(tenant,owner,date.today())
        self.request_date_046(tenant,owner,extra,date.today())
        self.approve_date_046(tenant,reviewer,extra)
        self.assertEqual(
            self.history_asof_046(tenant,owner,date.today())[0][3:5],
            (Decimal("4"),Decimal("50.00")))

    def test_046_reviewer_cannot_approve_wrong_gl_date_or_closed_period(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        event=uuid4()
        self.commerce_execute(
            tenant,owner,
            "SELECT one_fulfill_commercial_line(%s,%s,1,1,%s,%s)",
            (tenant,purchase,event,owner))
        self.value_stock_event(tenant,owner,event,"grni",period_id=period)
        yesterday=date.today()-timedelta(days=1)
        self.request_date_046(tenant,owner,event,yesterday)
        with self.assertRaises(self.psycopg.Error):
            self.approve_date_046(tenant,reviewer,event)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_inventory_effective_dates "
            "WHERE tenant_id=%s AND stock_event_id=%s",
            (tenant,event)).fetchone()[0],0)
        with self.assertRaises(self.psycopg.Error):
            self.request_date_046(tenant,owner,event,date.today())
        with self.assertRaises(self.psycopg.Error):
            self.history_asof_046(tenant,uuid4(),date.today())


    # 047 | Strict cutoff only after a separate admin request and approval.
    def activate_strict_047(self,tenant,owner,reviewer):
        self.assertEqual(self.commerce_execute(
            tenant,owner,
            "SELECT one_request_historical_inventory_close(%s,%s)",
            (tenant,owner)),tenant)
        self.assertEqual(self.commerce_execute(
            tenant,reviewer,
            "SELECT one_approve_historical_inventory_close(%s,%s)",
            (tenant,reviewer)),tenant)

    def history_net_047(self,tenant,actor,cutoff):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_inventory_historical_net_asof(%s,%s,%s)",
                (tenant,cutoff,actor)).fetchall()

    def test_047_strict_close_blocks_undated_stock_then_seals_true_cutoff(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture(
                period_end=date.today()+timedelta(days=1)))
        event=uuid4()
        self.commerce_execute(
            tenant,owner,
            "SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,purchase,event,owner))
        self.value_stock_event(tenant,owner,event,"grni",period_id=period)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,reviewer,
                "SELECT one_request_historical_inventory_close(%s,%s)",
                (tenant,reviewer))
        self.assertEqual(self.commerce_execute(
            tenant,owner,
            "SELECT one_request_historical_inventory_close(%s,%s)",
            (tenant,owner)),tenant)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "SELECT one_approve_historical_inventory_close(%s,%s)",
                (tenant,owner))
        self.assertEqual(self.commerce_execute(
            tenant,reviewer,
            "SELECT one_approve_historical_inventory_close(%s,%s)",
            (tenant,reviewer)),tenant)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,"SELECT one_close_period(%s,%s,%s)",
                (tenant,period,owner))
        self.assertFalse(self.admin.execute(
            "SELECT closed FROM one_periods WHERE tenant_id=%s AND id=%s",
            (tenant,period)).fetchone()[0])
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_period_historical_inventory_close "
            "WHERE tenant_id=%s AND period_id=%s",
            (tenant,period)).fetchone()[0],0)
        self.request_date_046(tenant,owner,event,date.today())
        self.approve_date_046(tenant,reviewer,event)
        self.assertEqual(self.history_net_047(
            tenant,reviewer,date.today())[0][3:7],
            (Decimal("5"),Decimal("62.50"),Decimal("0"),Decimal("62.50")))
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_close_period(%s,%s,%s)",
            (tenant,period,owner)),period)
        h=self.admin.execute(
            "SELECT cutoff_on,position_count,evidence_kind "
            "FROM one_period_historical_inventory_close "
            "WHERE tenant_id=%s AND period_id=%s",
            (tenant,period)).fetchone()
        self.assertEqual(h,(date.today(),1,
            "HISTORICAL_PERIOD_END_POSTED_GROSS_AND_NRV"))
        detail=self.admin.execute(
            "SELECT physical_qty,gross_book_value,nrv_allowance,net_book_value "
            "FROM one_period_historical_inventory_close_lines "
            "WHERE tenant_id=%s AND period_id=%s",
            (tenant,period)).fetchone()
        self.assertEqual(detail,(5,Decimal("62.50"),0,Decimal("62.50")))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "UPDATE one_period_historical_inventory_close_lines "
                "SET gross_book_value=10 WHERE tenant_id=%s AND period_id=%s "
                "RETURNING product_id",(tenant,period))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                self.tenant_b,reviewer,
                "SELECT count(*) FROM one_inventory_historical_close_details(%s,%s,%s)",
                (self.tenant_b,period,reviewer))
        with self.assertRaises(self.psycopg.Error):
            self.request_date_046(tenant,owner,uuid4(),date.today())

    def test_047_asof_nrv_posts_reconcile_and_seal_net_cost(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture(
                period_end=date.today()+timedelta(days=1)))
        stock_event=uuid4()
        self.commerce_execute(
            tenant,owner,
            "SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,purchase,stock_event,owner))
        self.value_stock_event(tenant,owner,stock_event,"grni",period_id=period)
        self.request_date_046(tenant,owner,stock_event,date.today())
        self.approve_date_046(tenant,reviewer,stock_event)
        for acct,code,kind in (
            ("nrvallow","1590","asset"),
            ("nrvexpense","5750","expense")):
            self.admin.execute(
                "INSERT INTO one_accounts(tenant_id,id,code,name,kind) "
                "VALUES(%s,%s,%s,%s,%s)",
                (tenant,acct,code,acct,kind))
        nrv_event,nrv_journal=uuid4(),uuid4()
        self.assertEqual(self.commerce_execute(
            tenant,owner,
            "SELECT one_post_inventory_nrv_adjustment("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant,warehouse,product,nrv_event,nrv_journal,period,
             "nrvallow","nrvexpense",owner,0,Decimal("5"),
             Decimal("62.50"),Decimal("10"),Decimal("0"),Decimal("1"),
             "NRV-047-"+nrv_event.hex,date.today())),nrv_event)
        rows=self.history_net_047(tenant,reviewer,date.today())
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][3:7],
                         (Decimal("5"),Decimal("62.50"),
                          Decimal("17.50"),Decimal("45.00")))
        self.activate_strict_047(tenant,owner,reviewer)
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_close_period(%s,%s,%s)",
            (tenant,period,owner)),period)
        entry=self.admin.execute(
            "SELECT gross_book_value,nrv_allowance,net_book_value "
            "FROM one_period_historical_inventory_close_lines "
            "WHERE tenant_id=%s AND period_id=%s",
            (tenant,period)).fetchone()
        self.assertEqual(entry,(Decimal("62.50"),Decimal("17.50"),
                                Decimal("45.00")))
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(reviewer),))
            details=self.db.execute(
                "SELECT gross_book_value,nrv_allowance,net_book_value,cutoff_on "
                "FROM one_inventory_historical_close_details(%s,%s,%s)",
                (tenant,period,reviewer)).fetchone()
        self.assertEqual(details,(*entry,date.today()))

    def test_047_future_cutoff_fails_without_partial_close_records(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        self.activate_strict_047(tenant,owner,reviewer)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,"SELECT one_close_period(%s,%s,%s)",
                (tenant,period,owner))
        for table in ("one_period_historical_inventory_close",
                      "one_period_inventory_seals","one_period_close_audit"):
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM "+table+
                " WHERE tenant_id=%s AND period_id=%s",
                (tenant,period)).fetchone()[0],0)


    def test_047_old_period_uses_period_end_not_newer_live_stock(self):
        # Two nonoverlapping dates: historical period ended yesterday,
        # today's later physical receipt is valued in a separate open period.
        tenant,owner,reviewer,product,warehouse,old_period,purchase,sale=(
            self.inventory_history_046_fixture(period_end=date.today()))
        current_period=uuid4()
        self.admin.execute(
            "INSERT INTO one_periods(tenant_id,id,begins,ends_exclusive) "
            "VALUES(%s,%s,%s,%s)",
            (tenant,current_period,date.today(),date.today()+timedelta(days=7)))
        event=uuid4()
        self.commerce_execute(
            tenant,owner,
            "SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,purchase,event,owner))
        self.value_stock_event(tenant,owner,event,"grni",
                               period_id=current_period)
        self.request_date_046(tenant,owner,event,date.today())
        self.approve_date_046(tenant,reviewer,event)
        self.assertEqual(self.history_net_047(
            tenant,reviewer,date.today()-timedelta(days=1)),[])
        self.activate_strict_047(tenant,owner,reviewer)
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_close_period(%s,%s,%s)",
            (tenant,old_period,owner)),old_period)
        historic=self.admin.execute(
            "SELECT cutoff_on,position_count FROM "
            "one_period_historical_inventory_close WHERE "
            "tenant_id=%s AND period_id=%s",
            (tenant,old_period)).fetchone()
        observed=self.admin.execute(
            "SELECT position_count FROM one_period_inventory_seals "
            "WHERE tenant_id=%s AND period_id=%s",
            (tenant,old_period)).fetchone()[0]
        self.assertEqual(historic,(date.today()-timedelta(days=1),0))
        self.assertEqual(observed,1)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_period_historical_inventory_close_lines "
            "WHERE tenant_id=%s AND period_id=%s",
            (tenant,old_period)).fetchone()[0],0)


    # 048 | User/role policy overrides are independently approved, append-only
    # and enforced in the database, not just client-side button visibility.
    def propose_permission_048(self,tenant,author,permission,decision,
                               target_user=None,target_role=None):
        request=uuid4()
        self.assertEqual(self.commerce_execute(
            tenant,author,
            "SELECT one_request_permission_change("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant,request,target_user,target_role,permission,decision,
             date.today(),None,author)),request)
        return request

    def approve_permission_048(self,tenant,approver,request):
        return self.commerce_execute(
            tenant,approver,
            "SELECT one_approve_permission_change(%s,%s,%s)",
            (tenant,request,approver))

    def has_permission_048(self,tenant,actor,permission):
        return self.commerce_execute(
            tenant,actor,
            "SELECT one_actor_has_permission(%s,%s,%s)",
            (tenant,actor,permission))

    def test_048_admin_can_lose_close_permission_without_losing_membership(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture(
                period_end=date.today()+timedelta(days=1)))
        self.assertTrue(self.has_permission_048(
            tenant,owner,'finance.period.hard_close'))
        self.assertFalse(self.has_permission_048(
            tenant,reviewer,'finance.period.hard_close'))
        self.assertFalse(self.has_permission_048(
            tenant,uuid4(),'finance.period.hard_close'))
        self.assertFalse(self.has_permission_048(
            self.tenant_b,owner,'finance.period.hard_close'))
        denial=self.propose_permission_048(
            tenant,owner,'finance.period.hard_close','deny',target_user=owner)
        with self.assertRaises(self.psycopg.Error):
            self.approve_permission_048(tenant,owner,denial)
        self.assertTrue(self.has_permission_048(
            tenant,owner,'finance.period.hard_close'))
        self.assertEqual(self.approve_permission_048(
            tenant,reviewer,denial),denial)
        self.assertFalse(self.has_permission_048(
            tenant,owner,'finance.period.hard_close'))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,"SELECT one_close_period(%s,%s,%s)",
                (tenant,period,owner))
        self.assertFalse(self.admin.execute(
            "SELECT closed FROM one_periods WHERE tenant_id=%s AND id=%s",
            (tenant,period)).fetchone()[0])
        restore=self.propose_permission_048(
            tenant,owner,'finance.period.hard_close','allow',target_user=owner)
        self.approve_permission_048(tenant,reviewer,restore)
        self.assertTrue(self.has_permission_048(
            tenant,owner,'finance.period.hard_close'))
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_close_period(%s,%s,%s)",
            (tenant,period,owner)),period)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,"UPDATE one_permission_change_approvals "
                "SET approved_by=%s WHERE tenant_id=%s AND request_id=%s "
                "RETURNING request_id",(owner,tenant,denial))

    def test_048_role_denial_user_exception_and_high_risk_approval(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        self.assertTrue(self.has_permission_048(
            tenant,reviewer,'finance.inventory.history.activate.approve'))
        role_deny=self.propose_permission_048(
            tenant,owner,'finance.inventory.history.activate.approve','deny',
            target_role='accountant')
        self.approve_permission_048(tenant,reviewer,role_deny)
        self.assertFalse(self.has_permission_048(
            tenant,reviewer,'finance.inventory.history.activate.approve'))
        self.commerce_execute(
            tenant,owner,"SELECT one_request_historical_inventory_close(%s,%s)",
            (tenant,owner))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,reviewer,
                "SELECT one_approve_historical_inventory_close(%s,%s)",
                (tenant,reviewer))
        # A more specific independently approved USER allow overrides ROLE deny.
        user_allow=self.propose_permission_048(
            tenant,owner,'finance.inventory.history.activate.approve','allow',
            target_user=reviewer)
        self.approve_permission_048(tenant,reviewer,user_allow)
        self.assertTrue(self.has_permission_048(
            tenant,reviewer,'finance.inventory.history.activate.approve'))
        self.assertEqual(self.commerce_execute(
            tenant,reviewer,
            "SELECT one_approve_historical_inventory_close(%s,%s)",
            (tenant,reviewer)),tenant)
        self.assertFalse(self.has_permission_048(
            tenant,reviewer,'finance.unknown.administration'))
        with self.assertRaises(self.psycopg.Error):
            self.propose_permission_048(
                tenant,reviewer,'finance.period.soft_adjust','allow',
                target_user=reviewer)

    def test_048_date_attestation_responds_to_individual_permission_denial(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        event=uuid4()
        self.commerce_execute(
            tenant,owner,
            "SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,purchase,event,owner))
        self.value_stock_event(tenant,owner,event,"grni",period_id=period)
        restriction=self.propose_permission_048(
            tenant,owner,'inventory.date.approve','deny',
            target_user=reviewer)
        self.approve_permission_048(tenant,reviewer,restriction)
        self.request_date_046(tenant,owner,event,date.today())
        with self.assertRaises(self.psycopg.Error):
            self.approve_date_046(tenant,reviewer,event)
        self.assertFalse(self.admin.execute(
            "SELECT EXISTS(SELECT 1 FROM one_inventory_effective_dates "
            "WHERE tenant_id=%s AND stock_event_id=%s)",
            (tenant,event)).fetchone()[0])
        restore=self.propose_permission_048(
            tenant,owner,'inventory.date.approve','allow',
            target_user=reviewer)
        self.approve_permission_048(tenant,reviewer,restore)
        self.assertEqual(self.approve_date_046(
            tenant,reviewer,event),event)


    # 049 | Soft close is a reviewed state, not permission to overwrite books.
    def soft_state_049(self,tenant,actor,period):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_finance_period_state(%s,%s,%s)",
                (tenant,period,actor)).fetchone()

    def soft_request_049(self,tenant,actor,period):
        return self.commerce_execute(
            tenant,actor,"SELECT one_request_period_soft_close(%s,%s,%s)",
            (tenant,period,actor))

    def soft_approve_049(self,tenant,actor,period):
        return self.commerce_execute(
            tenant,actor,"SELECT one_approve_period_soft_close(%s,%s,%s)",
            (tenant,period,actor))

    def adjust_request_049(self,tenant,actor,period,*,amount="7.50",when=None):
        request,journal=uuid4(),uuid4()
        result=self.commerce_execute(
            tenant,actor,
            "SELECT one_request_soft_adjustment("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant,request,period,journal,when or date.today(),"SAR",
             "cogs","grni",Decimal(amount),
             "Approved reclassification discovered after soft close",actor))
        self.assertEqual(result,request)
        return request,journal

    def test_049_soft_close_requires_independent_users_and_freezes_ordinary_gl(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        self.assertEqual(self.soft_state_049(
            tenant,owner,period)[0],"open")
        with self.assertRaises(self.psycopg.Error):
            self.soft_request_049(tenant,reviewer,period)
        self.assertEqual(self.soft_request_049(tenant,owner,period),period)
        with self.assertRaises(self.psycopg.Error):
            self.soft_approve_049(tenant,owner,period)
        self.assertEqual(self.soft_approve_049(tenant,reviewer,period),period)
        self.assertEqual(self.soft_state_049(
            tenant,owner,period)[0],"soft_closed")
        unauthorized=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "INSERT INTO one_journal_entries("
                "tenant_id,id,period_id,reference,idempotency_key,"
                "occurred_on,currency) "
                "VALUES(%s,%s,%s,'Without soft review',%s,%s,'SAR') "
                "RETURNING id",
                (tenant,unauthorized,period,str(uuid4()),date.today()))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_journal_entries "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,unauthorized)).fetchone()[0],0)

        request,journal=self.adjust_request_049(tenant,owner,period)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,"SELECT one_post_journal(%s,%s,%s)",
                (tenant,journal,owner))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "SELECT one_approve_soft_adjustment(%s,%s,%s)",
                (tenant,request,owner))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "UPDATE one_journal_lines SET debit=9 "
                "WHERE tenant_id=%s AND entry_id=%s AND line_number=1 "
                "RETURNING debit",
                (tenant,journal))
        self.assertEqual(self.soft_state_049(tenant,reviewer,period)[2:5],
                         (1,0,0))
        self.assertEqual(self.commerce_execute(
            tenant,reviewer,
            "SELECT one_approve_soft_adjustment(%s,%s,%s)",
            (tenant,request,reviewer)),request)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,reviewer,"SELECT one_post_journal(%s,%s,%s)",
                (tenant,journal,reviewer))
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_post_journal(%s,%s,%s)",
            (tenant,journal,owner)),journal)
        self.assertEqual(self.soft_state_049(tenant,owner,period)[2:5],
                         (0,1,1))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_soft_adjustment_post_audit WHERE "
            "tenant_id=%s AND request_id=%s AND journal_id=%s AND posted_by=%s",
            (tenant,request,journal,owner)).fetchone()[0],1)
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_journal_entries "
            "WHERE tenant_id=%s AND id=%s",
            (tenant,journal)).fetchone()[0],"posted")

    def test_049_soft_adjust_and_approval_respect_user_scoped_permissions(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        denied=self.propose_permission_048(
            tenant,owner,'finance.period.soft_close.approve','deny',
            target_user=reviewer)
        self.approve_permission_048(tenant,reviewer,denied)
        self.soft_request_049(tenant,owner,period)
        with self.assertRaises(self.psycopg.Error):
            self.soft_approve_049(tenant,reviewer,period)
        restored=self.propose_permission_048(
            tenant,owner,'finance.period.soft_close.approve','allow',
            target_user=reviewer)
        self.approve_permission_048(tenant,reviewer,restored)
        self.soft_approve_049(tenant,reviewer,period)
        revoked=self.propose_permission_048(
            tenant,owner,'finance.period.soft_adjust','deny',
            target_user=owner)
        self.approve_permission_048(tenant,reviewer,revoked)
        with self.assertRaises(self.psycopg.Error):
            self.adjust_request_049(tenant,owner,period)
        granted=self.propose_permission_048(
            tenant,owner,'finance.period.soft_adjust','allow',
            target_user=owner)
        self.approve_permission_048(tenant,reviewer,granted)
        request,journal=self.adjust_request_049(tenant,owner,period)
        deny_approval=self.propose_permission_048(
            tenant,owner,'finance.period.soft_adjust.approve','deny',
            target_user=reviewer)
        self.approve_permission_048(tenant,reviewer,deny_approval)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,reviewer,
                "SELECT one_approve_soft_adjustment(%s,%s,%s)",
                (tenant,request,reviewer))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_soft_adjustment_approvals "
            "WHERE tenant_id=%s AND request_id=%s",
            (tenant,request)).fetchone()[0],0)

    def test_049_soft_close_can_be_finalized_but_hard_close_stays_immutable(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        self.soft_request_049(tenant,owner,period)
        self.soft_approve_049(tenant,reviewer,period)
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_close_period(%s,%s,%s)",
            (tenant,period,owner)),period)
        self.assertEqual(self.soft_state_049(
            tenant,reviewer,period)[0],"hard_closed")
        with self.assertRaises(self.psycopg.Error):
            self.adjust_request_049(tenant,owner,period)
        with self.assertRaises(self.psycopg.Error):
            self.soft_approve_049(tenant,reviewer,period)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_period_close_audit "
            "WHERE tenant_id=%s AND period_id=%s",
            (tenant,period)).fetchone()[0],1)

    def test_049_soft_close_rejects_existing_unposted_journals(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        journal=uuid4()
        self.commerce_execute(
            tenant,owner,
            "INSERT INTO one_journal_entries("
            "tenant_id,id,period_id,reference,idempotency_key,"
            "occurred_on,currency) VALUES(%s,%s,%s,'Draft for preflight',%s,%s,'SAR') "
            "RETURNING id",
            (tenant,journal,period,str(uuid4()),date.today()))
        with self.assertRaises(self.psycopg.Error):
            self.soft_request_049(tenant,owner,period)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_period_soft_close_requests "
            "WHERE tenant_id=%s AND period_id=%s",
            (tenant,period)).fetchone()[0],0)


    # 050 | Costed multi-line adjustments and per-person, per-currency caps.
    def multi_request_050(self,tenant,actor,period,lines,*,currency="SAR"):
        import json
        request,journal=uuid4(),uuid4()
        result=self.commerce_execute(
            tenant,actor,
            "SELECT one_request_soft_adjustment_lines("
            "%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
            (tenant,request,period,journal,date.today(),currency,
             json.dumps(lines),
             "Four-line accrued services late adjusting entry",actor))
        self.assertEqual(result,request)
        return request,journal

    def preview_050(self,tenant,actor,request):
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            return self.db.execute(
                "SELECT * FROM one_soft_adjustment_impact_preview(%s,%s,%s)",
                (tenant,request,actor)).fetchall()

    def propose_limit_050(self,tenant,author,target,amount,currency="SAR"):
        request=uuid4()
        result=self.commerce_execute(
            tenant,author,
            "SELECT one_request_soft_adjust_limit("
            "%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant,request,target,currency,Decimal(str(amount)),
             date.today(),None,author))
        self.assertEqual(result,request)
        return request

    def approve_limit_050(self,tenant,actor,request):
        return self.commerce_execute(
            tenant,actor,
            "SELECT one_approve_soft_adjust_limit(%s,%s,%s)",
            (tenant,request,actor))

    def test_050_multi_line_approval_evidence_and_per_account_preview(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        self.soft_request_049(tenant,owner,period)
        self.soft_approve_049(tenant,reviewer,period)
        lines=[
            {"account_id":"cogs","debit":30,"credit":0},
            {"account_id":"inventory","debit":20,"credit":0},
            {"account_id":"grni","debit":0,"credit":40},
            {"account_id":"inventory","debit":0,"credit":10},
        ]
        request,journal=self.multi_request_050(tenant,owner,period,lines)
        self.assertEqual(self.commerce_execute(
            tenant,owner,
            "SELECT one_actor_soft_adjust_limit(%s,%s,'SAR')",
            (tenant,owner)),Decimal("100000"))
        preview=self.preview_050(tenant,reviewer,request)
        self.assertEqual(len(preview),3)
        self.assertEqual({r[0]:r[6] for r in preview},
                         {"cogs":Decimal("30"),"grni":Decimal("-40"),
                          "inventory":Decimal("10")})
        self.assertTrue(all(not r[7] for r in preview))
        with self.assertRaises(self.psycopg.Error):
            self.preview_050(tenant,uuid4(),request)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,"SELECT one_post_journal(%s,%s,%s)",
                (tenant,journal,owner))
        self.assertEqual(self.commerce_execute(
            tenant,reviewer,"SELECT one_approve_soft_adjustment(%s,%s,%s)",
            (tenant,request,reviewer)),request)
        sealed=self.admin.execute(
            "SELECT journal_lines,total_debit,total_credit,ledger_fingerprint "
            "FROM one_soft_adjustment_approval_evidence "
            "WHERE tenant_id=%s AND request_id=%s",
            (tenant,request)).fetchone()
        self.assertEqual(sealed[:3],(4,Decimal("50"),Decimal("50")))
        self.assertEqual(len(sealed[3]),32)
        self.assertTrue(all(r[7] for r in
                            self.preview_050(tenant,owner,request)))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "UPDATE one_soft_adjustment_approval_evidence "
                "SET total_debit=1 WHERE tenant_id=%s AND request_id=%s "
                "RETURNING request_id",
                (tenant,request))
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_post_journal(%s,%s,%s)",
            (tenant,journal,owner)),journal)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_soft_adjustment_post_audit "
            "WHERE tenant_id=%s AND request_id=%s",
            (tenant,request)).fetchone()[0],1)

    def test_050_multiline_rejects_invalid_accounts_unbalanced_and_bad_shape(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        self.soft_request_049(tenant,owner,period)
        self.soft_approve_049(tenant,reviewer,period)
        valid=[{"account_id":"cogs","debit":10,"credit":0},
               {"account_id":"inventory","debit":0,"credit":10}]
        invalid_cases=[
            [{"account_id":"cogs","debit":10,"credit":0},
             {"account_id":"inventory","debit":0,"credit":9}],
            [{"account_id":"missing","debit":10,"credit":0},
             {"account_id":"inventory","debit":0,"credit":10}],
            [{"account_id":"cogs","debit":1.234,"credit":0},
             {"account_id":"inventory","debit":0,"credit":1.234}],
            [{"account_id":"cogs","debit":"10","credit":0},
             {"account_id":"inventory","debit":0,"credit":10}],
            [dict(valid[0],extra_field=0),valid[1]],
            valid[:1],
        ]
        for payload in invalid_cases:
            before=self.admin.execute(
                "SELECT count(*) FROM one_soft_adjustment_requests "
                "WHERE tenant_id=%s",(tenant,)).fetchone()[0]
            with self.assertRaises(self.psycopg.Error):
                self.multi_request_050(tenant,owner,period,payload)
            self.assertEqual(self.admin.execute(
                "SELECT count(*) FROM one_soft_adjustment_requests "
                "WHERE tenant_id=%s",(tenant,)).fetchone()[0],before)
        # Non-SAR currencies cannot silently reuse SAR-denominated limits.
        with self.assertRaises(self.psycopg.Error):
            self.multi_request_050(tenant,owner,period,valid,currency="USD")

    def test_050_reviewer_amount_cap_and_changed_requester_cap_at_post(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        second=uuid4()
        self.admin.execute(
            "INSERT INTO one_memberships(tenant_id,actor_id,role) "
            "VALUES(%s,%s,'accountant')",(tenant,second))
        self.soft_request_049(tenant,owner,period)
        self.soft_approve_049(tenant,reviewer,period)
        lines=[{"account_id":"cogs","debit":30000,"credit":0},
               {"account_id":"grni","debit":0,"credit":30000}]
        request,journal=self.multi_request_050(tenant,owner,period,lines)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,reviewer,
                "SELECT one_approve_soft_adjustment(%s,%s,%s)",
                (tenant,request,reviewer))
        raise_reviewer=self.propose_limit_050(tenant,owner,reviewer,50000)
        with self.assertRaises(self.psycopg.Error):
            self.approve_limit_050(tenant,owner,raise_reviewer)
        with self.assertRaises(self.psycopg.Error):
            self.approve_limit_050(tenant,reviewer,raise_reviewer)
        self.assertEqual(self.approve_limit_050(
            tenant,second,raise_reviewer),raise_reviewer)
        self.assertEqual(self.commerce_execute(
            tenant,reviewer,"SELECT one_actor_soft_adjust_limit(%s,%s,'SAR')",
            (tenant,reviewer)),Decimal("50000"))
        self.commerce_execute(
            tenant,reviewer,"SELECT one_approve_soft_adjustment(%s,%s,%s)",
            (tenant,request,reviewer))
        lower_owner=self.propose_limit_050(tenant,owner,owner,10000)
        self.approve_limit_050(tenant,reviewer,lower_owner)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,"SELECT one_post_journal(%s,%s,%s)",
                (tenant,journal,owner))
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_journal_entries "
            "WHERE tenant_id=%s AND id=%s",(tenant,journal)).fetchone()[0],
            "draft")
        restore_owner=self.propose_limit_050(tenant,owner,owner,100000)
        self.approve_limit_050(tenant,reviewer,restore_owner)
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_post_journal(%s,%s,%s)",
            (tenant,journal,owner)),journal)

    def test_050_original_two_line_adjustments_also_obey_amount_gate(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        self.soft_request_049(tenant,owner,period)
        self.soft_approve_049(tenant,reviewer,period)
        request,journal=self.adjust_request_049(
            tenant,owner,period,amount="20000.00")
        self.assertEqual(self.commerce_execute(
            tenant,reviewer,"SELECT one_approve_soft_adjustment(%s,%s,%s)",
            (tenant,request,reviewer)),request)
        self.assertEqual(self.admin.execute(
            "SELECT journal_lines,total_debit "
            "FROM one_soft_adjustment_approval_evidence "
            "WHERE tenant_id=%s AND request_id=%s",
            (tenant,request)).fetchone(),(2,Decimal("20000")))
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_post_journal(%s,%s,%s)",
            (tenant,journal,owner)),journal)


    # 051 | Financial statements show real POSTED accounting amounts separately
    # from adjustment what-if amounts, frozen approval basis and later GL drift.
    def statement_051(self,tenant,actor,request,*,drift=False):
        fn=("one_soft_adjustment_statement_drift" if drift else
            "one_soft_adjustment_statement_comparison")
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(actor),))
            rows=self.db.execute(
                "SELECT * FROM "+fn+"(%s,%s,%s)",
                (tenant,request,actor)).fetchall()
        return {row[0]:row for row in rows}

    def test_051_ledger_comparison_separates_posted_assets_profit_and_proposal(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        receipt=uuid4()
        self.commerce_execute(
            tenant,owner,
            "SELECT one_fulfill_commercial_line(%s,%s,1,5,%s,%s)",
            (tenant,purchase,receipt,owner))
        self.value_stock_event(
            tenant,owner,receipt,"grni",period_id=period)
        self.soft_request_049(tenant,owner,period)
        self.soft_approve_049(tenant,reviewer,period)
        request,journal=self.multi_request_050(
            tenant,owner,period,[
             {"account_id":"cogs","debit":30,"credit":0},
             {"account_id":"inventory","debit":20,"credit":0},
             {"account_id":"grni","debit":0,"credit":40},
             {"account_id":"inventory","debit":0,"credit":10}])
        result=self.statement_051(tenant,reviewer,request)
        self.assertEqual(len(result),7)
        self.assertEqual(result["assets"][3:6],
                         (Decimal("62.50"),Decimal("10"),
                          Decimal("72.50")))
        self.assertEqual(result["liabilities"][3:6],
                         (Decimal("62.50"),Decimal("40"),
                          Decimal("102.50")))
        self.assertEqual(result["expenses"][3:6],
                         (Decimal("0"),Decimal("30"),
                          Decimal("30")))
        self.assertEqual(result["net_income"][3:6],
                         (Decimal("0"),Decimal("-30"),
                          Decimal("-30")))
        self.assertEqual(result["accounting_equation_gap"][3:6],
                         (Decimal("0"),Decimal("0"),Decimal("0")))
        self.assertEqual(result["net_income"][6:8],
                         ("draft","LIVE_PERIOD_POSTED_GL_WITH_DRAFT_WHAT_IF"))
        with self.assertRaises(self.psycopg.Error):
            self.statement_051(tenant,uuid4(),request)
        with self.assertRaises(self.psycopg.Error):
            self.statement_051(self.tenant_b,reviewer,request)
        with self.assertRaises(self.psycopg.Error):
            self.statement_051(tenant,owner,request,drift=True)
        self.assertEqual(self.commerce_execute(
            tenant,reviewer,"SELECT one_approve_soft_adjustment(%s,%s,%s)",
            (tenant,request,reviewer)),request)
        frozen=self.statement_051(tenant,owner,request,drift=True)
        self.assertEqual(len(frozen),7)
        self.assertEqual(frozen["net_income"][3:10],
                         (Decimal("0"),Decimal("-30"),Decimal("-30"),
                          Decimal("0"),Decimal("-30"),
                          Decimal("-30"),Decimal("0")))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_soft_adjustment_statement_approval_seals "
            "WHERE tenant_id=%s AND request_id=%s",
            (tenant,request)).fetchone()[0],7)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "UPDATE one_soft_adjustment_statement_approval_seals "
                "SET before_at_approval=1 WHERE tenant_id=%s AND request_id=%s "
                "RETURNING section",(tenant,request))
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_post_journal(%s,%s,%s)",
            (tenant,journal,owner)),journal)
        posted=self.statement_051(tenant,reviewer,request)
        self.assertEqual(posted["assets"][3:6],result["assets"][3:6])
        self.assertEqual(posted["assets"][6],"posted")
        self.assertEqual(posted["assets"][7],
                         "CURRENT_PERIOD_POSTED_GL_EXCLUDING_THIS_POSTED_ADJUSTMENT")
        self.assertEqual(self.statement_051(
            tenant,owner,request,drift=True)["net_income"][9],0)

    def test_051_approval_baseline_remains_frozen_after_other_journal_posts(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        self.soft_request_049(tenant,owner,period)
        self.soft_approve_049(tenant,reviewer,period)
        first,first_journal=self.adjust_request_049(
            tenant,owner,period,amount="12.50")
        self.commerce_execute(
            tenant,reviewer,"SELECT one_approve_soft_adjustment(%s,%s,%s)",
            (tenant,first,reviewer))
        sealed_first=self.statement_051(tenant,owner,first,drift=True)
        self.assertEqual(sealed_first["liabilities"][3],Decimal("0"))
        later,later_journal=self.adjust_request_049(
            tenant,owner,period,amount="7.50")
        self.commerce_execute(
            tenant,reviewer,"SELECT one_approve_soft_adjustment(%s,%s,%s)",
            (tenant,later,reviewer))
        self.commerce_execute(
            tenant,owner,"SELECT one_post_journal(%s,%s,%s)",
            (tenant,later_journal,owner))
        drift=self.statement_051(tenant,reviewer,first,drift=True)
        self.assertEqual(drift["liabilities"][3],Decimal("0"))
        self.assertEqual(drift["liabilities"][6],Decimal("7.50"))
        self.assertEqual(drift["liabilities"][9],Decimal("7.50"))
        self.assertEqual(drift["net_income"][9],Decimal("-7.50"))
        self.assertEqual(drift["accounting_equation_gap"][9],Decimal("0"))
        self.assertEqual(drift["liabilities"][5],Decimal("12.50"))
        self.assertEqual(drift["liabilities"][8],Decimal("20.00"))
        self.assertEqual(drift["liabilities"][10],"draft")
        self.commerce_execute(
            tenant,owner,"SELECT one_post_journal(%s,%s,%s)",
            (tenant,first_journal,owner))
        posted=self.statement_051(tenant,reviewer,first,drift=True)
        self.assertEqual(posted["liabilities"][9],Decimal("7.50"))
        self.assertEqual(posted["liabilities"][10],"posted")
        self.assertEqual(self.admin.execute(
            "SELECT projected_at_approval FROM "
            "one_soft_adjustment_statement_approval_seals "
            "WHERE tenant_id=%s AND request_id=%s AND section='liabilities'",
            (tenant,first)).fetchone()[0],Decimal("12.50"))

    def test_051_financial_report_profit_is_revenue_less_expenses(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        self.admin.execute(
            "INSERT INTO one_accounts(tenant_id,id,code,name,kind) "
            "VALUES(%s,'revenue','4190','Revenue for statement CI','revenue')",
            (tenant,))
        old_entry=uuid4()
        with self.db.transaction():
            self.db.execute("SELECT set_config('one.tenant_id',%s,true)",
                            (str(tenant),))
            self.db.execute("SELECT set_config('one.actor_id',%s,true)",
                            (str(owner),))
            self.db.execute(
                "INSERT INTO one_journal_entries(tenant_id,id,period_id,"
                "reference,idempotency_key,occurred_on,currency) "
                "VALUES(%s,%s,%s,'Recognized revenue',%s,%s,'SAR')",
                (tenant,old_entry,period,str(uuid4()),date.today()))
            self.db.execute(
                "INSERT INTO one_journal_lines(tenant_id,entry_id,"
                "line_number,account_id,debit,credit) VALUES"
                "(%s,%s,1,'inventory',100,0),"
                "(%s,%s,2,'revenue',0,100)",
                (tenant,old_entry,tenant,old_entry))
            self.db.execute("SELECT one_post_journal(%s,%s,%s)",
                            (tenant,old_entry,owner))
        self.soft_request_049(tenant,owner,period)
        self.soft_approve_049(tenant,reviewer,period)
        request,journal=self.adjust_request_049(
            tenant,owner,period,amount="20.00")
        vals=self.statement_051(tenant,owner,request)
        self.assertEqual(vals["revenue"][3:6],
                         (Decimal("100"),Decimal("0"),Decimal("100")))
        self.assertEqual(vals["expenses"][3:6],
                         (Decimal("0"),Decimal("20"),Decimal("20")))
        self.assertEqual(vals["net_income"][3:6],
                         (Decimal("100"),Decimal("-20"),Decimal("80")))
        self.assertEqual(vals["assets"][5],Decimal("100"))
        self.assertEqual(vals["liabilities"][5],Decimal("20"))
        self.assertEqual(vals["accounting_equation_gap"][5],0)

    def test_051_per_user_report_permission_revocation_blocks_snapshot_access(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        self.soft_request_049(tenant,owner,period)
        self.soft_approve_049(tenant,reviewer,period)
        request,journal=self.adjust_request_049(tenant,owner,period)
        self.commerce_execute(
            tenant,reviewer,"SELECT one_approve_soft_adjustment(%s,%s,%s)",
            (tenant,request,reviewer))
        self.assertEqual(len(self.statement_051(
            tenant,owner,request,drift=True)),7)
        denial=self.propose_permission_048(
            tenant,owner,'finance.period.statement.preview','deny',
            target_user=owner)
        self.approve_permission_048(tenant,reviewer,denial)
        with self.assertRaises(self.psycopg.Error):
            self.statement_051(tenant,owner,request)
        with self.assertRaises(self.psycopg.Error):
            self.statement_051(tenant,owner,request,drift=True)
        self.assertEqual(len(self.statement_051(
            tenant,reviewer,request,drift=True)),7)


    # 052: Real RS256 bearer -> control-plane subject mapping -> tenant-scoped
    # WSGI command -> PostgreSQL RLS + 048/049/050 financial permissions.
    def test_052_signed_http_actor_binds_real_period_and_adjustment_posting(self):
        import json
        import jwt
        from io import BytesIO
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        from one_erp_core.finance_commands_wsgi import OneFinanceCommandsWSGI
        from one_erp_core.finance_wsgi import TenantFinanceConnections

        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        for actor,label in ((owner,"owner"),(reviewer,"reviewer")):
            self.admin.execute(
                "INSERT INTO one_auth.subject_links(issuer,subject,actor_id) "
                "VALUES('https://identity.example.test',%s,%s)",
                ("052-"+label+"-"+tenant.hex,actor))
            self.admin.execute(
                "INSERT INTO one_auth.tenant_entitlements(actor_id,tenant_id) "
                "VALUES(%s,%s)",(actor,tenant))
        self.admin.execute(
            "INSERT INTO one_auth.tenant_registry(tenant_id,active) "
            "VALUES(%s,true)",(tenant,))
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        private_key=key.private_bytes(
            serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
        public_key=key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        verifier=VerifiedRS256FinanceAccess(
            RS256VerifierConfig(
                issuer="https://identity.example.test",
                audience="one-erp-finance-api",
                signing_keys_by_id={"052-ci":public_key},
            ),
            ControlPlaneIdentityDirectory(self.auth_connection),
            ControlPlaneRevocationChecker(self.auth_connection),
        )
        now=int(datetime.now(timezone.utc).timestamp())
        def signed(label):
            subject="052-"+label+"-"+tenant.hex
            return jwt.encode({
                "iss":"https://identity.example.test",
                "aud":"one-erp-finance-api","sub":subject,
                "iat":now-5,"nbf":now-5,"exp":now+240,
                "jti":str(uuid4()),"token_use":"access",
            },private_key,algorithm="RS256",headers={"kid":"052-ci"})
        def tenant_connection():
            return self.psycopg.connect(
                host=os.environ.get("ONE_TEST_PG_HOST","localhost"),
                port=int(os.environ.get("ONE_TEST_PG_PORT","5432")),
                dbname=os.environ.get("ONE_TEST_PG_DB","one_erp_test"),
                user="one_erp_command_ci",password="ci_command_only",
                autocommit=True,
            )
        app=OneFinanceCommandsWSGI(
            verifier=verifier,
            connections=TenantFinanceConnections({tenant:tenant_connection}),
            allowed_hosts=frozenset({"finance.example.test"}),
        )
        def call(label, payload):
            body=json.dumps(payload).encode()
            env={"REQUEST_METHOD":"POST","wsgi.url_scheme":"https",
                 "HTTP_HOST":"finance.example.test",
                 "PATH_INFO":"/api/v1/finance/commands",
                 "CONTENT_TYPE":"application/json",
                 "CONTENT_LENGTH":str(len(body)),
                 "wsgi.input":BytesIO(body),
                 "HTTP_AUTHORIZATION":"Bearer "+signed(label),
                 "QUERY_STRING":""}
            results={}
            def send(status,headers):
                results["status"]=status
                results["headers"]=dict(headers)
            returned=b"".join(app(env,send))
            return results["status"],json.loads(returned)
        base={"tenant":str(tenant),"period_id":str(period)}
        req=dict(base,action="request_soft_close")
        status,value=call("owner",req)
        self.assertEqual((status,value["id"]),("200 OK",str(period)))
        status,_=call("reviewer",req)
        self.assertEqual(status,"409 Conflict")
        status,_=call("owner",dict(req,actor_id=str(reviewer)))
        self.assertEqual(status,"400 Bad Request")
        status,_=call("owner",dict(req,tenant=str(uuid4())))
        self.assertEqual(status,"403 Forbidden")
        status,value=call("reviewer",dict(base,action="approve_soft_close"))
        self.assertEqual((status,value["id"]),("200 OK",str(period)))
        request,journal=uuid4(),uuid4()
        proposal=dict(base,action="request_soft_adjustment",
                      request_id=str(request),journal_id=str(journal),
                      occurred_on=date.today().isoformat(),currency="SAR",
                      reason="Reviewed and documented closing correction",
                      lines=[{"account_id":"cogs","debit":15.50,"credit":0},
                             {"account_id":"grni","debit":0,"credit":15.50}])
        status,value=call("owner",proposal)
        self.assertEqual((status,value["id"]),("200 OK",str(request)))
        status,_=call("owner",{
            "action":"post_soft_adjustment","tenant":str(tenant),
            "journal_id":str(journal)})
        self.assertIn(status,("403 Forbidden","409 Conflict"))
        status,value=call("reviewer",{
            "action":"approve_soft_adjustment","tenant":str(tenant),
            "request_id":str(request)})
        self.assertEqual((status,value["id"]),("200 OK",str(request)))
        status,_=call("reviewer",{
            "action":"post_soft_adjustment","tenant":str(tenant),
            "journal_id":str(journal)})
        self.assertEqual(status,"403 Forbidden")
        status,value=call("owner",{
            "action":"post_soft_adjustment","tenant":str(tenant),
            "journal_id":str(journal)})
        self.assertEqual((status,value["id"]),("200 OK",str(journal)))
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_journal_entries "
            "WHERE tenant_id=%s AND id=%s",(tenant,journal)
        ).fetchone()[0],"posted")
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_soft_adjustment_post_audit "
            "WHERE tenant_id=%s AND journal_id=%s AND posted_by=%s",
            (tenant,journal,owner)
        ).fetchone()[0],1)


    def command_role_053(self):
        return self.psycopg.connect(
            host=os.environ.get("ONE_TEST_PG_HOST","localhost"),
            port=int(os.environ.get("ONE_TEST_PG_PORT","5432")),
            dbname=os.environ.get("ONE_TEST_PG_DB","one_erp_test"),
            user="one_erp_command_ci",password="ci_command_only",
            autocommit=True,
        )

    def test_053_command_login_has_grants_only_for_finance_workflow(self):
        with self.command_role_053() as db:
            grants=db.execute(
                "SELECT rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,"
                "rolcreaterole FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            self.assertEqual(grants,(True,False,False,False,False))
            self.assertEqual(db.execute(
                "SELECT has_schema_privilege(current_user,'public','CREATE')"
            ).fetchone()[0],False)
            for name in (
                "one_tenants","one_memberships","one_invoice_docs",
                "one_payment_docs","one_stock_events",
                "one_permission_change_requests",
                "one_soft_adjustment_approval_evidence",
                "one_soft_adjustment_statement_approval_seals",
                "one_ledger_audit",
            ):
                self.assertFalse(db.execute(
                    "SELECT has_table_privilege(current_user,%s,'DELETE')",
                    (name,)).fetchone()[0],name)
                self.assertFalse(db.execute(
                    "SELECT has_table_privilege(current_user,%s,'TRUNCATE')",
                    (name,)).fetchone()[0],name)
            for name in (
                "one_memberships","one_invoice_docs","one_payment_docs",
                "one_stock_events","one_permission_change_requests",
            ):
                self.assertFalse(db.execute(
                    "SELECT has_table_privilege(current_user,%s,'INSERT')",
                    (name,)).fetchone()[0],name)
                self.assertFalse(db.execute(
                    "SELECT has_table_privilege(current_user,%s,'UPDATE')",
                    (name,)).fetchone()[0],name)
            self.assertFalse(db.execute(
                "SELECT has_table_privilege("
                "current_user,'one_invoice_docs','SELECT')"
            ).fetchone()[0])
            self.assertFalse(db.execute(
                "SELECT has_table_privilege("
                "current_user,'one_stock_balances','SELECT')"
            ).fetchone()[0])
            self.assertTrue(db.execute(
                "SELECT has_table_privilege("
                "current_user,'one_soft_adjustment_requests','INSERT')"
            ).fetchone()[0])
            self.assertTrue(db.execute(
                "SELECT has_column_privilege(current_user,"
                "'one_journal_entries','status','UPDATE')"
            ).fetchone()[0])
            self.assertFalse(db.execute(
                "SELECT has_column_privilege(current_user,"
                "'one_journal_entries','occurred_on','UPDATE')"
            ).fetchone()[0])
            self.assertFalse(db.execute(
                "SELECT has_function_privilege(current_user,"
                "'one_close_period(uuid,uuid,uuid)','EXECUTE')"
            ).fetchone()[0])
            self.assertFalse(db.execute(
                "SELECT has_function_privilege(current_user,"
                "'one_request_permission_change("
                "uuid,uuid,uuid,text,text,text,date,date,uuid)','EXECUTE')"
            ).fetchone()[0])
            self.assertTrue(db.execute(
                "SELECT has_function_privilege(current_user,"
                "'one_post_journal(uuid,uuid,uuid)','EXECUTE')"
            ).fetchone()[0])
            with self.assertRaises(self.psycopg.Error):
                db.execute("CREATE TABLE unauthorized_command_probe(x integer)")

    def test_053_command_role_blocks_other_company_reads_and_direct_mutation(self):
        tenant,owner,reviewer,product,warehouse,period,purchase,sale=(
            self.inventory_history_046_fixture())
        with self.command_role_053() as db:
            with db.transaction():
                db.execute("SELECT set_config('one.tenant_id',%s,true)",
                           (str(tenant),))
                db.execute("SELECT set_config('one.actor_id',%s,true)",
                           (str(owner),))
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM one_memberships "
                    "WHERE tenant_id=%s",(tenant,)).fetchone()[0],2)
                self.assertFalse(db.execute(
                    "SELECT EXISTS(SELECT 1 FROM one_memberships "
                    "WHERE tenant_id=%s)",(self.tenant_a,)).fetchone()[0])
                self.assertTrue(db.execute(
                    "SELECT one_actor_has_permission(%s,%s,"
                    "'finance.period.soft_close.request')",
                    (tenant,owner)).fetchone()[0])
            with self.assertRaises(self.psycopg.Error):
                db.execute(
                    "INSERT INTO one_memberships(tenant_id,actor_id,role)"
                    " VALUES(%s,%s,'company_admin')",(tenant,uuid4()))
            with self.assertRaises(self.psycopg.Error):
                db.execute("SELECT * FROM one_invoice_docs")
            with self.assertRaises(self.psycopg.Error):
                db.execute("DELETE FROM one_journal_entries")


    def test_054_public_execute_is_denied_and_command_role_is_exactly_allowlisted(self):
        # Full effective privilege scan catches PUBLIC, role memberships,
        # indirect grants, not just direct one_erp_finance_command_executor ACL.
        allowed=frozenset((
            "one_actor_has_permission",
            "one_actor_soft_adjust_limit",
            "one_request_period_soft_close",
            "one_approve_period_soft_close",
            "one_request_soft_adjustment_lines",
            "one_approve_soft_adjustment",
            "one_post_journal",
            "one_soft_adjustment_statement_comparison",
        ))
        statements=self.admin.execute(
            """
            SELECT p.proname,
                has_function_privilege(
                    'one_erp_finance_command_executor',p.oid,'EXECUTE'),
                EXISTS (
                  SELECT 1 FROM aclexplode(
                    coalesce(p.proacl,acldefault('f',p.proowner))) a
                   WHERE a.grantee=0 AND a.privilege_type='EXECUTE')
             FROM pg_proc p JOIN pg_namespace ns ON ns.oid=p.pronamespace
            WHERE ns.nspname='public'
            """
        ).fetchall()
        self.assertGreater(len(statements),80)
        permitted={name for name,can_run,_ in statements if can_run}
        self.assertEqual(permitted,allowed)
        self.assertFalse(any(public_can for _,_,public_can in statements))
        with self.command_role_053() as db:
            self.assertTrue(db.execute(
                "SELECT has_function_privilege(current_user,"
                "'public.one_post_journal(uuid,uuid,uuid)','EXECUTE')"
            ).fetchone()[0])
            for signature in (
                "public.one_close_period(uuid,uuid,uuid)",
                "public.one_issue_invoice(uuid,uuid,uuid)",
                "public.one_request_permission_change("
                "uuid,uuid,uuid,text,text,text,date,date,uuid)",
            ):
                self.assertFalse(db.execute(
                    "SELECT has_function_privilege(current_user,"
                    "%s,'EXECUTE')",(signature,)
                ).fetchone()[0],signature)

    def test_054_future_default_routines_are_not_public_callable(self):
        # Newly created routines inherit the NEW owner-specific global
        # defaults and cannot silently expose more administrative actions.
        self.admin.execute(
            "CREATE FUNCTION public.one_ci_054_future_private() "
            "RETURNS integer LANGUAGE sql AS $$SELECT 1$$")
        try:
            self.assertFalse(self.admin.execute(
                "SELECT has_function_privilege("
                "'one_erp_finance_command_executor',"
                "'public.one_ci_054_future_private()','EXECUTE')"
            ).fetchone()[0])
            self.assertFalse(self.admin.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_proc p "
                "WHERE p.oid='public.one_ci_054_future_private()'::regprocedure "
                "AND EXISTS(SELECT 1 FROM aclexplode("
                "coalesce(p.proacl,acldefault('f',p.proowner))) a "
                "WHERE a.grantee=0 AND a.privilege_type='EXECUTE'))"
            ).fetchone()[0])
        finally:
            self.admin.execute(
                "DROP FUNCTION public.one_ci_054_future_private()")

    def test_054_public_grant_leak_is_detectable_before_deployment(self):
        # Simulates a migration accidentally restoring PUBLIC EXECUTE:
        # effective SQL privilege audit must detect leakage, then restore
        # the CI database state in a finally block.
        signature="public.one_close_period(uuid,uuid,uuid)"
        self.assertFalse(self.admin.execute(
            "SELECT has_function_privilege("
            "'one_erp_finance_command_executor',%s,'EXECUTE')",
            (signature,)).fetchone()[0])
        self.admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "public.one_close_period(uuid,uuid,uuid) TO PUBLIC")
        try:
            self.assertTrue(self.admin.execute(
                "SELECT has_function_privilege("
                "'one_erp_finance_command_executor',%s,'EXECUTE')",
                (signature,)).fetchone()[0])
        finally:
            self.admin.execute(
                "REVOKE EXECUTE ON FUNCTION "
                "public.one_close_period(uuid,uuid,uuid) FROM PUBLIC")
        self.assertFalse(self.admin.execute(
            "SELECT has_function_privilege("
            "'one_erp_finance_command_executor',%s,'EXECUTE')",
            (signature,)).fetchone()[0])


    # 055 | Source-event permission gates protect old commerce APIs and their
    # accounting/physical stock side effects; existing admin-only checks
    # remain, so new ALLOW does not yet delegate through those legacy paths.
    def source_permission_055(self,tenant,owner,reviewer,permission,decision):
        proposal=self.propose_permission_048(
            tenant,owner,permission,decision,target_user=owner)
        self.assertEqual(self.approve_permission_048(
            tenant,reviewer,proposal),proposal)
        return proposal

    def commerce_055_reviewers(self,tenant):
        reviewer=uuid4()
        self.admin.execute(
            "INSERT INTO one_memberships(tenant_id,actor_id,role) "
            "VALUES(%s,%s,'accountant')",(tenant,reviewer))
        return reviewer

    def test_055_sales_purchase_confirmation_are_separate_user_rights(self):
        tenant,owner,product,warehouse,purchase,sale=self.commerce_fixture()
        reviewer=self.commerce_055_reviewers(tenant)
        self.source_permission_055(
            tenant,owner,reviewer,'sales.order.confirm','deny')
        self.assertFalse(self.has_permission_048(
            tenant,owner,'sales.order.confirm'))
        self.assertTrue(self.has_permission_048(
            tenant,owner,'purchase.order.confirm'))
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,sale,owner))
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_commercial_orders "
            "WHERE tenant_id=%s AND id=%s",(tenant,sale)
        ).fetchone()[0],"draft")
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,purchase,owner)),purchase)
        self.source_permission_055(
            tenant,owner,reviewer,'sales.order.confirm','allow')
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_confirm_commercial_order(%s,%s,%s)",
            (tenant,sale,owner)),sale)
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                self.tenant_b,owner,
                "SELECT one_source_actor_permitted(%s,%s,%s)",
                (tenant,owner,'sales.order.confirm'))

    def test_055_receipt_and_dispatch_denials_roll_back_stock_and_order(self):
        tenant,owner,product,warehouse,purchase,sale=self.commerce_fixture()
        reviewer=self.commerce_055_reviewers(tenant)
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,owner,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,owner))
        deny_receipt=self.source_permission_055(
            tenant,owner,reviewer,'inventory.purchase.receive','deny')
        receipt=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
                (tenant,purchase,receipt,owner))
        self.assertEqual(self.admin.execute(
            "SELECT fulfilled_qty FROM one_commercial_order_lines "
            "WHERE tenant_id=%s AND order_id=%s AND line_no=1",
            (tenant,purchase)).fetchone()[0],Decimal("0"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_events "
            "WHERE tenant_id=%s AND id=%s",(tenant,receipt)).fetchone()[0],0)
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],0)
        self.source_permission_055(
            tenant,owner,reviewer,'inventory.purchase.receive','allow')
        self.assertEqual(self.commerce_execute(
            tenant,owner,
            "SELECT one_fulfill_commercial_line(%s,%s,1,3,%s,%s)",
            (tenant,purchase,receipt,owner)),receipt)
        self.source_permission_055(
            tenant,owner,reviewer,'inventory.sale.dispatch','deny')
        dispatch=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
                (tenant,sale,dispatch,owner))
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances WHERE "
            "tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("3"))
        self.assertEqual(self.admin.execute(
            "SELECT fulfilled_qty FROM one_commercial_order_lines "
            "WHERE tenant_id=%s AND order_id=%s AND line_no=1",
            (tenant,sale)).fetchone()[0],Decimal("0"))
        self.assertFalse(self.admin.execute(
            "SELECT EXISTS(SELECT 1 FROM one_stock_events "
            "WHERE tenant_id=%s AND id=%s)",
            (tenant,dispatch)).fetchone()[0])
        self.source_permission_055(
            tenant,owner,reviewer,'inventory.sale.dispatch','allow')
        self.assertEqual(self.commerce_execute(
            tenant,owner,
            "SELECT one_fulfill_commercial_line(%s,%s,1,2,%s,%s)",
            (tenant,sale,dispatch,owner)),dispatch)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("1"))

    def test_055_warehouse_reservation_permission_separate_from_dispatch(self):
        tenant,owner,product,warehouse,purchase,sale=self.commerce_fixture()
        reviewer=self.commerce_055_reviewers(tenant)
        for order in (purchase,sale):
            self.commerce_execute(
                tenant,owner,"SELECT one_confirm_commercial_order(%s,%s,%s)",
                (tenant,order,owner))
        self.commerce_execute(
            tenant,owner,
            "SELECT one_fulfill_commercial_line(%s,%s,1,4,%s,%s)",
            (tenant,purchase,uuid4(),owner))
        self.source_permission_055(
            tenant,owner,reviewer,'inventory.sale.reserve','deny')
        reservation=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "SELECT one_reserve_sale_line(%s,%s,1,2,%s,%s)",
                (tenant,sale,reservation,owner))
        self.assertEqual(self.admin.execute(
            "SELECT reserved FROM one_stock_balances WHERE tenant_id=%s "
            "AND warehouse_id=%s AND product_id=%s",
            (tenant,warehouse,product)).fetchone()[0],Decimal("0"))
        self.assertEqual(self.admin.execute(
            "SELECT reserved_qty FROM one_commercial_order_lines WHERE "
            "tenant_id=%s AND order_id=%s AND line_no=1",
            (tenant,sale)).fetchone()[0],Decimal("0"))
        self.source_permission_055(
            tenant,owner,reviewer,'inventory.sale.reserve','allow')
        self.assertEqual(self.commerce_execute(
            tenant,owner,
            "SELECT one_reserve_sale_line(%s,%s,1,2,%s,%s)",
            (tenant,sale,reservation,owner)),reservation)

    def test_055_customer_return_request_and_review_separately_restrict(self):
        tenant,owner,product,warehouse,purchase,sale=(
            self.prepare_outbound_delivery())
        reviewer=self.commerce_055_reviewers(tenant)
        delivery,dispatch,quarantine=uuid4(),uuid4(),uuid4()
        self.admin.execute(
            "INSERT INTO one_warehouses("
            "tenant_id,id,code,name,warehouse_type) "
            "VALUES(%s,%s,%s,'QA storage','quarantine')",
            (tenant,quarantine,"Q-"+quarantine.hex))
        self.commerce_execute(
            tenant,owner,
            "SELECT one_create_delivery(%s,%s,%s,1,2,%s)",
            (tenant,delivery,sale,owner))
        for stage in ("picked","packed"):
            self.delivery_stage(tenant,owner,delivery,stage)
        self.delivery_stage(tenant,owner,delivery,"dispatched",dispatch)
        request=uuid4()
        self.source_permission_055(
            tenant,owner,reviewer,'sales.return.request','deny')
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,
                "SELECT one_request_return(%s,%s,%s,1,%s,%s,%s)",
                (tenant,request,dispatch,quarantine,owner,"Checked item"))
        self.assertEqual(self.admin.execute(
            "SELECT count(*) FROM one_return_jobs "
            "WHERE tenant_id=%s AND id=%s",(tenant,request)).fetchone()[0],0)
        self.source_permission_055(
            tenant,owner,reviewer,'sales.return.request','allow')
        self.commerce_execute(
            tenant,owner,
            "SELECT one_request_return(%s,%s,%s,1,%s,%s,%s)",
            (tenant,request,dispatch,quarantine,owner,"Checked item"))
        self.source_permission_055(
            tenant,owner,reviewer,'inventory.customer_return.review','deny')
        accepted_event=uuid4()
        with self.assertRaises(self.psycopg.Error):
            self.commerce_execute(
                tenant,owner,"SELECT one_finish_return(%s,%s,1,%s,%s)",
                (tenant,request,accepted_event,owner))
        self.assertEqual(self.admin.execute(
            "SELECT status FROM one_return_jobs WHERE "
            "tenant_id=%s AND id=%s",(tenant,request)).fetchone()[0],
            "requested")
        self.assertFalse(self.admin.execute(
            "SELECT EXISTS(SELECT 1 FROM one_stock_events WHERE "
            "tenant_id=%s AND id=%s)",
            (tenant,accepted_event)).fetchone()[0])
        self.source_permission_055(
            tenant,owner,reviewer,'inventory.customer_return.review','allow')
        self.assertEqual(self.commerce_execute(
            tenant,owner,"SELECT one_finish_return(%s,%s,1,%s,%s)",
            (tenant,request,accepted_event,owner)),request)
        self.assertEqual(self.admin.execute(
            "SELECT on_hand FROM one_stock_balances "
            "WHERE tenant_id=%s AND warehouse_id=%s AND product_id=%s",
            (tenant,quarantine,product)).fetchone()[0],Decimal("1"))

if __name__ == "__main__":
    unittest.main()
