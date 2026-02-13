"""
P1.2 Test: Timezone-Aware Datetimes

Verifies that all datetime handling uses timezone-aware datetimes.
"""

import pytest
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from asyncgate.engine import AsyncGateEngine
from asyncgate.models import Principal, PrincipalKind


@pytest.mark.asyncio
async def test_task_result_has_timezone_aware_datetime(session: AsyncSession):
    """
    Test that task completion uses timezone-aware datetimes.
    
    P1.2: All datetime columns expect timezone-aware values.
    datetime.now(timezone.utc) vs datetime.utcnow() (naive).
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
        artifacts=[{"type": "test", "uri": "mem://timezone/result"}],
    )
    
    await session.commit()
    
    # Verify task result has timezone-aware datetime
    task = await engine.tasks.get(tenant_id, task_id)
    assert task.result is not None, "Task should have result"
    assert task.result.completed_at is not None, "Result should have completed_at"
    
    # Check that datetime has timezone info
    completed_at = task.result.completed_at
    assert completed_at.tzinfo is not None, \
        "completed_at should be timezone-aware (have tzinfo)"
    assert completed_at.tzinfo == timezone.utc, \
        "completed_at should be in UTC timezone"
    
    # Verify ISO format includes timezone
    iso_str = completed_at.isoformat()
    assert iso_str.endswith('+00:00') or iso_str.endswith('Z'), \
        f"ISO format should include timezone: {iso_str}"
    
    print(f"✅ Timezone-aware datetime verified: {iso_str}")


@pytest.mark.asyncio
async def test_failed_task_has_timezone_aware_datetime(session: AsyncSession):
    """
    Test that task failure also uses timezone-aware datetimes.
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "test-worker"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create task with no retries
    task_id = (await engine.create_task(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
        max_attempts=1,  # No retries
    ))["task_id"]
    
    await session.commit()
    
    leases = await engine.leases.claim_next(
        tenant_id=tenant_id,
        worker_id=worker_id,
        max_tasks=1,
    )
    lease = leases[0]
    
    await session.commit()
    
    # Fail task
    await engine.fail(
        tenant_id=tenant_id,
        worker_id=worker_id,
        task_id=task_id,
        lease_id=lease.lease_id,
        error={"message": "test error"},
        retryable=False,
    )
    
    await session.commit()
    
    # Verify task result has timezone-aware datetime
    task = await engine.tasks.get(tenant_id, task_id)
    assert task.result is not None
    assert task.result.completed_at is not None
    
    completed_at = task.result.completed_at
    assert completed_at.tzinfo is not None, \
        "completed_at should be timezone-aware"
    assert completed_at.tzinfo == timezone.utc, \
        "completed_at should be in UTC"
    
    print(f"✅ Failed task timezone-aware: {completed_at.isoformat()}")


@pytest.mark.asyncio
async def test_datetime_comparison_works_correctly(session: AsyncSession):
    """
    Test that timezone-aware datetimes compare correctly.
    
    This would fail with timezone-naive datetimes due to TypeError
    when comparing aware and naive datetimes.
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create task
    task_id = (await engine.create_task(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    ))["task_id"]
    
    await session.commit()
    
    task = await engine.tasks.get(tenant_id, task_id)
    
    # Get current time (timezone-aware)
    now = datetime.now(timezone.utc)
    
    # This should work without TypeError
    assert task.created_at <= now, \
        "Task creation time should be before or equal to now"
    
    # Verify we can do datetime arithmetic
    age = now - task.created_at
    assert age.total_seconds() >= 0, \
        "Task age should be non-negative"
    
    print(f"✅ Datetime comparison works: task age = {age.total_seconds():.3f}s")


@pytest.mark.asyncio  
async def test_timezone_info_preserved_through_db_roundtrip(session: AsyncSession):
    """
    Test that timezone info is preserved through database storage.
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "test-worker"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create and complete task
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
    
    # Complete with current time
    completion_time = datetime.now(timezone.utc)
    
    await engine.complete(
        tenant_id=tenant_id,
        worker_id=worker_id,
        task_id=task_id,
        lease_id=lease.lease_id,
        result={"status": "done"},
        artifacts=[{"type": "test", "uri": "mem://timezone/roundtrip"}],
    )
    
    await session.commit()
    
    # Retrieve from database in fresh query
    session.expire_all()  # Clear session cache
    task = await engine.tasks.get(tenant_id, task_id)
    
    # Verify timezone preserved
    assert task.result.completed_at.tzinfo is not None, \
        "Timezone should be preserved through DB roundtrip"
    assert task.result.completed_at.tzinfo == timezone.utc, \
        "Should still be UTC after DB roundtrip"
    
    # Verify time is close to when we completed
    time_diff = abs((task.result.completed_at - completion_time).total_seconds())
    assert time_diff < 1.0, \
        f"Completion time should be close to when we called complete: {time_diff}s"
    
    print(f"✅ Timezone preserved through DB: {task.result.completed_at.isoformat()}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
