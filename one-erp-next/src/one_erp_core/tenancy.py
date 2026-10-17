"""Per-company policy; database and API layers must separately enforce isolation."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID


class Role(str, Enum):
    COMPANY_ADMIN = "company_admin"
    ACCOUNTANT = "accountant"
    VIEWER = "viewer"


class Action(str, Enum):
    READ = "read"
    WRITE_ACCOUNTING = "write_accounting"
    PROCESS_STOCK = "process_stock"
    MANAGE_USERS = "manage_users"


@dataclass(frozen=True, slots=True)
class Membership:
    user_id: UUID
    tenant_id: UUID
    role: Role
    is_active: bool = True


_ALLOWED: dict[Role, frozenset[Action]] = {
    Role.COMPANY_ADMIN: frozenset(Action),
    Role.ACCOUNTANT: frozenset((Action.READ, Action.WRITE_ACCOUNTING)),
    Role.VIEWER: frozenset((Action.READ,)),
}


def can_access(*, actor_id: UUID, tenant_id: UUID, action: Action,
               memberships: tuple[Membership, ...]) -> bool:
    """Deny by default, including users in another company."""
    return any(
        m.user_id == actor_id and m.tenant_id == tenant_id and m.is_active
        and action in _ALLOWED.get(m.role, frozenset())
        for m in memberships
    )
