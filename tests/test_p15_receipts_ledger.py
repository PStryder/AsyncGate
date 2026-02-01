"""
Receipt ledger endpoint tests.
"""

import pytest


@pytest.mark.asyncio
async def test_receipts_ledger_endpoint_returns_memorygate_shape(client):
    """Receipt ledger returns MemoryGate-style receipt records."""
    create_response = await client.post(
        "/v1/tasks",
        params={"principal_id": "test-agent"},
        json={
            "type": "demo_task",
            "payload": {"note": "hello"},
            "payload_pointer": "depotgate://payload/demo-task",
            "principal_ai": "agent.test",
            "expected_outcome_kind": "response_text",
            "expected_artifact_mime": "text/plain",
        },
    )
    assert create_response.status_code == 200
    task_id = create_response.json()["task_id"]

    ledger_response = await client.get(
        "/v1/receipts/ledger",
        params={"task_id": task_id},
    )
    assert ledger_response.status_code == 200
    data = ledger_response.json()
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
