"""
P2.2 Regression Test: Unbucketed bootstrap

Tests that asyncgate.bootstrap returns a FLAT LIST, never bucketed/grouped.
This is a critical invariant: bootstrap = obligation dump, not inbox.

If this test fails, someone has introduced inbox bucketing logic.
That would be a violation of core design principles.
"""

import pytest

from asyncgate.config import settings
from httpx import AsyncClient
from uuid import UUID, uuid4

from asyncgate.models import Principal, PrincipalKind, ReceiptType
from asyncgate.principals import SYSTEM_PRINCIPAL_ID
from asyncgate.db.repositories import ReceiptRepository
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_obligations_open_returns_flat_list(session: AsyncSession):
    """
    Verify asyncgate.bootstrap returns a flat list of receipts.
    
    Bootstrap MUST be a pure dump with no bucketing, grouping, or inbox logic.
    """
    receipts = ReceiptRepository(session)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    system = Principal(kind=PrincipalKind.SYSTEM, id=SYSTEM_PRINCIPAL_ID)
    
    # Create 20 obligations of different types
    obligation_types = [
        ReceiptType.TASK_ASSIGNED,
        ReceiptType.TASK_ASSIGNED,
        ReceiptType.TASK_ASSIGNED,
        ReceiptType.TASK_ASSIGNED,
        ReceiptType.TASK_ASSIGNED,
    ]
    
    created_ids = []
    for i, receipt_type in enumerate(obligation_types):
        receipt = await receipts.create(
            tenant_id=tenant_id,
            receipt_type=receipt_type,
            from_principal=agent,
            to_principal=system,
            task_id=uuid4(),
            body={"index": i, "type": receipt_type.value},
        )
        created_ids.append(receipt.receipt_id)
    
    await session.commit()
    
    # Query open obligations via repository
    open_oblig, cursor = await receipts.list_open_obligations(
        tenant_id=tenant_id,
        to_kind=PrincipalKind.SYSTEM,
        to_id=SYSTEM_PRINCIPAL_ID,
        limit=100,
    )
    
    # CRITICAL ASSERTION: Result must be a flat list
    assert isinstance(open_oblig, list), "Result must be a list, not dict/nested structure"
    
    # Verify all created obligations are present
    assert len(open_oblig) >= len(obligation_types), \
        f"Expected at least {len(obligation_types)} obligations, got {len(open_oblig)}"
    
    # Verify structure: Each item should be a Receipt object, not a bucket/group
    for obligation in open_oblig:
        # Check it's a receipt object with expected attributes
        assert hasattr(obligation, 'receipt_id'), "Each item must have receipt_id"
        assert hasattr(obligation, 'receipt_type'), "Each item must have receipt_type"
        assert hasattr(obligation, 'from_'), "Each item must have from_ principal"
        assert hasattr(obligation, 'to_'), "Each item must have to_ principal"
        
        # Verify it's NOT a bucket/group structure
        assert not isinstance(obligation, dict) or 'items' not in obligation, \
            "Obligations must NOT be grouped into buckets"
        assert not isinstance(obligation, dict) or 'bucket' not in obligation, \
            "Obligations must NOT have bucket metadata"
    
    print(f"✅ Bootstrap returned flat list of {len(open_oblig)} obligations (no bucketing)")


