> LEGACY NOTE (2026-02-03): AsyncGate is MCP-only. REST endpoint references in this review are historical.

# AsyncGate Code Review Report

**Project:** AsyncGate
**Version:** 0.1.0
**Review Date:** January 8, 2026
**Reviewer:** Code Review Agent

---

## Executive Summary

AsyncGate is a well-architected implementation of a durable, lease-based asynchronous task execution MCP server. The codebase demonstrates strong adherence to the specification with thoughtful implementation of the core task substrate pattern. The project shows evidence of iterative hardening with multiple "P0.x" priority fixes addressing critical concerns around atomicity, security, and performance.

### Strengths
- **Strong spec alignment**: Core data model, state machine, and MCP tool surface match the specification closely
- **Defense in depth**: Multiple security layers including API key validation, startup config validation, rate limiting, and tenant isolation
- **Atomic operations**: Proper use of database savepoints for transactional integrity
- **Comprehensive error handling**: Well-defined exception hierarchy with clear error codes
- **Dual protocol support**: Both MCP and REST APIs exposing equivalent functionality

### Key Concerns
- **Missing `running` status**: State machine lacks the `running` intermediate state from spec
- **MCP server auth gap**: MCP tool handlers bypass authentication verification
- **No migration files**: Alembic versions directory is empty; database schema changes untracked
- **Limited test coverage**: Only 4 test files, focused on specific priority fixes
- **Missing scheduler TASKEE**: Phase C scheduler worker not implemented

---

## 1. Spec Compliance Analysis

### 1.1 Implemented Features

| Feature | Status | Location | Notes |
|---------|--------|----------|-------|
| Task CRUD | Implemented | `engine/core.py` | Full create/get/list/cancel |
| Lease operations | Implemented | `db/repositories.py` | claim_next, renew, release, validate |
| State machine | Partial | `models/task.py:76-90` | Missing `running` state |
| Idempotent task creation | Implemented | `db/repositories.py:96-107` | DB-first approach with IntegrityError handling |
| Capability matching | Implemented | `db/repositories.py:382-389` | Set containment check |
| Exponential backoff | Implemented | `db/repositories.py:215-219` | With max cap |
| Receipt emission | Implemented | `engine/core.py` | All major receipt types |
| Bootstrap | Implemented | `engine/core.py:62-141` | Deprecated in favor of `/obligations/open` |
| Open obligations query | Implemented | `db/repositories.py:997-1112` | Batch termination check with GIN index |
| Lease expiry sweep | Implemented | `tasks/sweep.py` | With jitter and batch processing |
| Multi-tenant isolation | Implemented | `db/tables.py` | tenant_id as composite PK |
| Receipt hash/deduplication | Implemented | `engine/core.py:881-928` | SHA256 with canonical JSON |
| Lease renewal limits | Implemented | `db/repositories.py:453-527` | P1.1: max renewals + lifetime |
| Rate limiting | Implemented | `middleware/rate_limit.py` | In-memory and Redis backends |
| CORS configuration | Implemented | `main.py:74-80`, `config.py:139-154` | Explicit allowlist |
| Instance ID detection | Implemented | `instance.py` | Fly.io, K8s, ECS, Cloud Run |

### 1.2 Missing or Incomplete Features

#### Critical Gaps

1. **`running` Status Not Implemented**
   - **Spec Reference:** Section 3 (Task status enum includes `running`), Section 5 (State Machine: `leased -> running`)
   - **Current State:** `TaskStatus` enum in `models/enums.py:6-13` only has: `QUEUED`, `LEASED`, `SUCCEEDED`, `FAILED`, `CANCELED`
   - **Impact:** Workers cannot signal they've started work; spec allows `leased | running -> succeeded/failed`

2. **Scheduler TASKEE (Phase C)**
   - **Spec Reference:** Section 8 (Scheduler as TASKEE) and Phase C MVP
   - **Current State:** Not implemented - no scheduler service or `schedule.*` task types
   - **Impact:** No scheduled/recurring task support

3. **OAuth 2.0 Authentication**
   - **Spec Reference:** Section 21.2.1
   - **Current State:** Only API key authentication implemented (`api/deps.py:50-90`)
   - **Impact:** No interactive client support, human agent authentication

#### Medium Gaps

4. **Reference Worker (Phase B)**
   - **Spec Reference:** Section 10 (Phase B - Worker reference implementation)
   - **Current State:** Only `workers/command_executor/` exists (appears to be a custom worker)
   - **Impact:** Missing spec-defined `sleep_then_return`, `http_get`, `echo` handlers

5. **REST API - Missing Endpoint `/v1/receipts/{receipt_id}`**
   - **Spec Reference:** Section 18.5 implies single receipt retrieval
   - **Current State:** `get_receipt()` exists in engine but not exposed via REST
   - **Impact:** Agents cannot retrieve specific receipt by ID via REST

