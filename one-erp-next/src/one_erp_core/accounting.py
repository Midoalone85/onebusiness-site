"""ONE ERP independent double-entry accounting domain rules.

Pure policies only; database transactions must enforce tenant-bound writes,
durable audit history, and unique (tenant_id, idempotency_key) constraints.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Mapping, Sequence
from uuid import UUID

from .tenancy import Action, Membership, can_access


class AccountingError(ValueError):
    """A journal violates accounting, security, or business rules."""


class AccountKind(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class EntryStatus(str, Enum):
    DRAFT = "draft"
    POSTED = "posted"


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    tenant_id: UUID
    code: str
    name: str
    kind: AccountKind
    active: bool = True
    posting_allowed: bool = True

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.code.strip() or not self.name.strip():
            raise AccountingError("Account id, code and name are required")


@dataclass(frozen=True, slots=True)
class AccountingPeriod:
    tenant_id: UUID
    begins: date
    ends_exclusive: date
    closed: bool = False

    def __post_init__(self) -> None:
        if self.begins >= self.ends_exclusive:
            raise AccountingError("Period must have a non-empty date range")


@dataclass(frozen=True, slots=True)
class JournalLine:
    account_id: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class JournalEntry:
    id: UUID
    tenant_id: UUID
    reference: str
    occurred_on: date
    currency: str
    idempotency_key: str
    lines: tuple[JournalLine, ...]
    status: EntryStatus = EntryStatus.DRAFT
    reversal_of: UUID | None = None


def _money(value: Decimal, *, minor_units: int) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AccountingError("Amounts must be finite Decimal values (not floats)")
    if value < 0 or value != value.quantize(Decimal(1).scaleb(-minor_units)):
        raise AccountingError("Negative amounts or unsupported decimal precision")
    return value


def validate_entry(
    entry: JournalEntry,
    *,
    chart: Mapping[str, Account],
    period: AccountingPeriod,
    minor_units: int = 2,
) -> None:
    """Check ledger invariants independently from authorization and persistence."""
    if minor_units not in (0, 2, 3):
        raise AccountingError("Specify a supported currency minor-unit precision")
    if entry.status != EntryStatus.DRAFT:
        raise AccountingError("Only draft entries may be posted")
    if entry.reversal_of == entry.id:
        raise AccountingError("An entry cannot reverse itself")
    if entry.tenant_id != period.tenant_id:
        raise AccountingError("Period belongs to a different tenant")
    if period.closed or not period.begins <= entry.occurred_on < period.ends_exclusive:
        raise AccountingError("Accounting period is closed or does not include entry date")
    if not entry.reference.strip() or not entry.idempotency_key.strip():
        raise AccountingError("Reference and idempotency key are required")
    if len(entry.currency) != 3 or not entry.currency.isascii() or not entry.currency.isalpha() or entry.currency != entry.currency.upper():
        raise AccountingError("Currency must be uppercase ISO-style code")
    if len(entry.lines) < 2:
        raise AccountingError("A journal requires at least two lines")
    own_accounts = [a for a in chart.values() if a.tenant_id == entry.tenant_id]
    if len({a.code for a in own_accounts}) != len(own_accounts):
        raise AccountingError("Account codes must be unique per tenant")
    debit_total = Decimal(0)
    credit_total = Decimal(0)
    for line in entry.lines:
        account = chart.get(line.account_id)
        if account is None or account.tenant_id != entry.tenant_id:
            raise AccountingError("Account not found in entry tenant")
        if not account.active or not account.posting_allowed:
            raise AccountingError("Account is inactive or non-postable")
        debit = _money(line.debit, minor_units=minor_units)
        credit = _money(line.credit, minor_units=minor_units)
        if (debit == 0) == (credit == 0):
            raise AccountingError("Each line must have exactly one positive side")
        debit_total += debit
        credit_total += credit
    if debit_total != credit_total:
        raise AccountingError("Unbalanced journal: total debits must equal total credits")


def post_entry(
    entry: JournalEntry,
    *,
    chart: Mapping[str, Account],
    period: AccountingPeriod,
    actor_id: UUID,
    memberships: tuple[Membership, ...],
    existing_posted: Sequence[JournalEntry] = (),
    minor_units: int = 2,
) -> JournalEntry:
    """Return validated posted entry; caller must persist it atomically.

    In-memory duplicate checks are not a replacement for transactional
    uniqueness enforcement in PostgreSQL.
    """
    if not can_access(actor_id=actor_id, tenant_id=entry.tenant_id,
                      action=Action.WRITE_ACCOUNTING, memberships=memberships):
        raise AccountingError("Not authorized to post for this company")
    validate_entry(entry, chart=chart, period=period, minor_units=minor_units)
    for old in existing_posted:
        if old.tenant_id == entry.tenant_id and old.status == EntryStatus.POSTED and (
            old.id == entry.id or old.idempotency_key == entry.idempotency_key
        ):
            raise AccountingError("Duplicate posted journal or idempotency key")
    return replace(entry, status=EntryStatus.POSTED)
