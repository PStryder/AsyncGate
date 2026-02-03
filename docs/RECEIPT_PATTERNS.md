# Receipt Patterns Guide

## Overview

Receipts are immutable contracts in the AsyncGate ledger. This guide shows correct patterns for creating, linking, and using receipts to track obligations.

## Core Concepts

### Receipts as Contracts

```python
# Receipts are evidence, not messages
receipt = {
    "receipt_id": "uuid",
    "receipt_type": "task.assigned",
    "from": {"kind": "system", "id": "asyncgate"},
    "to": {"kind": "agent", "id": "my-agent"},
    "task_id": "uuid",
    "parents": [],  # Empty for obligation-creating receipts
    "body": {...},  # Contract payload
    "created_at": "timestamp",
}
```

**Immutable:** Created once, never modified
**Locatable:** Success receipts must include artifacts or delivery_proof
**Linked:** Terminal receipts must reference parents

## Pattern 1: Task Assignment (Creates Obligation)

Agent queues task → AsyncGate creates obligation receipt:

```python
# Agent calls: asyncgate.create_task (MCP)
payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "asyncgate.create_task",
        "arguments": {
            "type": "data_analysis",
            "payload": {"dataset_url": "s3://..."},
            "principal_ai": "my-agent",
            "agent_id": "my-agent",
            "tenant_id": "TENANT_ID",
        },
    },
}
response = await client.post("/mcp", json=payload)

# AsyncGate emits: task.assigned receipt
# This creates an OBLIGATION for the agent
{
    "receipt_type": "task.assigned",
    "from": {"kind": "system", "id": "asyncgate"},
    "to": {"kind": "agent", "id": "my-agent"},
    "task_id": "uuid",
    "parents": [],  # ← Creates obligation (no parent)
    "body": {
        "task_type": "data_analysis",
        "assigned_at": "2026-01-05T12:00:00Z"
    }
}
```

## Pattern 2: Task Completion (Terminates Obligation)

Worker completes task → sends success receipt with locatability:

```python
# Worker calls: asyncgate.complete (MCP)
payload = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
        "name": "asyncgate.complete",
        "arguments": {
            "worker_id": "worker-1",
            "task_id": "TASK_ID",
            "lease_id": "LEASE_ID",
            "result": {
                "summary": "Analysis complete",
                "payload": {"row_count": 10000}
            },
            "artifacts": [  # ??? LOCATABILITY REQUIRED
                {
                    "type": "s3",
                    "bucket": "results",
                    "key": "analysis/output.csv",
                    "url": "s3://results/analysis/output.csv"
                }
            ],
            "tenant_id": "TENANT_ID",
        },
    },
}
await client.post("/mcp", json=payload)

# AsyncGate emits: task.completed receipt
{
    "receipt_type": "task.completed",
    "from": {"kind": "worker", "id": "worker-1"},
    "to": {"kind": "agent", "id": "my-agent"},
    "task_id": "uuid",
    "parents": ["assign_receipt_id"],  # ← Links to obligation
    "body": {
        "result_summary": "Analysis complete",
        "result_payload": {"row_count": 10000},
        "artifacts": [  # ← Work is locatable
            {"type": "s3", "url": "s3://..."}
        ],
        "completion_metadata": {
            "worker_instance": "worker-1-abc123",
            "duration_seconds": 45.2
        }
    }
}
```

**CRITICAL:** Without parent linkage, obligation is NOT terminated.

## Pattern 3: Locatability (Store vs Push)

### Store Pointers (Artifacts)

Work stored externally, receipt points to it:

```python
{
    "receipt_type": "task.completed",
    "parents": ["assign_receipt_id"],
    "body": {
        "result_summary": "Report generated",
        "artifacts": [
            # S3 storage
            {"type": "s3", "url": "s3://bucket/report.pdf"},
            
            # Database row
            {"type": "db", "table": "reports", "row_id": 12345},
            
            # Google Drive
            {"type": "gdrive", "file_id": "abc123xyz"},
        ]
    }
}
```

### Push Delivery (delivery_proof)

Work pushed to endpoint, receipt confirms delivery:

```python
{
    "receipt_type": "task.completed",
    "parents": ["assign_receipt_id"],
    "body": {
        "result_summary": "Data pushed to webhook",
        "delivery_proof": {
            "mode": "push",
            "target": {
                "endpoint": "https://example.com/webhook",
                "method": "POST"
            },
            "status": "succeeded",
            "at": "2026-01-05T12:30:00Z",
            "proof": {
                "request_id": "req_abc123",
                "http_status": 200,
                "response_headers": {
                    "x-correlation-id": "xyz789"
                }
            }
        }
    }
}
```

### Combined (Both Modes)

```python
{
    "receipt_type": "task.completed",
    "parents": ["assign_receipt_id"],
    "body": {
        "result_summary": "Processed and delivered",
        "artifacts": [  # Stored for audit
            {"type": "s3", "url": "s3://archive/data.json"}
        ],
        "delivery_proof": {  # Also pushed to client
            "mode": "push",
            "target": {"endpoint": "https://..."},
            "status": "succeeded",
            "at": "..."
        }
    }
}
```

## Pattern 4: Failure with Retry

Worker fails task → obligation stays open, task requeued:

```python
# Worker calls: asyncgate.fail (MCP)
payload = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
        "name": "asyncgate.fail",
        "arguments": {
            "worker_id": "worker-1",
            "task_id": "TASK_ID",
            "lease_id": "LEASE_ID",
            "error": {
                "type": "DataValidationError",
                "message": "Invalid CSV format"
            },
            "retryable": True,  # ??? Task will be requeued
            "tenant_id": "TENANT_ID",
        },
    },
}
await client.post("/mcp", json=payload)



# AsyncGate does NOT emit task.failed receipt yet
# Task goes back to QUEUED with backoff
# Obligation STAYS OPEN until success or terminal failure
```

## Pattern 5: Terminal Failure

Worker fails task (max attempts reached) → obligation discharged:

```python
# After max_attempts exhausted, AsyncGate emits:
{
    "receipt_type": "task.failed",
    "from": {"kind": "system", "id": "asyncgate"},
    "to": {"kind": "agent", "id": "my-agent"},
    "task_id": "uuid",
    "parents": ["assign_receipt_id"],  # ← Terminates obligation
    "body": {
        "error_type": "DataValidationError",
        "error_message": "Invalid CSV format",
        "final_attempt": 5,
        "max_attempts": 5,
        "terminal_reason": "max_attempts_exceeded"
    }
}
```

## Pattern 6: Cancellation

Agent cancels task → obligation discharged:

```python
# Agent calls: asyncgate.cancel_task (MCP)
payload = {
    "jsonrpc": "2.0",
    "id": 4,
    "method": "tools/call",
    "params": {
        "name": "asyncgate.cancel_task",
        "arguments": {
            "task_id": "TASK_ID",
            "agent_id": "my-agent",
            "reason": "Requirements changed",
            "tenant_id": "TENANT_ID",
        },
    },
}
await client.post("/mcp", json=payload)

# AsyncGate emits: task.canceled receipt
{
    "receipt_type": "task.canceled",
    "from": {"kind": "agent", "id": "my-agent"},
    "to": {"kind": "agent", "id": "my-agent"},
    "task_id": "uuid",
    "parents": ["assign_receipt_id"],  # ← Terminates obligation
    "body": {
        "canceled_by": {"kind": "agent", "id": "my-agent"},
        "reason": "Requirements changed",
        "canceled_at": "2026-01-05T13:00:00Z"
    }
}
```

## Pattern 7: Lease Expiry (Obligation Stays Open)

Worker lease expires → task requeued, obligation persists:

```python
# Background sweep detects expired lease
# AsyncGate emits: lease.expired receipt (informational)
{
    "receipt_type": "lease.expired",
    "from": {"kind": "system", "id": "asyncgate"},
    "to": {"kind": "agent", "id": "my-agent"},
    "task_id": "uuid",
    "lease_id": "uuid",
    "parents": [],  # ← Does NOT terminate obligation
    "body": {
        "task_id": "uuid",
        "previous_worker_id": "worker-1",
        "attempt": 2,  # ← NOT incremented
        "requeued": True
    }
}

# Obligation STAYS OPEN - worker crash doesn't discharge work
```

## Anti-Patterns

### ❌ Terminal Receipt Without Parents

```python
# WRONG: Success without parent linkage
{
    "receipt_type": "task.completed",
    "parents": [],  # ← MISSING! Obligation never discharged
    "body": {"result_summary": "Done"}
}

# Result: Haunted bootstrap - obligation persists forever
# Validation: Raises ValueError
```