6. **Receipt Acknowledgment Chain**
   - **Spec Reference:** Section 17 (asyncgate.ack_receipt creates append-only `receipt.acknowledged`)
   - **Current State:** Implemented but `RECEIPT_ACKNOWLEDGED` type creates receipt without parent validation
   - **Impact:** Ack receipts don't link to the receipt being acknowledged in parents field

7. **Anomaly Receipt Emission**
   - **Spec Reference:** Section H.7 lists 5 anomaly triggers
   - **Current State:** Only locatability anomaly is implemented (`db/repositories.py:727-754`)
   - **Impact:** Missing: max_attempts exceeded, repeated lease expiry, excessive renewals, stale schedule, receipt backlog

### 1.3 Spec Deviations

| Deviation | Spec Says | Implementation Does | Severity |
|-----------|-----------|---------------------|----------|
| Running state | Include in enum | Omitted | Medium |
| Lease grace period | `LEASE_GRACE_SECONDS` configurable | Hardcoded to 0 | Low |
| `TASK_CANCELED` receipt type | Implied for cancellation | Uses `TASK_RESULT_READY` instead | Low |
| Receipt `signature` field | Optional field mentioned | Not implemented | Low |
| Worker bootstrap routing | Returns `receipt_routing` in integrated mode | Not implemented | Low |

---

## 2. Code Quality Assessment

### 2.1 Architecture

**Rating: Excellent**

The codebase follows a clean layered architecture:

```
API Layer (router.py, schemas.py)
       |
       v
Engine Layer (core.py, errors.py)
       |
       v
Repository Layer (repositories.py)
       |
       v
Database Layer (tables.py, base.py)
```

Positive patterns observed:
- Single responsibility per module
- Dependency injection via constructor (repositories)
- Clear separation between domain models and ORM models
- Async-first design throughout

### 2.2 Code Organization

```
src/asyncgate/
  api/            # REST endpoints and schemas
  db/             # Database access layer
  engine/         # Core business logic
  integrations/   # External service clients
  middleware/     # Request middleware
  mcp/            # MCP protocol implementation
  models/         # Domain models and enums
  tasks/          # Background tasks
```

**Observations:**
- Module naming is clear and consistent
- `__init__.py` files properly export public interfaces
- Configuration centralized in `config.py`

### 2.3 Code Style

**Rating: Good**

- Consistent use of type hints throughout
- Docstrings present on public functions with clear descriptions
- Line length appears consistent (~100 chars, matches `pyproject.toml` config)
- Uses modern Python features (match statements could be used more)

**Minor Issues:**
- Some long functions could be split (e.g., `list_open_obligations` at 116 lines)
- Mixed use of string formatting (`f""` vs `.format()`)

### 2.4 Naming Conventions

**Rating: Good**

- Clear, descriptive function names (`claim_next`, `requeue_with_backoff`)
- Model names match spec terminology (`Task`, `Lease`, `Receipt`, `Principal`)
- Consistent use of `_row_to_model` pattern in repositories

### 2.5 Pydantic Model Usage

**Rating: Excellent**

- Proper separation between API schemas (`api/schemas.py`) and domain models (`models/`)
- Consistent use of `Field()` with descriptions
- Good use of validation constraints (`ge=1`, `le=200`)

---

## 3. Security Review

### 3.1 Authentication

| Control | Status | Location | Notes |
|---------|--------|----------|-------|
| API Key validation | Implemented | `api/deps.py:50-90` | Timing-safe comparison via `secrets.compare_digest` |
| Startup auth validation | Implemented | `api/deps.py:93-133` | Fails fast on misconfiguration |
| Insecure dev mode | Implemented | `config.py:136` | Explicit opt-in, loud warnings |
| MCP auth | **MISSING** | `mcp/server.py` | Tool handlers don't verify authentication |

#### Critical: MCP Server Authentication Gap

**File:** `src/asyncgate/mcp/server.py:230-241`

```python
@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls."""
    try:
        result = await _handle_tool(name, arguments)  # No auth check!
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
```

**Issue:** MCP tool calls bypass authentication entirely. Any client connected via MCP can execute operations without verification.

**Recommendation:** Add authentication middleware to MCP server or validate credentials in `_handle_tool()`.

### 3.2 Authorization

| Control | Status | Location |
|---------|--------|----------|
| Task cancellation auth | Implemented | `engine/core.py:256-263` |
| Lease ownership validation | Implemented | `db/repositories.py:433-451` |
| Tenant isolation | Implemented | All queries filter by `tenant_id` |

**Observation:** Authorization is correctly implemented at the engine layer. Only task owners can cancel tasks. Lease operations require matching `worker_id`.

### 3.3 Input Validation

