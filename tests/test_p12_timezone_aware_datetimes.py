"""
P1.2 Tests: Timezone-Aware Datetimes

Tests that all datetimes are timezone-aware (UTC) to prevent serialization
and comparison issues.
"""

import pytest
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from asyncgate.db.repositories import TaskRepository, LeaseRepository, ReceiptRepository
from asyncgate.engine import AsyncGateEngine
from asyncgate.models import Principal, PrincipalKind, TaskStatus, ReceiptType
from asyncgate.principals import SYSTEM_PRINCIPAL_ID


@pytest.mark.asyncio
async def test_task_created_at_is_timezone_aware(session: AsyncSession):
    """
    Test that task created_at has timezone info (UTC).
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    task = await engine.tasks.create(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    )
    
    # Verify timezone-aware
    assert task.created_at.tzinfo is not None, \
        "created_at should be timezone-aware"
    assert task.created_at.tzinfo == timezone.utc, \
        "created_at should be UTC"
    
    print(f"✅ Task created_at is timezone-aware: {task.created_at}")


@pytest.mark.asyncio
async def test_task_completed_at_is_timezone_aware(session: AsyncSession):
    """
    Test that task completed_at has timezone info (UTC).
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "test-worker"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create and claim task
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
    
    # Complete task
    await engine.complete(
        tenant_id=tenant_id,
        worker_id=worker_id,
        task_id=task_id,
        lease_id=lease.lease_id,
        result={"status": "done"},
        artifacts=[{"type": "test", "uri": "mem://timezone/completed"}],
    )
    
    await session.commit()
    
    # Verify completed_at is timezone-aware
    task = await engine.tasks.get(tenant_id, task_id)
    assert task.result is not None
    assert task.result.completed_at.tzinfo is not None, \
        "completed_at should be timezone-aware"
    assert task.result.completed_at.tzinfo == timezone.utc, \
        "completed_at should be UTC"
    
    print(f"✅ Task completed_at is timezone-aware: {task.result.completed_at}")


@pytest.mark.asyncio
async def test_lease_created_at_is_timezone_aware(session: AsyncSession):
    """
    Test that lease created_at has timezone info (UTC).
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "test-worker"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create and claim task
    task_id = (await engine.tasks.create(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    )).task_id
    
    await session.commit()
    
    leases = await engine.leases.claim_next(
        tenant_id=tenant_id,
        worker_id=worker_id,
        max_tasks=1,
    )
    lease = leases[0]
    
    # Verify timezone-aware
    assert lease.created_at.tzinfo is not None, \
        "lease created_at should be timezone-aware"
    assert lease.created_at.tzinfo == timezone.utc, \
        "lease created_at should be UTC"
    
    print(f"✅ Lease created_at is timezone-aware: {lease.created_at}")


@pytest.mark.asyncio
async def test_lease_expires_at_is_timezone_aware(session: AsyncSession):
    """
    Test that lease expires_at has timezone info (UTC).
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "test-worker"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create and claim task
    task_id = (await engine.tasks.create(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    )).task_id
    
    await session.commit()
    
    leases = await engine.leases.claim_next(
        tenant_id=tenant_id,
        worker_id=worker_id,
        max_tasks=1,
    )
    lease = leases[0]
    
    # Verify timezone-aware
    assert lease.expires_at.tzinfo is not None, \
        "lease expires_at should be timezone-aware"
    assert lease.expires_at.tzinfo == timezone.utc, \
        "lease expires_at should be UTC"
    
    print(f"✅ Lease expires_at is timezone-aware: {lease.expires_at}")


