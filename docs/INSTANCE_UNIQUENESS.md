# Instance ID Uniqueness and Multi-Instance Safety

## Problem

AsyncGate's multi-instance lease safety depends on each instance having a **unique `instance_id`**. If two instances share the same ID, they will:

1. Both sweep the same lease partition
2. Cause duplicate task requeuing
3. Create race conditions on lease operations
4. Risk data corruption and lost work

**The Footgun:**
```bash
# DEFAULT (DANGEROUS IN PRODUCTION)
ASYNCGATE_INSTANCE_ID="asyncgate-1"  # Shared by all instances 🔥
```

With the default ID, all replicas think they own the same partition, breaking isolation completely.

## Solution: Auto-Detection + Validation

AsyncGate now **auto-detects** unique instance identifiers from deployment platforms and **validates** at startup to prevent conflicts.

### 1. Auto-Detection (Priority Order)

When `instance_id` is set to the default (`"asyncgate-1"`), AsyncGate automatically detects a unique ID from the environment:

```
Priority 1: Fly.io
  └─ FLY_ALLOC_ID (e.g., "01j9k2m3n4p5q6r7")

Priority 2: Kubernetes
  └─ HOSTNAME (e.g., "asyncgate-deployment-7d8f9b-xyz12")

Priority 3: AWS ECS
  └─ ECS_CONTAINER_METADATA_URI_V4 → "ecs-{container_id}"

Priority 4: Google Cloud Run
  └─ K_REVISION → "{revision}-{random}"

Priority 5: Explicit Override
  └─ ASYNCGATE_INSTANCE_ID (if not default)

Priority 6: Fallback
  └─ {hostname}-{random8}
```

**Example Fly.io Detection:**
```bash
# No configuration needed - automatically uses allocation ID
# FLY_ALLOC_ID=01j9k2m3n4p5q6r7

$ asyncgate start
INFO: Auto-detected instance ID: 01j9k2m3n4p5q6r7
INFO: Instance ID validated: 01j9k2m3n4p5q6r7 (env: production)
```

### 2. Startup Validation

Before accepting any requests, AsyncGate validates the instance ID is safe:

**Rejected in Production/Staging:**
- `asyncgate-1` (the default)
- `localhost`
- `127.0.0.1`
- Any ID shorter than 8 characters (warning)

**Behavior:**
```bash
# Production with default ID
$ ASYNCGATE_ENV=production asyncgate start
RuntimeError: INSTANCE ID CONFLICT RISK: instance_id='asyncgate-1' is not
safe for production environment. Multiple instances could share the same ID,
causing lease conflicts and data corruption.
```

## Deployment Patterns

### Fly.io (Recommended)
```bash
# fly.toml
[env]
  ASYNCGATE_ENV = "production"
  ASYNCGATE_API_KEY = "your-secure-key"

# FLY_ALLOC_ID automatically injected
# Each replica gets unique allocation ID
```

**Result:**
```
Instance 1: 01j9k2m3n4p5q6r7
Instance 2: 01j9k2m3n4p5q6r8
Instance 3: 01j9k2m3n4p5q6r9
```

### Kubernetes
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: asyncgate
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: asyncgate
        env:
        - name: ASYNCGATE_ENV
          value: "production"
        - name: ASYNCGATE_API_KEY
          valueFrom:
            secretKeyRef:
              name: asyncgate-secrets
              key: api-key
        # HOSTNAME automatically set by K8s
```

**Result:**
```
asyncgate-deployment-7d8f9b-abc12
asyncgate-deployment-7d8f9b-def34
asyncgate-deployment-7d8f9b-ghi56
```

### AWS ECS
```json
{
  "containerDefinitions": [{
    "name": "asyncgate",
    "environment": [
      { "name": "ASYNCGATE_ENV", "value": "production" },
      { "name": "ASYNCGATE_API_KEY", "value": "your-key" }
    ]
  }]
}
```

**Result:**
```
ecs-a1b2c3d4e5f6
ecs-f6e5d4c3b2a1
ecs-1a2b3c4d5e6f
```

### Manual Configuration
For platforms without auto-detection:

```bash
# Generate unique ID at deploy time
export INSTANCE_ID=$(uuidgen | cut -d'-' -f1)

