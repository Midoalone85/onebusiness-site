"""Fail-closed, read-only PostgreSQL runtime reader privilege audit.

An independent authorized operator supplies a privileged *inspector* connection
to an isolated tenant DB. No tenant UI, document, connection URI, or secret is
accepted here. This component cannot provision accounts or approve production.

The audit looks at EFFECTIVE grants, including PUBLIC and inherited roles.
A granted NOINHERIT membership can still enable SET ROLE, so permitted direct
or indirect memberships are enumerated and every reachable group checked.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .accounting import AccountingError

_ROLE = re.compile(r"[a-z][a-z0-9_]{5,62}\Z")
_TABLE = re.compile(r"[a-z][a-z0-9_]{2,62}\Z")
_FUNCTION = re.compile(r"public\.[a-z][a-z0-9_]*\([^)]*\)\Z")
_READ_FORBIDDEN = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
_SEQUENCE_FORBIDDEN = ("USAGE", "SELECT", "UPDATE")


@dataclass(frozen=True, slots=True)
class RestrictedReaderPolicy:
    """Names must come from an independently reviewed deployment manifest."""
    login_role: str
    required_tables: frozenset[str]
    allowed_tables: frozenset[str]
    globally_readable_tables: frozenset[str] = frozenset()
    allowed_function_signatures: frozenset[str] = frozenset()
    permitted_group_roles: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RestrictedReaderEvidence:
    """Catalog facts only; not a signed security certification."""
    database_name: str
    login_role: str
    checked_tables: int
    checked_functions: int
    reachable_group_roles: tuple[str, ...]
    approved_table_reads: tuple[str, ...]
    approved_function_executes: tuple[str, ...]


def _validate_policy(policy: RestrictedReaderPolicy, db_name: str) -> None:
    if (not isinstance(policy, RestrictedReaderPolicy) or
        not isinstance(db_name, str) or not _ROLE.fullmatch(db_name) or
        not isinstance(policy.login_role, str) or
        not _ROLE.fullmatch(policy.login_role)):
        raise AccountingError("Exact trusted database and reader role required")
    for values in (policy.required_tables, policy.allowed_tables,
                   policy.globally_readable_tables, policy.allowed_function_signatures,
                   policy.permitted_group_roles):
        if not isinstance(values, frozenset):
            raise AccountingError("Immutable reviewed reader allowlists required")
    if (not policy.required_tables or
        not policy.required_tables <= policy.allowed_tables or
        not policy.globally_readable_tables <= policy.allowed_tables or
        not all(isinstance(name, str) and _TABLE.fullmatch(name)
                for name in policy.allowed_tables) or
        not all(isinstance(name, str) and _ROLE.fullmatch(name)
                for name in policy.permitted_group_roles) or
        policy.login_role in policy.permitted_group_roles or
        not all(isinstance(name, str) and _FUNCTION.fullmatch(name)
                for name in policy.allowed_function_signatures)):
        raise AccountingError("Malformed or incomplete reader privilege policy")


def inspect_restricted_reader(connection, *, expected_database: str,
                              policy: RestrictedReaderPolicy) -> RestrictedReaderEvidence:
    """Audit a pinned dedicated DB using an independent privileged inspector.

    A pass only proves this snapshot of SQL catalog rights. It cannot prove
    pg_hba/firewall policies, token identity, RLS predicates, code injection
    immunity, off-cluster isolation, or backup restoration.
    """
    _validate_policy(policy, expected_database)
    try:
        with connection.transaction():
            with connection.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                # The SQL inspector is pre-authenticated outside this module.
                cur.execute("SELECT current_database(), current_user")
                if cur.fetchone()[0] != expected_database:
                    raise AccountingError("Inspector connected to unexpected database")
                cur.execute(
                    "SELECT rolsuper,rolbypassrls FROM pg_catalog.pg_roles "
                    "WHERE rolname=current_user")
                inspector = cur.fetchone()
                if not inspector or not any(inspector):
                    raise AccountingError("Independent inspector must see catalog grants")

                cur.execute(
                    """SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,
                              rolreplication,rolbypassrls
                       FROM pg_catalog.pg_roles WHERE rolname=%s""",
                    (policy.login_role,),
                )
                if cur.fetchone() != (True,False,False,False,False,False):
                    raise AccountingError("Reader login role is absent or elevated")

                # Read ALL reachable memberships: NOINHERIT group memberships
                # still provide SET ROLE and cannot be ignored.
                cur.execute(
                    """WITH RECURSIVE memberships(member,roleid) AS (
                         SELECT m.member,m.roleid
                         FROM pg_catalog.pg_auth_members m
                         JOIN pg_catalog.pg_roles r ON r.oid=m.member
                         WHERE r.rolname=%s
                         UNION
                         SELECT m.member,m.roleid
                         FROM pg_catalog.pg_auth_members m
                         JOIN memberships previous ON m.member=previous.roleid
                       )
                       SELECT DISTINCT r.rolname FROM memberships m
                       JOIN pg_catalog.pg_roles r ON r.oid=m.roleid""",
                    (policy.login_role,),
                )
                groups = frozenset(row[0] for row in cur.fetchall())
                if groups != policy.permitted_group_roles:
                    raise AccountingError("Unexpected inherited or SET ROLE capability")

                # A reader must not CREATE objects or open a temp schema.
                cur.execute(
                    """SELECT has_database_privilege(%s,current_database(),'CONNECT'),
                              has_database_privilege(%s,current_database(),'CREATE'),
                              has_database_privilege(%s,current_database(),'TEMPORARY'),
                              has_schema_privilege(%s,'public','USAGE'),
                              has_schema_privilege(%s,'public','CREATE')""",
                    (policy.login_role,)*5,
                )
                if cur.fetchone() != (True,False,False,True,False):
                    raise AccountingError("Reader has unsafe database/schema privileges")

                # Include every reachable member role, even a NOINHERIT member
                # that could switch identity with SET ROLE.
                principals = (policy.login_role, *sorted(groups))
                cur.execute(
                    """SELECT c.oid,c.relname,c.relkind,c.relrowsecurity,
                              c.relforcerowsecurity
                       FROM pg_catalog.pg_class c
                       JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                       WHERE n.nspname='public'
                         AND c.relkind IN ('r','p','v','m','f','S')
                       ORDER BY c.relname""")
                relations = cur.fetchall()
                tables_seen = set()
                for oid,name,kind,rls,force in relations:
                    for role in principals:
                        if kind == "S":
                            for privilege in _SEQUENCE_FORBIDDEN:
                                cur.execute(
                                    "SELECT has_sequence_privilege(%s,%s,%s)",
                                    (role,oid,privilege),
                                )
                                if cur.fetchone()[0]:
                                    raise AccountingError("Read-only role can use a sequence")
                            continue
                        for privilege in _READ_FORBIDDEN:
                            cur.execute(
                                "SELECT has_table_privilege(%s,%s,%s)",
                                (role,oid,privilege),
                            )
                            if cur.fetchone()[0]:
                                raise AccountingError("Read-only role can modify a relation")
                        cur.execute(
                            "SELECT has_table_privilege(%s,%s,'SELECT')",
                            (role,oid),
                        )
                        can_select = cur.fetchone()[0]
                        if can_select:
                            if name not in policy.allowed_tables:
                                raise AccountingError("Unapproved SELECT grant on a relation")
                            # Views and materialized views can sidestep the
                            # source table's RLS/privilege boundary.
                            if kind not in ("r","p"):
                                raise AccountingError("Unreviewed view relation access")
                            if name not in policy.globally_readable_tables and not (
                                rls and force
                            ):
                                raise AccountingError("Reader-accessible table lacks forced RLS")
                            if role == policy.login_role:
                                tables_seen.add(name)
                if not policy.required_tables <= tables_seen:
                    raise AccountingError("Missing required reader table privileges")

                cur.execute(
                    """SELECT p.oid,p.oid::regprocedure::text
                       FROM pg_catalog.pg_proc p
                       JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
                       WHERE n.nspname='public' ORDER BY p.oid""")
                routines = cur.fetchall()
                allowed_seen = set()
                for oid, signature in routines:
                    for role in principals:
                        cur.execute(
                            "SELECT has_function_privilege(%s,%s,'EXECUTE')",
                            (role,oid),
                        )
                        if cur.fetchone()[0]:
                            if signature not in policy.allowed_function_signatures:
                                raise AccountingError(
                                    "Reader or inherited role can invoke unapproved function")
                            if role == policy.login_role:
                                allowed_seen.add(signature)
                if allowed_seen != policy.allowed_function_signatures:
                    raise AccountingError("Approved reader function grants missing")

    except AccountingError:
        raise
    except Exception:
        # No role names, SQL text, tenant identifiers or DSNs leaked to clients.
        raise AccountingError("Runtime reader privilege audit unavailable") from None
    return RestrictedReaderEvidence(
        database_name=expected_database,
        login_role=policy.login_role,
        checked_tables=len(relations),
        checked_functions=len(routines),
        reachable_group_roles=tuple(sorted(groups)),
        approved_table_reads=tuple(sorted(tables_seen)),
        approved_function_executes=tuple(sorted(allowed_seen)),
    )
