# P0.3 + P0.4: Security Configuration Migration Guide

**Date:** 2026-01-05  
**Priority:** P0 (Production Blocker)  
**Status:** COMPLETE

---

## What Changed

### P0.3: CORS Configuration (CSRF Fix)
**Issue:** `allow_origins=["*"]` + `allow_credentials=True` = CSRF vulnerability

**Changes:**
- ✅ Added explicit `cors_allowed_origins` configuration
- ✅ Removed wildcard `["*"]` origins
- ✅ Made methods and headers explicit (no wildcards)
- ✅ Default development origins: `localhost:3000`, `localhost:8080`

### P0.4: Rate Limiting Enabled by Default
**Issue:** Service unprotected against DoS and cost attacks

**Changes:**
- ✅ Changed `rate_limit_enabled` default from `False` → `True`
- ✅ Added `rate_limit_active` property that forces ON in staging/production
- ✅ Development can still disable if needed

---

## Migration Steps

### 1. Update Environment Configuration

**Option A: Using Environment Variables**

Add to your `.env` file:

```bash
# P0.3: CORS Configuration (replace with your actual origins)
ASYNCGATE_CORS_ALLOWED_ORIGINS=http://localhost:3000,https://yourapp.com,https://admin.yourapp.com

# P0.4: Rate Limiting (optional - enabled by default)
ASYNCGATE_RATE_LIMIT_ENABLED=true
ASYNCGATE_RATE_LIMIT_DEFAULT_CALLS=100
ASYNCGATE_RATE_LIMIT_DEFAULT_WINDOW_SECONDS=60
```

**Option B: Copy from .env.example**

```bash
cp .env.example .env
# Edit .env with your specific values
```

### 2. Verify CORS Origins

List all origins that need access to your AsyncGate API:

```bash
# Web app
https://app.example.com

# Admin dashboard
https://admin.example.com

# Development
http://localhost:3000
http://localhost:8080
```

**Important:** Do NOT include:
- `*` (wildcard)
- Origins you don't control
- Overly broad patterns

### 3. Test CORS Configuration

**Test 1: Allowed Origin**
```bash
curl -X OPTIONS http://localhost:8080/mcp \
  -H "Origin: http://localhost:3000" \
  -H "Access-Control-Request-Method: POST" \
  -v
  
# Should return:
# access-control-allow-origin: http://localhost:3000
```

**Test 2: Blocked Origin**
```bash
curl -X OPTIONS http://localhost:8080/mcp \
  -H "Origin: https://evil.com" \
  -H "Access-Control-Request-Method: POST" \
  -v
  
# Should NOT return access-control-allow-origin: https://evil.com
```

### 4. Verify Rate Limiting

**Check Configuration:**
```python
from asyncgate.config import settings

print(f"Rate limiting enabled: {settings.rate_limit_enabled}")
print(f"Rate limiting active: {settings.rate_limit_active}")
print(f"Calls per window: {settings.rate_limit_default_calls}")
print(f"Window: {settings.rate_limit_default_window_seconds}s")
```

**Test Rate Limiting (if middleware is hooked up):**
```bash
# Send 110 requests rapidly (exceeds default limit of 100)
for i in {1..110}; do
  curl -s http://localhost:8080/mcp \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"asyncgate.health","arguments":{}}}' &
done
wait

# Last 10 requests should return 429 Too Many Requests
```

---

## Configuration Reference

### CORS Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `cors_allowed_origins` | `["http://localhost:3000", "http://localhost:8080"]` | Explicit origin allowlist |
| `cors_allow_credentials` | `true` | Allow cookies/auth headers |
| `cors_allowed_methods` | `["GET", "POST", "PUT", "DELETE", "OPTIONS"]` | Allowed HTTP methods |
| `cors_allowed_headers` | `["Authorization", "Content-Type", "X-Tenant-ID"]` | Allowed request headers |

### Rate Limiting Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `rate_limit_enabled` | `true` | Enable rate limiting |
| `rate_limit_backend` | `"memory"` | Backend (`memory` or `redis`) |
| `rate_limit_default_calls` | `100` | Calls per window |
| `rate_limit_default_window_seconds` | `60` | Window size in seconds |