# Or use hostname + random
export INSTANCE_ID="$(hostname)-$(openssl rand -hex 4)"

# Pass to container
docker run -e ASYNCGATE_INSTANCE_ID="$INSTANCE_ID" asyncgate
```

### Local Development
```bash
# Development mode allows default ID
ASYNCGATE_ENV=development
ASYNCGATE_ALLOW_INSECURE_DEV=true
# Uses default: "asyncgate-1" (single instance)
```

## Verification

**Check Instance ID at Runtime:**
```bash
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"asyncgate.get_config","arguments":{"tenant_id":"TENANT_ID"}}}'
```

```json
{
  "instance_id": "01j9k2m3n4p5q6r7",
  "env": "production",
  "version": "0.1.0"
}
```

**Check Logs at Startup:**
```
INFO: Auto-detected instance ID: 01j9k2m3n4p5q6r7
INFO: Instance ID validated: 01j9k2m3n4p5q6r7 (env: production)
INFO: Environment: production
```

## Safety Guarantees

### ✓ Automatic Detection
Fly.io, K8s, ECS, Cloud Run all provide unique IDs automatically - no manual configuration required.

### ✓ Startup Validation
Server refuses to start if instance ID is unsafe for the environment.

### ✓ Fail Fast
Conflict detected at startup, not discovered later through data corruption.

### ✓ Clear Errors
Error messages explain the problem and provide solutions.

### ✓ Development Friendly
Default works fine for local single-instance development.

## Migration Guide

### Existing Deployments

**If you explicitly set `ASYNCGATE_INSTANCE_ID`:**
- No changes needed if values are unique per instance
- Verify uniqueness across all replicas

**If you relied on default `instance_id`:**
```bash
# BEFORE (all replicas had same ID)
ASYNCGATE_INSTANCE_ID=asyncgate-1  # 🔥 Conflict!

# AFTER (auto-detected or explicit)
# Option 1: Remove variable, let auto-detection work
unset ASYNCGATE_INSTANCE_ID

# Option 2: Generate unique ID per replica
ASYNCGATE_INSTANCE_ID=$(generate_unique_id_here)
```

### Testing Instance Safety

**Test 1: Multiple instances with same ID (should fail)**
```bash
# Terminal 1
ASYNCGATE_INSTANCE_ID=test-1 ASYNCGATE_ENV=production asyncgate start
# Should fail: "instance_id='test-1' is not safe for production"

# Terminal 2 (if first succeeded somehow)
ASYNCGATE_INSTANCE_ID=test-1 ASYNCGATE_ENV=production asyncgate start
# Both would conflict
```

**Test 2: Auto-detection (should succeed)**
```bash
# Let platform provide unique ID
ASYNCGATE_ENV=production ASYNCGATE_API_KEY=test asyncgate start
# Should auto-detect and start successfully
```

## Implementation Details

**Files Changed:**
- `src/asyncgate/instance.py`: Detection and validation logic
- `src/asyncgate/main.py`: Startup sequence with ID detection
- `src/asyncgate/config.py`: Updated instance_id documentation

**Detection Function:**
```python
def detect_instance_id() -> str:
    """Auto-detect unique ID from platform environment."""
    # Checks FLY_ALLOC_ID, HOSTNAME, ECS metadata, etc.
    # Falls back to hostname + random suffix
```

**Validation Function:**
```python
def validate_instance_uniqueness(instance_id: str, env: str) -> None:
    """Ensure instance_id is safe for environment."""
    # Rejects default/generic IDs in staging/production
    # Raises RuntimeError with clear guidance
```

## Related Security Features

This builds on AsyncGate's defense-in-depth approach:
- **Auth validation**: Prevents missing API keys (see `docs/SECURITY_HARDENING.md`)
- **Instance validation**: Prevents shared instance IDs (this document)
- **Sweep isolation**: Each instance only touches its own partition
- **Receipt deduplication**: Prevents duplicate processing

All four layers work together to ensure multi-instance safety.
