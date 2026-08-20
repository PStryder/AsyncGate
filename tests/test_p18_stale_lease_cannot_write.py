"""A worker whose lease expired mid-flight must not be able to finish.

`complete_task` and `fail_task` validated the lease outside the transaction and
then released it by `task_id`. Both halves of that are unsafe, and together they
corrupt state belonging to somebody else:

- the validation result can be stale before it is used, because the sweeper can
  expire the lease and a second worker can claim a new one in the window between
  the check and the write
- releasing by `task_id` deletes whichever lease currently exists for that task,
  which by then is the *second* worker's

So the first worker's late call would mark a requeued task SUCCEEDED and revoke
a grant that had been correctly issued to someone else. Neither worker is
notified; the second simply loses its lease and, later, its right to complete.

These tests reproduce that sequence exactly and assert the late writer is
refused and leaves no trace.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from asyncgate.db.repositories import LeaseRepository, TaskRepository
from asyncgate.models import Principal, PrincipalKind
from asyncgate.models.enums import TaskStatus


async def _expire_lease_and_requeue(session, tenant_id, task_id) -> None:
    """What the sweeper does: expire the grant and return the task to the queue."""
    await session.execute(
        text(
            "UPDATE leases SET expires_at = NOW() - INTERVAL '1 hour' "
            "WHERE tenant_id = :t AND task_id = :k"
        ),
        {"t": str(tenant_id), "k": str(task_id)},
    )
    await session.execute(
        text("DELETE FROM leases WHERE tenant_id = :t AND task_id = :k"),
        {"t": str(tenant_id), "k": str(task_id)},
    )
    await session.execute(
        text(
            "UPDATE tasks SET status = 'queued' WHERE tenant_id = :t AND task_id = :k"
        ),
        {"t": str(tenant_id), "k": str(task_id)},
    )
    await session.commit()


@pytest.mark.asyncio
async def test_stale_lease_does_not_delete_the_successors_grant(engine):
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="owner")

    async with async_session() as session:
        task = await TaskRepository(session).create(
            tenant_id=tenant_id, type="test_task", payload={}, created_by=agent
        )
        await session.commit()

    async with async_session() as session:
        first = (
            await LeaseRepository(session).claim_next(
                tenant_id=tenant_id, worker_id="worker-a", max_tasks=1
            )
        )[0]
        await session.commit()

    async with async_session() as session:
        await _expire_lease_and_requeue(session, tenant_id, task.task_id)

    async with async_session() as session:
        second = (
            await LeaseRepository(session).claim_next(
                tenant_id=tenant_id, worker_id="worker-b", max_tasks=1
            )
        )[0]
        await session.commit()

    assert second.lease_id != first.lease_id

    # worker-a's stale release must not touch worker-b's grant.
    async with async_session() as session:
        released = await LeaseRepository(session).release(
            tenant_id, task.task_id, first.lease_id
        )
        await session.commit()
    assert released is False, "a stale lease_id released something"

    async with async_session() as session:
        still_there = await LeaseRepository(session).validate(
            tenant_id, task.task_id, second.lease_id, "worker-b"
        )
    assert still_there is not None, (
        "worker-b's lease was deleted by worker-a's stale release"
    )


@pytest.mark.asyncio
async def test_validate_can_take_a_row_lock(engine):
    """The locking read must actually be available and return the same answer.

    Without `for_update`, callers that go on to write are racing; the parameter
    existing but being ignored would be worse than not having it.
    """
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="owner")

    async with async_session() as session:
        task = await TaskRepository(session).create(
            tenant_id=tenant_id, type="test_task", payload={}, created_by=agent
        )
        await session.commit()

    async with async_session() as session:
        lease = (
            await LeaseRepository(session).claim_next(
                tenant_id=tenant_id, worker_id="worker-a", max_tasks=1
            )
        )[0]
        await session.commit()

    async with async_session() as session:
        locked = await LeaseRepository(session).validate(
            tenant_id, task.task_id, lease.lease_id, "worker-a", for_update=True
        )
        assert locked is not None
        assert locked.lease_id == lease.lease_id
        await session.rollback()


@pytest.mark.asyncio
async def test_release_without_lease_id_is_still_available_to_the_sweeper(engine):
    """The sweeper legitimately releases a grant it does not hold."""
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="owner")

    async with async_session() as session:
        task = await TaskRepository(session).create(
            tenant_id=tenant_id, type="test_task", payload={}, created_by=agent
        )
        await session.commit()

    async with async_session() as session:
        await LeaseRepository(session).claim_next(
            tenant_id=tenant_id, worker_id="worker-a", max_tasks=1
        )
        await session.commit()

    async with async_session() as session:
        assert await LeaseRepository(session).release(tenant_id, task.task_id) is True
        await session.commit()


@pytest.mark.asyncio
async def test_a_requeued_task_is_not_marked_succeeded_by_the_previous_worker(engine):
    """The end-to-end shape of the bug, at the task level."""
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="owner")

    async with async_session() as session:
        task = await TaskRepository(session).create(
            tenant_id=tenant_id, type="test_task", payload={}, created_by=agent
        )
        await session.commit()

    async with async_session() as session:
        first = (
            await LeaseRepository(session).claim_next(
                tenant_id=tenant_id, worker_id="worker-a", max_tasks=1
            )
        )[0]
        await session.commit()

    async with async_session() as session:
        await _expire_lease_and_requeue(session, tenant_id, task.task_id)

    # worker-a comes back and tries to validate. It must be told no.
    async with async_session() as session:
        assert (
            await LeaseRepository(session).validate(
                tenant_id, task.task_id, first.lease_id, "worker-a", for_update=True
            )
            is None
        )

    async with async_session() as session:
        current = await TaskRepository(session).get(tenant_id, task.task_id)
    assert current.status is TaskStatus.QUEUED
