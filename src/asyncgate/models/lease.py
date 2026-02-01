"""Lease model - worker claim on a task."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from asyncgate.utils.time import utc_now


class Lease(BaseModel):
    """Represents a worker's exclusive claim on a task."""

    lease_id: UUID
    tenant_id: UUID
    task_id: UUID
    worker_id: str
    expires_at: datetime
    created_at: datetime
    acquired_at: datetime  # P1.1: When lease was initially acquired
    renewal_count: int = 0  # P1.1: Number of renewals

    def is_expired(self, now: datetime | None = None) -> bool:
        """Check if lease has expired."""
        if now is None:
            now = utc_now()
        return now >= self.expires_at


class LeaseInfo(BaseModel):
    """Lease information returned to workers."""

    tenant_id: UUID | None = None
    task_id: UUID
    lease_id: UUID
    type: str
    payload: dict
    payload_pointer: str | None = None
    principal_ai: str | None = None
    attempt: int
    expires_at: datetime
    requirements: dict | None = None
    expected_outcome_kind: str | None = None
    expected_artifact_mime: str | None = None
