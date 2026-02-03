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

## API

### REST Endpoints

#### Health & Config

- `GET /v1/health` - Health check endpoint
- `GET /v1/config` - Get server configuration

#### Obligations (Canonical Bootstrap)

- `GET /v1/obligations/open` - Get open obligations for a principal (ledger dump, no bucketing)

#### TASKER (Agent) Endpoints

- `GET /v1/bootstrap` - **DEPRECATED**: Use `/v1/obligations/open` instead
- `POST /v1/tasks` - Create a new task
- `GET /v1/tasks/{task_id}` - Get task by ID
- `GET /v1/tasks` - List tasks
- `POST /v1/tasks/{task_id}/cancel` - Cancel a task
- `GET /v1/receipts` - List receipts for a principal
- `POST /v1/receipts/{receipt_id}/ack` - Acknowledge a receipt

#### TASKEE (Worker) Endpoints

- `POST /v1/leases/claim` - Claim next available tasks
- `POST /v1/leases/renew` - Renew a lease
- `POST /v1/tasks/{task_id}/progress` - Report progress
- `POST /v1/tasks/{task_id}/complete` - Mark task completed
- `POST /v1/tasks/{task_id}/fail` - Mark task failed

### MCP Tools

TASKER tools:
- `asyncgate.bootstrap`
- `asyncgate.create_task`
- `asyncgate.get_task`
- `asyncgate.list_tasks`
- `asyncgate.cancel_task`
- `asyncgate.list_receipts`
- `asyncgate.ack_receipt`

TASKEE tools:
- `asyncgate.lease_next`
- `asyncgate.renew_lease`
- `asyncgate.report_progress`
- `asyncgate.complete`
- `asyncgate.fail`

System:
- `asyncgate.get_config`

## Configuration

Environment variables (prefix `ASYNCGATE_`):

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | postgresql+asyncpg://... | PostgreSQL connection URL |
| `REDIS_URL` | - | Redis URL for rate limiting |
| `ENV` | development | Environment (development/staging/production) |
| `INSTANCE_ID` | asyncgate-1 | Instance identifier |
| `LOG_LEVEL` | INFO | Logging level |
| `DEBUG` | false | Debug mode |
| `DEFAULT_LEASE_TTL_SECONDS` | 120 | Default lease TTL |
| `MAX_LEASE_TTL_SECONDS` | 1800 | Maximum lease TTL (30 min) |
| `MAX_LEASE_RENEWALS` | 10 | Maximum lease renewals before forced release |
| `MAX_LEASE_LIFETIME_SECONDS` | 7200 | Absolute max lease lifetime (2 hours) |
| `DEFAULT_MAX_ATTEMPTS` | 2 | Default max retry attempts |
| `DEFAULT_RETRY_BACKOFF_SECONDS` | 15 | Default retry backoff |
| `RECEIPT_MODE` | standalone | Receipt storage mode (standalone/receiptgate_integrated) |
| `RECEIPTGATE_ENDPOINT` | - | ReceiptGate MCP endpoint URL (accepts RECEIPTGATE_URL alias) |
| `RECEIPTGATE_AUTH_TOKEN` | - | ReceiptGate auth token/API key (accepts RECEIPTGATE_API_KEY alias) |
| `RECEIPTGATE_TENANT_ID` | - | Tenant ID to stamp in ReceiptGate receipts |
| `RECEIPTGATE_EMISSION_TIMEOUT_MS` | 500 | ReceiptGate request timeout (ms) |
| `RECEIPTGATE_EMISSION_MAX_RETRIES` | 10 | ReceiptGate retry count |
| `RECEIPTGATE_CIRCUIT_BREAKER_ENABLED` | true | Enable circuit breaker |
| `ESCALATION_ENABLED` | false | Emit escalation receipts |
| `ESCALATION_TARGETS` | - | JSON array of escalation targets |
| `ESCALATION_LEASE_EXPIRY_CLASS` | 1 | Escalation class for lease expiry |
| `API_KEY` | - | API key for authentication |
| `ALLOW_INSECURE_DEV` | false | Allow unauthenticated in dev mode |
| `RATE_LIMIT_ENABLED` | true | Enable rate limiting |
| `RATE_LIMIT_BACKEND` | memory | Rate limit backend (memory/redis) |
| `RATE_LIMIT_DEFAULT_CALLS` | 100 | Default calls per window |
| `RATE_LIMIT_DEFAULT_WINDOW_SECONDS` | 60 | Rate limit window size |

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

## License

MIT
