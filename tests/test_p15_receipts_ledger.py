"""
Receipt ledger endpoint tests.
"""

import pytest
from uuid import uuid4


@pytest.mark.asyncio
async def test_receipts_ledger_endpoint_returns_memorygate_shape(client):
    """Receipt ledger returns MemoryGate-style receipt records."""
    tenant_id = str(uuid4())
    create_response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "asyncgate.create_task",
                "arguments": {
                    "type": "demo_task",
                    "payload": {"note": "hello"},
                    "payload_pointer": "depotgate://payload/demo-task",
                    "principal_ai": "agent.test",
                    "expected_outcome_kind": "response_text",
                    "expected_artifact_mime": "text/plain",
                    "agent_id": "test-agent",
                    "tenant_id": tenant_id,
                },
            },
        },
    )
    assert create_response.status_code == 200
    task_id = create_response.json()["result"]["task_id"]

    ledger_response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "asyncgate.list_receipts_ledger",
                "arguments": {"task_id": task_id, "tenant_id": tenant_id},
            },
        },
    )
    assert ledger_response.status_code == 200
    data = ledger_response.json()["result"]
    assert data["receipts"], "Expected at least one receipt"

    assigned = None
    for receipt in data["receipts"]:
        if receipt.get("metadata", {}).get("receipt_type") == "task.assigned":
            assigned = receipt
            break

    assert assigned is not None, "Expected task.assigned receipt"
    assert assigned["schema_version"] == "1.0"
    assert assigned["task_id"] == task_id
    assert assigned["expected_outcome_kind"] == "response_text"
    assert assigned["expected_artifact_mime"] == "text/plain"
    assert assigned["inputs"]["payload_pointer"] == "depotgate://payload/demo-task"
    assert "metadata" in assigned
