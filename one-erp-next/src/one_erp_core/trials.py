"""Seven-day trial rules. This domain layer does not verify emails by itself."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID, uuid4

TRIAL_DURATION = timedelta(days=7)


class PlanState(str, Enum):
    TRIAL = "trial"
    TRIAL_EXPIRED = "trial_expired"
    ACTIVE_PAID = "active_paid"
    SUSPENDED = "suspended"


class TrialError(ValueError):
    pass


def ensure_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise TrialError("Naive time is not allowed")
    return instant.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class TenantTrial:
    tenant_id: UUID
    company_name: str
    country_code: str
    sector_code: str
    administrator_email: str
    started_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.company_name.strip():
            raise TrialError("Company name is required")
        if len(self.country_code) != 2 or not self.country_code.isalpha() or not self.country_code.isascii():
            raise TrialError("Use two-letter ISO country code")
        if not self.sector_code or not self.sector_code.replace("_", "").isalnum():
            raise TrialError("Invalid sector code")
        if not self.administrator_email or "@" not in self.administrator_email:
            raise TrialError("Administrator email is required")
        start, end = ensure_utc(self.started_at), ensure_utc(self.expires_at)
        if end - start != TRIAL_DURATION:
            raise TrialError("Trial must last exactly seven days")
        object.__setattr__(self, "started_at", start)
        object.__setattr__(self, "expires_at", end)

    def state_at(self, now: datetime) -> PlanState:
        return PlanState.TRIAL if ensure_utc(now) < self.expires_at else PlanState.TRIAL_EXPIRED

    def remaining_seconds_at(self, now: datetime) -> int:
        return max(0, int((self.expires_at - ensure_utc(now)).total_seconds()))


def create_trial(*, company_name: str, country_code: str, sector_code: str,
                 administrator_email: str, verified_email: str, now: datetime) -> TenantTrial:
    """Trusted server-only call: verified_email must come from backend verification.

    NEVER trust browser-submitted verified_email; caller must prove it first.
    """
    admin = administrator_email.strip().lower()
    if not admin or admin != verified_email.strip().lower():
        raise TrialError("Verified administrator email is required")
    start = ensure_utc(now)
    return TenantTrial(
        tenant_id=uuid4(), company_name=company_name.strip(),
        country_code=country_code.upper(), sector_code=sector_code.lower(),
        administrator_email=admin, started_at=start, expires_at=start + TRIAL_DURATION,
    )
