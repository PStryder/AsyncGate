"""
P1.1 Tests: Lease Renewal Limits

Tests that lease renewal limits are enforced to prevent lease hoarding DoS.
"""

import pytest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from asyncgate.config import settings
from asyncgate.db.repositories import LeaseRepository, TaskRepository
from asyncgate.engine import AsyncGateEngine, LeaseRenewalLimitExceeded, LeaseLifetimeExceeded
from asyncgate.models import Principal, PrincipalKind, TaskStatus


@pytest.mark.asyncio
async def test_renewal_count_limit_enforced(session: AsyncSession):
    """
    Test that max_lease_renewals limit is enforced.
    
    P1.1: Prevents workers from renewing leases infinitely.
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
    
    # Renew up to the limit (default: 10 renewals)
    max_renewals = settings.max_lease_renewals
    
    for i in range(max_renewals):
        renewed_lease = await engine.leases.renew(
            tenant_id=tenant_id,
            task_id=task_id,
            lease_id=lease.lease_id,
            worker_id=worker_id,
        )
        assert renewed_lease is not None, f"Renewal {i+1} should succeed"
        await session.commit()
    
    # Next renewal should fail (hit limit)
    with pytest.raises(LeaseRenewalLimitExceeded) as exc_info:
        await engine.leases.renew(
            tenant_id=tenant_id,
            task_id=task_id,
            lease_id=lease.lease_id,
            worker_id=worker_id,
        )
    
    assert exc_info.value.renewal_count == max_renewals
    assert exc_info.value.max_renewals == max_renewals
    
    print(f"✅ Renewal count limit enforced: {max_renewals} renewals allowed")


@pytest.mark.asyncio
async def test_absolute_lifetime_limit_enforced(session: AsyncSession):
    """
    Test that max_lease_lifetime_seconds limit is enforced.
    
    P1.1: Prevents workers from holding leases indefinitely by renewing before expiry.
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
    
    # Manually set acquired_at to past (simulate lease held for too long)
    from asyncgate.db.tables import LeaseTable
    from sqlalchemy import update
    
    max_lifetime = settings.max_lease_lifetime_seconds
    past_time = datetime.now(timezone.utc) - timedelta(seconds=max_lifetime + 10)
    
    await session.execute(
        update(LeaseTable)
        .where(LeaseTable.lease_id == lease.lease_id)
        .values(acquired_at=past_time)
    )
    await session.commit()
    
    # Try to renew - should fail (lifetime exceeded)
    with pytest.raises(LeaseLifetimeExceeded) as exc_info:
        await engine.leases.renew(
            tenant_id=tenant_id,
            task_id=task_id,
            lease_id=lease.lease_id,
            worker_id=worker_id,
        )
    
    assert exc_info.value.lifetime_seconds >= max_lifetime
    assert exc_info.value.max_lifetime == max_lifetime
    
    print(f"✅ Absolute lifetime limit enforced: {max_lifetime}s maximum")


@pytest.mark.asyncio
async def test_renewal_count_increments_correctly(session: AsyncSession):
    """
    Test that renewal_count is incremented on each renewal.
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
    
    # Check initial renewal_count is 0
    from asyncgate.db.tables import LeaseTable
    from sqlalchemy import select
    
    result = await session.execute(
        select(LeaseTable).where(LeaseTable.lease_id == lease.lease_id)
    )
    lease_row = result.scalar_one()
    assert lease_row.renewal_count == 0, "Initial renewal_count should be 0"
    
    # Renew 3 times
    for expected_count in range(1, 4):
        await engine.leases.renew(
            tenant_id=tenant_id,
            task_id=task_id,
            lease_id=lease.lease_id,
            worker_id=worker_id,
        )
        await session.commit()
        
        # Verify count incremented
        result = await session.execute(
            select(LeaseTable).where(LeaseTable.lease_id == lease.lease_id)
        )
        lease_row = result.scalar_one()
        assert lease_row.renewal_count == expected_count, \
            f"After {expected_count} renewals, count should be {expected_count}"
    
    print(f"✅ Renewal count increments correctly: 0 → 1 → 2 → 3")


@pytest.mark.asyncio
async def test_acquired_at_preserved_across_renewals(session: AsyncSession):
    """
    Test that acquired_at timestamp is not changed during renewals.
    
    This is critical for lifetime enforcement.
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
    
    # Get initial acquired_at
    from asyncgate.db.tables import LeaseTable
    from sqlalchemy import select
    
    result = await session.execute(
        select(LeaseTable).where(LeaseTable.lease_id == lease.lease_id)
    )
    initial_acquired_at = result.scalar_one().acquired_at
    
    # Renew multiple times
    for _ in range(3):
        await engine.leases.renew(
            tenant_id=tenant_id,
            task_id=task_id,
            lease_id=lease.lease_id,
            worker_id=worker_id,
        )
        await session.commit()
    
    # Verify acquired_at unchanged
    result = await session.execute(
        select(LeaseTable).where(LeaseTable.lease_id == lease.lease_id)
    )
    final_acquired_at = result.scalar_one().acquired_at
    
    assert initial_acquired_at == final_acquired_at, \
        "acquired_at should not change during renewals"
    
    print(f"✅ acquired_at preserved across renewals")