| Control | Status | Location |
|---------|--------|----------|
| UUID validation | Implemented | FastAPI path parameters |
| Request body validation | Implemented | Pydantic schemas |
| Receipt size limits | Implemented | `db/repositories.py:619-648` |
| Pagination limits | Implemented | `config.py:123-124` |

**Receipt Size Limits (T5.1):**
- Body: 64KB max
- Parents: 10 max
- Artifacts: 100 max

### 3.4 SQL Injection

**Rating: Safe**

All database queries use SQLAlchemy ORM with parameterized queries. No raw SQL or string concatenation observed.

### 3.5 Rate Limiting

**Rating: Good**

- Enabled by default (`config.py:80`)
- Forced on in production/staging (`config.py:91-96`)
- Sliding window implementation in memory and Redis
- Per-client keying with fallback to IP

**Potential Improvement:** Rate limit key uses API key hash when auth enabled, but tenant_id when in insecure mode. Attackers could spoof tenant_id headers.

### 3.6 CORS

**Rating: Good**

- Explicit origin allowlist (no wildcards with credentials)
- Configurable methods and headers
- Default includes only localhost origins

### 3.7 Security Recommendations

1. **HIGH: Add MCP authentication** - MCP server currently accepts unauthenticated tool calls
2. **MEDIUM: Add RLS policies** - Spec mentions PostgreSQL Row-Level Security but not implemented
3. **MEDIUM: Implement token rotation** - API keys currently have no expiry/rotation mechanism
4. **LOW: Add request logging** - Security-relevant events not logged with audit trail

---

## 4. Error Handling Review

### 4.1 Exception Hierarchy

**File:** `src/asyncgate/engine/errors.py`

```python
AsyncGateError (base)
  |- TaskNotFound
  |- InvalidStateTransition
  |- LeaseInvalidOrExpired
  |- UnauthorizedError
  |- QuotaExceededError
  |- RateLimitExceededError
  |- LeaseRenewalLimitExceeded
  |- LeaseLifetimeExceeded
```

**Rating: Excellent**

- All errors include `code` for programmatic handling
- Error messages are descriptive and include relevant context
- Errors are properly caught and translated to HTTP status codes in router

### 4.2 HTTP Error Mapping

| Exception | HTTP Status | Location |
|-----------|-------------|----------|
| TaskNotFound | 404 | `router.py:258-259` |
| UnauthorizedError | 403 | `router.py:312-313` |
| LeaseInvalidOrExpired | 400 | `router.py:412-413` |
| InvalidStateTransition | 400 | `router.py:459-460` |
| RateLimitExceeded | 429 | `middleware/rate_limit.py:267-276` |

### 4.3 Error Handling Gaps

1. **Database errors not specifically handled** - Generic `Exception` catches may hide DB connectivity issues
2. **MCP errors returned as JSON** - `mcp/server.py:239-240` wraps errors but loses exception type
3. **No circuit breaker on DB** - Long-running transactions during DB issues could exhaust connection pool

---

## 5. Testing Review

### 5.1 Test Coverage

| Test File | Focus Area | Tests |
|-----------|------------|-------|
| `test_p01_batch_termination.py` | Performance: batch queries | 3 |
| `test_p02_atomic_transactions.py` | Atomicity: rollback on failure | 7 |
| `test_p03_p04_security_config.py` | Security: CORS, rate limiting | 9 |
| `test_p05_hash_parents.py` | Receipt hash includes parents | Unknown |
| `test_p11_lease_renewal_limits.py` | Lease hoarding prevention | Unknown |
| `test_p12_timezone_aware*.py` | Timezone handling | Unknown |
| `test_p2_2_unbucketed_bootstrap.py` | Bootstrap obligations model | Unknown |

**Rating: Insufficient**

**Critical Gaps:**
- No unit tests for core engine operations (`create_task`, `complete`, `fail`)
- No tests for MCP tool handlers
- No integration tests for REST endpoints
- No tests for state machine transitions
- No tests for idempotency (same `idempotency_key` returns same task)
- No tests for capability matching
- No tests for lease expiry sweep

### 5.2 Test Quality Observations

Tests that exist are well-written:
- Use `pytest-asyncio` correctly
- Proper fixture usage
- Clear assertions with descriptive messages
- Good edge case coverage for their specific concerns

### 5.3 Testing Recommendations

1. **Add core engine tests** - Cover all TASKER and TASKEE operations
2. **Add state machine tests** - Verify all valid/invalid transitions
3. **Add idempotency tests** - Ensure duplicate tasks are prevented
4. **Add integration tests** - End-to-end REST/MCP flows
5. **Add property-based tests** - Use Hypothesis for capability matching

---

## 6. Documentation Review

### 6.1 Code Documentation

| Element | Status |
|---------|--------|
| Module docstrings | Present |
| Function docstrings | Mostly present |
| Inline comments | Sparse but targeted |
| Type hints | Comprehensive |

