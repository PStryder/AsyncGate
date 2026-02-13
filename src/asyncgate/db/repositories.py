"""Database repositories for AsyncGate entities."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from asyncgate.config import settings
from asyncgate.constants import (
    MAX_RECEIPT_ARTIFACTS,
    MAX_RECEIPT_BODY_BYTES,
    MAX_RECEIPT_PARENTS,
)
from asyncgate.db.tables import (
    AuditEventTable,
    LeaseTable,
    ProgressTable,
    ReceiptTable,
    RelationshipTable,
    TaskTable,
)
from asyncgate.models import (
    Lease,
    Principal,
    PrincipalKind,
    Progress,
    Receipt,
    ReceiptType,
    Relationship,
    Task,
    TaskRequirements,
    TaskResult,
    TaskStatus,
    TaskSummary,
)
from asyncgate.models.enums import Outcome
from asyncgate.models.receipt import compute_receipt_hash
from asyncgate.models.termination import (
    get_obligation_types,
    get_terminal_types,
    is_terminal_type,
    TERMINAL_RECEIPT_TYPES,
)
from asyncgate.principals import SERVICE_PRINCIPAL_ID, principal_id_variants
from asyncgate.utils.time import utc_now


class TaskRepository:
    """Repository for task operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        tenant_id: UUID,
        type: str,
        payload: dict[str, Any],
        created_by: Principal,
        principal_ai: str | None = None,
        payload_pointer: str | None = None,
        requirements: TaskRequirements | None = None,
        expected_outcome_kind: str | None = None,
        expected_artifact_mime: str | None = None,
        priority: int = 0,
        idempotency_key: str | None = None,
        max_attempts: int | None = None,
        retry_backoff_seconds: int | None = None,
        delay_seconds: int | None = None,
    ) -> Task:
        """
        Create a new task.
        
        Uses DB-first approach for idempotency: attempts insert and catches
        unique constraint violation, then fetches existing task. This prevents
        race conditions from check-then-insert pattern.
        """
        now = utc_now()
        task_id = uuid4()

        next_eligible_at = now + timedelta(seconds=delay_seconds) if delay_seconds else None
        resolved_payload_pointer = payload_pointer or f"inline://task/{task_id}"
        resolved_principal_ai = principal_ai or created_by.instance_id or created_by.id

        task_row = TaskTable(
            tenant_id=tenant_id,
            task_id=task_id,
            type=type,
            payload=payload,
            payload_pointer=resolved_payload_pointer,
            created_by_kind=created_by.kind,
            created_by_id=created_by.id,
            created_by_instance_id=created_by.instance_id,
            principal_ai=resolved_principal_ai,
            requirements=requirements.model_dump() if requirements else {},
            expected_outcome_kind=expected_outcome_kind,
            expected_artifact_mime=expected_artifact_mime,
            priority=priority,
            status=TaskStatus.QUEUED,
            attempt=0,
            max_attempts=max_attempts or settings.default_max_attempts,
            retry_backoff_seconds=retry_backoff_seconds or settings.default_retry_backoff_seconds,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
            next_eligible_at=next_eligible_at,
            started_at=None,
            asyncgate_instance=settings.instance_id,
        )

        self.session.add(task_row)
        
        try:
            await self.session.flush()
            return self._row_to_model(task_row)
        except IntegrityError:
            # Unique constraint violation on idempotency_key - fetch existing
            await self.session.rollback()
            if idempotency_key:
                existing = await self._get_by_idempotency_key(tenant_id, idempotency_key)
                if existing:
                    return existing
            # Re-raise if not idempotency-related
            raise

    async def get(self, tenant_id: UUID, task_id: UUID) -> Task | None:
        """Get a task by ID."""
        result = await self.session.execute(
            select(TaskTable).where(
                TaskTable.tenant_id == tenant_id,
                TaskTable.task_id == task_id,
            )
        )
        row = result.scalar_one_or_none()
        return self._row_to_model(row) if row else None

    async def get_many(self, tenant_id: UUID, task_ids: list[UUID]) -> dict[UUID, Task]:
        """Get multiple tasks by ID in a single query."""
        if not task_ids:
            return {}

        result = await self.session.execute(
            select(TaskTable).where(
                TaskTable.tenant_id == tenant_id,
                TaskTable.task_id.in_(task_ids),
            )
        )
        rows = result.scalars().all()
        return {row.task_id: self._row_to_model(row) for row in rows}

    async def list(
        self,
        tenant_id: UUID,
        status: TaskStatus | None = None,
        type: str | None = None,
        created_by_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Task], str | None]:
        """List tasks with optional filtering."""
        query = select(TaskTable).where(TaskTable.tenant_id == tenant_id)

        if status:
            query = query.where(TaskTable.status == status)
        if type:
            query = query.where(TaskTable.type == type)
        if created_by_id:
            created_by_ids = principal_id_variants(created_by_id)
            query = query.where(TaskTable.created_by_id.in_(created_by_ids))

        # Cursor-based pagination
        if cursor:
            cursor_time = datetime.fromisoformat(cursor)
            query = query.where(TaskTable.created_at < cursor_time)

        query = query.order_by(TaskTable.created_at.desc()).limit(limit + 1)

        result = await self.session.execute(query)
        rows = list(result.scalars().all())

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = rows[-1].created_at.isoformat()

        return [self._row_to_model(r) for r in rows], next_cursor

    async def update_status(
        self,
        tenant_id: UUID,
        task_id: UUID,
        new_status: TaskStatus,
        result: TaskResult | None = None,
        started_at: datetime | None = None,
    ) -> Task | None:
        """Update task status."""
        now = utc_now()

        values: dict[str, Any] = {
            "status": new_status,
            "updated_at": now,
        }

        if result:
            values["result_outcome"] = result.outcome.value
            values["result_data"] = result.result
            values["result_error"] = result.error
            values["result_artifacts"] = result.artifacts
            values["completed_at"] = result.completed_at

        if started_at is not None:
            values["started_at"] = started_at

        await self.session.execute(
            update(TaskTable)
            .where(TaskTable.tenant_id == tenant_id, TaskTable.task_id == task_id)
            .values(**values)
        )

        return await self.get(tenant_id, task_id)

    async def cancel(self, tenant_id: UUID, task_id: UUID, reason: str | None = None) -> Task | None:
        """Cancel a task."""
        task = await self.get(tenant_id, task_id)
        if not task or task.is_terminal():
            return task

        result = TaskResult(
            outcome=Outcome.CANCELED,
            error={"reason": reason} if reason else None,
            completed_at=utc_now(),
        )

        return await self.update_status(tenant_id, task_id, TaskStatus.CANCELED, result)

    async def requeue_with_backoff(
        self,
        tenant_id: UUID,
        task_id: UUID,
        increment_attempt: bool = True,
    ) -> Task | None:
        """Requeue a task with backoff delay."""
        task = await self.get(tenant_id, task_id)
        if not task:
            return None

        now = utc_now()
        attempt = task.attempt + 1 if increment_attempt else task.attempt

        # Calculate backoff: base * 2^(attempt-1), capped at max
        backoff = min(
            task.retry_backoff_seconds * (2 ** (attempt - 1)),
            settings.max_retry_backoff_seconds,
        )
        next_eligible_at = now + timedelta(seconds=backoff)

        values = {
            "status": TaskStatus.QUEUED,
            "attempt": attempt,
            "next_eligible_at": next_eligible_at,
            "updated_at": now,
            "started_at": None,
        }

        await self.session.execute(
            update(TaskTable)
            .where(TaskTable.tenant_id == tenant_id, TaskTable.task_id == task_id)
            .values(**values)
        )

        return await self.get(tenant_id, task_id)

    async def requeue_on_expiry(
        self,
        tenant_id: UUID,
        task_id: UUID,
        jitter_seconds: float = 0.0,
    ) -> Task | None:
        """
        Requeue a task after lease expiry (lost authority, NOT task failure).
        
        Critical difference from requeue_with_backoff:
        - Does NOT increment attempt counter (worker crash shouldn't eat retries)
        - Uses minimal jitter (0-5s) instead of exponential backoff
        - Prevents worker crashes from causing false terminal failures
        
        Args:
            tenant_id: Tenant ID
            task_id: Task ID
            jitter_seconds: Optional jitter to add (0-5s recommended for anti-storm)
        
        Returns:
            Updated task or None if not found
        """
        task = await self.get(tenant_id, task_id)
        if not task:
            return None

        now = utc_now()
        # CRITICAL: Do NOT increment attempt - lease expiry is "lost authority" not "task failed"
        attempt = task.attempt
        
        # Minimal jitter instead of exponential backoff
        next_eligible_at = now + timedelta(seconds=jitter_seconds)

        values = {
            "status": TaskStatus.QUEUED,
            "attempt": attempt,
            "next_eligible_at": next_eligible_at,
            "updated_at": now,
            "started_at": None,
        }

        await self.session.execute(
            update(TaskTable)
            .where(TaskTable.tenant_id == tenant_id, TaskTable.task_id == task_id)
            .values(**values)
        )

        return await self.get(tenant_id, task_id)

    async def _get_by_idempotency_key(self, tenant_id: UUID, key: str) -> Task | None:
        """Get task by idempotency key."""
        result = await self.session.execute(
            select(TaskTable).where(
                TaskTable.tenant_id == tenant_id,
                TaskTable.idempotency_key == key,
            )
        )
        row = result.scalar_one_or_none()
        return self._row_to_model(row) if row else None

    def _row_to_model(self, row: TaskTable) -> Task:
        """Convert database row to model."""
        result = None
        if row.result_outcome:
            result = TaskResult(
                outcome=Outcome(row.result_outcome),
                result=row.result_data,
                error=row.result_error,
                artifacts=row.result_artifacts,
                completed_at=row.completed_at,
            )

        return Task(
            task_id=row.task_id,
            tenant_id=row.tenant_id,
            type=row.type,
            payload=row.payload,
            payload_pointer=row.payload_pointer,
            created_by=Principal(
                kind=PrincipalKind(row.created_by_kind),
                id=row.created_by_id,
                instance_id=row.created_by_instance_id,
            ),
            principal_ai=row.principal_ai or row.created_by_id,
            requirements=TaskRequirements(**row.requirements),
            priority=row.priority,
            status=row.status,
            attempt=row.attempt,
            max_attempts=row.max_attempts,
            retry_backoff_seconds=row.retry_backoff_seconds,
            idempotency_key=row.idempotency_key,
            created_at=row.created_at,
            updated_at=row.updated_at,
            next_eligible_at=row.next_eligible_at,
            started_at=row.started_at,
            result=result,
            expected_outcome_kind=row.expected_outcome_kind,
            expected_artifact_mime=row.expected_artifact_mime,
            asyncgate_instance=row.asyncgate_instance,
        )


