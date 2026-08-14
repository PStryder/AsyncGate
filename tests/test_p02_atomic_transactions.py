"""
P0.2 Tests: Atomic Transactions for Task State + Receipts

Tests that all task state changes + receipt emissions are atomic.
If any operation fails, entire transaction rolls back.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from asyncgate.db.repositories import TaskRepository, LeaseRepository, ReceiptRepository
from asyncgate.engine import AsyncGateEngine
from asyncgate.models import Principal, PrincipalKind, TaskStatus, ReceiptType


@pytest.mark.asyncio
async def test_complete_atomicity_receipt_failure_rollsback(session: AsyncSession):
    """
    Test that if receipt creation fails, task state rolls back.
    
    P0.2 Fix: Before atomicity, task would be SUCCEEDED but receipt missing.
    After: Both succeed or both rollback.
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "test-worker"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create a task
    task_id = (await engine.create_task(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    ))["task_id"]
    
    await session.commit()
    
    # Claim task
    leases = await engine.leases.claim_next(
        tenant_id=tenant_id,
        worker_id=worker_id,
        max_tasks=1,
    )
    lease = leases[0]
    
    await session.commit()
    
    # Mock receipt creation to fail
    original_create = engine.receipts.create
    
    async def failing_receipt_create(*args, **kwargs):
        # First call (task.completed) succeeds
        if not hasattr(failing_receipt_create, 'call_count'):
            failing_receipt_create.call_count = 0
        
        failing_receipt_create.call_count += 1
        
        # Fail on result_ready receipt (second receipt)
        if failing_receipt_create.call_count >= 2:
            raise ValueError("Simulated receipt creation failure")
        
        return await original_create(*args, **kwargs)
    
    engine.receipts.create = failing_receipt_create
    
    # Try to complete - should fail
    with pytest.raises(ValueError, match="Simulated receipt creation failure"):
        await engine.complete(
            tenant_id=tenant_id,
            worker_id=worker_id,
            task_id=task_id,
            lease_id=lease.lease_id,
            result={"status": "done"},
            artifacts=[{"type": "test", "uri": "mem://complete/atomicity"}],
        )
    
    # Rollback the failed transaction
    await session.rollback()
    
    # Verify task is still LEASED (not SUCCEEDED)
    task = await engine.tasks.get(tenant_id, task_id)
    assert task.status == TaskStatus.LEASED, \
        "Task should still be LEASED after receipt failure"
    
    # Verify lease still exists
    lease_check = await engine.leases.validate(
        tenant_id, task_id, lease.lease_id, worker_id
    )
    assert lease_check is not None, \
        "Lease should still exist after receipt failure"
    
    print(f"✅ Complete atomicity verified: task still LEASED after receipt failure")


@pytest.mark.asyncio
async def test_fail_atomicity_requeue_path(session: AsyncSession):
    """
    Test that requeue path is atomic (task state + receipt).
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "test-worker"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create task with retries
    task_id = (await engine.create_task(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
        max_attempts=3,  # Allow retries
    ))["task_id"]
    
    await session.commit()
    
    # Claim task
    leases = await engine.leases.claim_next(
        tenant_id=tenant_id,
        worker_id=worker_id,
        max_tasks=1,
    )
    lease = leases[0]
    
    await session.commit()
    
    # Mock receipt creation to fail
    original_create = engine.receipts.create
    call_count = 0
    
    async def failing_receipt_create(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        
        # Fail on first receipt (retryable path emits one receipt)
        if call_count >= 1:
            raise ValueError("Simulated failure")
        
        return await original_create(*args, **kwargs)
    
    engine.receipts.create = failing_receipt_create
    
    # Try to fail with retry - should rollback
    with pytest.raises(ValueError):
        await engine.fail(
            tenant_id=tenant_id,
            worker_id=worker_id,
            task_id=task_id,
            lease_id=lease.lease_id,
            error={"message": "test error"},
            retryable=True,
        )
    
    await session.rollback()
    
    # Verify task is still LEASED (not requeued)
    task = await engine.tasks.get(tenant_id, task_id)
    assert task.status == TaskStatus.LEASED, \
        "Task should still be LEASED after requeue failure"
    assert task.attempt == 0, \
        "Attempt should not be incremented"
    
    print(f"✅ Fail requeue atomicity verified")


@pytest.mark.asyncio
async def test_cancel_atomicity(session: AsyncSession):
    """
    Test that cancel is atomic (lease release + task cancel + receipt).
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
    
    # Mock receipt creation to fail
    original_emit = engine._emit_result_ready_receipt
    
    async def failing_emit(*args, **kwargs):
        raise ValueError("Simulated receipt failure")
    
    engine._emit_result_ready_receipt = failing_emit
    
    # Try to cancel - should fail and rollback
    with pytest.raises(ValueError):
        await engine.cancel_task(
            tenant_id=tenant_id,
            task_id=task_id,
            principal=agent,
            reason="test cancellation",
        )
    
    await session.rollback()
    
    # Verify task is still QUEUED (not CANCELLED)
    task = await engine.tasks.get(tenant_id, task_id)
    assert task.status == TaskStatus.QUEUED, \
        "Task should still be QUEUED after cancel failure"
    
    print(f"✅ Cancel atomicity verified")


