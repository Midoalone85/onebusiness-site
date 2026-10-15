"""ONE ERP Next internal supplier goods bill with exact PO/receipt/GRNI match.

No governmental invoice issuance, official VAT recovery, vendor payment or
production approval is implied. Matching quantity and original cost are
rechecked from immutable PostgreSQL source events AFTER internal invoice
and journal creation but inside ONE outer transaction: any mismatch rolls
back the AP bill, journal, audit and source allocations.
"""
from __future__ import annotations

from typing import Mapping
from uuid import UUID

from .accounting import Account, AccountingError, AccountingPeriod
from .invoicing import DocumentType, InvoiceDocument, InvoiceLedgerAccounts, InvoicePostingProposal
from .invoice_persistence import persist_invoice_with_journal
from .tenancy import Membership, Action, can_access


def persist_valued_goods_vendor_bill(
    connection,
    *,
    order_id: UUID,
    order_line_numbers: tuple[int, ...],
    grni_account_id: str,
    document: InvoiceDocument,
    accounts: InvoiceLedgerAccounts,
    chart: Mapping[str, Account],
    period: AccountingPeriod,
    period_id: UUID,
    actor_id: UUID,
    memberships: tuple[Membership, ...],
    minor_units: int = 2,
) -> InvoicePostingProposal:
    """Create an internal vendor bill and debit GRNI only for valued receipts."""
    if (not isinstance(order_id, UUID)
        or not isinstance(actor_id, UUID)
        or document.kind is not DocumentType.VENDOR_BILL
        or not isinstance(grni_account_id, str) or not grni_account_id
        or not isinstance(order_line_numbers, tuple)
        or not order_line_numbers or len(order_line_numbers)>1000
        or len(order_line_numbers)!=len(document.lines)
        or any(type(no) is not int or no<=0 for no in order_line_numbers)
        or len(order_line_numbers)!=len(set(order_line_numbers))
        or any(line.account_id!=grni_account_id for line in document.lines)):
        raise AccountingError("Complete goods vendor bill to PO line mapping required")
    if not can_access(
        actor_id=actor_id,tenant_id=document.tenant_id,
        action=Action.WRITE_ACCOUNTING,memberships=memberships
    ):
        raise AccountingError("Company accounting authorization required")
    def match_before_issue(cursor) -> None:
        # This callback runs after invoice+lines exist but before the guarded
        # internal issue operation. Both validations and posting roll back
        # together if order/receipt/price/quantity checks fail.
        cursor.execute("SELECT one_link_goods_vendor_bill(%s,%s,%s,%s,%s,%s)",
                       (document.tenant_id,document.id,order_id,
                        grni_account_id,list(order_line_numbers),actor_id))
        row=cursor.fetchone()
        if row is None or row[0]!=document.id:
            raise AccountingError("Vendor bill/GRNI goods matching not confirmed")

    with connection.transaction():
        proposal=persist_invoice_with_journal(
            connection,document=document,accounts=accounts,chart=chart,
            period=period,period_id=period_id,actor_id=actor_id,
            memberships=memberships,minor_units=minor_units,
            goods_receipt_liability_accounts=frozenset((grni_account_id,)),
            before_issue=match_before_issue,
        )
    return proposal