class LeaseRepository:
    """Repository for lease operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def claim_next(
        self,
        tenant_id: UUID,
        worker_id: str,
        capabilities: list[str] | None = None,
        accept_types: list[str] | None = None,
        max_tasks: int = 1,
        lease_ttl_seconds: int | None = None,
    ) -> list[Lease]:
        """Atomically claim next available tasks."""
        now = utc_now()
        ttl = min(
            lease_ttl_seconds or settings.default_lease_ttl_seconds,
            settings.max_lease_ttl_seconds,
        )
        expires_at = now + timedelta(seconds=ttl)

        # Build query for eligible tasks
        query = (
            select(TaskTable)
            .where(
                TaskTable.tenant_id == tenant_id,
                TaskTable.status == TaskStatus.QUEUED,
                or_(
                    TaskTable.next_eligible_at.is_(None),
                    TaskTable.next_eligible_at <= now,
                ),
            )
            .order_by(
                TaskTable.priority.desc(),
                TaskTable.created_at.asc(),
            )
            .limit(max_tasks)
            .with_for_update(skip_locked=True)
        )

        if accept_types:
            query = query.where(TaskTable.type.in_(accept_types))

        result = await self.session.execute(query)
        tasks = list(result.scalars().all())

        leases = []
        for task in tasks:
            # Check capability matching
            task_caps = task.requirements.get("capabilities", [])
            if task_caps and capabilities:
                if not set(task_caps).issubset(set(capabilities)):
                    continue
            elif task_caps and not capabilities:
                continue

            # Create lease (P1.1: track acquisition time and renewal count)
            lease_id = uuid4()
            lease_row = LeaseTable(
                lease_id=lease_id,
                tenant_id=tenant_id,
                task_id=task.task_id,
                worker_id=worker_id,
                expires_at=expires_at,
                created_at=now,
                acquired_at=now,  # P1.1: Track when lease was first acquired
                renewal_count=0,  # P1.1: Start at 0 renewals
            )
            self.session.add(lease_row)

            # Update task status
            task.status = TaskStatus.LEASED
            task.updated_at = now

            leases.append(
                Lease(
                    lease_id=lease_id,
                    tenant_id=tenant_id,
                    task_id=task.task_id,
                    worker_id=worker_id,
                    expires_at=expires_at,
                    created_at=now,
                    acquired_at=now,
                    renewal_count=0,
                )
            )

        await self.session.flush()
        return leases

    async def get(self, tenant_id: UUID, task_id: UUID) -> Lease | None:
        """Get active lease for a task."""
        result = await self.session.execute(
            select(LeaseTable).where(
                LeaseTable.tenant_id == tenant_id,
                LeaseTable.task_id == task_id,
            )
        )
        row = result.scalar_one_or_none()
        return self._row_to_model(row) if row else None

    async def validate(
        self,
        tenant_id: UUID,
        task_id: UUID,
        lease_id: UUID,
        worker_id: str,
    ) -> Lease | None:
        """Validate a lease is active and owned by worker."""
        result = await self.session.execute(
            select(LeaseTable).where(
                LeaseTable.tenant_id == tenant_id,
                LeaseTable.task_id == task_id,
                LeaseTable.lease_id == lease_id,
                LeaseTable.worker_id == worker_id,
                LeaseTable.expires_at > utc_now(),
            )
        )
        row = result.scalar_one_or_none()
        return self._row_to_model(row) if row else None

    async def renew(
        self,
        tenant_id: UUID,
        task_id: UUID,
        lease_id: UUID,
        worker_id: str,
        extend_by_seconds: int | None = None,
    ) -> Lease | None:
        """
        Renew a lease.
        
        P1.1: Enforces renewal limits to prevent lease hoarding:
        - max_lease_renewals: Maximum number of renewals allowed
        - max_lease_lifetime_seconds: Absolute maximum lifetime from acquisition
        
        Raises:
            LeaseRenewalLimitExceeded: If renewal count limit exceeded
            LeaseLifetimeExceeded: If absolute lifetime limit exceeded
        """
        # Get the full lease record (including renewal_count and acquired_at)
        result = await self.session.execute(
            select(LeaseTable)
            .where(
                LeaseTable.tenant_id == tenant_id,
                LeaseTable.task_id == task_id,
                LeaseTable.lease_id == lease_id,
                LeaseTable.worker_id == worker_id,
            )
            .with_for_update()
        )
        lease_row = result.scalar_one_or_none()
        
        if not lease_row:
            return None
        
        now = utc_now()
        
        # P1.1 ENFORCEMENT: Check renewal count limit
        if lease_row.renewal_count >= settings.max_lease_renewals:
            from asyncgate.engine.errors import LeaseRenewalLimitExceeded
            raise LeaseRenewalLimitExceeded(
                task_id=str(task_id),
                lease_id=str(lease_id),
                renewal_count=lease_row.renewal_count,
                max_renewals=settings.max_lease_renewals
            )
        
        # P1.1 ENFORCEMENT: Check absolute lifetime limit
        lifetime = now - lease_row.acquired_at
        lifetime_seconds = int(lifetime.total_seconds())
        max_lifetime_seconds = settings.max_lease_lifetime_seconds
        
        if lifetime_seconds >= max_lifetime_seconds:
            from asyncgate.engine.errors import LeaseLifetimeExceeded
            raise LeaseLifetimeExceeded(
                task_id=str(task_id),
                lease_id=str(lease_id),
                lifetime_seconds=lifetime_seconds,
                max_lifetime=max_lifetime_seconds
            )

        # Calculate new expiry
        extend_by = min(
            extend_by_seconds or settings.default_lease_ttl_seconds,
            settings.max_lease_ttl_seconds,
        )
        new_expires_at = now + timedelta(seconds=extend_by)
        new_renewal_count = lease_row.renewal_count + 1

        # Update lease: increment renewal_count and extend expiry
        await self.session.execute(
            update(LeaseTable)
            .where(LeaseTable.lease_id == lease_id)
            .values(
                expires_at=new_expires_at,
                renewal_count=new_renewal_count,  # P1.1: Increment counter
            )
        )

        # Return updated lease
        return Lease(
            lease_id=lease_row.lease_id,
            tenant_id=lease_row.tenant_id,
            task_id=lease_row.task_id,
            worker_id=lease_row.worker_id,
            expires_at=new_expires_at,
            created_at=lease_row.created_at,
            acquired_at=lease_row.acquired_at,
            renewal_count=new_renewal_count,
        )

    async def release(self, tenant_id: UUID, task_id: UUID) -> bool:
        """Release a lease."""
        result = await self.session.execute(
            delete(LeaseTable).where(
                LeaseTable.tenant_id == tenant_id,
                LeaseTable.task_id == task_id,
            )
        )
        return result.rowcount > 0

    async def get_expired(self, limit: int = 100, instance_id: str | None = None) -> list[Lease]:
        """
        Get expired leases for cleanup, optionally filtered by instance.
        
        Args:
            limit: Maximum number of leases to return
            instance_id: Optional instance filter for multi-instance deployments
        """
        now = utc_now()
        
        # Build query with join to tasks for instance filtering
        query = (
            select(LeaseTable)
            .join(
                TaskTable,
                and_(
                    LeaseTable.tenant_id == TaskTable.tenant_id,
                    LeaseTable.task_id == TaskTable.task_id,
                ),
            )
            .where(
                LeaseTable.expires_at < now,
                TaskTable.status.in_([TaskStatus.LEASED, TaskStatus.RUNNING]),
            )
        )
        
        # Filter by instance if provided (for multi-instance safety)
        if instance_id:
            query = query.where(TaskTable.asyncgate_instance == instance_id)
        
        query = query.limit(limit)
        
        result = await self.session.execute(query)
        return [self._row_to_model(r) for r in result.scalars().all()]

    def _row_to_model(self, row: LeaseTable) -> Lease:
        """Convert database row to model."""
        return Lease(
            lease_id=row.lease_id,
            tenant_id=row.tenant_id,
            task_id=row.task_id,
            worker_id=row.worker_id,
            expires_at=row.expires_at,
            created_at=row.created_at,
            acquired_at=row.acquired_at,
            renewal_count=row.renewal_count,
        )


class ReceiptRepository:
    """Repository for receipt operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        tenant_id: UUID,
        receipt_type: ReceiptType,
        from_principal: Principal,
        to_principal: Principal,
        task_id: UUID | None = None,
        lease_id: UUID | None = None,
        schedule_id: str | None = None,
        parents: list[UUID] | None = None,
        body: dict[str, Any] | None = None,
        receipt_hash: str | None = None,
    ) -> Receipt:
        """Create a new receipt."""
        import json
        
        now = utc_now()
        receipt_id = uuid4()

        # T5.1: Receipt size limits - receipts are contracts, not chat
        # Prevent ledger bloat and abuse
        
        # Validate body size (64KB max)
        if body:
            body_json = json.dumps(body, separators=(',', ':'))
            body_size = len(body_json.encode('utf-8'))
            if body_size > MAX_RECEIPT_BODY_BYTES:
                raise ValueError(
                    f"Receipt body too large: {body_size} bytes "
                    f"(max {MAX_RECEIPT_BODY_BYTES} bytes). "
                    f"Receipt bodies are contracts, not chat messages. "
                    f"Store large payloads externally and reference via artifacts or delivery_proof."
                )
        
        # Validate parents count (10 max - prevent mega-chains)
        if parents and len(parents) > MAX_RECEIPT_PARENTS:
            raise ValueError(
                f"Too many parent receipts: {len(parents)} (max {MAX_RECEIPT_PARENTS}). "
                f"Receipt bodies are contracts, not chat messages. "
                f"Avoid creating deep chains - use flat structures where possible."
            )
        
        # Validate artifacts count if present (100 max - prevent stuffing)
        if body and 'artifacts' in body:
            artifacts = body['artifacts']
            if isinstance(artifacts, list) and len(artifacts) > MAX_RECEIPT_ARTIFACTS:
                raise ValueError(
                    f"Too many artifacts: {len(artifacts)} (max {MAX_RECEIPT_ARTIFACTS}). "
                    f"Receipt bodies are contracts, not chat messages. "
                    f"If you have this many artifacts, you're doing it wrong."
                )

        # T1.1: Enforce parent linkage on terminal receipts
        if is_terminal_type(receipt_type):
            if not parents or len(parents) == 0:
                raise ValueError(
                    f"Terminal receipt type {receipt_type.value} must specify parents. "
                    f"Without parent linkage, obligations remain open forever (haunted bootstrap). "
                    f"Terminal receipts discharge obligations - they must reference what they terminate."
                )
            
            # Validate parent exists and shares tenant
            for parent_id in parents:
                parent_exists = await self.session.execute(
                    select(ReceiptTable.receipt_id).where(
                        ReceiptTable.tenant_id == tenant_id,
                        ReceiptTable.receipt_id == parent_id,
                    ).limit(1)
                )
                if not parent_exists.scalar_one_or_none():
                    raise ValueError(
                        f"Parent receipt {parent_id} not found for tenant {tenant_id}. "
                        f"Terminal receipts must reference existing obligations."
                    )

        # T1.2: Enforce locatability on success terminal receipts (Phase 1 - lenient)
        # If success without locatability: strip parents so obligation stays open, emit anomaly
        parents_to_use = parents
        emit_locatability_anomaly = False
        
        if receipt_type == ReceiptType.TASK_COMPLETED:
            body_data = body or {}
            has_artifacts = body_data.get('artifacts') is not None
            has_delivery_proof = body_data.get('delivery_proof') is not None
            
            if not (has_artifacts or has_delivery_proof):
                # Phase 1 (lenient): Allow creation but strip parents
                # This keeps obligation open until proper locatable receipt created
                parents_to_use = []
                emit_locatability_anomaly = True
                
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(
                    f"SUCCESS WITHOUT LOCATABILITY: Receipt {receipt_id} for task {task_id} "
                    f"lacks artifacts or delivery_proof. Parents stripped - obligation stays open. "
                    f"Emitting anomaly receipt to principal."
                )
                
                # In Phase 2 (strict), this would raise ValueError instead

        receipt_hash_to_use = receipt_hash
        if not receipt_hash_to_use:
            receipt_hash_to_use = compute_receipt_hash(
                receipt_type=receipt_type,
                task_id=task_id,
                from_principal=from_principal,
                to_principal=to_principal,
                lease_id=lease_id,
                body=body,
                parents=parents_to_use,
            )

        # Check for duplicate by hash
        if receipt_hash_to_use:
            existing = await self._get_by_hash(tenant_id, receipt_hash_to_use)
            if existing:
                return existing

        receipt_row = ReceiptTable(
            tenant_id=tenant_id,
            receipt_id=receipt_id,
            receipt_type=receipt_type,
            created_at=now,
            from_kind=from_principal.kind,
            from_id=from_principal.id,
            to_kind=to_principal.kind,
            to_id=to_principal.id,
            task_id=task_id,
            lease_id=lease_id,
            schedule_id=schedule_id,
            parents=[str(p) for p in (parents_to_use or [])],  # Use potentially stripped parents
            body=body or {},
            hash=receipt_hash_to_use,
            asyncgate_instance=settings.instance_id,
        )

        self.session.add(receipt_row)
        await self.session.flush()
        
        # Emit anomaly receipt if locatability was missing
        if emit_locatability_anomaly:
            trace_id = None
            if isinstance(body, dict):
                trace_id = body.get("trace_id")
            if not trace_id:
                from asyncgate.observability.trace import get_trace_id
                trace_id = get_trace_id()

            anomaly_body = {
                "anomaly_type": "locatability_missing",
                "message": (
                    f"Success receipt {receipt_id} lacks locatability (no artifacts or delivery_proof). "
                    "Obligation remains open until properly locatable success is provided."
                ),
                "original_receipt_id": str(receipt_id),
                "stripped_parents": [str(p) for p in (parents or [])],
            }
            if trace_id:
                anomaly_body["trace_id"] = trace_id

            anomaly_receipt = ReceiptTable(
                tenant_id=tenant_id,
                receipt_id=uuid4(),
                receipt_type=ReceiptType.SYSTEM_ANOMALY,
                created_at=utc_now(),
                from_kind=PrincipalKind.SERVICE,
                from_id=SERVICE_PRINCIPAL_ID,
                to_kind=to_principal.kind,
                to_id=to_principal.id,
                task_id=task_id,
                lease_id=None,
                schedule_id=None,
                parents=[str(receipt_id)],  # References the stripped-parent receipt
                body=anomaly_body,
                hash=None,
                asyncgate_instance=settings.instance_id,
            )
            self.session.add(anomaly_receipt)
            await self.session.flush()

        return self._row_to_model(receipt_row)

    async def list(
        self,
        tenant_id: UUID,
        to_kind: PrincipalKind,
        to_id: str,
        since_receipt_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[list[Receipt], UUID | None]:
        """List receipts for a principal."""
        to_ids = principal_id_variants(to_id)
        query = select(ReceiptTable).where(
            ReceiptTable.tenant_id == tenant_id,
            ReceiptTable.to_kind == to_kind,
            ReceiptTable.to_id.in_(to_ids),
        )

        if since_receipt_id:
            # Get created_at of cursor receipt
            cursor_result = await self.session.execute(
                select(ReceiptTable.created_at).where(
                    ReceiptTable.tenant_id == tenant_id,
                    ReceiptTable.receipt_id == since_receipt_id,
                )
            )
            cursor_time = cursor_result.scalar_one_or_none()
            if cursor_time:
                query = query.where(ReceiptTable.created_at > cursor_time)

        query = query.order_by(ReceiptTable.created_at.asc()).limit(limit + 1)

        result = await self.session.execute(query)
        rows = list(result.scalars().all())

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = rows[-1].receipt_id

        return [self._row_to_model(r) for r in rows], next_cursor

    async def count_by_status(self, tenant_id: UUID) -> dict[TaskStatus, int]:
        """Count tasks by status for a tenant."""
        result = await self.session.execute(
            select(TaskTable.status, func.count())
            .where(TaskTable.tenant_id == tenant_id)
            .group_by(TaskTable.status)
        )
        return {row[0]: row[1] for row in result.all()}

    async def list_filtered(
        self,
        tenant_id: UUID,
        task_id: UUID | None = None,
        lease_id: UUID | None = None,
        receipt_type: ReceiptType | None = None,
        task_status: TaskStatus | None = None,
        since_receipt_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[list[Receipt], UUID | None]:
        """List receipts with optional task/lease/status filters."""
        query = select(ReceiptTable).where(ReceiptTable.tenant_id == tenant_id)

        if task_id:
            query = query.where(ReceiptTable.task_id == task_id)
        if lease_id:
            query = query.where(ReceiptTable.lease_id == lease_id)
        if receipt_type:
            query = query.where(ReceiptTable.receipt_type == receipt_type)
        if task_status:
            query = query.join(
                TaskTable,
                and_(
                    ReceiptTable.tenant_id == TaskTable.tenant_id,
                    ReceiptTable.task_id == TaskTable.task_id,
                ),
            ).where(TaskTable.status == task_status)

        if since_receipt_id:
            cursor_result = await self.session.execute(
                select(ReceiptTable.created_at).where(
                    ReceiptTable.tenant_id == tenant_id,
                    ReceiptTable.receipt_id == since_receipt_id,
                )
            )
            cursor_time = cursor_result.scalar_one_or_none()
            if cursor_time:
                query = query.where(ReceiptTable.created_at > cursor_time)

        query = query.order_by(ReceiptTable.created_at.asc()).limit(limit + 1)

        result = await self.session.execute(query)
        rows = list(result.scalars().all())

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = rows[-1].receipt_id

        return [self._row_to_model(r) for r in rows], next_cursor

    async def mark_delivered(self, tenant_id: UUID, receipt_ids: list[UUID]) -> int:
        """Mark receipts as delivered."""
        if not receipt_ids:
            return 0

        result = await self.session.execute(
            update(ReceiptTable)
            .where(
                ReceiptTable.tenant_id == tenant_id,
                ReceiptTable.receipt_id.in_(receipt_ids),
                ReceiptTable.delivered_at.is_(None),
            )
            .values(delivered_at=utc_now())
        )
        return result.rowcount

    async def get_undelivered_for_task(
        self,
        tenant_id: UUID,
        task_id: UUID,
        to_kind: PrincipalKind,
        to_id: str,
    ) -> list[Receipt]:
        """Get undelivered receipts for a task."""
        to_ids = principal_id_variants(to_id)
        result = await self.session.execute(
            select(ReceiptTable).where(
                ReceiptTable.tenant_id == tenant_id,
                ReceiptTable.task_id == task_id,
                ReceiptTable.to_kind == to_kind,
                ReceiptTable.to_id.in_(to_ids),
                ReceiptTable.delivered_at.is_(None),
            )
        )
        return [self._row_to_model(r) for r in result.scalars().all()]

    async def get_undelivered_by_type(
        self,
        tenant_id: UUID,
        to_kind: PrincipalKind,
        to_id: str,
        receipt_type: ReceiptType,
    ) -> list[Receipt]:
        """Get undelivered receipts of a specific type for a principal."""
        to_ids = principal_id_variants(to_id)
        result = await self.session.execute(
            select(ReceiptTable).where(
                ReceiptTable.tenant_id == tenant_id,
                ReceiptTable.to_kind == to_kind,
                ReceiptTable.to_id.in_(to_ids),
                ReceiptTable.receipt_type == receipt_type,
                ReceiptTable.delivered_at.is_(None),
            )
        )
        return [self._row_to_model(r) for r in result.scalars().all()]

    async def _get_by_hash(self, tenant_id: UUID, receipt_hash: str) -> Receipt | None:
        """Get receipt by hash for deduplication."""
        result = await self.session.execute(
            select(ReceiptTable).where(
                ReceiptTable.tenant_id == tenant_id,
                ReceiptTable.hash == receipt_hash,
            )
        )
        row = result.scalar_one_or_none()
        return self._row_to_model(row) if row else None

    async def get_by_id(self, tenant_id: UUID, receipt_id: UUID) -> Receipt | None:
        """
        Get a specific receipt by ID.
        
        Used for receipt chain traversal and obligation verification.
        """
        result = await self.session.execute(
            select(ReceiptTable).where(
                ReceiptTable.tenant_id == tenant_id,
                ReceiptTable.receipt_id == receipt_id,
            )
        )
        row = result.scalar_one_or_none()
        return self._row_to_model(row) if row else None

    async def get_task_obligation(
        self,
        tenant_id: UUID,
        task_id: UUID,
        owner: Principal | None = None,
    ) -> Receipt | None:
        """Get the obligation receipt associated with a task (task.assigned)."""
        query = select(ReceiptTable).where(
            ReceiptTable.tenant_id == tenant_id,
            ReceiptTable.task_id == task_id,
            ReceiptTable.receipt_type == ReceiptType.TASK_ASSIGNED,
        )

        if owner:
            owner_ids = principal_id_variants(owner.id)
            query = query.where(
                ReceiptTable.to_kind == owner.kind,
                ReceiptTable.to_id.in_(owner_ids),
            )

        result = await self.session.execute(
            query.order_by(ReceiptTable.created_at.desc()).limit(1)
        )
        row = result.scalar_one_or_none()
        return self._row_to_model(row) if row else None

    async def get_by_parent(
        self,
        tenant_id: UUID,
        parent_receipt_id: UUID,
        limit: int = 50,
    ) -> list[Receipt]:
        """
        Get receipts that reference a specific parent.
        
        Used for finding terminal receipts that discharge an obligation.
        The parents field in receipts is a JSON array, so we need to check containment.
        
        Args:
            tenant_id: Tenant identifier
            parent_receipt_id: The receipt ID to search for in parents arrays
            limit: Maximum results to return
            
        Returns:
            List of receipts that have parent_receipt_id in their parents array
        """
        # PostgreSQL JSONB containment check
        # We need to check if the parents array contains the UUID as a string
        parent_str = str(parent_receipt_id)
        
        result = await self.session.execute(
            select(ReceiptTable)
            .where(
                ReceiptTable.tenant_id == tenant_id,
                ReceiptTable.parents.contains([parent_str]),  # JSONB array containment
            )
            .order_by(ReceiptTable.created_at.asc())
            .limit(limit)
        )
        return [self._row_to_model(r) for r in result.scalars().all()]

    async def has_terminator(
        self,
        tenant_id: UUID,
        parent_receipt_id: UUID,
    ) -> bool:
        """
        Check if a terminator exists for a parent receipt (fast EXISTS query).
        
        DB-driven termination check: Does evidence exist that discharges this obligation?
        Uses EXISTS for O(1) performance - doesn't load receipt data.
        
        Args:
            tenant_id: Tenant identifier
            parent_receipt_id: The obligation receipt to check
            
        Returns:
            True if any receipt references this as parent, False otherwise
        """
        parent_str = str(parent_receipt_id)
        
        result = await self.session.execute(
            select(ReceiptTable.receipt_id)
            .where(
                ReceiptTable.tenant_id == tenant_id,
                ReceiptTable.parents.contains([parent_str]),
                ReceiptTable.receipt_type.in_(TERMINAL_RECEIPT_TYPES),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def get_terminators(
        self,
        tenant_id: UUID,
        parent_receipt_id: UUID,
        limit: int = 50,
    ) -> list[Receipt]:
        """
        Get all receipts that terminate a parent (may include retries/duplicates).
        
        Returns receipts that reference parent_receipt_id in their parents array.
        Agents use this to walk provenance chains.
        
        Args:
            tenant_id: Tenant identifier
            parent_receipt_id: The obligation receipt
            limit: Maximum results to return
            
        Returns:
            List of receipts that reference this parent, ordered by created_at
        """
        parent_str = str(parent_receipt_id)

        result = await self.session.execute(
            select(ReceiptTable)
            .where(
                ReceiptTable.tenant_id == tenant_id,
                ReceiptTable.parents.contains([parent_str]),
                ReceiptTable.receipt_type.in_(TERMINAL_RECEIPT_TYPES),
            )
            .order_by(ReceiptTable.created_at.asc())
            .limit(limit)
        )
        return [self._row_to_model(r) for r in result.scalars().all()]

    async def get_latest_terminator(
        self,
        tenant_id: UUID,
        parent_receipt_id: UUID,
    ) -> Receipt | None:
        """
        Get the most recent terminator for a parent receipt.
        
        Simplifies agent logic: when multiple terminators exist (retries, duplicates),
        return the canonical one (most recent by created_at).
        
        Args:
            tenant_id: Tenant identifier
            parent_receipt_id: The obligation receipt
            
        Returns:
            Most recent terminating receipt, or None if no terminator exists
        """
        parent_str = str(parent_receipt_id)
        
        result = await self.session.execute(
            select(ReceiptTable)
            .where(
                ReceiptTable.tenant_id == tenant_id,
                ReceiptTable.parents.contains([parent_str]),
                ReceiptTable.receipt_type.in_(TERMINAL_RECEIPT_TYPES),
            )
            .order_by(ReceiptTable.created_at.desc())  # Most recent first
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return self._row_to_model(row) if row else None

    async def list_open_obligations(
        self,
        tenant_id: UUID,
        to_kind: PrincipalKind,
        to_id: str,
        since_receipt_id: UUID | None = None,
        limit: int = 50,
    ) -> tuple[list[Receipt], UUID | None]:
        """
        List open obligations for a principal.
        
        OPTIMIZED (P0.1): Uses batch termination check instead of N+1 queries.
        Leverages GIN index on parents JSONB column for fast containment checks.
        
        An obligation is "open" if:
        1. It's a receipt type that can create obligations (in TERMINATION_RULES)
        2. It's addressed to the specified principal
        3. No terminal child receipt exists that references it as a parent
        
        This is the core bootstrap primitive: dump of uncommitted obligations.
        
        Args:
            tenant_id: Tenant identifier
            to_kind: Principal kind to filter by
            to_id: Principal ID to filter by
            since_receipt_id: Cursor for pagination (return obligations after this)
            limit: Maximum results to return
            
        Returns:
            Tuple of (obligations list, next_cursor)
        """
        # Import here to avoid circular dependency
        from asyncgate.models.termination import TERMINATION_RULES
        
        # Get obligation types (receipt types that create obligations)
        obligation_types = list(TERMINATION_RULES.keys())
        
        if not obligation_types:
            # No obligation types registered yet
            return [], None
        
        # Fetch candidates with hard cap to prevent runaway queries
        # Factor of 3 allows filtering, but cap at 1000 absolute max
        candidate_limit = min(limit * 3, 1000)
        
        # Base query: receipts to this principal of obligation types
        to_ids = principal_id_variants(to_id)
        query = select(ReceiptTable).where(
            ReceiptTable.tenant_id == tenant_id,
            ReceiptTable.to_kind == to_kind,
            ReceiptTable.to_id.in_(to_ids),
            ReceiptTable.receipt_type.in_(obligation_types),
        )
        
        # Pagination cursor
        if since_receipt_id:
            cursor_result = await self.session.execute(
                select(ReceiptTable.created_at).where(
                    ReceiptTable.tenant_id == tenant_id,
                    ReceiptTable.receipt_id == since_receipt_id,
                )
            )
            cursor_time = cursor_result.scalar_one_or_none()
            if cursor_time:
                query = query.where(ReceiptTable.created_at > cursor_time)
        
        query = query.order_by(ReceiptTable.created_at.asc()).limit(candidate_limit + 1)
        
        result = await self.session.execute(query)
        candidate_rows = list(result.scalars().all())
        
        if not candidate_rows:
            return [], None

        more_candidates = len(candidate_rows) > candidate_limit
        if more_candidates:
            candidate_rows = candidate_rows[:candidate_limit]
        
        # BATCH TERMINATION CHECK (P0.1 optimization)
        # Single query instead of N queries - uses GIN index on parents
        candidate_ids = [str(row.receipt_id) for row in candidate_rows]
        
        # Query: Find all receipts whose parents array contains any candidate ID
        # PostgreSQL JSONB array overlap using the GIN index
        terminated_result = await self.session.execute(
            select(ReceiptTable.parents)
            .where(
                ReceiptTable.tenant_id == tenant_id,
                ReceiptTable.receipt_type.in_(TERMINAL_RECEIPT_TYPES),
                # Check if parents contains any of our candidate IDs
                # This uses the GIN index: idx_receipts_parents_gin
                func.jsonb_array_length(ReceiptTable.parents) > 0,
            )
        )
        
        # Build set of terminated receipt IDs by checking overlap
        terminated_ids = set()
        candidate_id_set = set(candidate_ids)
        
        for row in terminated_result:
            # Check if any parent matches our candidates
            for parent_str in row.parents:
                if parent_str in candidate_id_set:
                    terminated_ids.add(UUID(parent_str))
        
        # Filter candidates: keep only those NOT terminated
        open_obligations = []
        
        for row in candidate_rows:
            if row.receipt_id not in terminated_ids:
                open_obligations.append(self._row_to_model(row))
                
            # Stop if we have enough
            if len(open_obligations) >= limit:
                break
        
        # Determine next cursor
        next_cursor = None
        if len(open_obligations) >= limit:
            next_cursor = open_obligations[-1].receipt_id
        elif more_candidates and candidate_rows:
            next_cursor = candidate_rows[-1].receipt_id
        
        return open_obligations[:limit], next_cursor

    def _row_to_model(self, row: ReceiptTable) -> Receipt:
        """Convert database row to model."""
        return Receipt(
            receipt_id=row.receipt_id,
            tenant_id=row.tenant_id,
            receipt_type=row.receipt_type,
            created_at=row.created_at,
            from_=Principal(kind=PrincipalKind(row.from_kind), id=row.from_id),
            to_=Principal(kind=PrincipalKind(row.to_kind), id=row.to_id),
            task_id=row.task_id,
            lease_id=row.lease_id,
            schedule_id=row.schedule_id,
            parents=[UUID(p) for p in row.parents],
            body=row.body,
            hash=row.hash,
            asyncgate_instance=row.asyncgate_instance,
            delivered_at=row.delivered_at,
        )


class ProgressRepository:
    """Repository for progress operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def update(
        self,
        tenant_id: UUID,
        task_id: UUID,
        progress: dict[str, Any],
    ) -> Progress:
        """Update or create progress for a task."""
        now = utc_now()

        # Upsert
        result = await self.session.execute(
            select(ProgressTable).where(
                ProgressTable.tenant_id == tenant_id,
                ProgressTable.task_id == task_id,
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.progress = progress
            existing.updated_at = now
        else:
            progress_row = ProgressTable(
                tenant_id=tenant_id,
                task_id=task_id,
                progress=progress,
                updated_at=now,
            )
            self.session.add(progress_row)

        await self.session.flush()

        return Progress(
            tenant_id=tenant_id,
            task_id=task_id,
            progress=progress,
            updated_at=now,
        )

    async def get(self, tenant_id: UUID, task_id: UUID) -> Progress | None:
        """Get progress for a task."""
        result = await self.session.execute(
            select(ProgressTable).where(
                ProgressTable.tenant_id == tenant_id,
                ProgressTable.task_id == task_id,
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            return None

        return Progress(
            tenant_id=row.tenant_id,
            task_id=row.task_id,
            progress=row.progress,
            updated_at=row.updated_at,
        )


class RelationshipRepository:
    """Repository for relationship tracking."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(
        self,
        tenant_id: UUID,
        principal_kind: PrincipalKind,
        principal_id: str,
        principal_instance_id: str | None = None,
    ) -> Relationship:
        """Create or update relationship."""
        now = utc_now()

        result = await self.session.execute(
            select(RelationshipTable).where(
                RelationshipTable.tenant_id == tenant_id,
                RelationshipTable.principal_kind == principal_kind,
                RelationshipTable.principal_id == principal_id,
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.last_seen_at = now
            existing.sessions_count += 1
            if principal_instance_id:
                existing.principal_instance_id = principal_instance_id
        else:
            existing = RelationshipTable(
                tenant_id=tenant_id,
                principal_kind=principal_kind,
                principal_id=principal_id,
                principal_instance_id=principal_instance_id,
                first_seen_at=now,
                last_seen_at=now,
                sessions_count=1,
            )
            self.session.add(existing)

        await self.session.flush()

        return Relationship(
            tenant_id=existing.tenant_id,
            principal_kind=PrincipalKind(existing.principal_kind),
            principal_id=existing.principal_id,
            principal_instance_id=existing.principal_instance_id,
            first_seen_at=existing.first_seen_at,
            last_seen_at=existing.last_seen_at,
            sessions_count=existing.sessions_count,
        )