### 6.2 API Documentation

- FastAPI auto-generates OpenAPI spec
- Pydantic schemas include field descriptions
- No manual API documentation found

### 6.3 Missing Documentation

1. **README.md** - Not found in project root
2. **CONTRIBUTING.md** - No contribution guidelines
3. **Architecture diagram** - Would help new developers
4. **Deployment guide** - Only `fly.toml` exists

---

## 7. Issues Found

### 7.1 Critical Issues

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| C1 | MCP server lacks authentication | `mcp/server.py` | Unauthenticated access to all operations |
| C2 | Missing database migrations | `alembic/versions/` (empty) | Schema changes untracked |
| C3 | `running` status not in enum | `models/enums.py` | State machine incomplete per spec |

### 7.2 High Severity Issues

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| H1 | Minimal test coverage | `tests/` | Regressions likely undetected |
| H2 | No anomaly emission for spec triggers | `engine/core.py` | 4 of 5 anomaly conditions unhandled |
| H3 | Receipt ack missing parent linkage | `engine/core.py:305-327` | Orphaned ack receipts |
| H4 | Scheduler TASKEE not implemented | N/A | No scheduled task support |

### 7.3 Medium Severity Issues

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| M1 | `get_receipt` not exposed via REST | `api/router.py` | Agents can't fetch single receipt |
| M2 | No connection pool monitoring | `db/base.py` | Pool exhaustion undetected |
| M3 | Lease model missing fields | `models/lease.py:27-37` | `LeaseInfo` doesn't include all spec fields |
| M4 | No request logging middleware | `main.py` | No audit trail for API calls |
| M5 | MCP lacks tool for open obligations | `mcp/server.py` | Only REST has `/obligations/open` |

### 7.4 Low Severity Issues

| ID | Issue | Location | Impact |
|----|-------|----------|--------|
| L1 | `TASK_CANCELED` receipt type unused | `models/enums.py` | Defined but not emitted |
| L2 | Uptime always 0 | `engine/core.py:120` | Bootstrap returns incorrect uptime |
| L3 | Receipt `signature` field missing | `models/receipt.py` | Spec mentions optional signature |
| L4 | No pagination in MCP list operations | `mcp/server.py` | Large result sets not handled |
| L5 | `created_by_id` filter unchecked | `db/repositories.py:137` | Could filter by non-owner ID |

---

## 8. Performance Observations

### 8.1 Database Indexes

**File:** `src/asyncgate/db/tables.py`

Indexes are well-designed:
- `idx_tasks_leasable`: Composite index for lease_next queries
- `idx_receipts_parents_gin`: GIN index for JSONB array containment
- `idx_leases_expires`: For expiry sweep efficiency

### 8.2 Query Optimization

**Positive:**
- `SELECT ... FOR UPDATE SKIP LOCKED` for atomic claiming
- Batch termination check reduces N+1 to 2 queries
- Pagination with cursors instead of offset

**Concerns:**
- `list_open_obligations` fetches up to 1000 candidates
- `get_expired` loads all expired leases into memory

### 8.3 Connection Pool

```python
pool_size=20,
max_overflow=10,
```

30 max connections - may be insufficient for high-concurrency scenarios.

---

## 9. Recommendations

### Priority 1 (Before Production)

1. **Add MCP authentication** - Implement credential verification in tool handlers
2. **Create database migrations** - Generate Alembic migration scripts for all tables
3. **Add core test coverage** - Minimum 80% coverage on engine operations
4. **Implement missing anomaly triggers** - Per spec section H.7

### Priority 2 (Production Hardening)

1. **Add `running` status** - Complete state machine per spec
2. **Expose `get_receipt` via REST** - `/v1/receipts/{receipt_id}`
3. **Add request logging** - Security audit trail
4. **Add PostgreSQL RLS** - Defense in depth for tenant isolation
5. **Add MCP tool for open obligations** - `asyncgate.list_open_obligations`

### Priority 3 (Operational Excellence)

1. **Add README** - Quick start, configuration, deployment
2. **Implement scheduler TASKEE** - Phase C of spec
3. **Add reference worker** - Phase B of spec
4. **Connection pool monitoring** - Metrics for pool utilization
5. **Add health checks** - Database connectivity, lease sweep status

---

## 10. Conclusion

AsyncGate demonstrates solid engineering with a clear understanding of the spec's design principles. The core task substrate functionality is well-implemented with proper attention to atomicity, tenant isolation, and error handling.

The main areas requiring attention before production are:
1. **MCP authentication gap** - Critical security issue
2. **Test coverage** - Insufficient to catch regressions
3. **Migration tracking** - Essential for schema evolution
4. **Missing state machine state** - `running` status needed for spec compliance

With these issues addressed, AsyncGate would be production-ready for its Phase A MVP scope.

---

**End of Code Review Report**
