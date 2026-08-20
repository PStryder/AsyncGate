"""Operational state is materialised from committed custody, not owned locally.

AsyncGate's leases and task rows are a projection of the ledger. When the two
disagree the ledger wins, and reconciliation is how that gets applied.

This is also the first real consumer of `receiptgate.list_inbox`. Nothing in the
stack called it outside tests, which meant the read side of the notary had never
been exercised by a component that depended on the answer.

Three situations produce a disagreement, none of them a bug:

  buffered then committed   a `complete` that could not reach the ledger sits in
                            the outbox and deliberately does not advance local
                            state; when replay commits it, the ledger stops
                            listing the obligation and local rows must follow
  departed instance         leases owned by a machine that went away were never
                            swept, so every rolling deploy stranded tasks
  rebuilt local state       a restored backup or a new deployment reading an
                            existing ledger
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from asyncgate.db.repositories import LeaseRepository, TaskRepository
from asyncgate.engine.reconcile import ReconciliationService
from asyncgate.models import Principal, PrincipalKind
from asyncgate.models.enums import TaskStatus


class _Ledger:
    """Stands in for ReceiptGate's committed custody."""

    def __init__(self, rows=None, fail=False):
        self.rows = rows or []
        self.fail = fail
        self.asked_for: list[str] = []

    async def list_custody(self, recipient_ai, *, limit=200):
        self.asked_for.append(recipient_ai)
        if self.fail:
            raise RuntimeError("receiptgate unreachable")
        return self.rows


@pytest.fixture
def ledger(monkeypatch):
    stub = _Ledger()
    monkeypatch.setattr(
        "asyncgate.engine.reconcile.get_receiptgate_client", lambda: stub
    )
    return stub


async def _task_with_lease(async_session, tenant_id, worker_id):
    agent = Principal(kind=PrincipalKind.AGENT, id="owner")
    async with async_session() as session:
        task = await TaskRepository(session).create(
            tenant_id=tenant_id, type="test_task", payload={}, created_by=agent
        )
        await session.commit()
    async with async_session() as session:
        lease = (
            await LeaseRepository(session).claim_next(
                tenant_id=tenant_id, worker_id=worker_id, max_tasks=1
            )
        )[0]
        await session.commit()
    return task, lease


@pytest.mark.asyncio
async def test_a_lease_the_ledger_no_longer_backs_is_released(engine, ledger):
    """The buffered-then-committed case, and the transferred case.

    Either way the ledger has stopped saying this worker owes it, so continuing
    to hold the grant blocks the task from being offered to whoever should have
    it now.
    """
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id = uuid4()
    task, lease = await _task_with_lease(async_session, tenant_id, "worker-a")

    ledger.rows = []  # the ledger says worker-a owes nothing

    async with async_session() as session:
        report = await ReconciliationService(session).reconcile_worker(
            tenant_id, "worker-a"
        )
        await session.commit()

    assert report.released_stale == [str(lease.lease_id)]
    assert report.settled_tasks == [str(task.task_id)]

    async with async_session() as session:
        assert await LeaseRepository(session).list_for_worker(tenant_id, "worker-a") == []
        current = await TaskRepository(session).get(tenant_id, task.task_id)
    assert current.status is TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_a_lease_the_ledger_still_backs_is_left_alone(engine, ledger):
    """Reconciliation must not release live grants."""
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id = uuid4()
    task, lease = await _task_with_lease(async_session, tenant_id, "worker-a")

    ledger.rows = [
        {"obligation_id": "obl-1", "task_id": str(task.task_id), "state": "OPEN"}
    ]

    async with async_session() as session:
        report = await ReconciliationService(session).reconcile_worker(
            tenant_id, "worker-a"
        )
        await session.commit()

    assert report.released_stale == []
    assert report.changed is False

    async with async_session() as session:
        still_held = await LeaseRepository(session).list_for_worker(tenant_id, "worker-a")
    assert [x.lease_id for x in still_held] == [lease.lease_id]


@pytest.mark.asyncio
async def test_an_overdue_obligation_is_still_owed(engine, ledger):
    """OVERDUE is not a release. The custodian has not changed, only the clock."""
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id = uuid4()
    task, lease = await _task_with_lease(async_session, tenant_id, "worker-a")

    ledger.rows = [
        {"obligation_id": "obl-1", "task_id": str(task.task_id), "state": "OVERDUE"}
    ]

    async with async_session() as session:
        report = await ReconciliationService(session).reconcile_worker(
            tenant_id, "worker-a"
        )
        await session.commit()

    assert report.released_stale == []


@pytest.mark.asyncio
async def test_obligations_with_no_local_lease_are_reported(engine, ledger):
    """The departed-instance and rebuilt-state case.

    The ledger says this worker owes something the projection has no record of.
    Reported rather than silently invented: a lease is an operational grant, and
    manufacturing one here would have AsyncGate deciding something it is meant
    to be reading.
    """
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id = uuid4()
    orphan = str(uuid4())
    ledger.rows = [{"obligation_id": "obl-9", "task_id": orphan, "state": "OPEN"}]

    async with async_session() as session:
        report = await ReconciliationService(session).reconcile_worker(
            tenant_id, "worker-ghost"
        )

    assert report.missing_locally == [orphan]
    assert report.released_stale == []


@pytest.mark.asyncio
async def test_an_unreadable_ledger_stops_reconciliation(engine, ledger):
    """Reconciling against a guess would release live grants on a failed call."""
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id = uuid4()
    task, lease = await _task_with_lease(async_session, tenant_id, "worker-a")
    ledger.fail = True

    async with async_session() as session:
        with pytest.raises(RuntimeError):
            await ReconciliationService(session).reconcile_worker(tenant_id, "worker-a")

    async with async_session() as session:
        held = await LeaseRepository(session).list_for_worker(tenant_id, "worker-a")
    assert [x.lease_id for x in held] == [lease.lease_id], (
        "a grant was released on the strength of a failed read"
    )


@pytest.mark.asyncio
async def test_reconciliation_never_emits_a_receipt(engine, ledger):
    """One-directional by construction.

    A reconciler that emitted receipts would let an operational component change
    the authoritative answer to who owes what -- the one thing the design exists
    to prevent.
    """
    import inspect

    from asyncgate.engine import reconcile

    source = inspect.getsource(reconcile)
    for forbidden in ("emit_receipt", "submit_receipt", "_emit_receipt"):
        assert forbidden not in source, (
            f"reconcile.py calls {forbidden}; reconciliation reads committed "
            "custody and writes local rows, and must never write receipts"
        )