@pytest.mark.asyncio
async def test_obligations_open_api_returns_flat_json(
    client: AsyncClient,
    session: AsyncSession,
):
    """
    Test the actual API endpoint returns flat JSON array.
    
    Regression guard: Ensures API layer doesn't introduce bucketing.
    """
    receipts = ReceiptRepository(session)
    # The tenant comes from the credential, not from the arguments. In insecure
    # dev that is settings.default_tenant_id, so the fixture data is created
    # there rather than in a tenant the caller names -- naming one is now
    # refused, which is the point.
    tenant_id = UUID(settings.default_tenant_id)
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    system = Principal(kind=PrincipalKind.SYSTEM, id=SYSTEM_PRINCIPAL_ID)
    
    # Create 10 obligations
    for i in range(10):
        await receipts.create(
            tenant_id=tenant_id,
            receipt_type=ReceiptType.TASK_ASSIGNED,
            from_principal=agent,
            to_principal=agent,
            task_id=uuid4(),
            body={"index": i},
        )
    
    await session.commit()
    
    # Call MCP bootstrap (open obligations)
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "asyncgate.bootstrap",
                "arguments": {
                    "agent_id": agent.id,
                },
            },
        },
    )
    
    assert response.status_code == 200
    data = response.json()["result"]
    
    # CRITICAL: Response must include a flat open_obligations list
    assert isinstance(data, dict), \
        f"API must return object, got {type(data).__name__}"
    assert "open_obligations" in data, "Response must include open_obligations"
    
    open_obligations = data["open_obligations"]
    assert isinstance(open_obligations, list), \
        "open_obligations must be a flat list"
    
    # Each item should be a receipt object
    if len(open_obligations) > 0:
        first_item = open_obligations[0]
        assert isinstance(first_item, dict), "Each item should be a receipt object"
        assert "receipt_id" in first_item, "Receipts must have receipt_id"
        assert "receipt_type" in first_item, "Receipts must have receipt_type"
        
        # Verify NO bucketing metadata
        assert "bucket" not in first_item, "Receipts must not have bucket metadata"
        assert "category" not in first_item, "Receipts must not have category metadata"
        assert "priority" not in first_item, "Receipts must not have priority metadata"
    
    print(f"OK. API returned flat JSON array of {len(open_obligations)} obligations")


@pytest.mark.asyncio
async def test_no_attention_heuristics(session: AsyncSession):
    """
    Verify bootstrap does not apply attention heuristics.
    
    All open obligations should be returned, regardless of:
    - Age
    - Priority
    - Type
    - Previous failures
    
    Bootstrap = pure dump, not intelligent inbox.
    """
    receipts = ReceiptRepository(session)
    tenant_id = uuid4()
    agent = Principal(kind=PrincipalKind.AGENT, id="test-agent")
    system = Principal(kind=PrincipalKind.SYSTEM, id=SYSTEM_PRINCIPAL_ID)
    
    # Create obligations with varying "importance" signals
    obligations = [
        {"type": ReceiptType.TASK_ASSIGNED, "body": {"priority": "high"}},
        {"type": ReceiptType.TASK_ASSIGNED, "body": {"priority": "low"}},
        {"type": ReceiptType.TASK_ASSIGNED, "body": {"failed_attempts": 5}},
        {"type": ReceiptType.TASK_ASSIGNED, "body": {"age_days": 30}},
        {"type": ReceiptType.TASK_ASSIGNED, "body": {}},
    ]
    
    created_ids = set()
    for spec in obligations:
        receipt = await receipts.create(
            tenant_id=tenant_id,
            receipt_type=spec["type"],
            from_principal=agent,
            to_principal=system,
            task_id=uuid4(),
            body=spec["body"],
        )
        created_ids.add(receipt.receipt_id)
    
    await session.commit()
    
    # Query open obligations
    open_oblig, _ = await receipts.list_open_obligations(
        tenant_id=tenant_id,
        to_kind=PrincipalKind.SYSTEM,
        to_id=SYSTEM_PRINCIPAL_ID,
        limit=100,
    )
    
    returned_ids = {o.receipt_id for o in open_oblig}
    
    # CRITICAL: ALL obligations must be present (no filtering)
    assert created_ids.issubset(returned_ids), \
        "Bootstrap must return ALL open obligations, not filtered subset"
    
    # Verify no ordering bias (if we had 5, we should get 5)
    matching = created_ids & returned_ids
    assert len(matching) == len(created_ids), \
        f"Expected {len(created_ids)} obligations, got {len(matching)}"
    
    print(f"✅ Bootstrap returned all {len(matching)} obligations without attention filtering")


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v", "-s"])