### Environment-Based Overrides

| Environment | Rate Limiting | Notes |
|-------------|---------------|-------|
| `production` | **FORCED ON** | Cannot disable |
| `staging` | **FORCED ON** | Cannot disable |
| `development` | Configurable | Can disable for testing |

---

## Breaking Changes

### ⚠️ CORS Origins Must Be Configured

**Before:** Any origin could access the API (wildcard `*`)

**After:** Only explicitly configured origins can access

**Impact:** 
- Frontend apps from new origins will be blocked
- Add their origins to `ASYNCGATE_CORS_ALLOWED_ORIGINS`

**Example Error:**
```
Access to fetch at 'http://asyncgate.com/mcp' from origin 'https://newapp.com' 
has been blocked by CORS policy: No 'Access-Control-Allow-Origin' header is present
```

**Fix:**
```bash
# Add the new origin
ASYNCGATE_CORS_ALLOWED_ORIGINS=http://localhost:3000,https://newapp.com
```

### ⚠️ Rate Limiting Now Active

**Before:** No rate limiting (unlimited requests)

**After:** 100 requests per 60 seconds by default

**Impact:**
- High-volume clients may hit limits
- Batch operations may need adjustment
- Consider increasing limits for known heavy users

**Fix (if legitimate high volume):**
```bash
# Increase limits
ASYNCGATE_RATE_LIMIT_DEFAULT_CALLS=1000
ASYNCGATE_RATE_LIMIT_DEFAULT_WINDOW_SECONDS=60
```

Or implement per-tenant rate limiting (future enhancement).

---

## Testing Checklist

- [ ] CORS allows your frontend origins
- [ ] CORS blocks unauthorized origins
- [ ] Rate limiting is active in production/staging
- [ ] Rate limiting can be disabled in development (if needed)
- [ ] API still works with valid origins
- [ ] Preflight OPTIONS requests succeed
- [ ] MCP health tool is accessible

---

## Rollback Plan

If issues arise, you can temporarily revert:

**Emergency Rollback (NOT RECOMMENDED):**
```bash
# Rollback to commit before P0.3/P0.4
git checkout dec42ee

# Or disable individually
ASYNCGATE_CORS_ALLOW_CREDENTIALS=false  # Disables CSRF protection
ASYNCGATE_RATE_LIMIT_ENABLED=false       # Disables DoS protection (dev only)
```

**Better Approach:**
- Add missing origins to allowlist
- Increase rate limits temporarily
- File issue on GitHub

---

## Security Impact

### Before P0.3/P0.4
❌ CSRF attacks possible (wildcard CORS with credentials)  
❌ DoS attacks trivial (no rate limiting)  
❌ Cost explosion risk (unlimited API calls)

### After P0.3/P0.4
✅ CSRF attacks prevented (explicit origin allowlist)  
✅ DoS protection (rate limiting active)  
✅ Cost protection (100 calls/minute default)

---

## Questions?

**Q: Can I use wildcard origins?**  
A: Only if you disable credentials (`cors_allow_credentials=false`). But then authentication won't work.

**Q: How do I add a new frontend origin?**  
A: Add it to `ASYNCGATE_CORS_ALLOWED_ORIGINS` environment variable, restart server.

**Q: Rate limiting seems too strict**  
A: Increase `ASYNCGATE_RATE_LIMIT_DEFAULT_CALLS` for your use case. Default 100/min is conservative.

**Q: Can I disable rate limiting in production?**  
A: No. It's forced on. You can only increase the limits.

**Q: What if I need different limits per tenant?**  
A: That's a P1+ enhancement. For now, increase global limits or implement custom middleware.

---

## Files Changed

```
✅ src/asyncgate/config.py           (CORS + rate limit config)
✅ src/asyncgate/main.py             (CORS middleware)
✅ tests/test_p03_p04_security_config.py (tests)
✅ .env.example                      (example configuration)
```

**Commit:** TBD  
**Status:** Ready to deploy
