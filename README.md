# AsyncGate

Durable, lease-based asynchronous task execution MCP server.

## Overview

AsyncGate is a standalone MCP server providing durable, lease-based asynchronous execution for agents. It solves: "delegate work without blocking, and reliably recover results later."

AsyncGate does **not** plan, reason, schedule, or orchestrate strategy. It stores work, leases it, and records outcomes.

## Architecture

### Roles

- **Agent (TASKER)**: Creates tasks, fetches status/results
- **AsyncGate Server**: Source of truth for task state, leases, results, audit trail
- **Worker Services (TASKEEs)**: External services that claim and execute tasks

### Core Concepts

- **Tasks**: Units of work with type, payload, requirements, and lifecycle state
- **Leases**: Time-bounded exclusive claims on tasks by workers
- **Receipts**: Immutable contract records for audit and coordination

### Task Schema Notes (LegiVellum)

- `principal_ai` is required on task creation and defines the obligation owner for receipt routing.
- `payload_pointer` is preferred for non-trivial payloads; `payload` is legacy inline data.

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 15+
- Docker (for deployment)

### Local Development

```bash
# Install dependencies
pip install -e ".[dev]"

# Set up environment
export ASYNCGATE_DATABASE_URL="postgresql+asyncpg://asyncgate:asyncgate@localhost:5432/asyncgate"

# Run migrations
alembic upgrade head

# Start server
asyncgate
```

### One-Command Local Run (Docker Compose)

```bash
./run_local.sh
# Windows PowerShell: .\run_local.ps1
```

### Docker

```bash
# Build image
docker build -t asyncgate .

# Run container
docker run -p 8080:8080 \
  -e ASYNCGATE_DATABASE_URL="postgresql+asyncpg://..." \
  asyncgate
```

## Deployment

AsyncGate supports three deployment methods:

### Fly.io (Recommended for Production)
```bash
./deploy-fly.sh
```

See [Fly Operations Guide](docs/FLY_OPERATIONS.md) for details.

### Kubernetes
```bash
# Create secrets
kubectl create secret generic asyncgate-secrets \
  --from-literal=ASYNCGATE_API_KEY=your-key \
  --from-literal=ASYNCGATE_DATABASE_URL=postgresql://... \
  -n asyncgate

# Deploy
kubectl apply -k k8s/overlays/prod
```

See [k8s/README.md](k8s/README.md) for details.

### Docker Compose (Development)
```bash
docker-compose up --build
```

## Golden Path Demo

```bash
python scripts/golden_path.py
```

## Tests

```bash
pytest tests/ -v
```

## MCP Interface

AsyncGate exposes MCP over HTTP at `/mcp` with JSON-RPC methods `tools/list` and `tools/call`.

### MCP Tools

TASKER tools:
- `asyncgate.bootstrap`
- `asyncgate.create_task`
- `asyncgate.get_task`
- `asyncgate.list_tasks`
- `asyncgate.cancel_task`
- `asyncgate.list_receipts`
- `asyncgate.list_receipts_ledger`
- `asyncgate.ack_receipt`

TASKEE tools:
- `asyncgate.lease_next`
- `asyncgate.renew_lease`
- `asyncgate.report_progress`
- `asyncgate.complete`
- `asyncgate.fail`

System:
- `asyncgate.get_config`
- `asyncgate.health`
- `asyncgate.check_terminator` — check for termination evidence on a receipt.
  Takes `parent_receipt_id` and `tenant_id`, and answers whether the obligation
  that receipt opened has been closed, so a caller can tell an outstanding
  obligation from one whose closure it simply has not seen.

That is the full set reported by `tools/list`.

## Configuration

Environment variables (prefix `ASYNCGATE_`). Generated from the `Settings`
class; MetaGate bootstrap variables are documented in their own section below.

`ASYNCGATE_API_KEY` is **required** outside the `development` environment; startup fails without it. `ASYNCGATE_ALLOW_INSECURE_DEV=true` disables auth checks and is for local development only.

See `.env.example` for a working starting point.

### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `ASYNCGATE_DEBUG` | `false` | Enable debug mode |
| `ASYNCGATE_ENV` | `development` | Deployment environment: `development`, `staging` or `production` |
| `ASYNCGATE_HOST` | `0.0.0.0` | Bind address |
| `ASYNCGATE_INSTANCE_ID` | `asyncgate-1` | Unique instance identifier (auto-detected at startup if not set) |
| `ASYNCGATE_LOG_LEVEL` | `INFO` | Logging verbosity |
| `ASYNCGATE_PORT` | `8080` | Bind port |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `ASYNCGATE_DATABASE_URL` | `postgresql+asyncpg://asyncgate:asyncgate@localhost:5432/asyncgate` | PostgreSQL connection string |
| `ASYNCGATE_REDIS_URL` | *(unset)* | Redis connection used for distributed rate limiting; an in-process limiter is used when unset |

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `ASYNCGATE_ALLOW_INSECURE_DEV` | `false` | Allow unauthenticated in dev |
| `ASYNCGATE_API_KEY` | *(unset)* | API key for MCP requests |
| `ASYNCGATE_JWT_ACCESS_TOKEN_TTL_DAYS` | `30` | Access token lifetime |
| `ASYNCGATE_JWT_ALGORITHM` | `RS256` | JWT signing algorithm |
| `ASYNCGATE_JWT_PRIVATE_KEY_PATH` | *(unset)* | Path to the JWT signing key |
| `ASYNCGATE_JWT_PUBLIC_KEY_PATH` | *(unset)* | Path to the JWT verification key |
| `ASYNCGATE_JWT_REFRESH_TOKEN_TTL_DAYS` | `90` | Refresh token lifetime |

### Upstream services

| Variable | Default | Description |
|----------|---------|-------------|
| `ASYNCGATE_RECEIPT_MODE` | `standalone` | `standalone` keeps receipts locally; `receiptgate_integrated` emits them to ReceiptGate |
| `ASYNCGATE_RECEIPTGATE_AUTH_TOKEN` | *(unset)* | Auth token presented to ReceiptGate. Also accepts `ASYNCGATE_RECEIPTGATE_API_KEY`, `RECEIPTGATE_AUTH_TOKEN`, `RECEIPTGATE_API_KEY` |
| `ASYNCGATE_RECEIPTGATE_CIRCUIT_BREAKER_ENABLED` | `true` | Enable circuit breaker for ReceiptGate calls |
| `ASYNCGATE_RECEIPTGATE_CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | Failures before opening circuit |
| `ASYNCGATE_RECEIPTGATE_CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS` | `3` | Test calls in half-open state |
| `ASYNCGATE_RECEIPTGATE_CIRCUIT_BREAKER_SUCCESS_THRESHOLD` | `2` | Successes to close from half-open |
| `ASYNCGATE_RECEIPTGATE_CIRCUIT_BREAKER_TIMEOUT_SECONDS` | `60` | Seconds before attempting half-open |
| `ASYNCGATE_RECEIPTGATE_EMISSION_BUFFER_PATH` | `.asyncgate/receiptgate_emission_buffer.json` | Where the buffer is persisted, so buffered receipts survive a restart |
| `ASYNCGATE_RECEIPTGATE_EMISSION_BUFFER_SIZE` | `10000` | Maximum receipts held while ReceiptGate is unreachable |
| `ASYNCGATE_RECEIPTGATE_EMISSION_MAX_RETRIES` | `10` | Replay attempts before a buffered receipt is given up on |
| `ASYNCGATE_RECEIPTGATE_EMISSION_RETRY_INTERVAL_SECONDS` | `30` | How often buffered receipts are replayed |
| `ASYNCGATE_RECEIPTGATE_EMISSION_TIMEOUT_MS` | `500` | Per-emission timeout |
| `ASYNCGATE_RECEIPTGATE_ENDPOINT` | *(unset)* | ReceiptGate MCP endpoint. Only used when `receipt_mode=receiptgate_integrated`. Also accepts `ASYNCGATE_RECEIPTGATE_URL`, `RECEIPTGATE_ENDPOINT`, `RECEIPTGATE_URL` |
| `ASYNCGATE_RECEIPTGATE_TENANT_ID` | *(unset)* | Tenant for receipt writes |

### Rate limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `ASYNCGATE_RATE_LIMIT_BACKEND` | `memory` | Rate limit backend: memory or redis |
| `ASYNCGATE_RATE_LIMIT_DEFAULT_CALLS` | `100` | Default calls per window |
| `ASYNCGATE_RATE_LIMIT_DEFAULT_WINDOW_SECONDS` | `60` | Default window size in seconds |
| `ASYNCGATE_RATE_LIMIT_ENABLED` | `true` | Enable rate limiting |

### CORS

| Variable | Default | Description |
|----------|---------|-------------|
| `ASYNCGATE_CORS_ALLOW_CREDENTIALS` | `true` | Allow credentials in CORS requests |
| `ASYNCGATE_CORS_ALLOWED_HEADERS` | `['Authorization', 'Content-Type', 'X-Tenant-ID', 'X-Trace-ID', 'X-Request-ID']` | Allowed request headers |
| `ASYNCGATE_CORS_ALLOWED_METHODS` | `['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS']` | Allowed HTTP methods |
| `ASYNCGATE_CORS_ALLOWED_ORIGINS` | `['http://localhost:3000', 'http://localhost:8080']` | Allowed CORS origins (explicit allowlist for security) |

### Behaviour and limits

| Variable | Default | Description |
|----------|---------|-------------|
| `ASYNCGATE_DEFAULT_BOOTSTRAP_MAX_ITEMS` | `50` | Default bootstrap items |
| `ASYNCGATE_DEFAULT_LEASE_TTL_SECONDS` | `120` | Default lease TTL (2 min) |
| `ASYNCGATE_DEFAULT_LIST_LIMIT` | `50` | Default list limit |
| `ASYNCGATE_DEFAULT_MAX_ATTEMPTS` | `2` | Default max task attempts |
| `ASYNCGATE_DEFAULT_PRIORITY` | `0` | Default task priority |
| `ASYNCGATE_DEFAULT_RETRY_BACKOFF_SECONDS` | `15` | Default retry backoff |
| `ASYNCGATE_ESCALATION_ENABLED` | `false` | Enable escalation receipts |
| `ASYNCGATE_ESCALATION_LEASE_EXPIRY_CLASS` | `1` | Escalation class to use for lease expiry events |
| `ASYNCGATE_ESCALATION_TARGETS` | *(empty)* | Escalation targets keyed by class |
| `ASYNCGATE_LEASE_GRACE_SECONDS` | `0` | Lease grace period |
| `ASYNCGATE_LEASE_SWEEP_INTERVAL_SECONDS` | `5` | Lease sweep cadence |
| `ASYNCGATE_MAX_BOOTSTRAP_MAX_ITEMS` | `200` | Max bootstrap items |
| `ASYNCGATE_MAX_LEASE_LIFETIME_SECONDS` | `7200` | Absolute maximum lifetime for a lease (acquired_at to now) |
| `ASYNCGATE_MAX_LEASE_RENEWALS` | `10` | Maximum times a lease can be renewed before forcing release |
| `ASYNCGATE_MAX_LEASE_TTL_SECONDS` | `1800` | Max lease TTL (30 min) |
| `ASYNCGATE_MAX_LIST_LIMIT` | `200` | Max list limit |
| `ASYNCGATE_MAX_RETRY_BACKOFF_SECONDS` | `900` | Max retry backoff (15 min) |
| `ASYNCGATE_RECEIPT_RETENTION_DAYS` | `30` | Active receipt retention |
| `ASYNCGATE_TASK_RETENTION_DAYS` | `7` | Terminal task retention |

## Task Lifecycle

States: `queued`, `running`, `leased`, `succeeded`, `failed`, `canceled`

```
queued -> running -> leased -> succeeded
                            \-> failed -> queued (retry)
                             \-> canceled
