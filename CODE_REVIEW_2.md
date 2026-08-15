<!-- Generated 2026-08-15. Stack-level context: ../LV_STACK_REVIEW.md -->

> **Review 2 — AsyncGate**
> Part of a full-stack review of LV_Stack (11 repos, ~97k LOC) conducted 2026-08-15.
> Stack-wide findings that affect this repo but are not fixable inside it are in
> `../LV_STACK_REVIEW.md` and `../_CROSS_REPO_ANALYSIS.md`. Read the stack report first —
> several findings below have a shared root cause.

---

# AsyncGate — Code Review

Reviewed: `/home/claude/lv/AsyncGate/` @ baseline freeze (2026-02-23 tree), ~12.1k LOC across `src/`, `workers/`, `tests/`, `alembic/`, `migrations/`.
Normative sources used: `LegiVellum/docs/canonical/receipt.rules.md`, `receipt.schema.v1.json`, `asyncgate.lease.md`, `worker.contract.md`, `mcp.naming.md`, `Gate v1 Exit Criteria Template.txt`.

## Verdict

AsyncGate is the most architecturally coherent gate I've read in this stack — the termination truth table, the savepoint discipline, the durable ReceiptGate buffer and the CI (migration smoke + docker build + schema contract tests) are real engineering. But it is not v1-taggable. Three things block it outright: `tenant_id` is a client-supplied argument with no binding to the credential (direct violation of `receipt.rules.md` §3.1 — any valid key reads and writes any tenant), the rate limiter that the README, config and exit-criteria doc all claim is "on by default" is never wired into the single FastAPI route, and the lease critical section (`validate → read → update`) has no row lock or status precondition, so a worker whose lease expired mid-flight can still overwrite a terminal task and delete another worker's lease. On top of that, the "success without locatability" path mints obligations that can never be closed, and the canonical receipt adapter stamps `phase: "accepted"` on progress/anomaly/ack/lease-expiry receipts — I ran it; those payloads fail the canonical schema.

## Exit Criteria Scorecard

| Section | Result | Note |
|---|---|---|
| 1) Build & Run | **PASS** | `run_local.sh`/`.ps1`, `docker build` and `alembic upgrade head` both run in CI; config table in README matches `Settings`. Caveat: `Settings()` is constructed at import and raises if `api_key` unset and `allow_insecure_dev=False` — cold start fails on a bare env even in development. |
| 2) API & Contract Stability | **PARTIAL** | MCP-only surface is stable and correctly namespaced, but the error envelope emits non-integer JSON-RPC `code`s and raw exception text (F-15), `tenant_id` is a request argument (F-1), and `bootstrap.max_items` is unclamped (F-18). |
| 3) Canonical Principals | **PASS** | `principals.py` defines `sys:legivellum` / `svc:asyncgate`; `_resolve_obligation_owner` implements internal-vs-external ownership and children inherit via the obligation receipt. Receipts are addressed to `owner_principal_id`. |
| 4) Receipt Model Invariants | **PARTIAL** | `TERMINAL_RECEIPT_TYPES` is explicit and every terminator query is type-gated — genuinely correct. But obligation-type compatibility is never checked (`can_terminate_type` is dead), locatability-stripping creates permanently open obligations (F-6), idempotent re-creation mints duplicate obligations (F-7), and the canonical adapter mislabels non-terminals as `accepted` (F-8). |
| 5) Persistence & Migration | **PARTIAL** | Alembic chain 0001→0002→0003 is linear and CI-verified from empty DB. But `init_db()` runs `create_all` at startup, so production never actually executes the migrations, and `migrations/*.sql` is a third, overlapping tree (F-14). No DB-level constraint backs the "terminal receipts must have parents" invariant. |
| 6) Core Behavioral Guarantees | **PARTIAL** | `scripts/golden_path.py` exists and the happy path is coherent. Lease claim/complete races (F-3, F-4), unswept orphan leases after any redeploy (F-12), and a reference worker that blocks its own event loop past lease TTL (F-24) mean the guarantees don't hold under failure. |
| 7) Test Requirements | **PARTIAL** | 4 of the 5 required regressions exist and are real (`test_p13`, `test_p11`, `test_p05`, `test_p16`). Dedupe is verified only at the repository level; the actual emission path defeats it (F-7). No test covers auth, tenant isolation, complete-after-expiry, or cancel-vs-complete. `test_p03_p04` asserts config values for a rate limiter that is not installed. |
| 8) Observability | **PARTIAL** | `_emit_receipt` logs receipt/lease/obligation/trace ids via `extra` — good. But the caller's `X-Trace-Id` is clobbered (F-19), `get_metrics_snapshot` raises `AttributeError` (F-23), and there is no MCP tool to reach metrics anyway. |
| 9) v1 Lock Rules | **FAIL** | Tagging now freezes two things that must change: `tenant_id` as a public request field, and the adapter's phase mapping. Both are semver-breaking to fix afterwards. |
| 10) Open Issues / Deferred | **PARTIAL** | `ASYNCGATE_V1_EXIT_CRITERIA.md` lists only "verify container build" and "create tag" — both already done in CI — while omitting every known-open item from `🔎 Key Findings.txt` (tenant binding, rate-limit keying, idempotent receipt duplication). The deferral list is not honest about state. |

---

## Critical & High Findings

### F-1 (CRITICAL) — `tenant_id` is caller-supplied and bound to nothing; any valid credential reads/writes any tenant

`src/asyncgate/mcp/server.py:327`
```python
principal_id = _extract_principal_id(arguments)
tenant_id_value = arguments.get("tenant_id")
auth = await verify_auth_token(auth_token, session, tenant_id=tenant_id_value, principal_id=principal_id)
```
`src/asyncgate/auth/token.py:71`
```python
if tenant_id and tenant_claim and tenant_claim != tenant_id:
    raise UnauthorizedError("JWT tenant does not match request")
```
The tenant check is triple-conditional: it only fires for JWTs *and* only when the token carries a `tenant_id`/`tid` claim. The two other accepted credential types have no tenant path at all — the legacy shared key (`token.py:88`) and DB-backed `ag_` keys (`token.py:77`, `auth/models.py:55` has no tenant column) return an `AuthContext` that never sees `tenant_id`. Every handler then does `UUID(arguments["tenant_id"])` directly.

