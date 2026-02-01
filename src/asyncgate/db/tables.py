"""SQLAlchemy table definitions."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from asyncgate.db.base import Base
from asyncgate.models.enums import PrincipalKind, ReceiptType, TaskStatus


class TaskTable(Base):
    """Tasks table - core work units."""

    __tablename__ = "tasks"

    # Primary key with tenant partitioning
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)

    # Type and payload
    type: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default={})
    payload_pointer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Ownership (immutable)
    created_by_kind: Mapped[str] = mapped_column(
        Enum(PrincipalKind, values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    created_by_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by_instance_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    principal_ai: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Requirements
    requirements: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default={})

    # Expected outcome metadata
    expected_outcome_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    expected_artifact_mime: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Priority
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Status
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, values_callable=lambda x: [e.value for e in x]), 
        nullable=False, 
        default=TaskStatus.QUEUED
    )

    # Retry config
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    retry_backoff_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    # Idempotency
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Result (for terminal tasks)
    result_outcome: Mapped[str | None] = mapped_column(String(50), nullable=True)
    result_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    result_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    result_artifacts: Mapped[dict[str, Any] | list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Instance ownership
    asyncgate_instance: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
    lease: Mapped["LeaseTable | None"] = relationship(
        "LeaseTable", back_populates="task", uselist=False
    )
    progress: Mapped["ProgressTable | None"] = relationship(
        "ProgressTable", back_populates="task", uselist=False
    )

    __table_args__ = (
        # Unique idempotency key per tenant
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_task_idempotency"),
        # Index for lease_next queries
        Index(
            "idx_tasks_leasable",
            "tenant_id",
            "status",
            "next_eligible_at",
            "priority",
            "created_at",
        ),
        # Index for listing by status
        Index("idx_tasks_tenant_status", "tenant_id", "status", "created_at"),
        # Index for listing running tasks
        Index("idx_tasks_running", "tenant_id", "status", "started_at"),
        # Index for instance ownership
        Index("idx_tasks_instance", "asyncgate_instance"),
        # Index for principal AI ownership queries
        Index("idx_tasks_principal_ai", "tenant_id", "principal_ai"),
    )


class LeaseTable(Base):
    """Leases table - worker claims on tasks."""

    __tablename__ = "leases"

    lease_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    
    # P1.1: Renewal tracking
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        nullable=False,
        comment="When lease was initially acquired (for absolute lifetime tracking)"
    )
    renewal_count: Mapped[int] = mapped_column(
        Integer, 
        nullable=False, 
        default=0,
        comment="Number of times lease has been renewed (P1.1 - prevents hoarding)"
    )

    # Relationship to task
    task: Mapped[TaskTable] = relationship("TaskTable", back_populates="lease")

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            ondelete="CASCADE",
        ),
        # Unique active lease per task
        UniqueConstraint("tenant_id", "task_id", name="uq_lease_task"),
        # Index for expiry sweeps
        Index("idx_leases_expires", "expires_at"),
        # Index for worker lookups
        Index("idx_leases_worker", "tenant_id", "worker_id"),
    )


class ReceiptTable(Base):
    """Receipts table - immutable contract records."""

    __tablename__ = "receipts"

    # Primary key with tenant partitioning
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    receipt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)

    receipt_type: Mapped[ReceiptType] = mapped_column(
        Enum(ReceiptType, values_callable=lambda x: [e.value for e in x]), 
        nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Sender
    from_kind: Mapped[str] = mapped_column(
        Enum(PrincipalKind, values_callable=lambda x: [e.value for e in x]), 
        nullable=False
    )
    from_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Recipient
    to_kind: Mapped[str] = mapped_column(
        Enum(PrincipalKind, values_callable=lambda x: [e.value for e in x]), 
        nullable=False
    )
    to_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Related entities
    task_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    lease_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    schedule_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Causal linkage
    parents: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=[])

    # Body
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default={})

    # Integrity hash
    hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Instance
    asyncgate_instance: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Delivery tracking
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Unique constraint for receipt deduplication
        UniqueConstraint("tenant_id", "hash", name="uq_receipt_hash"),
        # Index for receipt queries by recipient
        Index("idx_receipts_to", "tenant_id", "to_kind", "to_id", "created_at"),
        # Index for task receipts
        Index("idx_receipts_task", "tenant_id", "task_id", "created_at"),
        # Index for lease receipts
        Index("idx_receipts_lease", "tenant_id", "lease_id", "created_at"),
        # Index for receipt type queries
        Index("idx_receipts_type", "tenant_id", "receipt_type", "created_at"),
        # Index for deduplication (redundant with unique constraint, but kept for explicit queries)
        Index("idx_receipts_hash", "tenant_id", "hash"),
        # GIN index for parents array containment queries (P0.1 - performance critical)
        Index("idx_receipts_parents_gin", "parents", postgresql_using="gin"),
    )


class ProgressTable(Base):
    """Progress table - task execution progress."""

    __tablename__ = "progress"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default={})
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Relationship to task
    task: Mapped[TaskTable] = relationship("TaskTable", back_populates="progress")

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            ondelete="CASCADE",
        ),
    )


class AuditEventTable(Base):
    """Audit events table - lifecycle tracking."""

    __tablename__ = "audit_events"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default={})
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_audit_task", "tenant_id", "task_id", "created_at"),
        Index("idx_audit_type", "tenant_id", "event_type", "created_at"),
    )


class RelationshipTable(Base):
    """Relationships table - principal session tracking."""

    __tablename__ = "relationships"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    principal_kind: Mapped[str] = mapped_column(
        Enum(PrincipalKind, values_callable=lambda x: [e.value for e in x]), 
        primary_key=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    principal_instance_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sessions_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        Index("idx_relationships_last_seen", "tenant_id", "last_seen_at"),
    )
