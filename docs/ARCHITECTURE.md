# AsyncGate Architecture

## Overview

AsyncGate is a task orchestration service implementing the **obligation ledger model**. The system enables long-running async tasks across ephemeral AI agent sessions by maintaining durable state in PostgreSQL and using receipts as immutable contracts.

## Core Principles

### 1. Obligation Ledger (Not Attention Inbox)

**OLD MODEL (WRONG):**
- Bootstrap = Attention system with semantic bucketing
- `delivered_at` controls visibility
- Task state is source of truth for obligations

**NEW MODEL (CORRECT):**
- Bootstrap = Open obligations dump from ledger
- Receipt chains define termination (via parents → terminal receipts)
- Task state = execution tracking ONLY

### 2. Receipts as Contracts

Receipts are **immutable evidence** of state transitions, not messages:
- Created once, never modified
- Reference obligations via `parents` array
- Terminate obligations through type semantics + DB evidence
- Must be locatable (artifacts OR delivery_proof required for success)

### 3. Termination via Type Semantics + DB Evidence

Obligations persist until explicitly terminated by receipt chain evidence:

```python
# Static: What types CAN terminate what (type semantics)
TERMINATION_RULES: dict[ReceiptType, set[ReceiptType]] = {
    ReceiptType.TASK_ASSIGNED: {
        ReceiptType.TASK_COMPLETED,
        ReceiptType.TASK_FAILED,
        ReceiptType.TASK_CANCELED,
    },
    # ...
}

# Runtime: Did termination happen? (DB evidence)
has_terminator = await receipts.has_terminator(tenant_id, parent_receipt_id)
```

**AsyncGate stays dumb** - no semantic inference, no ledger scanning.

## Three-Layer Architecture

```
┌─────────────────────────────────────────────┐
│ Layer 2: Workers                            │
│ - Execute tasks                             │
│ - Report results via receipts               │
└─────────────────────────────────────────────┘
                    ↕ (lease protocol)
┌─────────────────────────────────────────────┐
│ Layer 1: AsyncGate (Execution Engine)       │
│ - Task state machine                        │
│ - Lease coordination                        │
│ - Receipt ledger                            │
└─────────────────────────────────────────────┘
                    ↕ (obligations API)
┌─────────────────────────────────────────────┐
│ Layer 0: Principals (Agents/Services)       │
│ - Query open obligations                    │
│ - Queue tasks                               │
│ - Track work via receipt chains            │
└─────────────────────────────────────────────┘
```

## State Separation

### Task State (Execution Tracking)

Located in `tasks` table, tracks execution lifecycle:

```
QUEUED → LEASED → SUCCEEDED
              ↓
           FAILED → QUEUED (with backoff)
              ↓
           CANCELED
```

**Used for:**
- Worker lease coordination
- Retry logic and backoff
- Execution instance tracking

