"""Task model - core work unit."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from asyncgate.models.enums import Outcome, TaskStatus
from asyncgate.models.principal import Principal


class TaskRequirements(BaseModel):
    """Requirements for task execution."""

    capabilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class TaskResult(BaseModel):
    """Terminal outcome of a task."""

    outcome: Outcome
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] | dict[str, Any] | None = None
    completed_at: datetime


class Task(BaseModel):
    """Core task entity representing a unit of work."""

    # Identity
    task_id: UUID
    tenant_id: UUID

    # Type and payload
    type: str
    # NOTE: payload is legacy inline data; prefer payload_pointer for LegiVellum compliance.
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_pointer: str | None = None

    # Ownership (immutable after creation)
    created_by: Principal
    # Principal AI that owns the obligation (LegiVellum requirement)
    principal_ai: str

    # Requirements for execution
    requirements: TaskRequirements = Field(default_factory=TaskRequirements)

    # Priority (higher = more urgent)
    priority: int = 0

    # Status
    status: TaskStatus = TaskStatus.QUEUED

    # Retry configuration
    attempt: int = 0
    max_attempts: int = 3
    retry_backoff_seconds: int = 30

    # Idempotency
    idempotency_key: str | None = None

    # Timestamps
    created_at: datetime
    updated_at: datetime
    next_eligible_at: datetime | None = None
    started_at: datetime | None = None

    # Result (populated when terminal)
    result: TaskResult | None = None

    # Expected outcome metadata (for receipt shaping)
    expected_outcome_kind: str | None = None
    expected_artifact_mime: str | None = None

    # AsyncGate instance ownership (for multi-instance deployments)
    asyncgate_instance: str | None = None

    def is_terminal(self) -> bool:
        """Check if task is in a terminal state."""
        return self.status.is_terminal()

    def can_transition_to(self, new_status: TaskStatus) -> bool:
        """Check if transition to new status is valid per state machine."""
        valid_transitions: dict[TaskStatus, set[TaskStatus]] = {
            TaskStatus.QUEUED: {TaskStatus.LEASED, TaskStatus.CANCELED},
            TaskStatus.LEASED: {
                TaskStatus.RUNNING,
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELED,
                TaskStatus.QUEUED,  # On lease expiry (system-driven)
            },
            TaskStatus.RUNNING: {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELED,
                TaskStatus.QUEUED,  # On lease expiry (system-driven)
            },
            TaskStatus.FAILED: {TaskStatus.QUEUED},  # Only if retry requeues
            TaskStatus.SUCCEEDED: set(),
            TaskStatus.CANCELED: set(),
        }
        return new_status in valid_transitions.get(self.status, set())


class TaskSummary(BaseModel):
    """Lightweight task summary for bootstrap responses."""

    task_id: UUID
    type: str
    status: TaskStatus
    priority: int
    attempt: int
    created_at: datetime
    updated_at: datetime
    next_eligible_at: datetime | None = None
