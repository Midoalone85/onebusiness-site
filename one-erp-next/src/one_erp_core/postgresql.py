"""Atomic PostgreSQL adapter for the independent ONE ERP accounting core.

Requires a psycopg 3 connection, an existing tenant, membership, chart and
accounting period. DB connection credentials and tenant identity must come
from trusted server-side authentication, never browser-supplied data.
"""
from __future__ import annotations

from typing import Mapping
from uuid import UUID

from .accounting import (
    Account, AccountingError, AccountingPeriod, JournalEntry, post_entry,
)
from .tenancy import Membership


def persist_and_post(
    connection,
    *,
    entry: JournalEntry,
    period_id: UUID,
    period: AccountingPeriod,
    chart: Mapping[str, Account],
    actor_id: UUID,
    memberships: tuple[Membership, ...],
    minor_units: int = 2,
) -> JournalEntry:
    """Create journal+lines, post, and audit in ONE PostgreSQL transaction.

    This is deliberately fail-closed: duplicate idempotency keys and any
    database error abort the transaction. It does not silently auto-retry.
    Before calling, the service MUST verify the user, memberships and tenant,
    and use a DB role with RLS enabled (not the table owner/superuser).
    """
    posted = post_entry(
        entry, chart=chart, period=period, actor_id=actor_id,
        memberships=memberships, minor_units=minor_units,
    )

    # The database period is authoritative, not the policy object supplied
    # by a request. one_post_journal locks and rechecks it at posting time.
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('one.tenant_id', %s, true)",
                (str(entry.tenant_id),),
            )
            cursor.execute(
                "SELECT set_config('one.actor_id', %s, true)",
                (str(actor_id),),
            )
            cursor.execute(
                """INSERT INTO one_journal_entries
                   (tenant_id,id,period_id,reference,idempotency_key,
                    occurred_on,currency,reversal_of)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (entry.tenant_id, entry.id, period_id, entry.reference,
                 entry.idempotency_key, entry.occurred_on, entry.currency,
                 entry.reversal_of),
            )
            for number, line in enumerate(entry.lines, start=1):
                cursor.execute(
                    """INSERT INTO one_journal_lines
                       (tenant_id,entry_id,line_number,account_id,debit,credit)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (entry.tenant_id, entry.id, number, line.account_id,
                     line.debit, line.credit),
                )
            cursor.execute(
                "SELECT one_post_journal(%s,%s,%s)",
                (entry.tenant_id, entry.id, actor_id),
            )
            result = cursor.fetchone()
            if result is None or result[0] != entry.id:
                raise AccountingError("Database did not confirm journal posting")
    return posted


def close_accounting_period(
    connection,
    *,
    tenant_id: UUID,
    period_id: UUID,
    actor_id: UUID,
) -> UUID:
    """Close period and append audit atomically, database verifying admin role.

    This must only be called from a trusted authenticated server session.
    Transaction-local context is necessary but not an authentication boundary.
    """
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('one.tenant_id', %s, true)",
                (str(tenant_id),),
            )
            cursor.execute(
                "SELECT set_config('one.actor_id', %s, true)",
                (str(actor_id),),
            )
            cursor.execute(
                "SELECT one_close_period(%s,%s,%s)",
                (tenant_id, period_id, actor_id),
            )
            row = cursor.fetchone()
            if row is None or row[0] != period_id:
                raise AccountingError("Database did not confirm period closure")
    return period_id
