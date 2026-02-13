"""
Running state and receipt tests.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from asyncgate.db.tables import LeaseTable
from asyncgate.engine import AsyncGateEngine, LeaseInvalidOrExpired
from asyncgate.models import Principal, PrincipalKind, ReceiptType, Task, TaskStatus
from asyncgate.utils.time import utc_now


@pytest.mark.asyncio
async def test_task_state_machine_allows_running_transition():
    """Task state machine allows leased -> running and rejects queued -> running."""
    now = utc_now()
    principal = Principal(kind=PrincipalKind.AGENT, id="test-agent")

    leased_task = Task(
        task_id=uuid4(),
        tenant_id=uuid4(),
        type="test",
        payload={},
        created_by=principal,
        principal_ai=principal.id,
        created_at=now,
        updated_at=now,
        status=TaskStatus.LEASED,
    )
    queued_task = Task(
        task_id=uuid4(),
        tenant_id=uuid4(),
        type="test",
        payload={},
        created_by=principal,
        principal_ai=principal.id,
        created_at=now,
        updated_at=now,
        status=TaskStatus.QUEUED,
    )
    running_task = Task(
        task_id=uuid4(),
        tenant_id=uuid4(),
        type="test",
        payload={},
        created_by=principal,
        principal_ai=principal.id,
        created_at=now,
        updated_at=now,
        status=TaskStatus.RUNNING,
    )

    assert leased_task.can_transition_to(TaskStatus.RUNNING)
    assert not queued_task.can_transition_to(TaskStatus.RUNNING)
    assert running_task.can_transition_to(TaskStatus.SUCCEEDED)
    assert running_task.can_transition_to(TaskStatus.QUEUED)


@pytest.mark.asyncio
async def test_start_task_sets_running_and_emits_receipt(session: AsyncSession):
    """start_task marks running, sets started_at, and emits task.started receipt."""
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "worker-1"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")

    task_id = (await engine.create_task(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    ))["task_id"]
    await session.commit()

    leases = await engine.leases.claim_next(
        tenant_id=tenant_id,
        worker_id=worker_id,
        max_tasks=1,
    )
    lease = leases[0]
    await session.commit()

    first = await engine.start_task(
        tenant_id=tenant_id,
        worker_id=worker_id,
        task_id=task_id,
        lease_id=lease.lease_id,
    )
    await session.commit()

    second = await engine.start_task(
        tenant_id=tenant_id,
        worker_id=worker_id,
        task_id=task_id,
        lease_id=lease.lease_id,
    )
    await session.commit()

    task = await engine.tasks.get(tenant_id, task_id)
    assert task.status == TaskStatus.RUNNING
    assert task.started_at is not None
    assert first["started_at"] == second["started_at"]

    receipts, _ = await engine.receipts.list(
        tenant_id=tenant_id,
        to_kind=PrincipalKind.AGENT,
        to_id=agent.id,
        limit=25,
    )
    started_receipts = [
        receipt for receipt in receipts if receipt.receipt_type == ReceiptType.TASK_STARTED
    ]
    assert len(started_receipts) == 1


@pytest.mark.asyncio
async def test_report_progress_transitions_to_running(session: AsyncSession):
    """report_progress transitions leased task to running and emits task.started."""
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "worker-2"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")

    task_id = (await engine.create_task(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    ))["task_id"]
    await session.commit()

    leases = await engine.leases.claim_next(
        tenant_id=tenant_id,
        worker_id=worker_id,
        max_tasks=1,
    )
    lease = leases[0]
    await session.commit()

    await engine.report_progress(
        tenant_id=tenant_id,
        worker_id=worker_id,
        task_id=task_id,
        lease_id=lease.lease_id,
        progress_data={"pct": 10},
    )
    await session.commit()

    task = await engine.tasks.get(tenant_id, task_id)
    assert task.status == TaskStatus.RUNNING
    assert task.started_at is not None

    receipts, _ = await engine.receipts.list(
        tenant_id=tenant_id,
        to_kind=PrincipalKind.AGENT,
        to_id=agent.id,
        limit=25,
    )
    assert any(r.receipt_type == ReceiptType.TASK_STARTED for r in receipts)


@pytest.mark.asyncio
async def test_start_task_requires_valid_lease(session: AsyncSession):
    """start_task fails when no valid lease exists."""
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "worker-3"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")

    task_id = (await engine.create_task(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    ))["task_id"]
    await session.commit()

    with pytest.raises(LeaseInvalidOrExpired):
        await engine.start_task(
            tenant_id=tenant_id,
            worker_id=worker_id,
            task_id=task_id,
            lease_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_lease_expiry_clears_started_at(session: AsyncSession):
    """Lease expiry requeues running tasks and clears started_at."""
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "worker-4"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")

    task_id = (await engine.create_task(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    ))["task_id"]
    await session.commit()

    leases = await engine.leases.claim_next(
        tenant_id=tenant_id,
        worker_id=worker_id,
        max_tasks=1,
    )
    lease = leases[0]
    await session.commit()

    await engine.start_task(
        tenant_id=tenant_id,
        worker_id=worker_id,
        task_id=task_id,
        lease_id=lease.lease_id,
    )
    await session.commit()

    task = await engine.tasks.get(tenant_id, task_id)
    assert task.status == TaskStatus.RUNNING
    assert task.started_at is not None

    await session.execute(
        update(LeaseTable)
        .where(LeaseTable.lease_id == lease.lease_id)
        .values(expires_at=utc_now() - timedelta(seconds=5))
    )
    await session.commit()

    expired_count = await engine.expire_leases(batch_size=1)
    await session.commit()

    assert expired_count >= 1
    task = await engine.tasks.get(tenant_id, task_id)
    assert task.status == TaskStatus.QUEUED
    assert task.started_at is None