**NOT used for:**
- Obligation truth (that's receipts)
- Bootstrap queries (that's the ledger)

### Receipt State (Obligation Ledger)

Located in `receipts` table, immutable contracts:

```
task.assigned (creates obligation)
      ↓ (parent relationship)
task.completed (terminates obligation)
```

**Used for:**
- Obligation tracking
- Work provenance
- Termination evidence

**NOT used for:**
- Execution state (that's tasks)

## Critical Semantic Splits

### 1. Lease Expiry vs Task Failure

```python
# Lease expiry = "lost authority" (worker crash, network issue)
# Does NOT increment attempt counter
await tasks.requeue_on_expiry(tenant_id, task_id, jitter_seconds=3.0)

# Task failure = "task actually failed" (error in execution)
# DOES increment attempt counter, uses exponential backoff
await tasks.requeue_with_backoff(tenant_id, task_id, increment_attempt=True)
```

**Why critical:** Worker crashes shouldn't eat retry attempts.

### 2. Locatability Requirement

Success receipts (`task.completed`) MUST include locatability:

```python
# Valid: Store pointers
artifacts = [
    {"type": "s3", "url": "s3://bucket/key"},
    {"type": "db", "row_id": 12345}
]

# Valid: Push delivery confirmation
delivery_proof = {
    "mode": "push",
    "target": {"endpoint": "https://..."},
    "status": "succeeded",
    "at": "2026-01-05T12:00:00Z",
    "proof": {"request_id": "req_abc123"}
}
```

**Without locatability:** Parents are stripped, obligation stays open.

## MCP Interface

AsyncGate exposes MCP over HTTP at `/mcp` with JSON-RPC methods `tools/list` and `tools/call`.

### Primary: `asyncgate.bootstrap`

Returns an unbucketed obligation dump in `open_obligations`:

```json
{
  "server": {"name": "AsyncGate"},
  "relationship": {...},
  "open_obligations": [
    {
      "receipt_id": "...",
      "receipt_type": "task.assigned",
      "created_at": "...",
      "body": {...}
    }
  ],
  "cursor": "..."
}
```

**No bucketing, no attention semantics, no task state interpretation.**

### Terminator checks: `asyncgate.check_terminator`

Use `asyncgate.check_terminator` to verify if a parent receipt has terminating evidence.

## Receipt Chain Patterns

### Correct: Parent Linkage

```python
# Agent queues task → AsyncGate creates obligation
assign_receipt = await receipts.create(
    receipt_type=ReceiptType.TASK_ASSIGNED,
    to_principal=agent,
    task_id=task.task_id,
)

# Worker completes task → terminates obligation
complete_receipt = await receipts.create(
    receipt_type=ReceiptType.TASK_COMPLETED,
    to_principal=agent,
    task_id=task.task_id,
    parents=[assign_receipt.receipt_id],  # ← Links to obligation
    body={"artifacts": [...]},  # ← Locatable
)
```

### Incorrect: No Parent Linkage

```python
# Terminal receipt without parents → eternal obligation
complete_receipt = await receipts.create(
    receipt_type=ReceiptType.TASK_COMPLETED,
    to_principal=agent,
    task_id=task.task_id,
    # parents=[...],  ← MISSING! Obligation never discharged
)
# Result: Haunted bootstrap - obligation persists forever
```

## Agent Patterns

### Checking Obligation Status

```python
# Query open obligations
payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "asyncgate.bootstrap",
        "arguments": {
            "agent_id": "my-agent",
            "tenant_id": "TENANT_ID",
        },
    },
}
response = await client.post("/mcp", json=payload)

obligations = response["result"]["open_obligations"]

# For specific obligation, check if terminated
check_payload = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
        "name": "asyncgate.check_terminator",
        "arguments": {
            "parent_receipt_id": obligation_id,
            "tenant_id": "TENANT_ID",
        },
    },
}
check_resp = await client.post("/mcp", json=check_payload)
has_terminator = check_resp["result"]["has_terminator"]

if has_terminator:
    # Obligation discharged - work is done or failed
    ...
```

### Detecting "Work Done But No Receipt"

If task state is terminal but obligation is still open:

```python
task = await get_task(task_id)
if task.status == "SUCCEEDED":
    # Check if obligation discharged
    obligation_still_open = await check_obligation(assign_receipt_id)
    if obligation_still_open:
        # SUCCESS WITHOUT LOCATABILITY
        # Producer didn't send proper completion receipt
        # Obligation stays open until fixed
        log_anomaly("work_done_but_unlinkable", task_id)
```

## Migration Guide

### From Old Bootstrap to MCP Bootstrap

**Before (legacy REST):**
```python
response = await client.get("/v1/bootstrap", ...)
waiting = response["attention"]["waiting_results"]
assigned = response["attention"]["assigned_tasks"]
```

**After (MCP):**
```python
payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "asyncgate.bootstrap",
        "arguments": {
            "agent_id": "my-agent",
            "tenant_id": "TENANT_ID",
        },
    },
}
response = await client.post("/mcp", json=payload)
obligations = response["result"]["open_obligations"]

# Filter by type if needed
assigned = [o for o in obligations if o["receipt_type"] == "task.assigned"]
```

### Receipt Body Updates

**Before:**
```python
body = {
    "result_summary": "Done",
    "result_payload": {...},
}
```

**After:**
```python
body = {
    "result_summary": "Done",
    "result_payload": {...},
    "artifacts": [  # ← Add locatability
        {"type": "s3", "url": "s3://..."}
    ],
}
```

## Invariants

1. **Terminal receipts must have parents** (enforced by validation)
2. **Success receipts must be locatable** (artifacts OR delivery_proof)
3. **Termination is type semantics + DB evidence** (not semantic inference)
4. **Tasks for execution, receipts for obligations** (never mix)
5. **delivered_at is telemetry only** (never used for control logic)
6. **Lease expiry ≠ task failure** (separate requeue paths)

## Performance Characteristics

- **Obligation queries:** O(1) per obligation via EXISTS checks
- **Receipt chain traversal:** O(depth) via parent array queries
- **Bootstrap:** ~50% faster after task-state removal
- **Lease expiry:** Batched with jitter (anti-storm)

## Size Limits

Prevent ledger bloat and abuse:

- Receipt body: **64KB max**
- Parents array: **10 max**
- Artifacts array: **100 max**

Error message: "Receipt bodies are contracts, not chat messages."

## Next Steps

For implementation details:
- See `RECEIPT_PATTERNS.md` for concrete examples
- See `docs/` for API specs and MCP integration
- See `.claude/REALIGNMENT_PROGRESS.md` for implementation status
