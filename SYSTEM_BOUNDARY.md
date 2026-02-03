# AsyncGate System Boundary

**What is AsyncGate?**  
A single-purpose task substrate that tracks obligations via immutable receipts. It does NOT schedule, retry, or interpret task semantics—it simply tracks what's open, what's closed, and why.

This document defines the **complete deployable system** with no hidden dependencies, scripts, or assumptions.

---

## Deployable Unit

**Single FastAPI Application**
- Entry point: `uvicorn asyncgate.main:app`
- Language: Python 3.11+
- Framework: FastAPI + SQLAlchemy (async)

**Required Infrastructure**
- PostgreSQL 14+ (with JSONB support)
- Optional: Redis (for multi-instance rate limiting)

**No Additional Components**
- No separate scheduler process
- No background workers
- No message queue
- No external state management

---

## Required Environment Variables

### Minimal Configuration (Development)
```bash
ASYNCGATE_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/asyncgate
ASYNCGATE_ALLOW_INSECURE_DEV=true  # Disables auth checks
ASYNCGATE_ENV=development
```

### Production Configuration
```bash
# Database (REQUIRED)
ASYNCGATE_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/asyncgate

# Authentication (REQUIRED in production)
ASYNCGATE_API_KEY=<secure-token-here>
ASYNCGATE_ENV=production

# Rate Limiting (RECOMMENDED)
ASYNCGATE_RATE_LIMIT_ACTIVE=true
ASYNCGATE_RATE_LIMIT_BACKEND=redis  # or "memory" for single-instance
ASYNCGATE_REDIS_URL=redis://host:6379/0

# Optional Tuning
ASYNCGATE_RATE_LIMIT_DEFAULT_CALLS=100
ASYNCGATE_RATE_LIMIT_DEFAULT_WINDOW_SECONDS=60
ASYNCGATE_LEASE_DEFAULT_TTL_SECONDS=300
ASYNCGATE_RECEIPT_RETENTION_DAYS=30
```

### Security Notes
- `ALLOW_INSECURE_DEV=true` ONLY works in `ENV=development`
- Production without `API_KEY` configured will refuse to start
- See `src/asyncgate/api/deps.py:validate_auth_config()` for enforcement

---

## MCP Interface

AsyncGate exposes MCP over HTTP at `/mcp` with JSON-RPC methods `tools/list` and `tools/call`.

### Core Tools

- `asyncgate.create_task` - Submit new task obligations. Returns task ID.
- `asyncgate.get_task` - Retrieve task metadata and current state.
- `asyncgate.bootstrap` - Returns flat list of open obligations (no bucketing).
- `asyncgate.list_receipts` - Query receipt ledger by principal.
- `asyncgate.list_receipts_ledger` - Query receipt ledger with filters (task_id, type).

### System Tools

- `asyncgate.health` - Health check (used by container health checks).

## Core Invariants

### Design Laws (from `.claude/` documentation)

1. **Receipts are immutable contracts, not messages**
   - Once written, never modified
   - Causality tracked via `parent_receipt_ids`

2. **Termination = Type Semantics + DB Evidence**
   - NOT semantic inference or ledger scanning
   - Static rules in `termination.py` + O(1) EXISTS queries
   - AsyncGate stays dumb and fast

3. **Bootstrap = Obligation Dump**
   - No inbox bucketing
   - No attention heuristics
   - Pure query: "what's currently open?"

4. **Different actors may discharge obligations**
   - Principal match NOT required
   - Enables delegation and multi-tenant patterns

5. **Success without locatability keeps obligation open**
   - Work product MUST be locatable (artifacts OR delivery_proof)
   - If missing, system emits `system.anomaly.locatability_missing` receipt

### State Machine Rules

**Task States:** `PENDING → ACTIVE → COMPLETED | FAILED | CANCELLED`
- State tracks **execution progress**, not obligation status
- Lease expiry moves `ACTIVE → PENDING` (lost authority, not failure)

**Receipt Types and Termination:**
- `task.started` - Does NOT terminate
- `task.progress` - Does NOT terminate
- `task.success` - Terminates IFF has artifacts/delivery_proof
- `task.failure` - Terminates
- `task.cancelled` - Terminates
- `system.anomaly.*` - Does NOT terminate (alerts only)

See `src/asyncgate/models/termination.py` for complete truth table.

---

## Accepted Failure Modes

These are **intentional behaviors**, not bugs:

