"""AsyncGate core engine - canonical operations."""

import asyncio
import logging
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from asyncgate.config import ReceiptMode, settings
from asyncgate.db.repositories import (
    LeaseRepository,
    ProgressRepository,
    ReceiptRepository,
    RelationshipRepository,
    TaskRepository,
)
from asyncgate.engine.errors import (
    InvalidStateTransition,
    LeaseInvalidOrExpired,
    TaskNotFound,
    UnauthorizedError,
)
from asyncgate.integrations import get_receiptgate_client
from asyncgate.models import (
    Principal,
    PrincipalKind,
    Receipt,
    ReceiptType,
    Task,
    TaskRequirements,
    TaskResult,
    TaskStatus,
    TaskSummary,
)
from asyncgate.models.enums import Outcome
from asyncgate.models.receipt import ReceiptBody, compute_receipt_hash
from asyncgate.observability.metrics import metrics
from asyncgate.observability.trace import ensure_trace_id, get_trace_id
from asyncgate.principals import (
    SERVICE_PRINCIPAL_ID,
    SYSTEM_PRINCIPAL_ID,
    is_internal_principal_id,
    is_system,
    normalize_external,
)
from asyncgate.receipts import to_memorygate_receipt
from asyncgate.utils.time import utc_now

logger = logging.getLogger(__name__)

