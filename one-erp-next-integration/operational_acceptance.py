"""Read-only test coverage inventory; does NOT execute a customer transaction.

The execution evidence is provided by the PostgreSQL 14 finance CI matrix.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = ROOT / "one-erp-next/tests/test_postgres_integration.py"
REQUIRED_TESTS = {
    "refund_approval": "test_refund_approval_report",
    "sales_dispatch_gl": "test_sales_dispatch_to_invoice_journal_has_traceable_source",
    "sales_invoice_atomicity": "test_sales_invoice_cannot_bill_undelivered_quantity_or_leave_partial_gl",
    "partial_payment": "test_partial_payments_use_db_outstanding_not_stale_python_data",
    "overpayment_control": "test_payment_and_credit_note_cannot_exceed_same_invoice",
    "double_billing": "test_two_competing_invoices_cannot_bill_same_dispatched_stock_twice",
    "stock_finance_atomicity": "test_atomic_delivery_pick_pack_ship_also_posts_cogs",
    "real_postgres_sales_read": "test_056_real_postgres_sales_purchase_inventory_returns_view",
    "parallel_bank": "test_parallel_bank_candidate_submission_has_one_winner_and_no_duplicate",
    "nrv_period": "test_nrv_period_report_recognizes_expense_and_reversal_once",
    "bank_tenant_roles": "test_bank_candidate_enforces_separate_roles_and_tenant",
    "refund_control_pulse": "test_refund_control_pulse_tracks_every_unpaid_stage_without_false_cash_claim",
}
def inventory(path=TEST_FILE):
    """Find callable test methods through AST (never execute source at inspection)."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names
def missing_coverage(path=TEST_FILE):
    found = inventory(path)
    missing = {area: name for area, name in REQUIRED_TESTS.items() if not any(test == name or test.startswith(name + "_") for test in found)}
    return missing
def main():
    missing = missing_coverage()
    if missing:
        for area, name in missing.items():
            print("MISSING COVERAGE", area, name)
        raise SystemExit(1)
    print("Operational risk test inventory PASS: ", ", ".join(sorted(REQUIRED_TESTS)))
    print("This checks test presence, not deployment or live transaction behavior.")
if __name__ == "__main__":
    main()
