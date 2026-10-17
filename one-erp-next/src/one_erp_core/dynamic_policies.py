"""ONE ERP NEXT: independent, side-effect-free company policy change preview.

This module NEVER grants backend permissions, persists changes, modifies stock,
or posts a journal. Trusted service code must resolve identities/memberships from
its authenticated session, persist approved transitions and append audit records
in one serializable/optimistically guarded tenant-scoped database transaction.
Accounting balance, tenant isolation, and audit immutability remain hard invariants.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias
from uuid import UUID

from .tenancy import Membership, Role

PolicyValue: TypeAlias = bool | int | str


class PolicyError(ValueError):
    """Invalid policy definition, stale change or unapproved transition."""


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Scope(str, Enum):
    COMPANY = "company"
    BRANCH = "branch"


@dataclass(frozen=True, slots=True)
class PolicyDefinition:
    key: str
    allowed_values: tuple[PolicyValue, ...]
    default_value: PolicyValue
    risk: Risk
    branch_override_allowed: bool = False

    def __post_init__(self) -> None:
        if not self.key or not all(part.isidentifier() for part in self.key.split(".")):
            raise PolicyError("Policy key must be namespaced identifiers")
        if not self.allowed_values or not _allowed(self.default_value, self.allowed_values):
            raise PolicyError("Policy default must be present in allowed values")
        if len({(type(v), v) for v in self.allowed_values}) != len(self.allowed_values):
            raise PolicyError("Policy allowed values must be unique and typed")


@dataclass(frozen=True, slots=True)
class PolicyState:
    tenant_id: UUID
    definition_key: str
    value: PolicyValue
    revision: int
    scope: Scope = Scope.COMPANY
    branch_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise PolicyError("Policy revision cannot be negative")
        if (self.scope is Scope.COMPANY and self.branch_id is not None) or (
            self.scope is Scope.BRANCH and self.branch_id is None
        ):
            raise PolicyError("Branch scope requires a branch; company scope forbids it")


@dataclass(frozen=True, slots=True)
class PolicyChange:
    tenant_id: UUID
    definition_key: str
    proposed_value: PolicyValue
    expected_revision: int
    requested_by: UUID
    scope: Scope = Scope.COMPANY
    branch_id: UUID | None = None
    approver_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyChangePreview:
    current_value: PolicyValue
    proposed_value: PolicyValue
    risk: Risk
    required_approvals: int
    eligible_approvals: int
    can_apply: bool
    reasons: tuple[str, ...]
    affected_scope: Scope
    current_revision: int


@dataclass(frozen=True, slots=True)
class PolicyAuditIntent:
    """To be persisted atomically by the trusted service, not proof of an audit log."""
    tenant_id: UUID
    definition_key: str
    scope: Scope
    branch_id: UUID | None
    old_revision: int
    new_revision: int
    old_value: PolicyValue
    new_value: PolicyValue
    requested_by: UUID
    approver_ids: tuple[UUID, ...]


def _allowed(value: PolicyValue, values: tuple[PolicyValue, ...]) -> bool:
    # True == 1 in Python, but finance/configuration types must never coerce.
    return any(type(value) is type(option) and value == option for option in values)


def _member(actor: UUID, tenant: UUID, memberships: tuple[Membership, ...]) -> Role | None:
    for m in memberships:
        if m.user_id == actor and m.tenant_id == tenant and m.is_active:
            return m.role
    return None


def preview_policy_change(
    definition: PolicyDefinition, state: PolicyState, change: PolicyChange,
    *, memberships: tuple[Membership, ...],
) -> PolicyChangePreview:
    """Dry-run an adjustment. No code is executed and no database is changed.

    Roles here are *trusted inputs* to a domain function, not authentication.
    LOW requires no separate approval; MEDIUM one; HIGH two (one admin).
    CRITICAL is never auto-applied, even with approvers.
    """
    reasons: list[str] = []
    required = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2, Risk.CRITICAL: 2}[definition.risk]
    if (state.tenant_id != change.tenant_id or
        state.definition_key != definition.key or
        change.definition_key != definition.key or
        state.scope is not change.scope or state.branch_id != change.branch_id):
        reasons.append("scope_or_tenant_mismatch")
    if state.scope is Scope.BRANCH and not definition.branch_override_allowed:
        reasons.append("branch_override_forbidden")
    if not _allowed(state.value, definition.allowed_values):
        reasons.append("current_value_invalid")
    if not _allowed(change.proposed_value, definition.allowed_values):
        reasons.append("proposed_value_not_allowed")
    if change.expected_revision != state.revision:
        reasons.append("stale_revision")
    if type(state.value) is type(change.proposed_value) and state.value == change.proposed_value:
        reasons.append("no_change")
    if _member(change.requested_by, change.tenant_id, memberships) is not Role.COMPANY_ADMIN:
        reasons.append("requester_not_authorized")
    if len(change.approver_ids) != len(set(change.approver_ids)):
        reasons.append("duplicate_approver")
    if change.requested_by in change.approver_ids:
        reasons.append("self_approval_forbidden")
    approver_roles = [
        _member(approver, change.tenant_id, memberships)
        for approver in change.approver_ids if approver != change.requested_by
    ]
    eligible = sum(role in (Role.COMPANY_ADMIN, Role.ACCOUNTANT) for role in approver_roles)
    if eligible < required:
        reasons.append("insufficient_independent_approvals")
    if definition.risk in (Risk.HIGH, Risk.CRITICAL) and Role.COMPANY_ADMIN not in approver_roles:
        reasons.append("company_admin_approval_required")
    if definition.risk is Risk.CRITICAL:
        reasons.append("critical_requires_external_controlled_workflow")
    return PolicyChangePreview(
        current_value=state.value, proposed_value=change.proposed_value,
        risk=definition.risk, required_approvals=required,
        eligible_approvals=eligible, can_apply=not reasons,
        reasons=tuple(reasons), affected_scope=state.scope,
        current_revision=state.revision,
    )


def prepare_approved_policy_change(
    definition: PolicyDefinition, state: PolicyState, change: PolicyChange,
    *, memberships: tuple[Membership, ...],
) -> tuple[PolicyState, PolicyAuditIntent]:
    """Build a proposed transition; caller MUST atomically CAS + audit in DB."""
    preview = preview_policy_change(definition, state, change, memberships=memberships)
    if not preview.can_apply:
        raise PolicyError("Change denied: " + ", ".join(preview.reasons))
    next_state = PolicyState(
        tenant_id=state.tenant_id, definition_key=state.definition_key,
        value=change.proposed_value, revision=state.revision + 1,
        scope=state.scope, branch_id=state.branch_id,
    )
    audit = PolicyAuditIntent(
        tenant_id=state.tenant_id, definition_key=state.definition_key,
        scope=state.scope, branch_id=state.branch_id,
        old_revision=state.revision, new_revision=next_state.revision,
        old_value=state.value, new_value=next_state.value,
        requested_by=change.requested_by, approver_ids=change.approver_ids,
    )
    return next_state, audit


# First examples; the actual catalog will be tenant-specific, versioned and
# signed off by the enterprise policy service before editing through the UI.
INITIAL_POLICY_CATALOG: tuple[PolicyDefinition, ...] = (
    PolicyDefinition("sales.partial_delivery", (True, False), True, Risk.LOW, True),
    PolicyDefinition("inventory.receipt_requires_qa", (True, False), True, Risk.HIGH, True),
    PolicyDefinition("finance.journal_approval_threshold_sar", (0, 5000, 10000, 50000), 5000, Risk.HIGH),
    # Core controls must not be switchable from configuration.
    PolicyDefinition("finance.enforce_balanced_journals", (True,), True, Risk.CRITICAL),
    PolicyDefinition("security.enforce_tenant_isolation", (True,), True, Risk.CRITICAL),
)