### ❌ Success Without Locatability

```python
# WRONG: Success without artifacts or delivery_proof
{
    "receipt_type": "task.completed",
    "parents": ["assign_receipt_id"],
    "body": {
        "result_summary": "Trust me bro, it worked"
        # No artifacts, no delivery_proof
    }
}

# Result: Parents stripped, obligation stays open
# Phase 1 (lenient): Allowed but logged
# Phase 2 (strict): Raises ValueError
```

### ❌ Receipts as Chat Messages

```python
# WRONG: Large body (>64KB)
{
    "receipt_type": "task.completed",
    "body": {
        "result_summary": "Done",
        "result_payload": {
            # 10MB of data inline
            "data": [...]
        }
    }
}

# Result: ValueError - "Receipt bodies are contracts, not chat messages"
# Fix: Store externally, use artifacts
```

### ❌ Mega-Chains

```python
# WRONG: Too many parents
{
    "receipt_type": "task.completed",
    "parents": [
        "receipt_1", "receipt_2", ..., "receipt_50"  # ← 50 parents!
    ]
}

# Result: ValueError - max 10 parents
# Fix: Flatten structure, use single parent for simple workflows
```

## Correct Workflow Examples

### Example 1: Simple Task Completion

```python
# 1. Agent queues task
task = await create_task(type="report", payload={...})

# 2. AsyncGate emits task.assigned (creates obligation)
assign_receipt = {
    "receipt_id": "r1",
    "receipt_type": "task.assigned",
    "to": agent,
    "parents": []
}

# 3. Worker claims lease
lease = await claim_lease()

# 4. Worker completes work
await complete_task(
    lease_id=lease.lease_id,
    result={...},
    artifacts=[{"type": "s3", "url": "..."}]  # ← Locatable
)

# 5. AsyncGate emits task.completed (terminates obligation)
complete_receipt = {
    "receipt_id": "r2",
    "receipt_type": "task.completed",
    "to": agent,
    "parents": ["r1"],  # ← Links to obligation
    "body": {
        "artifacts": [...]  # ← Locatable
    }
}

# 6. Agent queries open obligations → empty (work done)
obligations = await get_open_obligations()  # []
```

### Example 2: Retryable Failure

```python
# 1-3. Same as above (task created, assigned, leased)

# 4. Worker fails (retryable)
await fail_task(
    lease_id=lease.lease_id,
    error={"type": "NetworkError"},
    retryable=True
)

# 5. AsyncGate requeues task (no receipt yet)
# Obligation STAYS OPEN

# 6. Worker 2 claims lease
lease2 = await claim_lease()

# 7. Worker 2 succeeds
await complete_task(lease_id=lease2.lease_id, ...)

# 8. AsyncGate emits task.completed (finally terminates)
complete_receipt = {
    "parents": ["assign_receipt_id"],  # ← Original obligation
    "body": {"artifacts": [...]}
}
```

### Example 3: Detecting Work Without Receipt

```python
# Agent perspective
obligations = await get_open_obligations()

for obligation in obligations:
    if obligation["receipt_type"] == "task.assigned":
        task_id = obligation["task_id"]
        task = await get_task(task_id)
        
        if task.status == "SUCCEEDED":
            # Task succeeded but obligation still open
            # SUCCESS WITHOUT LOCATABILITY detected
            log_anomaly(
                "work_done_but_unlinkable",
                task_id=task_id,
                assign_receipt_id=obligation["receipt_id"]
            )
```

## Size Limits

Prevent ledger bloat:

- **Body:** 64KB max
- **Parents:** 10 max
- **Artifacts:** 100 max

Error message: "Receipt bodies are contracts, not chat messages."

## Best Practices

1. **Always link terminal receipts** - use `parents` array
2. **Always provide locatability** - use `artifacts` OR `delivery_proof`
3. **Keep bodies small** - store large payloads externally
4. **Use flat structures** - avoid deep parent chains
5. **One parent for simple workflows** - most tasks need only one parent
6. **Store then point** - don't inline large data
7. **Validate early** - check for parent existence before terminal receipt

## See Also

- `ARCHITECTURE.md` - System design and principles
- `docs/api/` - API specifications
- `.claude/REALIGNMENT_PROGRESS.md` - Implementation status
