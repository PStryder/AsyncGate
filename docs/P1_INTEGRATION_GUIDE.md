# P1.2 & P1.3 Integration Guide

## P1.2: Circuit Breaker for MemoryGate

### Overview
Protects AsyncGate from MemoryGate failures through circuit breaker pattern with automatic fallback to local buffer.

### Configuration (config.py)
```python
# Enable/disable circuit breaker
memorygate_circuit_breaker_enabled: bool = True

# Circuit opens after 5 consecutive failures
memorygate_circuit_breaker_failure_threshold: int = 5

# Wait 60s before attempting recovery
memorygate_circuit_breaker_timeout_seconds: int = 60

# Test with 3 calls in half-open state
memorygate_circuit_breaker_half_open_max_calls: int = 3

# Close after 2 consecutive successes
memorygate_circuit_breaker_success_threshold: int = 2
```

### Usage
```python
from asyncgate.integrations import get_memorygate_client

# Get singleton client (automatically configured)
client = get_memorygate_client()

# Emit receipt (protected by circuit breaker if enabled)
result = await client.emit_receipt(
    tenant_id=tenant_id,
    receipt_type="task.assigned",
    from_principal={"kind": "agent", "id": "agent-123"},
    to_principal={"kind": "system", "id": "asyncgate"},
    task_id=task_id,
    body={"instructions": "..."}
)

# Check circuit status
stats = client.get_circuit_stats()
print(stats["state"])  # closed, open, or half_open

# Manual reset if needed
await client.reset_circuit()
```

### States
- **CLOSED**: Normal operation, all requests pass through
- **OPEN**: Circuit broken, requests fail fast with fallback
- **HALF_OPEN**: Testing recovery with limited probe requests

### Behavior
1. **Failures accumulate**: Each MemoryGate call failure increments counter
2. **Circuit opens**: After threshold failures, circuit opens
3. **Fallback engaged**: Requests buffer locally for background retry
4. **Recovery attempt**: After timeout, circuit enters half-open
5. **Probe requests**: Limited test calls attempt service recovery
6. **Circuit closes**: After success threshold, normal operation resumes

---

## P1.3: Rate Limiting

### Overview
Configurable rate limiting with pluggable backends (in-memory for dev, Redis for production).

### Configuration (config.py)
```python
# Enable/disable rate limiting
rate_limit_enabled: bool = False

# Backend: "memory" or "redis"
rate_limit_backend: str = "memory"

# Default limits (if no endpoint-specific config)
rate_limit_default_calls: int = 100
rate_limit_default_window_seconds: int = 60

# Redis URL (required if backend = "redis")
redis_url: Optional[str] = "redis://localhost:6379"
```

### Usage - Global Rate Limiting
Add dependency to router:

```python
from fastapi import Depends
from asyncgate.middleware import rate_limit_dependency

router = APIRouter(
    prefix="/mcp",
    dependencies=[
        Depends(verify_api_key),
        Depends(rate_limit_dependency),  # Add this
    ]
)
```

### Usage - Per-Endpoint Configuration
```python
from asyncgate.middleware import get_rate_limiter

# Configure specific endpoints
limiter = get_rate_limiter()
limiter.configure_endpoint(
    path="/mcp",
    calls=50,          # 50 calls
    window_seconds=60, # per minute
    key_prefix="create-task:"
)
```

### Module Structure
```
asyncgate/
├── integrations/
│   ├── circuit_breaker.py      # Generic circuit breaker
│   └── memorygate_client.py    # MemoryGate with circuit breaker
└── middleware/
    └── rate_limit.py            # Rate limiting with backends
```
