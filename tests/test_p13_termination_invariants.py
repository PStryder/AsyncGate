"""
P1.x Tests: Termination invariants and non-terminal gating.
"""

import pytest
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from asyncgate.db.repositories import ReceiptRepository
from asyncgate.engine import AsyncGateEngine
from asyncgate.models import Principal, PrincipalKind, ReceiptType
from asyncgate.principals import SERVICE_PRINCIPAL_ID


@pytest.mark.asyncio
async def test_cancel_emits_terminal_receipt_and_closes(session: AsyncSession):
    """
    Cancel should emit task.canceled and close the obligation.
    """
    engine = AsyncGateEngine(session)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")

    task_id = (await engine.create_task(
        tenant_id=tenant_id,
        type="test_task",
        payload={"data": "test"},
        created_by=agent,
        principal_ai=agent.id,
    ))["task_id"]

    await session.commit()

    obligation = await engine.receipts.get_task_obligation(
        tenant_id=tenant_id,
        task_id=task_id,
        owner=agent,
    )
    assert obligation is not None, "Expected task.assigned receipt"

    result = await engine.cancel_task(
        tenant_id=tenant_id,
        task_id=task_id,
        principal=agent,
        reason="test cancellation",
    )

    await session.commit()
    assert result["ok"] is True

    terminators = await engine.receipts.get_terminators(
        tenant_id=tenant_id,
        parent_receipt_id=obligation.receipt_id,
        limit=10,
    )
    canceled = [r for r in terminators if r.receipt_type == ReceiptType.TASK_CANCELED]
    assert canceled, "Expected task.canceled receipt to terminate obligation"
    assert obligation.receipt_id in canceled[0].parents

    open_obligations, _ = await engine.receipts.list_open_obligations(
        tenant_id=tenant_id,
        to_kind=agent.kind,
        to_id=agent.id,
        limit=10,
    )
    open_ids = {o.receipt_id for o in open_obligations}
    assert obligation.receipt_id not in open_ids, "Canceled obligation should be closed"

    print("OK. Cancel emits terminal receipt and closes obligation")


@pytest.mark.asyncio
async def test_non_terminal_receipts_do_not_close_obligation(session: AsyncSession):
    """
    Non-terminal receipts with matching parents must not terminate obligations.
    """
    receipts = ReceiptRepository(session)
    tenant_id = uuid4()
    owner = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    service = Principal(kind=PrincipalKind.SERVICE, id=SERVICE_PRINCIPAL_ID)
    worker = Principal(kind=PrincipalKind.WORKER, id="worker-1")

    obligation = await receipts.create(
        tenant_id=tenant_id,
        receipt_type=ReceiptType.TASK_ASSIGNED,
        from_principal=service,
        to_principal=owner,
        task_id=uuid4(),
        body={"task_type": "test"},
    )

    await receipts.create(
        tenant_id=tenant_id,
        receipt_type=ReceiptType.TASK_PROGRESS,
        from_principal=worker,
        to_principal=owner,
        task_id=obligation.task_id,
        parents=[obligation.receipt_id],
        body={"progress": {"pct": 10}},
    )

    await receipts.create(
        tenant_id=tenant_id,
        receipt_type=ReceiptType.RECEIPT_ACKNOWLEDGED,
        from_principal=owner,
        to_principal=service,
        parents=[obligation.receipt_id],
        body={"acknowledged_receipt_id": str(obligation.receipt_id)},
    )

    await receipts.create(
        tenant_id=tenant_id,
        receipt_type=ReceiptType.SYSTEM_ANOMALY,
        from_principal=service,
        to_principal=owner,
        task_id=obligation.task_id,
        parents=[obligation.receipt_id],
        body={"anomaly_type": "test", "message": "non-terminal"},
    )

    await session.commit()

    assert await receipts.has_terminator(tenant_id, obligation.receipt_id) is False

    open_obligations, _ = await receipts.list_open_obligations(
        tenant_id=tenant_id,
        to_kind=owner.kind,
        to_id=owner.id,
        limit=10,
    )
    open_ids = {o.receipt_id for o in open_obligations}
    assert obligation.receipt_id in open_ids, "Non-terminal receipts must not close obligations"

    print("OK. Non-terminal receipts do not terminate obligations")
