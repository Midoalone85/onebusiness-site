"""Read-only AR/AP aging built from traceable issued invoice and payment snapshots.

The caller must load a complete, tenant-scoped, as-of source snapshot from a
consistent DB transaction. This report has no write or invoice-issuance side
effects and never manufactures bank reconciliation.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID
from typing import Sequence
from .accounting import AccountingError
from .invoicing import DocumentType


@dataclass(frozen=True, slots=True)
class IssuedInvoiceSnapshot:
    id: UUID
    tenant_id: UUID
    number: str
    partner_id: str
    kind: DocumentType
    issued_on: date
    due_on: date
    currency: str
    gross: Decimal
    source_invoice_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AppliedPaymentSnapshot:
    payment_id: UUID
    tenant_id: UUID
    invoice_id: UUID
    occurred_on: date
    currency: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class AgingRow:
    invoice_id: UUID
    number: str
    partner_id: str
    due_on: date
    gross: Decimal
    credits: Decimal
    payments: Decimal
    outstanding: Decimal
    days_overdue: int
    bucket: str
    source_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class AgingReport:
    tenant_id: UUID
    as_of: date
    kind: DocumentType
    currency: str
    rows: tuple[AgingRow, ...]
    totals: dict[str, Decimal]
    grand_total: Decimal


def _safe_money(amount: Decimal) -> None:
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
        raise AccountingError("Report amount must be positive finite Decimal")


def _bucket(days: int) -> str:
    if days <= 0:
        return "current"
    if days <= 30:
        return "1-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def aging_report(
    *,
    tenant_id: UUID,
    as_of: date,
    kind: DocumentType,
    currency: str,
    invoices: Sequence[IssuedInvoiceSnapshot],
    payments: Sequence[AppliedPaymentSnapshot],
) -> AgingReport:
    """Compute AR/AP outstanding at an inclusive date.

    Only source customer invoices or vendor bills are reported; credit notes
    and applied payments are separately linked to original invoice UUIDs.
    """
    if kind not in (DocumentType.CUSTOMER_INVOICE,DocumentType.VENDOR_BILL):
        raise AccountingError("Aging kind must be customer invoice or vendor bill")
    if any(i.tenant_id != tenant_id for i in invoices) or any(p.tenant_id != tenant_id for p in payments):
        raise AccountingError("Cross-tenant rows supplied to aging report")
    if len({i.id for i in invoices}) != len(invoices):
        raise AccountingError("Duplicate source document")
    allowed_credit = (DocumentType.CUSTOMER_CREDIT if kind is DocumentType.CUSTOMER_INVOICE
                      else DocumentType.VENDOR_CREDIT)
    primary = {}
    credits = {}
    credit_ids = {}
    for invoice in invoices:
        _safe_money(invoice.gross)
        if invoice.currency != currency:
            raise AccountingError("Mixed currency without approved FX conversion")
        if invoice.issued_on > as_of:
            continue
        if invoice.kind is kind:
            if invoice.source_invoice_id is not None:
                raise AccountingError("Normal invoice cannot be credit note")
            primary[invoice.id] = invoice
        elif invoice.kind is allowed_credit:
            if invoice.source_invoice_id is None:
                raise AccountingError("Credit note missing source invoice")
            credits[invoice.source_invoice_id] = (
                credits.get(invoice.source_invoice_id,Decimal(0)) + invoice.gross
            )
            credit_ids.setdefault(invoice.source_invoice_id,[]).append(invoice.id)
        else:
            raise AccountingError("Unexpected document type in report source")
    for source_id in credits:
        if source_id not in primary:
            raise AccountingError("Missing original source invoice for credit note")
    paid = {}
    payment_ids = {}
    for p in payments:
        _safe_money(p.amount)
        if p.currency != currency:
            raise AccountingError("Mixed payment currency")
        if p.occurred_on > as_of:
            continue
        if p.invoice_id not in primary:
            raise AccountingError("Payment lacks original invoice in source snapshot")
        paid[p.invoice_id] = paid.get(p.invoice_id,Decimal(0)) + p.amount
        payment_ids.setdefault(p.invoice_id,[]).append(p.payment_id)
    totals = {key:Decimal(0) for key in ("current","1-30","31-60","61-90","90+")}
    rows = []
    for doc in primary.values():
        applied = paid.get(doc.id,Decimal(0))
        credited = credits.get(doc.id,Decimal(0))
        outstanding = doc.gross - applied - credited
        if outstanding < 0:
            raise AccountingError("Negative outstanding means over-settlement or incomplete sources")
        if outstanding == 0:
            continue
        overdue = (as_of-doc.due_on).days
        bucket = _bucket(overdue)
        totals[bucket] += outstanding
        rows.append(AgingRow(
            doc.id,doc.number,doc.partner_id,doc.due_on,
            doc.gross,credited,applied,outstanding,max(0,overdue),bucket,
            tuple([doc.id]+credit_ids.get(doc.id,[])+payment_ids.get(doc.id,[])),
        ))
    rows.sort(key=lambda row:(-row.days_overdue,row.due_on,row.number))
    return AgingReport(
        tenant_id,as_of,kind,currency,tuple(rows),totals,
        sum(totals.values(),Decimal(0)),
    )
