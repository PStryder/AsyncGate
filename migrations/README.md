# AsyncGate Database Migrations

Manual SQL migrations for legacy schema changes.

AsyncGate now supports Alembic migrations for schema evolution.

## Running Migrations

Migrations should be run in order. Each migration is idempotent (safe to run multiple times).

```bash
# Preferred (Alembic)
alembic upgrade head

# Legacy SQL (manual, only if needed)
psql $DATABASE_URL -f migrations/001_add_parents_gin_index.sql
```

## Migration List

### 001_add_parents_gin_index.sql (P0.1 - PRIORITY)
**Date:** 2026-01-05  
**Status:** Required for production  

Adds GIN index on `receipts.parents` JSONB column for fast containment queries.

**Impact:**
- Reduces `open-obligation queries (asyncgate.bootstrap)` from O(n²) to O(n) performance
- Changes query pattern from N+1 to batched (2 queries total)
- With 100K receipts: 60M row scans → ~200 rows scanned

**Before:** 
```sql
-- For each candidate receipt (e.g., 600):
SELECT EXISTS (
    SELECT 1 FROM receipts 
    WHERE parents @> '["receipt_id"]'
)
-- = 600 full table scans
```

**After:**
```sql
-- Single batched query using GIN index:
SELECT parents FROM receipts 
WHERE jsonb_array_length(parents) > 0
-- Uses idx_receipts_parents_gin index
-- Filters in application layer
```

**Verification:**
```sql
-- Check index exists
SELECT indexname, indexdef 
FROM pg_indexes 
WHERE tablename = 'receipts' 
  AND indexname = 'idx_receipts_parents_gin';

-- Verify index is used in query plans
EXPLAIN ANALYZE
SELECT receipt_id, parents 
FROM receipts
WHERE parents @> '["some-uuid"]'::jsonb;
-- Should show: "Index Scan using idx_receipts_parents_gin"
```

## Alembic Notes

Alembic migrations live under `alembic/versions/` and are the
authoritative schema history going forward. Manual SQL files are
kept for backward compatibility with early deployments.
