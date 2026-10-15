"""Trusted-server-side PostgreSQL snapshot reader for ONE ERP finance reports.

Security model: the calling backend MUST first authenticate the actor and
authorize the requested company from its trusted identity/session provider.
The caller must never pass through a user-controlled actor_id, tenant_id or
raw DB connection without those validations. PostgreSQL GUCs are NOT auth.

The reader enforces DB membership again and uses RLS, repeatable-read and
read-only transactions as defense in depth. A dedicated per-tenant database
is still required before a commercial rollout.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from .accounting import (
    Account, AccountKind, AccountingError, EntryStatus, JournalEntry, JournalLine,
)
from .tenancy import Action, Membership, Role, can_access


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    tenant_id: UUID
    actor_id: UUID
    as_of: date
    chart: dict[str, Account]
    entries: tuple[JournalEntry, ...]
    memberships: tuple[Membership, ...]
    company_name: str = ""


def load_finance_snapshot(
    connection,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    as_of: date,
) -> LedgerSnapshot:
    """Read complete posted GL history through date from one consistent TX.

    This function never writes financial tables and never silently drops a
    posted journal with no lines. User-supplied company and actor IDs cannot
    be used as proof of authentication, even if the membership matches.
    """
    if not isinstance(tenant_id, UUID) or not isinstance(actor_id, UUID):
        raise AccountingError("Verified actor and tenant UUIDs required")
    if not isinstance(as_of, date):
        raise AccountingError("Report date required")
    with connection.transaction():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute(
                """SELECT rolsuper,rolbypassrls FROM pg_roles
                   WHERE rolname = current_user"""
            )
            role_row = cur.fetchone()
            if role_row is None or bool(role_row[0]) or bool(role_row[1]):
                raise AccountingError("Privileged PostgreSQL role cannot read tenant reports")

            cur.execute(
                "SELECT set_config('one.tenant_id',%s,true)", (str(tenant_id),)
            )
            cur.execute(
                "SELECT set_config('one.actor_id',%s,true)", (str(actor_id),)
            )
            cur.execute(
                """SELECT role,active FROM one_memberships
                   WHERE tenant_id=%s AND actor_id=%s""",
                (tenant_id, actor_id),
            )
            membership_row = cur.fetchone()
            if membership_row is None or not membership_row[1]:
                raise AccountingError("No active membership for requested company")
            try:
                role = Role(membership_row[0])
            except ValueError as error:
                raise AccountingError("Unrecognized company role") from error
            memberships = (Membership(actor_id,tenant_id,role,True),)
            cur.execute(
                "SELECT company_name FROM one_tenants WHERE id=%s", (tenant_id,)
            )
            company_row = cur.fetchone()
            if company_row is None or not company_row[0].strip():
                raise AccountingError("Company identity unavailable")
            company_name = company_row[0]
            if not can_access(
                actor_id=actor_id, tenant_id=tenant_id,
                action=Action.READ, memberships=memberships,
            ):
                raise AccountingError("Company report read permission denied")

            cur.execute(
                """SELECT id,code,name,kind,active,posting_allowed
                   FROM one_accounts
                   WHERE tenant_id=%s ORDER BY code,id""",
                (tenant_id,),
            )
            chart: dict[str, Account] = {}
            for account_id, code, name, kind, active, posting_allowed in cur.fetchall():
                if account_id in chart:
                    raise AccountingError("Duplicate account ID returned from database")
                try:
                    account_kind = AccountKind(kind)
                except ValueError as error:
                    raise AccountingError("Unknown chart account kind") from error
                chart[account_id] = Account(
                    account_id, tenant_id, code, name, account_kind,
                    bool(active), bool(posting_allowed),
                )

            # LEFT JOIN catches posted entries that have accidentally lost all
            # their lines. Such corruption must not appear as a clean report.
            cur.execute(
                """SELECT j.id,j.reference,j.occurred_on,j.currency,
                          j.idempotency_key,j.reversal_of,
                          l.line_number,l.account_id,l.debit,l.credit
                   FROM one_journal_entries j
                   LEFT JOIN one_journal_lines l
                     ON l.tenant_id=j.tenant_id AND l.entry_id=j.id
                   WHERE j.tenant_id=%s AND j.status='posted'
                     AND j.occurred_on<=%s
                   ORDER BY j.occurred_on,j.id,l.line_number""",
                (tenant_id,as_of),
            )
            rows = cur.fetchall()
            entries = []
            prev_id: UUID | None = None
            lines: list[JournalLine] = []
            meta = None
            for journal_id, reference, occurred_on, currency, key, reversal_of, \
                    line_num, account_id, debit, credit in rows:
                if prev_id != journal_id:
                    if meta is not None:
                        if len(lines)<2:
                            raise AccountingError("Incomplete posted journal in DB")
                        entries.append(JournalEntry(
                            meta[0],tenant_id,meta[1],meta[2],meta[3],meta[4],
                            tuple(lines),EntryStatus.POSTED,meta[5],
                        ))
                    prev_id=journal_id
                    meta=(journal_id,reference,occurred_on,currency,key,reversal_of)
                    lines=[]
                    previous_line_num=0
                if line_num is None or account_id is None:
                    raise AccountingError("Posted journal has missing lines")
                if line_num <= previous_line_num:
                    raise AccountingError("Duplicate/out-of-order journal lines")
                previous_line_num=line_num
                if not isinstance(debit, Decimal) or not isinstance(credit, Decimal):
                    raise AccountingError("DB financial amounts must be Decimal")
                lines.append(JournalLine(account_id,debit,credit))
            if meta is not None:
                if len(lines)<2:
                    raise AccountingError("Incomplete posted journal in DB")
                entries.append(JournalEntry(
                    meta[0],tenant_id,meta[1],meta[2],meta[3],meta[4],
                    tuple(lines),EntryStatus.POSTED,meta[5],
                ))
    # Snapshot is now fully materialized and detached from the DB connection.
    return LedgerSnapshot(tenant_id,actor_id,as_of,chart,tuple(entries),memberships,company_name)