**Failure scenario:** tenant A's agent holds a valid `ASYNCGATE_API_KEY` (or any `ag_` key). It calls `asyncgate.list_receipts_ledger` with `tenant_id` set to tenant B's UUID and receives B's receipts including `body.result_payload` and task payloads; it calls `asyncgate.cancel_task` on B's task and the cancel succeeds because the owner check compares the caller's `agent_id` string to B's obligation owner — which the caller can also read from the ledger dump and then impersonate, since `agent_id` is likewise a free-text argument.

This directly violates `receipt.rules.md` §3.1: "`tenant_id` MUST be assigned by the server from authenticated context… Clients MUST NOT be able to override or spoof `tenant_id`." It is also the still-open Key Finding #5.

**Fix:** derive `tenant_id` from the credential — add a `tenant_id` column to `auth_api_keys`/`auth_users`, require the claim on JWTs, reject requests whose argument `tenant_id` disagrees, and delete `tenant_id` from the public tool schemas (or keep it and validate equality) before v1 freezes it.

---

### F-2 (HIGH) — Rate limiting is fully implemented and never installed

`src/asyncgate/main.py:92-101` — the only middleware/routes:
```python
app.add_middleware(CORSMiddleware, ...)
app.include_router(mcp_router)
```
`src/asyncgate/mcp/http.py:37` — the only route in the app:
```python
@router.post("")
async def mcp_entry(request: MCPRequest, http_request: Request):
```
No `dependencies=[Depends(rate_limit_dependency)]`, no middleware. `grep -rn "Depends("` across `src/` returns hits only inside `api/deps.py` itself, which nothing imports at runtime. 305 lines of sliding-window limiter with a Redis backend are dead code, as are `verify_api_key` and `get_tenant_id` in `api/deps.py`.

**Failure scenario:** an attacker posts 10k `tools/call` requests/second at `/mcp` with a wrong `ag_` key. Each one performs a DB lookup plus a bcrypt verify (see F-17) on the event loop; the server stops serving legitimate traffic. Meanwhile `config.py:143 rate_limit_active` reports `True` in production and `test_p03_p04_security_config.py:74` asserts it, so the dashboard and the test suite both say the control is on.