### 1. Lease Expiry ≠ Failure
- Expired lease = lost execution authority
- Does NOT consume retry attempts
- Task returns to `PENDING` for re-lease
- **Why:** Lease expiry could be network partition, not task failure

### 2. Success Without Locatability
- `task.success` receipt submitted without artifacts/delivery_proof
- System strips parent linkage (obligation stays open)
- Emits `system.anomaly.locatability_missing` to principal
- **Why:** Work completed but not retrievable = incomplete contract

### 3. Stale Bootstrap Data
- `asyncgate.bootstrap` may return recently-closed tasks
- Eventual consistency model (bounded by DB replication lag)
- **Why:** O(1) performance > perfect consistency for bootstrap

### 4. No Automatic Retry
- AsyncGate does NOT implement retry logic
- Scheduler (TASKEE) is responsible for retry decisions
- **Why:** Separation of concerns—task substrate vs. task orchestration

### 5. Orphaned Tasks
- Tasks with no active scheduler may remain `PENDING` indefinitely
- No automatic timeout or cleanup
- **Why:** AsyncGate cannot assume semantic intent (task might be waiting for external event)

---

## What AsyncGate Is NOT

**Not a Scheduler**  
AsyncGate does not decide when or how many times to retry tasks.

**Not a Workflow Engine**  
AsyncGate does not understand task dependencies or execution graphs.

**Not an Inference System**  
AsyncGate does not interpret receipt semantics beyond the static truth table.

**Not a Message Queue**  
Receipts are legal contracts in a ledger, not transient messages.

**Not a Retry Manager**  
Retry logic lives in the scheduler (TASKEE), not the substrate.

---

## Performance Characteristics

### Database Access Patterns
- Bootstrap: Single SELECT with WHERE filter (no joins)
- Termination check: O(1) EXISTS query per obligation
- Receipt write: Single INSERT (no cascade updates)

### GIN Index Required
```sql
CREATE INDEX idx_receipts_parents_gin 
ON receipts USING GIN (parent_receipt_ids);
```
Without this index, parent lookups degrade to O(n²). See `C7` in `.claude/SECURITY_AUDIT.md`.

### Rate Limiting
- In-memory backend: Single-instance only
- Redis backend: Multi-instance safe, ~2-5ms per request
- Keys by API key hash (prevents tenant ID spoofing)

---

## Migration Path

**Database Schema**
```bash
# Apply migrations
alembic upgrade head

# Verify
psql $DATABASE_URL -c "SELECT tablename FROM pg_tables WHERE schemaname='public';"
```

Expected tables: `tasks`, `receipts`, `alembic_version`

---

## Verification Commands

**Health Check**
```bash
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"asyncgate.health","arguments":{}}}'
```

**Auth Test (Production)**
```bash
curl -s http://localhost:8080/mcp \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"asyncgate.list_tasks","arguments":{"tenant_id":"TENANT_ID"}}}'
```

**Bootstrap Test**
```bash
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"asyncgate.bootstrap","arguments":{"agent_id":"my-agent","tenant_id":"TENANT_ID"}}}'
```

## Debug / Introspection

**Check Rate Limit Status**
- Response headers: `X-RateLimit-Remaining`, `X-RateLimit-Reset`
- 429 response includes `Retry-After` header

**Check Receipt Chain**
```bash
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"asyncgate.list_receipts_ledger","arguments":{"task_id":"TASK_ID","tenant_id":"TENANT_ID"}}}'
# Returns all receipts for task, ordered by timestamp
```

**Check Open Obligations**
```bash
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"asyncgate.bootstrap","arguments":{"agent_id":"my-agent","tenant_id":"TENANT_ID"}}}'
# If task appears here after success receipt, check for locatability_missing anomaly
```

---

## Quick Start (5 Minutes)

```bash
# 1. Clone and setup
git clone https://github.com/PStryder/asyncgate
cd asyncgate
pip install -e .

# 2. Start PostgreSQL (Docker)
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=dev postgres:14

# 3. Configure
export ASYNCGATE_DATABASE_URL=postgresql+asyncpg://postgres:dev@localhost/postgres
export ASYNCGATE_ALLOW_INSECURE_DEV=true
export ASYNCGATE_ENV=development

# 4. Migrate
alembic upgrade head

# 5. Run
uvicorn asyncgate.main:app --reload

# 6. Test
curl -s http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"asyncgate.health","arguments":{}}}'
curl -s http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"asyncgate.bootstrap","arguments":{"agent_id":"my-agent","tenant_id":"TENANT_ID"}}}'
```

You now understand the complete system boundary.