class AsyncGateEngine:
    """Core engine implementing canonical AsyncGate operations."""

    def __init__(self, session: AsyncSession) -> None:
        # Annotated so the repository types resolve. Without a return
        # annotation this body is untyped, mypy gives every `self.<repo>` a
        # partial type, and every call through them degrades to a spurious
        # "has no attribute" on results that are correctly typed at source.
        self.session: AsyncSession = session
        self.tasks: TaskRepository = TaskRepository(session)
        self.leases: LeaseRepository = LeaseRepository(session)
        self.receipts: ReceiptRepository = ReceiptRepository(session)
        self.progress: ProgressRepository = ProgressRepository(session)
        self.relationships: RelationshipRepository = RelationshipRepository(session)

    def _service_principal(self) -> Principal:
        """Return the canonical service principal for AsyncGate-emitted receipts."""
        return Principal(kind=PrincipalKind.SERVICE, id=SERVICE_PRINCIPAL_ID)

    def _system_owner(self) -> Principal:
        """Return the canonical system owner principal."""
        return Principal(kind=PrincipalKind.SYSTEM, id=SYSTEM_PRINCIPAL_ID)

    def _normalize_principal(self, principal: Principal) -> Principal:
        """Normalize external principal IDs without rewriting ownership."""
        return Principal(
            kind=principal.kind,
            id=normalize_external(principal.id),
            instance_id=principal.instance_id,
        )

    def _resolve_obligation_owner(self, created_by: Principal) -> Principal:
        """Resolve obligation owner from the creator principal."""
        normalized = self._normalize_principal(created_by)
        if is_system(normalized.id) or normalized.id == SERVICE_PRINCIPAL_ID:
            return self._system_owner()
        return normalized

    async def _get_task_obligation(
        self,
        tenant_id: UUID,
        task_id: UUID,
        owner_hint: Principal | None = None,
    ) -> Receipt | None:
        """Fetch the task.assigned receipt for a task, if present."""
        obligation = await self.receipts.get_task_obligation(
            tenant_id=tenant_id,
            task_id=task_id,
            owner=owner_hint,
        )
        if not obligation and owner_hint:
            obligation = await self.receipts.get_task_obligation(
                tenant_id=tenant_id,
                task_id=task_id,
                owner=None,
            )
        return obligation

    async def _get_task_owner(self, tenant_id: UUID, task: Task) -> Principal:
        """Resolve task owner via obligation receipt, falling back to creator."""
        owner_hint = self._resolve_obligation_owner(task.created_by)
        obligation = await self._get_task_obligation(tenant_id, task.task_id, owner_hint)
        if obligation:
            return obligation.to_
        return owner_hint

    async def _get_task_obligation_or_raise(
        self,
        tenant_id: UUID,
        task_id: UUID,
        owner_hint: Principal | None = None,
    ) -> Receipt:
        """Fetch task obligation receipt or raise if missing."""
        obligation = await self._get_task_obligation(tenant_id, task_id, owner_hint)
        if not obligation:
            raise ValueError(f"Missing task.assigned receipt for task {task_id}")
        return obligation

    # =========================================================================
    # TASKER Operations (post/observe/control)
    # =========================================================================

    async def bootstrap(
        self,
        tenant_id: UUID,
        principal: Principal,
        since_receipt_id: UUID | None = None,
        max_items: int | None = None,
    ) -> dict[str, Any]:
        """
        DEPRECATED: Use list_open_obligations() instead.

        Legacy bootstrap with attention semantics. Simplified to remove
        task-state queries (wrong model). Returns minimal response for
        API compatibility while clients migrate to asyncgate.bootstrap.

        Bootstrap is idempotent and safe to call frequently.
        """
        principal = self._normalize_principal(principal)
        max_items = min(
            max_items or settings.default_bootstrap_max_items,
            settings.max_bootstrap_max_items,
        )

        # Update relationship
        relationship = await self.relationships.upsert(
            tenant_id=tenant_id,
            principal_kind=principal.kind,
            principal_id=principal.id,
            principal_instance_id=principal.instance_id,
        )

        # Get inbox receipts
        inbox_receipts, next_cursor = await self.receipts.list(
            tenant_id=tenant_id,
            to_kind=principal.kind,
            to_id=principal.id,
            since_receipt_id=since_receipt_id,
            limit=max_items,
        )

        # Mark receipts as delivered (telemetry only - not control logic)
        if inbox_receipts:
            receipt_ids = [r.receipt_id for r in inbox_receipts]
            await self.receipts.mark_delivered(tenant_id, receipt_ids)

        # Build anomalies (from receipts or detected conditions)
        anomalies = [
            r.body for r in inbox_receipts
            if r.receipt_type == ReceiptType.SYSTEM_ANOMALY
        ]

        # DEPRECATED: Task-state bucketing removed (Tier 3 cleanup)
        # Clients should migrate to asyncgate.bootstrap for correct model.
        # Return empty lists for backward compatibility.

        return {
            "server": {
                "name": "AsyncGate",
                "version": "0.1.0",
                "instance_id": settings.instance_id,
                "uptime": 0,  # TODO: track server uptime
                "environment": settings.env.value,
            },
            "relationship": {
                "principal_kind": relationship.principal_kind.value,
                "principal_id": relationship.principal_id,
                "principal_instance_id": relationship.principal_instance_id,
                "first_seen_at": relationship.first_seen_at.isoformat(),
                "last_seen_at": relationship.last_seen_at.isoformat(),
                "sessions_count": relationship.sessions_count,
            },
            "attention": {
                "inbox_receipts": [self._receipt_to_dict(r) for r in inbox_receipts],
                "assigned_tasks": [],  # REMOVED: Use asyncgate.bootstrap
                "waiting_results": [],  # REMOVED: Use asyncgate.bootstrap
                "running_or_scheduled": [],  # REMOVED: Use asyncgate.bootstrap
                "anomalies": anomalies,
            },
            "cursor": {
                "latest_receipt_id": str(next_cursor) if next_cursor else None,
            },
        }

    async def create_task(
        self,
        tenant_id: UUID,
        type: str,
        payload: dict[str, Any],
        created_by: Principal,
        principal_ai: str,
        payload_pointer: str | None = None,
        requirements: dict[str, Any] | None = None,
        expected_outcome_kind: str | None = None,
        expected_artifact_mime: str | None = None,
        priority: int | None = None,
        idempotency_key: str | None = None,
        max_attempts: int | None = None,
        retry_backoff_seconds: int | None = None,
        delay_seconds: int | None = None,
        actor_is_internal: bool = False,
    ) -> dict[str, Any]:
        """
        Create a new task.

        If idempotency_key is provided and matches existing task, returns that task.
        """
        created_by = self._normalize_principal(created_by)
        if is_internal_principal_id(created_by.id) and not actor_is_internal:
            raise UnauthorizedError(
                "Internal principal IDs require authenticated internal access"
            )
        task_requirements = None
        if requirements:
            task_requirements = TaskRequirements(**requirements)

        if not principal_ai:
            raise ValueError("principal_ai is required")

        task = await self.tasks.create(
            tenant_id=tenant_id,
            type=type,
            payload=payload,
            payload_pointer=payload_pointer,
            created_by=created_by,
            principal_ai=principal_ai,
            requirements=task_requirements,
            expected_outcome_kind=expected_outcome_kind,
            expected_artifact_mime=expected_artifact_mime,
            priority=priority or settings.default_priority,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            delay_seconds=delay_seconds,
        )

        owner = self._resolve_obligation_owner(created_by)
        service_principal = self._service_principal()

        # Emit task.assigned receipt
        await self._emit_receipt(
            tenant_id=tenant_id,
            receipt_type=ReceiptType.TASK_ASSIGNED,
            from_principal=service_principal,
            to_principal=owner,
            task_id=task.task_id,
            body=ReceiptBody.task_assigned(
                instructions=f"Execute task type: {type}",
                requirements=task.requirements.model_dump() if task.requirements else None,
            ),
        )

        return {"task_id": task.task_id, "status": task.status.value}

    async def get_task(
        self,
        tenant_id: UUID,
        task_id: UUID,
    ) -> dict[str, Any]:
        """Get a task by ID, including result if terminal."""
        task = await self.tasks.get(tenant_id, task_id)
        if not task:
            raise TaskNotFound(str(task_id))

        result = self._task_to_dict(task)

        # Include progress if available
        progress = await self.progress.get(tenant_id, task_id)
        if progress:
            result["progress"] = progress.progress

        return result

    async def list_tasks(
        self,
        tenant_id: UUID,
        status: str | None = None,
        type: str | None = None,
        created_by_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List tasks with optional filtering."""
        task_status = TaskStatus(status) if status else None
        limit = min(limit or settings.default_list_limit, settings.max_list_limit)

        tasks, next_cursor = await self.tasks.list(
            tenant_id=tenant_id,
            status=task_status,
            type=type,
            created_by_id=created_by_id,
            limit=limit,
            cursor=cursor,
        )

        return {
            "tasks": [self._task_to_dict(t) for t in tasks],
            "next_cursor": next_cursor,
        }

    async def cancel_task(
        self,
        tenant_id: UUID,
        task_id: UUID,
        principal: Principal,
        reason: str | None = None,
        actor_is_internal: bool = False,
    ) -> dict[str, Any]:
        """
        Cancel a task.

        P0.2: All state changes + receipt emissions are atomic via savepoint.
        """
        principal = self._normalize_principal(principal)
        task = await self.tasks.get(tenant_id, task_id)
        if not task:
            raise TaskNotFound(str(task_id))

        owner_hint = self._resolve_obligation_owner(task.created_by)
        obligation = await self._get_task_obligation_or_raise(
            tenant_id=tenant_id,
            task_id=task_id,
            owner_hint=owner_hint,
        )
        owner = obligation.to_
        parents = [obligation.receipt_id]

        # Authorization: only task owner or auth-authorized internal actors can cancel
        if not actor_is_internal:
            if owner.kind == PrincipalKind.SYSTEM and owner.id == SYSTEM_PRINCIPAL_ID:
                raise UnauthorizedError(
                    "Internal authorization required to cancel system-owned tasks"
                )
            if owner.id != principal.id or owner.kind != principal.kind:
                raise UnauthorizedError(
                    f"Principal {principal.kind.value}:{principal.id} "
                    f"not authorized to cancel task owned by "
                    f"{owner.kind.value}:{owner.id}"
                )

        if task.is_terminal():
            return {"ok": False, "status": task.status.value}

        # P0.2: ATOMIC BLOCK - Lease release + task cancellation + receipt
        async with self.session.begin_nested():  # SAVEPOINT
            # 1. Release any active lease
            await self.leases.release(tenant_id, task_id)

            # 2. Cancel the task
            task = await self.tasks.cancel(tenant_id, task_id, reason)

            # 3. Emit task.canceled receipt to owner
            await self._emit_receipt(
                tenant_id=tenant_id,
                receipt_type=ReceiptType.TASK_CANCELED,
                from_principal=principal,
                to_principal=owner,
                task_id=task.task_id,
                parents=parents,
                body={
                    "canceled_by": {"kind": principal.kind.value, "id": principal.id},
                    "reason": reason,
                    "canceled_at": utc_now().isoformat(),
                },
            )

            # 4. Emit result_ready receipt to owner
            await self._emit_result_ready_receipt(tenant_id, task, owner=owner, parents=parents)

        return {"ok": True, "status": task.status.value}

    async def list_receipts(
        self,
        tenant_id: UUID,
        to_kind: str,
        to_id: str,
        since_receipt_id: UUID | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """List receipts for a principal."""
        to_id = normalize_external(to_id)
        limit = min(limit or settings.default_list_limit, settings.max_list_limit)

        receipts, next_cursor = await self.receipts.list(
            tenant_id=tenant_id,
            to_kind=PrincipalKind(to_kind),
            to_id=to_id,
            since_receipt_id=since_receipt_id,
            limit=limit,
        )

        return {
            "receipts": [self._receipt_to_dict(r) for r in receipts],
            "next_cursor": str(next_cursor) if next_cursor else None,
        }

    async def list_receipts_ledger(
        self,
        tenant_id: UUID,
        task_id: UUID | None = None,
        lease_id: UUID | None = None,
        receipt_type: ReceiptType | None = None,
        task_status: TaskStatus | None = None,
        since_receipt_id: UUID | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """List receipts with filters, formatted as MemoryGate receipts."""
        limit = min(limit or settings.default_list_limit, settings.max_list_limit)

        receipts: list[Receipt]
        receipts, next_cursor = await self.receipts.list_filtered(
            tenant_id=tenant_id,
            task_id=task_id,
            lease_id=lease_id,
            receipt_type=receipt_type,
            task_status=task_status,
            since_receipt_id=since_receipt_id,
            limit=limit,
        )

        task_ids = [r.task_id for r in receipts if r.task_id]
        tasks_by_id = await self.tasks.get_many(tenant_id, task_ids)

        return {
            "receipts": [
                to_memorygate_receipt(r, tasks_by_id.get(r.task_id)) for r in receipts
            ],
            "next_cursor": str(next_cursor) if next_cursor else None,
        }

    async def ack_receipt(
        self,
        tenant_id: UUID,
        receipt_id: UUID,
        principal: Principal,
    ) -> dict[str, Any]:
        """
        Acknowledge a receipt.

        Implemented as append-only receipt.acknowledged receipt, not a mutable flag.
        """
        principal = self._normalize_principal(principal)
        service_principal = self._service_principal()

        await self._emit_receipt(
            tenant_id=tenant_id,
            receipt_type=ReceiptType.RECEIPT_ACKNOWLEDGED,
            from_principal=principal,
            to_principal=service_principal,
            body={"acknowledged_receipt_id": str(receipt_id)},
            parents=[receipt_id],
        )

        return {"ok": True}

    # =========================================================================
    # TASKEE Operations (lease/execute/report)
    # =========================================================================

    async def lease_next(
        self,
        tenant_id: UUID,
        worker_id: str,
        capabilities: list[str] | None = None,
        accept_types: list[str] | None = None,
        max_tasks: int = 1,
        lease_ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """
        Claim next available tasks matching capabilities.

        Selection rules:
        - Only tasks with status=queued
        - Only tasks with next_eligible_at <= now (or null)
        - Requirements/capabilities must match if specified
        - Highest priority first, then FIFO by created_at
        """
        max_tasks = min(max_tasks, 10)  # Cap at 10 per request

        leases = await self.leases.claim_next(
            tenant_id=tenant_id,
            worker_id=worker_id,
            capabilities=capabilities,
            accept_types=accept_types,
            max_tasks=max_tasks,
            lease_ttl_seconds=lease_ttl_seconds,
        )

        # Build response with task details
        results = []
        for lease in leases:
            task = await self.tasks.get(tenant_id, lease.task_id)
            if task:
                owner_hint = self._resolve_obligation_owner(task.created_by)
                obligation = await self._get_task_obligation(
                    tenant_id,
                    task.task_id,
                    owner_hint=owner_hint,
                )
                owner = obligation.to_ if obligation else owner_hint
                parents = [obligation.receipt_id] if obligation else None

                # Emit task.accepted receipt
                worker_principal = Principal(kind=PrincipalKind.WORKER, id=worker_id)
                await self._emit_receipt(
                    tenant_id=tenant_id,
                    receipt_type=ReceiptType.TASK_ACCEPTED,
                    from_principal=worker_principal,
                    to_principal=owner,
                    task_id=task.task_id,
                    lease_id=lease.lease_id,
                    parents=parents,
                    body=ReceiptBody.task_accepted(
                        worker_capabilities=capabilities or [],
                    ),
                )

                caused_by_receipt_id = str(obligation.receipt_id) if obligation else "NA"
                results.append({
                    "tenant_id": task.tenant_id,
                    "task_id": task.task_id,
                    "lease_id": lease.lease_id,
                    "type": task.type,
                    "payload": task.payload,
                    "payload_pointer": task.payload_pointer,
                    "principal_ai": task.principal_ai,
                    "caused_by_receipt_id": caused_by_receipt_id,
                    "attempt": task.attempt,
                    "expires_at": lease.expires_at,
                    "requirements": task.requirements.model_dump() if task.requirements else None,
                    "expected_outcome_kind": task.expected_outcome_kind,
                    "expected_artifact_mime": task.expected_artifact_mime,
                })

        return {"tasks": results}

    async def renew_lease(
        self,
        tenant_id: UUID,
        worker_id: str,
        task_id: UUID,
        lease_id: UUID,
        extend_by_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Renew an active lease."""
        # Validate task is in correct state
        task = await self.tasks.get(tenant_id, task_id)
        if not task:
            raise TaskNotFound(str(task_id))

        if task.status not in {TaskStatus.LEASED, TaskStatus.RUNNING}:
            raise LeaseInvalidOrExpired(str(task_id), str(lease_id))

        lease = await self.leases.renew(
            tenant_id=tenant_id,
            task_id=task_id,
            lease_id=lease_id,
            worker_id=worker_id,
            extend_by_seconds=extend_by_seconds,
        )

        if not lease:
            raise LeaseInvalidOrExpired(str(task_id), str(lease_id))

        return {"ok": True, "expires_at": lease.expires_at}

    async def report_progress(
        self,
        tenant_id: UUID,
        worker_id: str,
        task_id: UUID,
        lease_id: UUID,
        progress_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Report task execution progress."""
        # Validate lease
        lease = await self.leases.validate(tenant_id, task_id, lease_id, worker_id)
        if not lease:
            raise LeaseInvalidOrExpired(str(task_id), str(lease_id))

        task = await self.tasks.get(tenant_id, task_id)
        if not task:
            raise TaskNotFound(str(task_id))

        owner_hint = self._resolve_obligation_owner(task.created_by)
        obligation = await self._get_task_obligation(tenant_id, task_id, owner_hint)
        owner = obligation.to_ if obligation else owner_hint
        parents = [obligation.receipt_id] if obligation else None

        if task.status == TaskStatus.LEASED:
            await self._transition_to_running(
                tenant_id=tenant_id,
                task=task,
                worker_id=worker_id,
                lease_id=lease_id,
                owner=owner,
                parents=parents,
            )

        # Update progress
        await self.progress.update(tenant_id, task_id, progress_data)

        # Emit progress receipt
        worker_principal = Principal(kind=PrincipalKind.WORKER, id=worker_id)
        await self._emit_receipt(
            tenant_id=tenant_id,
            receipt_type=ReceiptType.TASK_PROGRESS,
            from_principal=worker_principal,
            to_principal=owner,
            task_id=task_id,
            lease_id=lease_id,
            parents=parents,
            body={"progress": progress_data},
        )

        return {"ok": True}

    async def start_task(
        self,
        tenant_id: UUID,
        worker_id: str,
        task_id: UUID,
        lease_id: UUID,
    ) -> dict[str, Any]:
        """Mark a task as running after the worker starts processing."""
        lease = await self.leases.validate(tenant_id, task_id, lease_id, worker_id)
        if not lease:
            raise LeaseInvalidOrExpired(str(task_id), str(lease_id))

        task = await self.tasks.get(tenant_id, task_id)
        if not task:
            raise TaskNotFound(str(task_id))

        if task.status == TaskStatus.RUNNING:
            return {
                "ok": True,
                "status": task.status.value,
                "started_at": task.started_at,
            }

        owner_hint = self._resolve_obligation_owner(task.created_by)
        obligation = await self._get_task_obligation(tenant_id, task_id, owner_hint)
        owner = obligation.to_ if obligation else owner_hint
        parents = [obligation.receipt_id] if obligation else None

        started_at = await self._transition_to_running(
            tenant_id=tenant_id,
            task=task,
            worker_id=worker_id,
            lease_id=lease_id,
            owner=owner,
            parents=parents,
        )

        return {"ok": True, "status": TaskStatus.RUNNING.value, "started_at": started_at}

    async def complete(
        self,
        tenant_id: UUID,
        worker_id: str,
        task_id: UUID,
        lease_id: UUID,
        result: dict[str, Any],
        artifacts: list[dict[str, Any]] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Mark a task as successfully completed.

        P0.2: All state changes + receipt emissions are atomic via savepoint.
        If any operation fails, entire transaction rolls back.
        """
        # Validate lease (outside transaction - read-only check)
        lease = await self.leases.validate(tenant_id, task_id, lease_id, worker_id)
        if not lease:
            raise LeaseInvalidOrExpired(str(task_id), str(lease_id))

        task = await self.tasks.get(tenant_id, task_id)
        if not task:
            raise TaskNotFound(str(task_id))

        if not task.can_transition_to(TaskStatus.SUCCEEDED):
            raise InvalidStateTransition(task.status.value, TaskStatus.SUCCEEDED.value)

        owner_hint = self._resolve_obligation_owner(task.created_by)
        obligation = await self._get_task_obligation_or_raise(
            tenant_id=tenant_id,
            task_id=task_id,
            owner_hint=owner_hint,
        )
        owner = obligation.to_
        parents = [obligation.receipt_id]

        # P0.2: ATOMIC BLOCK - All or nothing
        async with self.session.begin_nested():  # SAVEPOINT
            # 1. Update task to succeeded
            task_result = TaskResult(
                outcome=Outcome.SUCCEEDED,
                result=result,
                artifacts=artifacts,
                completed_at=utc_now(),
            )
            await self.tasks.update_status(tenant_id, task_id, TaskStatus.SUCCEEDED, task_result)

            # 2. Release lease
            await self.leases.release(tenant_id, task_id)

            # 3. Emit task.completed receipt
            worker_principal = Principal(kind=PrincipalKind.WORKER, id=worker_id)
            await self._emit_receipt(
                tenant_id=tenant_id,
                receipt_type=ReceiptType.TASK_COMPLETED,
                from_principal=worker_principal,
                to_principal=owner,
                task_id=task_id,
                lease_id=lease_id,
                parents=parents,
                body=ReceiptBody.task_completed(
                    result_summary="Task completed successfully",
                    result_payload=result,
                    artifacts=artifacts,
                ),
            )

            # 4. Emit result_ready to task owner
            task = await self.tasks.get(tenant_id, task_id)
            await self._emit_result_ready_receipt(tenant_id, task, owner=owner, parents=parents)

        # If we reach here, all operations succeeded and were committed
        return {"ok": True}

    async def fail(
        self,
        tenant_id: UUID,
        worker_id: str,
        task_id: UUID,
        lease_id: UUID,
        error: dict[str, Any],
        retryable: bool = False,
    ) -> dict[str, Any]:
        """
        Mark a task as failed.

        P0.2: All state changes + receipt emissions are atomic via savepoint.
        Handles both requeue path and terminal failure path atomically.

        If retryable and attempts remaining, requeue with backoff.
        Otherwise mark as terminal failure.
        """
        # Validate lease (outside transaction - read-only check)
        lease = await self.leases.validate(tenant_id, task_id, lease_id, worker_id)
        if not lease:
            raise LeaseInvalidOrExpired(str(task_id), str(lease_id))

        task = await self.tasks.get(tenant_id, task_id)
        if not task:
            raise TaskNotFound(str(task_id))

        # Check if should retry (decision logic outside transaction)
        should_requeue = retryable and (task.attempt + 1) < task.max_attempts

        owner_hint = self._resolve_obligation_owner(task.created_by)
        obligation = await self._get_task_obligation_or_raise(
            tenant_id=tenant_id,
            task_id=task_id,
            owner_hint=owner_hint,
        )
        owner = obligation.to_
        parents = [obligation.receipt_id]

        # P0.2: ATOMIC BLOCK - All state changes + receipts
        async with self.session.begin_nested():  # SAVEPOINT
            # 1. Release lease first (common to both paths)
            await self.leases.release(tenant_id, task_id)

            worker_principal = Principal(kind=PrincipalKind.WORKER, id=worker_id)

            if should_requeue:
                # REQUEUE PATH: Task gets another attempt
                task = await self.tasks.requeue_with_backoff(tenant_id, task_id, increment_attempt=True)
                next_eligible_at = task.next_eligible_at

                # Emit retry scheduled receipt to task owner (non-terminal)
                await self._emit_receipt(
                    tenant_id=tenant_id,
                    receipt_type=ReceiptType.TASK_RETRY_SCHEDULED,
                    from_principal=worker_principal,
                    to_principal=owner,
                    task_id=task_id,
                    lease_id=lease_id,
                    parents=parents,
                    body={
                        "reason": "Worker reported retryable failure",
                        "error": error,
                        "requeued": True,
                        "attempt": task.attempt,
                        "max_attempts": task.max_attempts,
                        "next_eligible_at": next_eligible_at.isoformat() if next_eligible_at else None,
                    },
                )
            else:
                # TERMINAL FAILURE PATH: No more retries
                task_result = TaskResult(
                    outcome=Outcome.FAILED,
                    error=error,
                    completed_at=utc_now(),
                )
                await self.tasks.update_status(tenant_id, task_id, TaskStatus.FAILED, task_result)

                # Emit task.failed receipt to owner
                await self._emit_receipt(
                    tenant_id=tenant_id,
                    receipt_type=ReceiptType.TASK_FAILED,
                    from_principal=worker_principal,
                    to_principal=owner,
                    task_id=task_id,
                    lease_id=lease_id,
                    parents=parents,
                    body=ReceiptBody.task_failed(
                        error=error,
                        retry_recommended=False,
                    ),
                )

                # Emit result_ready to task owner
                task = await self.tasks.get(tenant_id, task_id)
                await self._emit_result_ready_receipt(tenant_id, task, owner=owner, parents=parents)

                next_eligible_at = None

        # If we reach here, all operations succeeded and were committed
        return {
            "ok": True,
            "requeued": should_requeue,
            "next_eligible_at": next_eligible_at,
        }

    # =========================================================================
    # System Operations
    # =========================================================================

    async def expire_leases(self, batch_size: int = 20) -> int:
        """
        Expire stale leases and requeue tasks with anti-storm protections.

        Called by background sweep task. Only processes leases for tasks
        owned by this AsyncGate instance (multi-instance safe).

        CRITICAL: Uses requeue_on_expiry() which does NOT increment attempt.
        Lease expiry is "lost authority" (worker crash), not "task failed".

        P0.2: Each lease expiry is atomic - requeue + lease release + receipt
        all succeed or all rollback.

        Args:
            batch_size: Number of leases to process per batch (default 20).
                       Smaller batches with jittered requeue times prevent
                       thundering herd when many leases expire simultaneously.

        Returns:
            Total number of expired leases processed.
        """
        import random

        expired_leases = await self.leases.get_expired(
            limit=100,
            instance_id=settings.instance_id
        )
        count = 0

        for lease in expired_leases:
            task = await self.tasks.get(lease.tenant_id, lease.task_id)
            if not task or task.is_terminal():
                continue

            owner_hint = self._resolve_obligation_owner(task.created_by)
            obligation = await self._get_task_obligation(
                lease.tenant_id,
                lease.task_id,
                owner_hint=owner_hint,
            )
            owner = obligation.to_ if obligation else owner_hint
            parents = [obligation.receipt_id] if obligation else None

            # Add jitter to requeue time: 0-5 seconds random delay
            # This prevents all expired tasks from becoming eligible simultaneously
            jitter_seconds = random.uniform(0, 5)

            # P0.2: ATOMIC BLOCK - Each lease expiry is atomic
            try:
                async with self.session.begin_nested():  # SAVEPOINT
                    # 1. CRITICAL: Use requeue_on_expiry (does NOT increment attempt)
                    # Lease expiry = "lost authority", NOT "task failed"
                    # P2-3: Lease expiry = lost authority, not failure; does not consume attempts
                    # This distinction is critical: worker crash ≠ task failure
                    await self.tasks.requeue_on_expiry(
                        lease.tenant_id,
                        lease.task_id,
                        jitter_seconds=jitter_seconds,
                    )

                    # 2. Release expired lease
                    await self.leases.release(lease.tenant_id, lease.task_id)

                    # 3. Emit lease.expired receipt to task owner
                    service_principal = self._service_principal()

                    await self._emit_receipt(
                        tenant_id=lease.tenant_id,
                        receipt_type=ReceiptType.LEASE_EXPIRED,
                        from_principal=service_principal,
                        to_principal=owner,
                        task_id=task.task_id,
                        lease_id=lease.lease_id,
                        parents=parents,
                        body=ReceiptBody.lease_expired(
                            task_id=task.task_id,
                            previous_worker_id=lease.worker_id,
                            attempt=task.attempt,
                            requeued=True,
                        ),
                    )

                    escalation_class = settings.escalation_lease_expiry_class
                    if isinstance(task.payload, dict):
                        payload_class = task.payload.get("escalation_class")
                        if isinstance(payload_class, (int, str)):
                            try:
                                escalation_class = int(payload_class)
                            except ValueError:
                                escalation_class = settings.escalation_lease_expiry_class

                    await self._emit_escalation_receipt(
                        tenant_id=lease.tenant_id,
                        task=task,
                        reason="lease_expired",
                        escalation_class=escalation_class,
                        escalation_class_label="policy",
                        obligation=obligation,
                        lease_id=lease.lease_id,
                    )

                count += 1
                metrics.inc_counter("leases.expired")
            except Exception as e:
                # Log but continue processing other leases
                import logging
                logger = logging.getLogger("asyncgate.engine")
                logger.error(f"Failed to expire lease {lease.lease_id}: {e}", exc_info=True)
                metrics.inc_counter("leases.expired.failed")
                continue

            # Batch processing: commit and pause between batches
            if count % batch_size == 0:
                await self.session.commit()
                # Small pause between batches (10-50ms) to avoid transaction pile-up
                await asyncio.sleep(random.uniform(0.01, 0.05))

        return count

    # =========================================================================
    # Receipt Query Operations (Tier 0 - Foundation)
    # =========================================================================

    async def get_receipt(
        self,
        tenant_id: UUID,
        receipt_id: UUID,
    ) -> dict[str, Any] | None:
        """
        Get a specific receipt by ID.

        Used for receipt chain traversal and obligation verification.
        """
        receipt = await self.receipts.get_by_id(tenant_id, receipt_id)
        if not receipt:
            return None
        return self._receipt_to_dict(receipt)

    async def list_receipts_by_parent(
        self,
        tenant_id: UUID,
        parent_receipt_id: UUID,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        List all receipts that reference a specific parent.

        Used by agents to find terminal receipts (may include retries/duplicates).
        """
        receipts: list[Receipt] = await self.receipts.get_by_parent(
            tenant_id, parent_receipt_id, limit
        )
        return [self._receipt_to_dict(r) for r in receipts]

    async def get_latest_terminator(
        self,
        tenant_id: UUID,
        parent_receipt_id: UUID,
    ) -> dict[str, Any] | None:
        """
        Get the most recent terminator for a parent receipt.

        Simplifies agent logic: when multiple terminators exist (retries, duplicates),
        return the canonical one (most recent).
        """
        receipt = await self.receipts.get_latest_terminator(
            tenant_id, parent_receipt_id
        )
        if not receipt:
            return None
        return self._receipt_to_dict(receipt)

    async def has_terminator(
        self,
        tenant_id: UUID,
        parent_receipt_id: UUID,
    ) -> bool:
        """
        Fast check: does a terminator exist for this obligation?

        DB-driven: O(1) EXISTS query, doesn't load receipt data.
        """
        return await self.receipts.has_terminator(tenant_id, parent_receipt_id)

    async def list_open_obligations(
        self,
        tenant_id: UUID,
        principal: Principal,
        since_receipt_id: UUID | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        List open obligations for a principal.

        This is the foundation for the new bootstrap model:
        - Returns receipts that create obligations (task.assigned, etc.)
        - Filters to only those without terminal child receipts
        - Pure ledger dump, no bucketing or interpretation

        An obligation is "open" if no terminator receipt exists that references
        it as a parent. Termination is detected via DB, not semantic inference.
        """
        principal = self._normalize_principal(principal)
        obligations: list[Receipt]
        obligations, next_cursor = await self.receipts.list_open_obligations(
            tenant_id=tenant_id,
            to_kind=principal.kind,
            to_id=principal.id,
            since_receipt_id=since_receipt_id,
            limit=limit,
        )

        return {
            "open_obligations": [self._receipt_to_dict(r) for r in obligations],
            "cursor": str(next_cursor) if next_cursor else None,
        }

    async def get_config(self) -> dict[str, Any]:
        """Return operational configuration."""
        config: dict[str, Any] = {
            "receipt_mode": settings.receipt_mode.value,
            "receiptgate_endpoint": settings.receiptgate_endpoint,
            "instance_id": settings.instance_id,
            "capabilities": ["lease_based_execution", "receipt_emission"],
            "version": "0.1.0",
        }

        if settings.receipt_mode == ReceiptMode.RECEIPTGATE_INTEGRATED:
            client = get_receiptgate_client()
            config["receiptgate_delivery"] = {
                "circuit": client.get_circuit_stats(),
                "buffer": client.get_buffer_stats(),
            }

        return config

    async def get_metrics_snapshot(self, tenant_id: UUID) -> dict[str, Any]:
        """Return metrics snapshot with queue size gauges."""
        status_counts = await self.tasks.count_by_status(tenant_id)
        queue_sizes = {status.value: int(status_counts.get(status, 0)) for status in TaskStatus}

        metrics.set_gauge("tasks.queued", queue_sizes.get(TaskStatus.QUEUED.value, 0))
        metrics.set_gauge("tasks.leased", queue_sizes.get(TaskStatus.LEASED.value, 0))
        metrics.set_gauge("tasks.running", queue_sizes.get(TaskStatus.RUNNING.value, 0))
        metrics.set_gauge("tasks.succeeded", queue_sizes.get(TaskStatus.SUCCEEDED.value, 0))
        metrics.set_gauge("tasks.failed", queue_sizes.get(TaskStatus.FAILED.value, 0))
        metrics.set_gauge("tasks.canceled", queue_sizes.get(TaskStatus.CANCELED.value, 0))

        return {
            "metrics": metrics.snapshot(),
            "queue_sizes": queue_sizes,
        }

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    async def _transition_to_running(
        self,
        tenant_id: UUID,
        task: Task,
        worker_id: str,
        lease_id: UUID,
        owner: Principal,
        parents: list[UUID] | None,
    ) -> Any:
        """Transition a leased task to running and emit task.started receipt."""
        if task.status == TaskStatus.RUNNING:
            return task.started_at or utc_now()

        if task.status != TaskStatus.LEASED:
            raise InvalidStateTransition(task.status.value, TaskStatus.RUNNING.value)

        started_at = task.started_at or utc_now()
        worker_principal = Principal(kind=PrincipalKind.WORKER, id=worker_id)

        async with self.session.begin_nested():  # SAVEPOINT
            await self.tasks.update_status(
                tenant_id,
                task.task_id,
                TaskStatus.RUNNING,
                started_at=started_at,
            )

            await self._emit_receipt(
                tenant_id=tenant_id,
                receipt_type=ReceiptType.TASK_STARTED,
                from_principal=worker_principal,
                to_principal=owner,
                task_id=task.task_id,
                lease_id=lease_id,
                parents=parents,
                body=ReceiptBody.task_started(started_at=started_at),
            )

        return started_at

    async def _emit_escalation_receipt(
        self,
        tenant_id: UUID,
        task: Task,
        reason: str,
        escalation_class: int,
        escalation_class_label: str = "other",
        obligation: Receipt | None = None,
        lease_id: UUID | None = None,
    ) -> Receipt | None:
        """Emit a task.escalated receipt when escalation targets are configured."""
        if not settings.escalation_enabled:
            return None

        target = settings.get_escalation_target(escalation_class)
        target_tenant_id = tenant_id
        if target and target.tenant_id:
            try:
                target_tenant_id = UUID(target.tenant_id)
            except ValueError:
                target_tenant_id = tenant_id

        if target:
            to_principal = Principal(
                kind=PrincipalKind(target.to_kind),
                id=normalize_external(target.to_id),
            )
            escalation_to = target.to_id
        else:
            fallback = obligation.to_ if obligation else task.created_by
            to_principal = fallback
            escalation_to = fallback.id

        parents = [obligation.receipt_id] if obligation else None

        return await self._emit_receipt(
            tenant_id=target_tenant_id,
            receipt_type=ReceiptType.TASK_ESCALATED,
            from_principal=self._service_principal(),
            to_principal=to_principal,
            task_id=task.task_id,
            lease_id=lease_id,
            parents=parents,
            body=ReceiptBody.task_escalated(
                escalation_class=escalation_class_label,
                escalation_reason=reason,
                escalation_to=escalation_to,
                expected_outcome_kind=task.expected_outcome_kind,
                expected_artifact_mime=task.expected_artifact_mime,
            ),
        )

    async def _emit_receipt(
        self,
        tenant_id: UUID,
        receipt_type: ReceiptType,
        from_principal: Principal,
        to_principal: Principal,
        task_id: UUID | None = None,
        lease_id: UUID | None = None,
        schedule_id: str | None = None,
        body: dict[str, Any] | None = None,
        parents: list[UUID] | None = None,
    ) -> Receipt:
        """Emit a receipt (either locally or to ReceiptGate)."""
        trace_id = get_trace_id() or ensure_trace_id()
        body_payload = dict(body) if body else {}
        if trace_id and "trace_id" not in body_payload:
            body_payload["trace_id"] = trace_id

        start_time = perf_counter()
        receipt = await self.receipts.create(
            tenant_id=tenant_id,
            receipt_type=receipt_type,
            from_principal=from_principal,
            to_principal=to_principal,
            task_id=task_id,
            lease_id=lease_id,
            schedule_id=schedule_id,
            parents=parents,
            body=body_payload,
            receipt_hash=None,
        )
        metrics.inc_counter("receipts.emitted.count")
        metrics.observe("receipts.emit_latency_ms", (perf_counter() - start_time) * 1000.0)

        obligation_id = None
        if receipt_type == ReceiptType.TASK_ASSIGNED:
            obligation_id = str(receipt.receipt_id)
        elif parents:
            obligation_id = str(parents[0])

        # stdlib logging takes structured fields via `extra`, not kwargs. Passing
        # them as kwargs raised TypeError on every single receipt emission.
        logger.info(
            "receipt_emitted receipt_id=%s receipt_type=%s task_id=%s",
            receipt.receipt_id,
            receipt_type.value,
            task_id,
            extra={
                "receipt_id": str(receipt.receipt_id),
                "receipt_type": receipt_type.value,
                "task_id": str(task_id) if task_id else None,
                "lease_id": str(lease_id) if lease_id else None,
                "obligation_id": obligation_id,
                "parents": [str(parent) for parent in parents] if parents else [],
                "trace_id": get_trace_id(),
            },
        )

        if settings.receipt_mode == ReceiptMode.RECEIPTGATE_INTEGRATED and settings.receiptgate_endpoint:
            eligible = {
                ReceiptType.TASK_ASSIGNED,
                ReceiptType.TASK_ACCEPTED,
                ReceiptType.TASK_COMPLETED,
                ReceiptType.TASK_FAILED,
                ReceiptType.TASK_CANCELED,
                ReceiptType.TASK_ESCALATED,
            }
            if receipt_type not in eligible:
                return receipt
            try:
                task = await self.tasks.get(tenant_id, task_id) if task_id else None
                receipt_payload = to_memorygate_receipt(receipt, task)
                client = get_receiptgate_client()
                await client.emit_receipt(receipt_payload)
            except Exception as exc:
                # stdlib logger, so structured fields go through `extra`.
                # Passing them as kwargs raised TypeError here, which then
                # propagated out of _emit_receipt and out of the enclosing
                # begin_nested(), rolling back the completion it was only
                # supposed to warn about. The identical bug was found and
                # fixed thirty lines above and left in place here.
                logger.warning(
                    "receiptgate_receipt_emit_failed",
                    extra={
                        "receipt_type": receipt_type.value,
                        "task_id": str(task_id) if task_id else None,
                        "error": str(exc),
                    },
                )

        return receipt

    async def _emit_result_ready_receipt(
        self,
        tenant_id: UUID,
        task: Task,
        owner: Principal | None = None,
        parents: list[UUID] | None = None,
    ) -> Receipt:
        """Emit task.result_ready receipt to task owner."""
        service_principal = self._service_principal()
        to_principal = owner or task.created_by

        return await self._emit_receipt(
            tenant_id=tenant_id,
            receipt_type=ReceiptType.TASK_RESULT_READY,
            from_principal=service_principal,
            to_principal=to_principal,
            task_id=task.task_id,
            parents=parents,
            body=ReceiptBody.task_result_ready(
                status=task.status.value,
                result_payload=task.result.result if task.result else None,
                error=task.result.error if task.result else None,
                artifacts=task.result.artifacts if task.result else None,
            ),
        )

    def _compute_receipt_hash(
        self,
        receipt_type: ReceiptType,
        task_id: UUID | None,
        from_principal: Principal,
        to_principal: Principal,
        lease_id: UUID | None,
        body: dict[str, Any] | None,
        parents: list[UUID] | None,  # P0.5: Include parents in hash
    ) -> str:
        """
        Compute hash for receipt deduplication.

        P0.5: Parents are now included in hash to prevent collisions
        where same body but different parents would dedupe incorrectly.

        Includes all fields that make a receipt unique:
        - receipt_type, task_id, lease_id
        - from (kind + id)
        - to (kind + id)
        - parents (sorted list of UUID strings)
        - body (canonical JSON)

        Returns full 64-character SHA256 hex digest.
        """
        return compute_receipt_hash(
            receipt_type=receipt_type,
            task_id=task_id,
            from_principal=from_principal,
            to_principal=to_principal,
            lease_id=lease_id,
            body=body,
            parents=parents,
        )

    def _task_to_dict(self, task: Task) -> dict[str, Any]:
        """Convert task to dictionary with native types."""
        result = {
            "task_id": task.task_id,
            "type": task.type,
            "payload": task.payload,
            "payload_pointer": task.payload_pointer,
            "created_by": {
                "kind": task.created_by.kind.value,
                "id": task.created_by.id,
            },
            "principal_ai": task.principal_ai,
            "requirements": task.requirements.model_dump() if task.requirements else {},
            "expected_outcome_kind": task.expected_outcome_kind,
            "expected_artifact_mime": task.expected_artifact_mime,
            "priority": task.priority,
            "status": task.status.value,
            "attempt": task.attempt,
            "max_attempts": task.max_attempts,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "next_eligible_at": task.next_eligible_at,
            "started_at": task.started_at,
        }

        if task.result:
            result["result"] = {
                "outcome": task.result.outcome.value,
                "result": task.result.result,
                "error": task.result.error,
                "artifacts": task.result.artifacts,
                "completed_at": task.result.completed_at,
            }

        return result

    def _task_to_summary(self, task: Task) -> TaskSummary:
        """Convert task to summary."""
        return TaskSummary(
            task_id=task.task_id,
            type=task.type,
            status=task.status,
            priority=task.priority,
            attempt=task.attempt,
            created_at=task.created_at,
            updated_at=task.updated_at,
            next_eligible_at=task.next_eligible_at,
        )

    def _receipt_to_dict(self, receipt: Receipt) -> dict[str, Any]:
        """Convert receipt to dictionary with native types."""
        return {
            "receipt_id": receipt.receipt_id,
            "receipt_type": receipt.receipt_type.value,
            "created_at": receipt.created_at,
            "from": {
                "kind": receipt.from_.kind.value,
                "id": receipt.from_.id,
            },
            "to": {
                "kind": receipt.to_.kind.value,
                "id": receipt.to_.id,
            },
            "task_id": receipt.task_id,
            "lease_id": receipt.lease_id,
            "parents": receipt.parents,
            "body": receipt.body,
            "delivered_at": receipt.delivered_at,
        }