```

### State Transitions

- `queued -> running`: Worker picks up task
- `running -> leased`: Task claimed with lease
- `leased -> succeeded`: Task completes successfully
- `leased -> failed`: Task fails (may retry)
- `queued/leased -> canceled`: Task canceled
- `failed -> queued`: Retry with backoff (if attempts remaining)
- `leased -> queued`: Lease expires (system-driven)

Terminal states: `succeeded`, `failed`, `canceled`

## Invariants

1. At most one active lease per task
2. Lease enforcement: mutations require matching lease_id + worker_id
3. Lease expiry: expired leases allow task to be reclaimed
4. Idempotent creation: same idempotency_key returns same task_id
5. Terminal states are immutable
6. State machine is authoritative; receipts are proofs

## Receipt Types & Termination

Receipt types are enumerated in `src/asyncgate/models/enums.py` (`ReceiptType`).
Termination rules and `TERMINAL_RECEIPT_TYPES` live in
`src/asyncgate/models/termination.py`.

Terminal receipt types for task obligations:

- `task.completed`
- `task.failed`
- `task.canceled`

Terminator detection is type-gated: only terminal receipt types close obligations.

## MetaGate Bootstrap

On startup this gate asks MetaGate for the topology it belongs to and fills in
endpoints the operator did not configure. It resolves: `receiptgate` → `receiptgate_endpoint`.

| Variable | Default | Meaning |
|----------|---------|---------|
| `ASYNCGATE_METAGATE_ENDPOINT` | *(unset)* | MetaGate MCP endpoint. Unset disables bootstrap; the gate starts on configured values alone. |
| `ASYNCGATE_METAGATE_API_KEY` | *(unset)* | Credential presented to MetaGate |
| `ASYNCGATE_METAGATE_COMPONENT_KEY` | `asyncgate` | Which component in the manifest this process is |
| `ASYNCGATE_METAGATE_BOOTSTRAP_TIMEOUT_SECONDS` | `5.0` | Per-call timeout |

Bootstrap never prevents startup. Every failure — unreachable, timeout, auth
rejected, no binding, malformed packet — degrades to a logged warning and
"carry on with configured values", because a bootstrap authority that can take
the mesh down would be a hidden master. Explicit configuration always wins;
bootstrap fills gaps and logs when the mesh disagrees rather than overriding.

See `LegiVellum/docs/canonical/metagate.bootstrap.md` for the full contract.

## License

MIT