@pytest.mark.asyncio
async def test_lease_acquired_at_is_timezone_aware(session: AsyncSession):
    """
    Test that lease acquired_at has timezone info (UTC).
    
    P1.1 field used for lifetime tracking.
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "test-worker"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create and claim task
    task_id = (await engine.tasks.create(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    )).task_id
    
    await session.commit()
    
    leases = await engine.leases.claim_next(
        tenant_id=tenant_id,
        worker_id=worker_id,
        max_tasks=1,
    )
    lease = leases[0]
    
    await session.commit()
    
    # Get lease with acquired_at
    from asyncgate.db.tables import LeaseTable
    from sqlalchemy import select
    
    result = await session.execute(
        select(LeaseTable).where(LeaseTable.lease_id == lease.lease_id)
    )
    lease_row = result.scalar_one()
    
    # Verify timezone-aware
    assert lease_row.acquired_at.tzinfo is not None, \
        "lease acquired_at should be timezone-aware"
    assert lease_row.acquired_at.tzinfo == timezone.utc, \
        "lease acquired_at should be UTC"
    
    print(f"✅ Lease acquired_at is timezone-aware: {lease_row.acquired_at}")


@pytest.mark.asyncio
async def test_receipt_created_at_is_timezone_aware(session: AsyncSession):
    """
    Test that receipt created_at has timezone info (UTC).
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    system = Principal(kind=PrincipalKind.SYSTEM, id=SYSTEM_PRINCIPAL_ID)
    
    # Create receipt
    receipt = await engine.receipts.create(
        tenant_id=tenant_id,
        receipt_type=ReceiptType.TASK_ASSIGNED,
        from_principal=agent,
        to_principal=system,
        body={"test": "data"},
    )
    
    # Verify timezone-aware
    assert receipt.created_at.tzinfo is not None, \
        "receipt created_at should be timezone-aware"
    assert receipt.created_at.tzinfo == timezone.utc, \
        "receipt created_at should be UTC"
    
    print(f"✅ Receipt created_at is timezone-aware: {receipt.created_at}")


@pytest.mark.asyncio
async def test_datetime_comparison_across_timezones(session: AsyncSession):
    """
    Test that datetime comparisons work correctly (no naive vs aware errors).
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create task
    task = await engine.tasks.create(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    )
    
    # Compare with current time (should not raise TypeError)
    now = datetime.now(timezone.utc)
    
    try:
        is_recent = (now - task.created_at).total_seconds() < 60
        assert is_recent, "Task should be created within last 60 seconds"
    except TypeError as e:
        pytest.fail(f"DateTime comparison failed (naive vs aware): {e}")
    
    print(f"✅ DateTime comparison works: {now} - {task.created_at}")


@pytest.mark.asyncio
async def test_datetime_serialization_includes_timezone(session: AsyncSession):
    """
    Test that datetimes serialize to ISO format with timezone info (+00:00 or Z).
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create task
    task = await engine.tasks.create(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    )
    
    # Serialize to ISO format
    iso_str = task.created_at.isoformat()
    
    # Should include timezone info
    assert '+' in iso_str or iso_str.endswith('Z'), \
        f"ISO format should include timezone: {iso_str}"
    
    # Parse back and compare
    parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
    assert parsed == task.created_at, \
        "Round-trip serialization should preserve datetime"
    
    print(f"✅ DateTime serialization includes timezone: {iso_str}")


@pytest.mark.asyncio
async def test_database_stores_timezone_correctly(session: AsyncSession):
    """
    Test that PostgreSQL correctly stores and retrieves timezone info.
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create task
    task = await engine.tasks.create(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    )
    original_time = task.created_at
    
    await session.commit()
    
    # Retrieve from database
    retrieved_task = await engine.tasks.get(tenant_id, task.task_id)
    retrieved_time = retrieved_task.created_at
    
    # Verify times match exactly (including timezone)
    assert retrieved_time == original_time, \
        "Retrieved time should match original"
    assert retrieved_time.tzinfo == original_time.tzinfo, \
        "Timezone info should be preserved"
    
    print(f"✅ Database preserves timezone: {original_time} == {retrieved_time}")


@pytest.mark.asyncio
async def test_all_datetime_columns_have_timezone_true(session: AsyncSession):
    """
    Verify that all DateTime columns in schema have timezone=True.
    
    This is a schema validation test.
    """
    from asyncgate.db.tables import TaskTable, LeaseTable, ReceiptTable
    from sqlalchemy import DateTime
    
    tables_to_check = [
        (TaskTable, ['created_at', 'updated_at', 'next_eligible_at', 'completed_at']),
        (LeaseTable, ['created_at', 'expires_at', 'acquired_at']),
        (ReceiptTable, ['created_at', 'delivered_at']),
    ]
    
    for table_class, column_names in tables_to_check:
        for col_name in column_names:
            if not hasattr(table_class, col_name):
                continue  # Column might be nullable and not always present
                
            column = getattr(table_class, col_name)
            col_type = column.type
            
            if isinstance(col_type, DateTime):
                assert col_type.timezone is True, \
                    f"{table_class.__name__}.{col_name} must have timezone=True"
    
    print(f"✅ All DateTime columns have timezone=True")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
