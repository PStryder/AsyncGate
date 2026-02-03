Gate v1 Exit Criteria

Component: AsyncGate
Repo: https://github.com/PStryder/AsyncGate
Owner: Technomancy Labs
Target tag: asyncgate-v1.0.0
Date locked: 2026-02-03

Definition of Done

1) Build & Run

- [x] One-command local run exists (`run_local.sh`, `run_local.ps1`).
- [x] Cold start succeeds (import + startup path).
- [x] Health endpoint returns OK (`asyncgate.health` MCP tool).
- [x] Config documented (README + `.env.example`).
- [ ] Container build verified (`docker build -t asyncgate .`).

Artifacts:
- Run instructions: `AsyncGate/README.md`
- Example env: `AsyncGate/.env.example`

2) API & Contract Stability

- [x] MCP tool surface is the v1 contract (`/mcp`, tools/list + tools/call).
- [x] Request/response schemas are stable and in code (`src/asyncgate/mcp/server.py`).
- [x] Error model is JSON-RPC error envelope (tool call errors surfaced via MCP).
- [x] REST endpoints removed; MCP-only.

Notes on v1 contract limitations:
- AsyncGate is single-tenant in v1 (tenant_id required, no cross-tenant routing).
- Receipt storage is local unless ReceiptGate integration enabled.

3) Canonical Principals (String IDs)

- [x] `SYSTEM_PRINCIPAL_ID = "sys:legivellum"`
- [x] `SERVICE_PRINCIPAL_ID = "svc:asyncgate"`
- [x] Ownership rules enforced in `AsyncGateEngine._resolve_obligation_owner`.

4) Receipt Model Invariants

- [x] Receipt types enumerated (`src/asyncgate/models/enums.py`).
- [x] Terminal receipt types defined (`TERMINAL_RECEIPT_TYPES` in `src/asyncgate/models/termination.py`).
- [x] Terminator detection type-gated (queries filter to terminal types).
- [x] Terminal outcomes exist: completed, failed, canceled.

5) Persistence & Migration

- [x] Database schema tracked via Alembic (`alembic/`).
- [x] Migrations runnable from empty DB (`alembic upgrade head`).
- [ ] Container build/tested in this pass.

DB notes:
- Storage engine: PostgreSQL
- Migration tool: Alembic

6) Core Behavioral Guarantees (Standalone)

Golden path:
create_task → lease_next → report_progress → complete → terminal receipt → closed obligation.

- [x] Golden path demo script exists (`scripts/golden_path.py`).
- [x] Receipts enforce locatability via artifacts for completions.

7) Test Requirements

- [x] Unit tests cover dedupe + termination invariants.
- [x] Regression tests present for scary bits:
  - cancel emits terminal receipt and closes obligation (`tests/test_p13_termination_invariants.py`)
  - ack/progress/anomaly does not close obligation (`tests/test_p13_termination_invariants.py`)
  - lease claim/renew/expire path works (`tests/test_p11_lease_renewal_limits.py`)
  - dedupe behavior verified (`tests/test_p05_hash_parents.py`)
  - terminator logic closes only on terminal receipt types (`tests/test_p13_termination_invariants.py`)

Test command:
`pytest tests/ -v`

8) Observability & Debuggability

- [x] Logs include correlation keys (receipt_id, lease_id, obligation_id) in `AsyncGateEngine._emit_receipt`.
- [x] Query path exists: `asyncgate.list_receipts` + `asyncgate.list_receipts_ledger`.
- [x] Failure modes logged (receipt emit failures, lease expiry exceptions).

9) v1 Lock Rules

Frozen at tag:
- Receipt types and terminal semantics
- Principal conventions and ownership rules
- MCP tool schemas (public contract)
- DB schema without migration plan

10) Open Issues / Deferred Work

- [ ] Validate container build in CI or locally.
- [ ] Create v1.0.0 tag after final sign-off.

Sign-off

- Owner sign-off: pending
- Integration readiness confirmed: pending
- Tag created: pending
