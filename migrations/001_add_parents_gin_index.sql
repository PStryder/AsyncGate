-- Migration: Add GIN index on receipts.parents for fast containment queries
-- Priority: P0.1 - Performance critical for open-obligation queries (asyncgate.bootstrap) endpoint
-- Date: 2026-01-05
-- Issue: Without this index, list_open_obligations performs O(n) full table scans
--        With 100K receipts and limit=200, this means 60M row scans per API call

-- This index enables fast JSONB array containment queries using GIN (Generalized Inverted Index)
-- Reduces list_open_obligations from N+1 queries to 2 queries total

-- Check if index already exists (idempotent)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes 
        WHERE tablename = 'receipts' 
        AND indexname = 'idx_receipts_parents_gin'
    ) THEN
        -- Create GIN index on parents JSONB column
        CREATE INDEX CONCURRENTLY idx_receipts_parents_gin 
        ON receipts USING GIN (parents);
        
        RAISE NOTICE 'Created GIN index on receipts.parents';
    ELSE
        RAISE NOTICE 'Index idx_receipts_parents_gin already exists';
    END IF;
END
$$;

-- Verify index was created
SELECT 
    schemaname,
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename = 'receipts' 
AND indexname = 'idx_receipts_parents_gin';

-- Performance check query (optional - run after index creation)
-- EXPLAIN ANALYZE
-- SELECT receipt_id, parents 
-- FROM receipts
-- WHERE tenant_id = 'YOUR_TENANT_ID'
--   AND parents @> '["SOME_RECEIPT_ID"]'::jsonb;
-- Should show: "Index Scan using idx_receipts_parents_gin"