**Fix:** attach `rate_limit_dependency` to the `/mcp` route (keyed on the resolved credential, not `settings.api_key` — see Key Finding #2, still open), or delete the module and the claims in README/`SYSTEM_BOUNDARY.md`.

---

### F-3 (HIGH) — Lease critical section has no row lock and releases by `task_id`, not `lease_id`

`src/asyncgate/engine/core.py:706-738`
```python
lease = await self.leases.validate(tenant_id, task_id, lease_id, worker_id)   # plain SELECT
...
task = await self.tasks.get(tenant_id, task_id)                                # plain SELECT
if not task.can_transition_to(TaskStatus.SUCCEEDED): ...
async with self.session.begin_nested():
    await self.tasks.update_status(tenant_id, task_id, TaskStatus.SUCCEEDED, task_result)
    await self.leases.release(tenant_id, task_id)
```
`src/asyncgate/db/repositories.py:594-602`
```python
async def release(self, tenant_id: UUID, task_id: UUID) -> bool:
    result = await self.session.execute(
        delete(LeaseTable).where(LeaseTable.tenant_id == tenant_id, LeaseTable.task_id == task_id)
    )
```
`validate()` (repositories.py:491) is a bare `SELECT` with no `FOR UPDATE`; `update_status` (repositories.py:219) has no status predicate in its `WHERE`.

**Failure scenario:** worker A's lease expires at T. At T-10ms A calls `complete`; `validate` passes. The sweep loop (separate session, `tasks/sweep.py:45`) commits `requeue_on_expiry` + `release` at T. Worker B claims the task at T+1s and gets lease L2. At T+1.2s worker A's transaction — still open, still holding stale reads — commits: the task flips `queued`/`leased` → `succeeded` with A's result, and `release(tenant_id, task_id)` **deletes B's lease row L2**. B then completes and gets `LeaseInvalidOrExpired`, its work is discarded, and the ledger contains a `task.completed` from a worker that had no authority. The same shape applies to `fail()` (core.py:783) and `report_progress` → `_transition_to_running` (core.py:1130), where a `SUCCEEDED` task can be dragged back to `RUNNING`.

**Fix:** `SELECT … FOR UPDATE` the lease row in `validate`, add `.where(LeaseTable.lease_id == lease_id)` to `release`, and make `update_status` conditional (`WHERE status IN ('leased','running')` returning rowcount, raising `LeaseInvalidOrExpired` on 0 rows).

---

### F-4 (HIGH) — Terminal state is not immutable: concurrent cancel + complete both land

`src/asyncgate/db/repositories.py:219-225`
```python
await self.session.execute(
    update(TaskTable)
    .where(TaskTable.tenant_id == tenant_id, TaskTable.task_id == task_id)
    .values(**values)
)
```
The only guard is the in-Python `task.is_terminal()` / `can_transition_to()` check against a value read earlier in the transaction (`core.py:375`, `core.py:714`).

**Failure scenario:** an agent calls `cancel_task` at the same moment the worker calls `complete`. Both read `status='leased'`, both pass their guard, both write. Final state: `status='succeeded'` with `result_outcome='canceled'` or vice versa depending on commit order, and the obligation carries **two** terminal receipts (`task.canceled` and `task.completed`) that disagree. README invariant 5 ("Terminal states are immutable") and the exit criteria's "cancel emits terminal receipt and closes obligation" both silently break.

**Fix:** same as F-3 — conditional `UPDATE … WHERE status = <expected>` with rowcount checking, or `SELECT … FOR UPDATE` on the task row at the top of every mutating engine method.

---

### F-5 (HIGH) — `list_open_obligations` scans and materialises every terminal receipt in the tenant on every bootstrap

`src/asyncgate/db/repositories.py:1267-1276`
```python
terminated_result = await self.session.execute(
    select(ReceiptTable.parents)
    .where(
        ReceiptTable.tenant_id == tenant_id,
        ReceiptTable.receipt_type.in_(TERMINAL_RECEIPT_TYPES),
        # This uses the GIN index: idx_receipts_parents_gin
        func.jsonb_array_length(ReceiptTable.parents) > 0,
    )
)
```
No `LIMIT`, and no predicate referencing `candidate_ids` — the candidate matching happens in Python at line 1282-1286. The comment is wrong twice: `jsonb_array_length(parents) > 0` is not an indexable operator for a GIN index on `parents` (only containment operators like `@>` are), so this is a sequential scan filtered by the `idx_receipts_type` index at best. `migrations/README.md` documents this exact query as the "After" optimisation.

**Failure scenario:** a tenant with 500k receipts, 200k of them terminal. Every `asyncgate.bootstrap` call streams 200k JSONB arrays into the Python process and builds a set from them, to answer a question about ≤1000 candidates. Two agents polling bootstrap every 5s saturate the connection pool (`pool_size=20`) and the process RSS. Note this is the *fixed* version of the old N+1 — it traded N indexed lookups for one unbounded scan.

**Fix:** push the filter into SQL: `WHERE parents ?| :candidate_ids` (jsonb key-exists-any, GIN-indexable on the default `jsonb_ops`) or `EXISTS (… parents @> to_jsonb(candidate))` per candidate with the GIN index, and select only `receipt_id` of the terminated candidates.

---

### F-6 (HIGH) — "Success without locatability" produces an obligation that can never be closed

`src/asyncgate/db/repositories.py:739-757`
```python
if receipt_type == ReceiptType.TASK_COMPLETED:
    ...
    if not (has_artifacts or has_delivery_proof):
        parents_to_use = []
        emit_locatability_anomaly = True
        logger.warning("SUCCESS WITHOUT LOCATABILITY: ... Parents stripped - obligation stays open. ...")
```
The comment at line 753 says "obligation stays open until proper locatable receipt created". But `complete()` has already set the task `SUCCEEDED` (`core.py:735`, inside the same savepoint), and `Task.can_transition_to` (`models/task.py:103`) gives `SUCCEEDED: set()`. There is no second call that can produce a locatable success.

**Failure scenario:** a worker calls `asyncgate.complete` with `result={...}` and no `artifacts` (the MCP schema at `mcp/server.py:245` does not require `artifacts`). Task → `succeeded`. The `task.assigned` obligation has no terminator forever, so `asyncgate.bootstrap` returns it on every poll for the life of the ledger, and `check_terminator` says `false`. The remediation the code documents is unreachable. This is exactly the "haunted bootstrap" the module docstring in `models/termination.py:19` warns about, self-inflicted.

**Fix:** decide Phase 2 now — either reject the `complete` call (`ValueError` → the worker retries with artifacts, task stays `leased`), or accept it as terminal and let the anomaly be advisory. Silently succeeding the task while leaving the obligation open is the one option that satisfies neither invariant.

---

### F-7 (HIGH) — Idempotent task creation mints a second obligation; `trace_id` in the body defeats receipt dedupe globally

`src/asyncgate/engine/core.py:254-285`
```python
task = await self.tasks.create(... idempotency_key=idempotency_key ...)
...
await self._emit_receipt(
    tenant_id=tenant_id, receipt_type=ReceiptType.TASK_ASSIGNED, ...
)
```
`create()` returns the *existing* task on an idempotency-key collision (`repositories.py:121-127`) but signals nothing, so `create_task` always emits. The hash-dedupe that would otherwise absorb the duplicate is disabled by `_emit_receipt` itself:

`src/asyncgate/engine/core.py:1236-1238`
```python
body_payload = dict(body) if body else {}
if trace_id and "trace_id" not in body_payload:
    body_payload["trace_id"] = trace_id
```
`compute_receipt_hash` (`models/receipt.py:71`) hashes the body, and every request gets a fresh `trace_id` (`mcp/server.py:325`, see F-19), so no two receipts emitted in different requests ever hash equal.

**Failure scenario:** an agent retries `create_task` with the same `idempotency_key` after a client timeout. It gets the same `task_id` back (correct) plus a **second** `task.assigned` receipt with a different `receipt_id` (wrong). `get_task_obligation` (`repositories.py:1045`) orders by `created_at DESC LIMIT 1`, so the terminal receipt will reference only the newest one; the first obligation is open forever and appears in every bootstrap. This is Key Finding #1, unfixed, and it also silently voids the "dedupe behavior verified" exit-criteria line — `test_p05_hash_parents.py` tests `compute_receipt_hash` directly and never exercises `_emit_receipt`.

**Fix:** return `(task, created)` from `TaskRepository.create` and skip receipt emission when `created is False`; move `trace_id` out of the hashed body into a dedicated column or into `metadata` excluded from the hash.

---

### F-8 (HIGH) — Canonical adapter stamps `phase: "accepted"` on progress/anomaly/ack/lease-expiry receipts and emits schema-invalid payloads

`src/asyncgate/receipts/memorygate_adapter.py:84-106`
```python
def _derive_phase_and_status(receipt: Receipt, task: Task | None) -> tuple[str, str]:
    if receipt.receipt_type == ReceiptType.TASK_ESCALATED: return "escalate", "NA"
    if receipt.receipt_type in { TASK_COMPLETED, TASK_FAILED, TASK_CANCELED, TASK_RESULT_READY }: ...
    return "accepted", "NA"        # <-- everything else
```
Everything not in those two sets — `task.progress`, `task.started`, `lease.expired`, `task.retry_scheduled`, `receipt.acknowledged`, `system.anomaly` — falls through to `accepted`. Per `receipt.rules.md` §1.1 an `accepted` receipt **MUST create an obligation** and MUST carry `completed_at: null`, `outcome_kind: "NA"` and `"NA"` artifact fields. The adapter fills those fields from the *task*, not the receipt (`memorygate_adapter.py:156, 174`), so once the task has a result they are populated.

I ran the adapter against the canonical schema (`LegiVellum/docs/canonical/receipt.schema.v1.json`, `additionalProperties: false`, 41/42 required):
```
task.progress        phase=accepted outcome_kind=artifact_pointer completed_at=2026-02-23T12:00:05+00:00
  errors: ['completed_at']: '...' is not of type 'null'
          ['outcome_kind']: 'NA' was expected
          ['artifact_location']: 'NA' was expected
```
identical for `system.anomaly`, `lease.expired`, `task.retry_scheduled`, `receipt.acknowledged`, `task.started`.

**Failure scenario:** any consumer calls `asyncgate.list_receipts_ledger` for a completed task; the progress and anomaly receipts in the response fail canonical validation and, if a consumer applies §4 derived-state rules, each one opens a *new* unresolved obligation for the same `task_id`. This is the exact class of defect the context file calls out — "`ack`/`progress`/`anomaly` must never close **or open** obligations". Live ReceiptGate emission is spared only because `core.py:1281-1289` filters to six eligible types; that filter is the only thing standing between this and a rejected ledger write.

**Fix:** add an explicit non-obligation phase mapping (or refuse to render non-protocol receipt types into canonical form at all), and derive `completed_at`/`outcome_kind`/artifact fields from the receipt body rather than the current task row. Extend `test_receipt_schema_contract.py::ALL_CASES` to cover all 14 `ReceiptType` values — it currently covers 5, which is why this shipped.

---

### F-9 (HIGH) — The ReceiptGate failure handler itself raises `TypeError`, aborting the enclosing savepoint

`src/asyncgate/engine/core.py:1296-1302`
```python
except Exception as exc:
    logger.warning(
        "receiptgate_receipt_emit_failed",
        receipt_type=receipt_type.value,
        task_id=str(task_id) if task_id else None,
        error=str(exc),
    )
```
stdlib `logging` takes structured fields via `extra=`, not kwargs — verified: `TypeError: Logger._log() got an unexpected keyword argument 'receipt_type'`. The identical bug was already found and fixed 30 lines above (comment at `core.py:1262`: "Passing them as kwargs raised TypeError on every single receipt emission") and left in place here.

**Failure scenario:** in `receiptgate_integrated` mode, anything in the emit block throws — `to_memorygate_receipt` hitting a malformed artifact, a `CanonicalReceipt.model_validate` failure from F-8, `get_receiptgate_client()` failing to create its buffer directory. The handler raises `TypeError`, which propagates out of `_emit_receipt`, out of the `async with self.session.begin_nested()` in `complete()`, and rolls the whole completion back. A degraded-ledger warning becomes a hard failure of task completion.

**Fix:** `logger.warning("receiptgate_receipt_emit_failed …", extra={...})`. Also add a lint rule; this is the second instance.

---

### F-10 (HIGH) — `TaskRepository.create` calls `session.rollback()` on `IntegrityError`, destroying the caller's transaction

`src/asyncgate/db/repositories.py:118-129`
```python
try:
    await self.session.flush()
    return self._row_to_model(task_row)
except IntegrityError:
    await self.session.rollback()
    if idempotency_key:
        existing = await self._get_by_idempotency_key(tenant_id, idempotency_key)
        if existing: return existing
    raise
```
`self.session` is the request-scoped session from `get_session()` (`mcp/server.py:321`). `rollback()` ends the whole transaction, not a savepoint.

**Failure scenario (a):** any `IntegrityError` that is *not* an idempotency collision — e.g. a `receipts.hash` unique violation flushed in the same unit of work, or the FK from `leases` — triggers a full rollback and then a re-raise, so the error surfaces with the transaction already gone and any prior writes in that request silently discarded rather than rolled back by the outer handler. **(b):** on a genuine collision, `_get_by_idempotency_key` executes in a fresh implicit transaction, and the returned `Task` is then used by `create_task` to emit a receipt in *that* transaction — which is a different consistency scope than the caller believes it has.

**Fix:** wrap the insert in `async with self.session.begin_nested():` and catch `IntegrityError` there; inspect `exc.orig` / constraint name before assuming it is the idempotency key.

---

### F-11 (HIGH) — Receipt dedupe is check-then-insert; a client retry produces a 500 instead of idempotency

`src/asyncgate/db/repositories.py:772-797`
```python
if receipt_hash_to_use:
    existing = await self._get_by_hash(tenant_id, receipt_hash_to_use)
    if existing:
        return existing
receipt_row = ReceiptTable(... hash=receipt_hash_to_use ...)
self.session.add(receipt_row)
await self.session.flush()
```
with `UniqueConstraint("tenant_id", "hash", name="uq_receipt_hash")` (`db/tables.py:215`). No `IntegrityError` handling.

**Failure scenario:** a worker retries `asyncgate.complete` after a network timeout and — correctly, per `worker.contract.md` §8 — reuses the same `trace_id`. Both requests compute the same hash, both pass the `_get_by_hash` check, the second `flush()` raises `IntegrityError` on `uq_receipt_hash`, `get_session` rolls back, and the caller gets a generic error envelope. The at-least-once retry that the whole lease design assumes turns into a hard failure. (The inverse case — differing `trace_id` — is F-7.)

**Fix:** `INSERT … ON CONFLICT (tenant_id, hash) DO NOTHING RETURNING *`, falling back to the select; or catch `IntegrityError` inside a savepoint and re-select.

---

### F-12 (HIGH) — Leases owned by a departed instance are never swept; every redeploy strands tasks

`src/asyncgate/db/repositories.py:604-637`
```python
if instance_id:
    query = query.where(TaskTable.asyncgate_instance == instance_id)
```
called as `get_expired(limit=100, instance_id=settings.instance_id)` (`core.py:897`). `asyncgate_instance` is stamped at task creation (`repositories.py:113`) and never updated, and `detect_instance_id()` (`instance.py:27-60`) returns values that change on every restart: `FLY_ALLOC_ID`, the K8s pod name, `K_REVISION`+random, or `hostname`+random.

**Failure scenario:** three replicas; a rolling deploy replaces all pods. Every task created by the old pods now has an `asyncgate_instance` no live sweeper matches. Any of those tasks that were `leased` when the worker crashed stay `leased` with an expired lease **forever** — never requeued, never expired, no `lease.expired` receipt, no escalation. The obligation stays open with no path to closure. The same happens if a single replica OOMs. `docs/INSTANCE_UNIQUENESS.md` covers ID collision but not ID *disappearance*.

**Fix:** add a reclaim path — sweep leases whose `expires_at` is older than some multiple of the max TTL regardless of instance, or maintain an instance heartbeat table and let live instances adopt tasks whose owner has not heartbeat in N intervals.

---

## Medium Findings

### F-13 (MEDIUM) — `claim_next` applies the capability filter after `LIMIT`, causing head-of-line blocking
`src/asyncgate/db/repositories.py:404-436`
```python
    .limit(max_tasks)
    .with_for_update(skip_locked=True)
...
for task in tasks:
    task_caps = task.requirements.get("capabilities", [])
    if task_caps and capabilities:
        if not set(task_caps).issubset(set(capabilities)): continue
    elif task_caps and not capabilities: continue
```
**Failure scenario:** one high-priority task requires capability `gpu`. Twenty CPU workers poll with `max_tasks=1`; each one selects that single row (highest priority, FIFO), fails the Python-side capability check, and returns zero tasks. A thousand ordinary queued tasks behind it are never leased until a GPU worker appears. `accept_types` is pushed into SQL (line 423) but capabilities are not.

**Fix:** push capability containment into the query (`requirements->'capabilities' <@ :worker_caps::jsonb`) so `LIMIT` applies to matching rows.

### F-14 (MEDIUM) — Three competing schema sources; production never runs Alembic
`src/asyncgate/db/base.py:68-71`
```python
async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
```
called unconditionally from `main.py:63`. So the runtime schema comes from the ORM models, `alembic/versions/` is only exercised by CI, and `migrations/001-003.sql` is a third partially-overlapping tree (`migrations/README.md` calls it "legacy" but ships it). **Failure scenario:** a model gains a column without a migration; dev and prod work (create_all adds it), CI's `alembic upgrade head` still passes because it doesn't compare against models, and the first environment that restores from a migration-built database gets an `UndefinedColumn` error at runtime. **Fix:** drop `init_db()` from lifespan (or gate it behind development), and add `alembic check`/autogenerate-diff to CI.

### F-15 (MEDIUM) — JSON-RPC error envelope uses non-integer codes and leaks internals
`src/asyncgate/mcp/http.py:68-69`
```python
except Exception as exc:
    return _jsonrpc_error(request.id, getattr(exc, "code", "ERROR"), str(exc))
```
JSON-RPC 2.0 requires `error.code` to be an integer; this emits `"TASK_NOT_FOUND"` or `"ERROR"`. And `str(exc)` on an unhandled `asyncpg`/SQLAlchemy error returns the failing SQL and connection details to an unauthenticated-shaped response. **Failure scenario:** a malformed `tenant_id` triggers `ValueError: badly formed hexadecimal UUID string`; a DB outage returns the DSN-bearing `OperationalError` text. A strict MCP client rejects the envelope on the code type. **Fix:** map `AsyncGateError` subclasses to fixed integer codes, put the string code in `error.data`, and return a generic message for unmapped exceptions while logging the detail.

### F-16 (MEDIUM) — TLS to Postgres is hardcoded off and cannot be enabled
`src/asyncgate/db/base.py:26-33`
```python
engine = create_async_engine(
    settings.database_url, ...,
    connect_args={"ssl": False},  # Disable SSL for Fly.io internal network
)
```
Same in `alembic/env.py:58`. **Failure scenario:** the operator points `ASYNCGATE_DATABASE_URL` at a managed Postgres (RDS/Neon/Supabase) reachable over the public internet; asyncpg is told `ssl=False` and either fails to connect or transmits credentials and every task payload in cleartext, with no env var to override. **Fix:** make it a setting (`ASYNCGATE_DB_SSL`) defaulting to on, or pass through the `sslmode` in the URL.

### F-17 (MEDIUM) — Auth path blocks the event loop on bcrypt and commits the request session
`src/asyncgate/auth/middleware.py:92-101`
```python
if not verify_api_key_hash(api_key, api_key_obj.key_hash):   # sync bcrypt.checkpw
    return None
...
api_key_obj.increment_usage()
await db.commit()
```
`db` here is the session that the tool handler is about to use (`token.py:79`). **Failure scenario:** bcrypt at default cost blocks the single event loop thread for ~50-300ms per request, so a handful of concurrent DB-key requests serialise the entire server (compounded by F-2). Separately, `commit()` inside auth ends the transaction the tool body assumes it is starting in, so a later `begin_nested()` opens a savepoint in a *different* transaction than the caller's mental model. **Fix:** `await asyncio.to_thread(verify_api_key_hash, ...)`, use a separate short-lived session for usage tracking (or a fire-and-forget counter).

### F-18 (MEDIUM) — `bootstrap` limit is unclamped
`src/asyncgate/mcp/server.py:355`: `limit=arguments.get("max_items") or settings.default_bootstrap_max_items` — passed straight to `engine.list_open_obligations` (`core.py:1058`, signature `limit: int = 50`, no clamp) and on to the repository, where the only bound is `candidate_limit = min(limit * 3, 1000)`. `settings.max_bootstrap_max_items` (200) is applied only in the deprecated `engine.bootstrap` (`core.py:154`). **Failure scenario:** `max_items: 1000000` returns up to 1000 full receipt bodies (each up to 64KB) in one response — a ~64MB payload — while F-5 scans the tenant. **Fix:** clamp with `min(..., settings.max_bootstrap_max_items)` in the handler.

### F-19 (MEDIUM) — Caller-supplied trace id is discarded; receipt trace ≠ response trace
`src/asyncgate/mcp/server.py:324-325`
```python
trace_id = arguments.pop("trace_id", None)
set_trace_id(trace_id)
```
`trace_id_middleware` (`middleware/trace.py:19`) has already set the context from the `X-Trace-Id` header; this unconditionally overwrites it with `None` when the argument is absent (the common case — the header is in the CORS allowlist, the argument is optional). `_emit_receipt` then calls `ensure_trace_id()` and mints a fresh UUID. **Failure scenario:** an operator correlates a client-side incident by `X-Trace-Id`; the header echoed in the response (set by the middleware from the request) matches nothing in the receipt bodies or the receipt-emission logs, which carry the generated id. **Fix:** `if trace_id: set_trace_id(trace_id)`.

### F-20 (MEDIUM) — Escalation can write a receipt into another tenant with a foreign-tenant parent
`src/asyncgate/engine/core.py:1184-1206`
```python
target_tenant_id = tenant_id
if target and target.tenant_id:
    try: target_tenant_id = UUID(target.tenant_id)
    ...
parents = [obligation.receipt_id] if obligation else None
return await self._emit_receipt(tenant_id=target_tenant_id, ..., parents=parents, ...)
```
`ReceiptRepository.create` only validates parent existence for *terminal* types (line 712), and `task.escalated` is not terminal, so nothing catches it. **Failure scenario:** an operator configures `ASYNCGATE_ESCALATION_TARGETS=[{"class":1,"to_kind":"agent","to_id":"ops","tenant_id":"<ops-tenant>"}]`. A lease expires in tenant A; the escalation receipt is written to the ops tenant with `parents=[<receipt in tenant A>]` and `task_id` pointing at a task that does not exist in the ops tenant. Provenance traversal in the ops tenant dead-ends, and `receipt.rules.md` §4's "all provenance queries MUST be scoped by tenant_id" cannot be satisfied. **Fix:** either forbid cross-tenant escalation, or drop `parents`/`task_id` and carry the origin in the body when the tenant changes.

### F-21 (MEDIUM) — No per-principal authorization on reads
`mcp/server.py:400-448` — `get_task`, `list_tasks`, `list_receipts`, `list_receipts_ledger` take no principal and apply no owner filter; `engine.list_tasks` accepts `created_by_id` but the MCP handler never passes it (`server.py:406`). **Failure scenario:** worker `w-1`, authenticated with its own `ag_` key, calls `asyncgate.list_tasks` and reads the inline `payload` of every task in the tenant, including tasks for other agents that may carry credentials or PII. Only `cancel_task` enforces ownership (`core.py:362-373`). **Fix:** require a principal on read tools and filter to receipts/tasks that principal owns, unless the caller is internal.

### F-22 (MEDIUM) — `ack_receipt` never validates the receipt it acknowledges
`src/asyncgate/engine/core.py:464-487`
```python
await self._emit_receipt(..., receipt_type=ReceiptType.RECEIPT_ACKNOWLEDGED,
                         body={"acknowledged_receipt_id": str(receipt_id)}, parents=[receipt_id])
```
No existence check, no tenant check, no check that the receipt was addressed to `principal`. **Failure scenario:** a caller acks a random UUID and the ledger gains an append-only receipt whose `parents` points at nothing — a dangling provenance edge that any chain-walker must special-case. A caller can also ack receipts addressed to a different principal, corrupting delivery telemetry. (CR1 H3 called this "missing parent linkage"; linkage was added, validation was not.) **Fix:** load the receipt, 404 if absent, and require `receipt.to_ == principal` unless internal.

### F-23 (MEDIUM) — `get_metrics_snapshot` raises `AttributeError`; `count_by_status` lives on the wrong repository
`src/asyncgate/engine/core.py:1111`: `status_counts = await self.tasks.count_by_status(tenant_id)`. `TaskRepository` (repositories.py:53-377) has no such method; `count_by_status` is defined on `ReceiptRepository` at `repositories.py:882` and queries `TaskTable`. **Failure scenario:** the method is unreachable from MCP today, so it is latent — but the moment a metrics tool is added (exit criteria §8 "basic metrics counters"), the first call raises `AttributeError: 'TaskRepository' object has no attribute 'count_by_status'`. It also means no test ever ran this code. **Fix:** move `count_by_status` to `TaskRepository`; add a test or delete the method.

### F-24 (MEDIUM) — Reference worker blocks its own loop past the lease TTL and never renews
`workers/command_executor/worker.py:138-144`
```python
result = subprocess.run(cmd, shell=self.allow_shell, capture_output=True, text=True, timeout=300)
```
called synchronously from `async def process_task` (line 262). `asyncgate.renew_lease` is never called anywhere in the worker. Default lease TTL is 120s (`config.py:151`). **Failure scenario:** a task runs a 180s command. At 120s the sweep expires the lease, requeues the task and emits `lease.expired`; another worker claims it and runs the same command again (duplicate side effects — prohibited by `worker.contract.md` §1.4). At 180s the first worker calls `complete` and gets `LEASE_INVALID_OR_EXPIRED`; its output file is orphaned and `report_completion` swallows the error at line 222-223. **Fix:** `await asyncio.create_subprocess_exec`, plus a renewal task on a timer at TTL/3. Also: `--tenant-id` defaults to `"default"`, which fails `UUID()` on the server (`server.py:472`), so the documented default invocation cannot work.

### F-25 (MEDIUM) — An expired lease can still be renewed
`src/asyncgate/db/repositories.py:523-533` — the renew `SELECT ... FOR UPDATE` filters on tenant/task/lease/worker but **not** `expires_at > now()`, unlike `validate` (line 497). The only expiry gate is `renew_lease`'s check that the task is still `LEASED`/`RUNNING` (`core.py:584`), which remains true until the sweeper runs (every ~5s, and never at all under F-12). **Failure scenario:** worker A's lease expires; the sweep is delayed by a slow batch; A calls `renew_lease` 3s later and gets a fresh 120s expiry on a dead lease. If the sweep had already requeued and another worker claimed it, A's renew updates a row that no longer represents authority (or returns None only by luck of the delete). `lease_grace_seconds` (config.py:154) exists but is read nowhere — grace is accidental, not configured. **Fix:** add `expires_at > now() - grace` to the renew predicate and honour `lease_grace_seconds`.

### F-26 (MEDIUM) — Termination is type-gated but not compatibility-gated
`repositories.py:1110` / `1143` / `1175` filter on `TERMINAL_RECEIPT_TYPES` (correct per the exit criteria) but never consult `can_terminate_type` (`models/termination.py:87`), which is dead code. **Failure scenario:** today `TERMINATION_RULES` has one key so the sets coincide. The moment the commented-out `LEASE_GRANTED` / `SCHEDULE_CREATED` rules at `termination.py:33-42` are enabled, a `lease.released` receipt whose parents include a `task.assigned` id will close the *task* obligation, because the query only asks "is this type terminal for anything?". **Fix:** join the obligation's own type into the terminator query, or assert compatibility at write time in `ReceiptRepository.create`.

---

## Low / Nits

- **(LOW) `SYSTEM_BOUNDARY.md` is stale in ways that will mislead integrators.** It documents states `PENDING → ACTIVE → COMPLETED | FAILED | CANCELLED` (line 110) and receipt types `task.started`/`task.success`/`task.failure`/`task.cancelled` (lines 115-120); the code uses `queued/leased/running/succeeded/failed/canceled` and `task.completed`/`task.failed`/`task.canceled`. It also claims "Expected tables: `tasks`, `receipts`, `alembic_version`" (line 210) when the schema has eight.
- **(LOW) `engine.bootstrap` is dead deprecated code** (`core.py:137-217`) — no MCP tool routes to it; it still returns `"uptime": 0` with a TODO (line 196) and calls `mark_delivered`, a mutation, from a nominally read-only path.
- **(LOW) `engine.start_task` is unreachable** (`core.py:651`) — there is no `asyncgate.start_task` tool, so `RUNNING` is only entered as a side effect of `report_progress`. Either expose it or fold it in.
- **(LOW) `.env.local` is present in the tree** with `ASYNCGATE_API_KEY=dev-test-key-not-for-production`. It is in `.gitignore`, so this is a packaging artifact rather than a leak, but it should not be in a distributed archive.
- **(LOW) Dead surface:** `AuditEventTable`/`models/audit.py` (never written), `QuotaExceededError`/`RateLimitExceededError` (never raised), 4 of 5 `AnomalyKind` members (never emitted — CR1 H2, still open), `lease_grace_seconds`, and all of `api/deps.py` at runtime.
- **(LOW) `verify_request_api_key` uses `scalar_one_or_none()` on an 11-char key prefix** (`auth/middleware.py:84-86`); two keys sharing a prefix raise `MultipleResultsFound` → 500 for both users. Use `.limit(2)` and compare hashes, or make `key_prefix` unique.
- **(LOW) `auth/models.py` uses naive `datetime.utcnow()`** (lines 35-37, 71, 86) with `DateTime` columns lacking `timezone=True`, while the rest of the codebase is rigorous about `utc_now()`. `APIKey.is_valid` compares naive-to-naive so it happens to work, but it is the one corner P12 missed — and `test_p12_timezone_aware_datetimes.py::test_all_datetime_columns_have_timezone_true` presumably doesn't cover the auth tables.
- **(NIT)** `from sqlalchemy import update` and `TaskTable`, `LeaseInfo`, `AnomalyKind`, `Relationship` are imported but unused in `engine/core.py`; `import random` / `import logging` are re-imported inside `expire_leases` (`core.py:895`, `979`).
- **(NIT)** `alembic/versions/0002` issues `ALTER TYPE … ADD VALUE` inside Alembic's transaction. Fine on PG ≥ 12 (and CI runs PG 16) as long as no later migration in the same run *uses* the new value — worth a comment so nobody adds a data migration that does.

---

## Test Coverage Gaps

What's genuinely covered: batch termination + pagination (`test_p01`), savepoint rollback on receipt failure (`test_p02` — good use of a targeted mock on the second receipt, not over-mocked), hash-includes-parents and order independence (`test_p05`), renewal count/lifetime limits and `acquired_at` preservation (`test_p11`), timezone-awareness end-to-end including a DB round trip (`test_p12` ×2), cancel-closes / non-terminal-doesn't-close (`test_p13`), running-state transitions (`test_p14`), ReceiptGate buffer persistence and replay (`test_p17`), single-winner concurrent claim (`test_p16`), and canonical schema validation for 5 receipt shapes (`test_receipt_schema_contract`).

Specific missing regressions, in the order I'd write them:

1. **complete-after-expiry** — claim, force `expires_at` into the past, run `expire_leases` in a second session, then call `complete` with the stale lease. Must fail, must not mutate the task, must not delete the new lease. (F-3)
2. **cancel vs complete** — interleave in two sessions; assert exactly one terminal receipt and one terminal status. (F-4)
3. **idempotent create emits one obligation** — call `create_task` twice with the same `idempotency_key` and assert exactly one `task.assigned` receipt exists. This is the "dedupe behaviour" regression the exit criteria claims; `test_p05` tests the hash function, not the emission path. (F-7)
4. **complete without artifacts** — assert the resulting state is *decided*: either the call is rejected, or the obligation is closed. Today it asserts nothing and the system enters a state with no exit. (F-6)
5. **adapter coverage for all 14 `ReceiptType` values** against the canonical schema — parametrise `ALL_CASES` over the enum so a new type cannot be added without a mapping. (F-8)
6. **auth** — there is not a single test for `verify_auth_token`. Needed: missing token rejected, wrong token rejected, JWT with mismatched `sub`/tenant rejected, `allow_insecure_dev` refused outside development. CI sets `ASYNCGATE_ALLOW_INSECURE_DEV=true` globally, so the entire auth path is untested by construction.
7. **tenant isolation** — create a task in tenant A, read it with a credential for tenant B, assert denial. Currently this would pass trivially and wrongly. (F-1)
8. **rate limit enforcement** — `test_p03_p04_security_config.py:65-125` asserts `settings.rate_limit_enabled is True` and prints a checkmark. It never sends a request. A test that asserted 429 after N calls would have caught F-2 on day one. This is the clearest example in the repo of a test that asserts configuration instead of behaviour.
9. **orphaned instance sweep** — create a task with `asyncgate_instance='dead-pod'`, expire its lease, run the sweep, assert it gets reclaimed. (F-12)
10. **capability starvation** — queue a `gpu` task at priority 10 and a plain task at priority 0; a CPU worker must receive the plain task. (F-13)

Note also that the `client` fixture (`conftest.py:88-112`) overrides `get_db_session`, `verify_api_key` and `rate_limit_dependency` — none of which the application uses — so those three overrides are inert; the HTTP tests actually reach the DB through the monkeypatched module-global `async_session_factory`. Harmless today, misleading tomorrow.

---

## Delta vs CODE_REVIEW_1.md

CR1 is stamped "LEGACY NOTE (2026-02-03): AsyncGate is MCP-only", and much of it has genuinely been addressed.

**Fixed:**
- **C1 MCP auth gap** — `_handle_tool` now calls `verify_auth_token` before dispatch (`mcp/server.py:328`) and every tool schema requires `auth_token` (`server.py:22-36`). Properly fixed.
- **C2 missing migrations** — `alembic/versions/` now holds a linear 0001→0003 chain, and CI runs `alembic upgrade head` from an empty database.
- **C3 `running` status** — present in `TaskStatus` (`enums.py:11`), in the state machine (`task.py:96`), in migration 0002, with tests (`test_p14`).
- **H3 ack missing parent linkage** — `ack_receipt` now sets `parents=[receipt_id]` (`core.py:484`). Linkage fixed; validation still absent (F-22).
- **M5 no MCP tool for open obligations** — `asyncgate.bootstrap` now returns the flat open-obligation dump (`server.py:351`).
- **§6.3 missing README** — README, DEVELOPMENT.md, ARCHITECTURE.md, deployment docs all exist now.
- **P1 "add core test coverage"** — partially: 15 test files vs 4, with a CI coverage gate at 40%.

**Still open:**
- **H1 insufficient tests** — improved but the scary paths (auth, tenant isolation, lease races) remain untested; see above.
- **H2 anomaly triggers** — still only `locatability_missing` (`repositories.py:808`); `AnomalyKind`'s other four members (`enums.py:76-80`) are never emitted.
- **H4 scheduler TASKEE** — not implemented; correctly declared out of scope in `SYSTEM_BOUNDARY.md`, but not listed in the exit-criteria deferral section.
- **§3.5 "rate limit key uses API key hash… attackers could spoof tenant_id"** — moot in the worst way: the limiter is not installed at all (F-2).
- **§3.7 M2 RLS** — no row-level security; tenant isolation is entirely application-level and now demonstrably bypassable (F-1).
- **§4.3.1 "database errors not specifically handled"** — worse now: `mcp/http.py:68` catches bare `Exception` and returns the message to the client (F-15).
- **L2 uptime always 0** — still `"uptime": 0` at `core.py:196`.
- **§8.2 "`get_expired` loads all expired leases into memory"** — bounded at 100 now, but `list_open_obligations` acquired a much bigger unbounded scan in the same area (F-5).

**Regressed / newly introduced since CR1:**
- The P0.1 "optimisation" replaced N indexed `@>` lookups with one unbounded sequential scan over all terminal receipts (F-5). CR1's §8.1 praised the GIN index; the query written to exploit it cannot use it.
- The `logger.warning(**kwargs)` `TypeError` was found and fixed at `core.py:1264` and left unfixed 30 lines below at `core.py:1297` (F-9).
- CR1 §3.4 rated SQL injection "Safe" — that still holds; no raw SQL interpolation anywhere except the enum-name f-string in migration 0002, which is derived from a `pg_type` lookup, not user input.

The separate `🔎 Key Findings.txt` (undated, post-CR1) lists 6 items: #1 (duplicate receipts on idempotent create) — **still open** (F-7); #2 (rate-limit keying) — **superseded, worse** (F-2); #3 (renewal-limit 500s) — moot after REST removal, now surfaces as an error envelope; #4 (enum `ValueError` → 500) — **still open**, e.g. `TaskStatus(status)` at `core.py:318` and `ReceiptType(...)` at `server.py:444`, both reachable with arbitrary strings; #5 (tenant isolation) — **still open** (F-1); #6 (MemoryGate placeholder) — **fixed**, the client is now a real HTTP + circuit-breaker + durable-buffer implementation.

---

## Cross-repo observations

- **Tenant model conflicts with the normative rules.** `receipt.rules.md` §3.1 is unambiguous that the server assigns `tenant_id` from auth. AsyncGate takes it as a tool argument on all 16 tools. If any other gate assumes AsyncGate enforces tenancy (ReceiptGate certainly must, since AsyncGate forwards `receiptgate_tenant_id` from config at `config.py:107`), the boundary is only as strong as the weakest gate. Worth checking whether the other repos made the same choice — if they all did, this is a stack-level design decision that contradicts the spec and should be fixed once, in the shared library.
- **Adapter drift is a shared-code smell.** `receipts/memorygate_adapter.py` is a hand-rolled 264-line renderer for a 42-field canonical schema that already has a model in `LegiVellum/shared/legivellum/models.py` — the adapter imports `CanonicalReceipt` opportunistically (lines 11-25) and falls back to an unvalidated dict when the sibling checkout is missing. So in a container (no LegiVellum beside it) *nothing validates the payload before it is POSTed to ReceiptGate*; the schema contract tests only pass because CI checks out LegiVellum explicitly. Any other gate doing the same fallback has the same hole. The phase-mapping table (F-8) is exactly the sort of thing that belongs in the shared library once, not per-gate.
- **The lease canonical spec and the implementation disagree about who accepts.** `asyncgate.lease.md` is built on "Offers are transient. Acceptance creates obligation" — the worker emits `accepted` after deciding it can do the work, and "worker receives offer but can't accept: simply don't emit accepted receipt". AsyncGate instead mints `task.accepted` on the worker's behalf at claim time (`core.py:538`), so declining by silence is impossible and the ledger records acceptance by a worker that may immediately die. Also, the spec says lease grant/expiry are transient and not receipted; AsyncGate emits `lease.expired` receipts. Neither is unreasonable, but one of the two documents is wrong and integrators reading the canonical spec will build the wrong worker.
- **Duplicated MetaGate bootstrap loader.** `integrations/metagate_client.py:33-56` load-by-path shim is (by its own docstring) the fix for "four identical `parents[4]` IndexErrors across four repos". The same pattern is copy-pasted again in `receipts/memorygate_adapter.py:17-25` with slightly different semantics. That's the fifth copy — it belongs in one place.
- **Version skew signal:** AsyncGate self-reports `"version": "0.1.0"` from three hardcoded string literals (`core.py:1097`, `server.py:362`, `server.py:524`) plus `pyproject.toml`. The exit-criteria target tag is `asyncgate-v1.0.0`. Nothing derives the reported version from the package, so a tagged v1 will report 0.1.0 to every peer that calls `asyncgate.health`.

---

## What's solid

- **Termination semantics.** `models/termination.py` is the cleanest expression of the receipt protocol I've seen in this stack: an explicit static truth table, `TERMINAL_RECEIPT_TYPES` as a real set, every terminator query type-gated in SQL, and a docstring that correctly separates "type semantics" from "DB evidence". `task.result_ready` and `task.retry_scheduled` being deliberately non-terminal is exactly right, and `test_p13` proves ack/progress/anomaly can't close an obligation.
- **Lease-expiry-is-not-failure.** `requeue_on_expiry` not incrementing `attempt` (`repositories.py:305`) is the correct call, clearly reasoned in both code and `SYSTEM_BOUNDARY.md`, and it's the sort of thing most implementations get wrong.
- **Savepoint discipline.** Every multi-write engine operation is wrapped in `begin_nested()` with an accompanying test that mocks a mid-sequence failure and asserts rollback. The atomicity work (P0.2) is real.
- **The ReceiptGate client.** Circuit breaker + disk-persisted buffer with atomic `tmp → replace`, bounded retries with exponential backoff, replay worker, and buffer/circuit stats exposed through `get_config` — with tests for persistence across restart. This is production-grade degradation behaviour.
- **CI.** Migration smoke from an empty DB, `pip check`, compile, ruff, mypy, coverage gate, docker build, and a deliberate checkout of LegiVellum so the canonical schema contract tests run instead of skipping. The comment explaining *why* (`ci.yml:36`) is the kind of thing that keeps a test suite honest.
- **Comments that explain the bug they fixed.** `core.py:1262`, `memorygate_adapter.py` module docstring, `metagate_client.py` docstring, `test_receipt_schema_contract.py` docstring. These are worth more than most documentation and I'd keep writing them.