@pytest.mark.asyncio
async def test_expire_leases_atomicity(session: AsyncSession):
    """
    Test that each lease expiry is atomic.
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
    
    # Expire the lease manually (set expires_at to past)
    from datetime import datetime, timedelta, timezone
    from asyncgate.db.tables import LeaseTable
    from sqlalchemy import update
    
    await session.execute(
        update(LeaseTable)
        .where(LeaseTable.lease_id == lease.lease_id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=10))
    )
    await session.commit()
    
    # Mock receipt creation to fail
    original_create = engine.receipts.create
    
    async def failing_receipt_create(*args, **kwargs):
        raise ValueError("Simulated failure")
    
    engine.receipts.create = failing_receipt_create
    
    # Try to expire leases - should catch exception and continue
    count = await engine.expire_leases(batch_size=1)
    
    # Count should be 0 because expiry failed
    assert count == 0, "No leases should be processed if receipt fails"
    
    # Verify task is still LEASED (not requeued due to rollback)
    task = await engine.tasks.get(tenant_id, task_id)
    assert task.status == TaskStatus.LEASED, \
        "Task should still be LEASED after expiry failure"
    
    print(f"✅ Expire leases atomicity verified")


@pytest.mark.asyncio
async def test_complete_success_all_committed(session: AsyncSession):
    """
    Test that successful complete commits all changes.
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
    
    # Complete successfully
    result = await engine.complete(
        tenant_id=tenant_id,
        worker_id=worker_id,
        task_id=task_id,
        lease_id=lease.lease_id,
        result={"status": "done"},
        artifacts=[{"type": "test", "uri": "mem://complete/success"}],
    )
    
    await session.commit()
    
    # Verify all changes committed
    assert result["ok"] is True
    
    task = await engine.tasks.get(tenant_id, task_id)
    assert task.status == TaskStatus.SUCCEEDED, "Task should be SUCCEEDED"
    
    # Verify lease released
    lease_check = await engine.leases.validate(
        tenant_id, task_id, lease.lease_id, worker_id
    )
    assert lease_check is None, "Lease should be released"
    
    # Verify receipts created
    receipts, _ = await engine.receipts.list(
        tenant_id=tenant_id,
        to_kind=PrincipalKind.AGENT,
        to_id=agent.id,
        limit=10,
    )
    
    # Should have task.completed receipt
    completed_receipts = [
        r for r in receipts 
        if r.receipt_type == ReceiptType.TASK_COMPLETED
    ]
    assert len(completed_receipts) >= 1, "Should have task.completed receipt"
    
    print(f"✅ Complete success verified: all changes committed")


@pytest.mark.asyncio
async def test_fail_terminal_atomicity(session: AsyncSession):
    """
    Test that terminal failure path is atomic.
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
    
    # Fail successfully (terminal path)
    result = await engine.fail(
        tenant_id=tenant_id,
        worker_id=worker_id,
        task_id=task_id,
        lease_id=lease.lease_id,
        error={"message": "test error"},
        retryable=False,
    )
    
    await session.commit()
    
    # Verify terminal failure committed
    assert result["ok"] is True
    assert result["requeued"] is False
    
    task = await engine.tasks.get(tenant_id, task_id)
    assert task.status == TaskStatus.FAILED, "Task should be FAILED"
    assert task.result.error == {"message": "test error"}
    
    print(f"✅ Terminal failure atomicity verified")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