@pytest.mark.asyncio
async def test_new_lease_after_limit_works(session: AsyncSession):
    """
    Test that releasing and reclaiming a task resets renewal limits.
    
    Verifies that limits apply per-lease, not per-task.
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "test-worker"
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    
    # Create task
    task_id = (await engine.tasks.create(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    )).task_id
    
    await session.commit()
    
    # Claim, renew to limit, hit error
    leases = await engine.leases.claim_next(
        tenant_id=tenant_id,
        worker_id=worker_id,
        max_tasks=1,
    )
    lease1 = leases[0]
    
    await session.commit()
    
    # Renew to limit
    for _ in range(settings.max_lease_renewals):
        await engine.leases.renew(
            tenant_id=tenant_id,
            task_id=task_id,
            lease_id=lease1.lease_id,
            worker_id=worker_id,
        )
        await session.commit()
    
    # Verify next renewal fails
    with pytest.raises(LeaseRenewalLimitExceeded):
        await engine.leases.renew(
            tenant_id=tenant_id,
            task_id=task_id,
            lease_id=lease1.lease_id,
            worker_id=worker_id,
        )
    
    # Release lease and reclaim (new lease)
    await engine.leases.release(tenant_id, task_id)
    await engine.tasks.requeue_on_expiry(tenant_id, task_id)
    await session.commit()
    
    leases2 = await engine.leases.claim_next(
        tenant_id=tenant_id,
        worker_id=worker_id,
        max_tasks=1,
    )
    lease2 = leases2[0]
    
    await session.commit()
    
    # New lease should allow renewals again
    renewed = await engine.leases.renew(
        tenant_id=tenant_id,
        task_id=task_id,
        lease_id=lease2.lease_id,
        worker_id=worker_id,
    )
    
    assert renewed is not None, "New lease should allow renewals"
    
    print(f"✅ New lease after limit allows renewals again")


@pytest.mark.asyncio
async def test_claim_and_renew_return_tracking_fields(session: AsyncSession):
    """
    Test that lease claim/renew returns acquired_at and renewal_count.
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "test-worker"
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

    assert lease.acquired_at is not None, "acquired_at should be populated on claim"
    assert lease.renewal_count == 0, "renewal_count should start at 0"

    await session.commit()

    renewed = await engine.leases.renew(
        tenant_id=tenant_id,
        task_id=task_id,
        lease_id=lease.lease_id,
        worker_id=worker_id,
    )

    assert renewed is not None, "Renewal should return a lease"
    assert renewed.acquired_at == lease.acquired_at, "acquired_at should be preserved on renew"
    assert renewed.renewal_count == 1, "renewal_count should increment on renew"

    print("OK. Lease claim/renew tracking fields verified")


@pytest.mark.asyncio
async def test_expire_leases_returns_without_validation_errors(session: AsyncSession):
    """
    Test that lease expiry path works without validation errors.
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    worker_id = "test-worker"
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

    from asyncgate.db.tables import LeaseTable
    from sqlalchemy import update

    await session.execute(
        update(LeaseTable)
        .where(LeaseTable.lease_id == lease.lease_id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=10))
    )
    await session.commit()

    expired = await engine.expire_leases(batch_size=1)
    assert expired >= 1, "Expected at least one lease to expire"

    task = await engine.tasks.get(tenant_id, task_id)
    assert task.status == TaskStatus.QUEUED, "Expired task should be requeued"

    print("OK. Lease expiry path verified")


@pytest.mark.asyncio
async def test_config_values_respected(session: AsyncSession):
    """
    Test that config values for limits are actually used.
    """
    from asyncgate.config import settings
    
    # Verify config values are set
    assert settings.max_lease_renewals > 0, "max_lease_renewals must be positive"
    assert settings.max_lease_lifetime_seconds > 0, "max_lease_lifetime_seconds must be positive"
    
    # Verify defaults are sane
    assert settings.max_lease_renewals >= 5, "Should allow at least 5 renewals"
    assert settings.max_lease_lifetime_seconds >= 1800, "Should allow at least 30 minutes"
    
    print(f"✅ Config values: {settings.max_lease_renewals} renewals, {settings.max_lease_lifetime_seconds}s lifetime")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
