"""Rebuild operational state from committed custody.

AsyncGate's leases and task statuses are a *projection*. The ledger holds the
authoritative answer to who owes what; these rows are a local index over it,
kept for scheduling. When the two disagree the ledger wins, and this module is
how that gets applied.

Three situations produce a disagreement, and none of them is a bug in either
component:

**A buffered transition later commits.** A `complete` that could not reach the
ledger sits in the durable outbox, and the emitter deliberately does not advance
its own state on it -- the task stays running and the lease stays held. When
replay commits the receipt, the ledger stops listing the obligation and this
settles the local rows to match.

**An instance departs.** Leases owned by a machine that has gone away were never
swept, because the sweeper filtered by instance, so every rolling deploy
stranded tasks. Under a projection model that is not a sweeper's job: the
question is not "whose machine issued this" but "does the ledger still say this
principal owes it".

**Local state is lost or rebuilt.** A restored backup, a recreated database, a
new deployment reading an existing ledger. The obligations are still owed; the
projection simply has not been built yet.

Deliberately one-directional: this reads committed custody and writes local
rows. It never writes receipts. Reconciliation that emitted receipts would let
an operational component change the authoritative answer to "who owes what",
which is the thing the whole design exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from asyncgate.db.repositories import LeaseRepository, TaskRepository
from asyncgate.integrations import get_receiptgate_client
from asyncgate.models.enums import TaskStatus

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationReport:
    """What reconciliation found and did. Returned rather than logged only.

    Callers need to be able to assert on this -- an operator, a test, or the
    adversarial harness -- and a routine that silently repairs state is one
    nobody can check.
    """

    worker_id: str
    held_by_ledger: int = 0
    local_leases: int = 0
    released_stale: list[str] = field(default_factory=list)
    settled_tasks: list[str] = field(default_factory=list)
    missing_locally: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.released_stale or self.settled_tasks)


class ReconciliationService:
    """Materialise AsyncGate's operational state from committed custody."""

    def __init__(self, session) -> None:
        self.session = session
        self.leases = LeaseRepository(session)
        self.tasks = TaskRepository(session)

    async def reconcile_worker(
        self, tenant_id: UUID, worker_id: str
    ) -> ReconciliationReport:
        """Bring one worker's local rows in line with what the ledger says.

        Raises if the ledger cannot be read. Reconciling against a guess is
        worse than not reconciling: it would release live grants on the strength
        of a failed HTTP call.
        """
        client = get_receiptgate_client()
        custody = await client.list_custody(worker_id)

        # The ledger keys custody by obligation; the local projection is keyed
        # by task. `list_inbox` returns both, so the join is already done.
        owed_task_ids = {
            str(row["task_id"]) for row in custody if row.get("task_id")
        }
        report = ReconciliationReport(
            worker_id=worker_id, held_by_ledger=len(custody)
        )

        local = await self.leases.list_for_worker(tenant_id, worker_id)
        report.local_leases = len(local)

        for lease in local:
            if str(lease.task_id) in owed_task_ids:
                continue

            # The ledger does not list this obligation as owed by this worker:
            # it was completed, or transferred to someone else. Either way the
            # local grant is a leftover, and holding it blocks the task from
            # being offered to whoever should have it now.
            await self.leases.release(lease.tenant_id, lease.task_id, lease.lease_id)
            report.released_stale.append(str(lease.lease_id))

            task = await self.tasks.get(tenant_id, lease.task_id)
            if task and not task.is_terminal():
                # Do not invent an outcome. The ledger settled this obligation;
                # what the local row must stop saying is that the work is still
                # in flight and leased. Returning it to the queue lets the next
                # offer be decided normally, and a task the ledger considers
                # closed will simply never be re-accepted.
                await self.tasks.update_status(
                    tenant_id, lease.task_id, TaskStatus.QUEUED
                )
                report.settled_tasks.append(str(lease.task_id))

        local_task_ids = {str(lease.task_id) for lease in local}
        report.missing_locally = sorted(owed_task_ids - local_task_ids)

        if report.changed or report.missing_locally:
            logger.info(
                "reconciled worker %s: ledger_holds=%d local_leases=%d "
                "released=%d settled=%d missing_locally=%d",
                worker_id,
                report.held_by_ledger,
                report.local_leases,
                len(report.released_stale),
                len(report.settled_tasks),
                len(report.missing_locally),
                extra={
                    "worker_id": worker_id,
                    "released_stale": report.released_stale,
                    "settled_tasks": report.settled_tasks,
                    "missing_locally": report.missing_locally,
                },
            )
        return report
