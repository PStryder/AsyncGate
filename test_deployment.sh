#!/bin/bash

# AsyncGate Deployment Smoke Tests (MCP)

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <base-url> [api-key] [tenant-id]"
    echo "Example: $0 https://asyncgate.fly.dev my-api-key 00000000-0000-0000-0000-000000000000"
    exit 1
fi

BASE_URL="$1"
API_KEY="${2:-}"
TENANT_ID="${3:-00000000-0000-0000-0000-000000000000}"

echo "=== Testing AsyncGate at $BASE_URL ==="
echo

# Test 1: Health check (no auth)
echo "Test 1: Health check..."
HEALTH=$(curl -s "$BASE_URL/mcp"   -H "Content-Type: application/json"   -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"asyncgate.health","arguments":{}}}')
if echo "$HEALTH" | grep -q "healthy"; then
    echo "? Health check passed"
else
    echo "? Health check failed"
    echo "$HEALTH"
    exit 1
fi

# Require API key for remaining tests
if [ -z "$API_KEY" ]; then
    echo "??  Skipping authenticated tests (no API key provided)"
    exit 0
fi

AUTH_HEADER="Authorization: Bearer $API_KEY"

# Test 2: Bootstrap
echo "Test 2: Bootstrap..."
BOOTSTRAP=$(curl -s -H "$AUTH_HEADER" "$BASE_URL/mcp"   -H "Content-Type: application/json"   -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"asyncgate.bootstrap","arguments":{"agent_id":"smoke-agent","tenant_id":"'"$TENANT_ID"'"}}}')
if echo "$BOOTSTRAP" | grep -q "open_obligations"; then
    echo "? Bootstrap passed"
else
    echo "? Bootstrap failed"
    echo "$BOOTSTRAP"
    exit 1
fi

# Test 3: Queue task
echo "Test 3: Queue task..."
TASK=$(curl -s -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json"     "$BASE_URL/mcp"     -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"asyncgate.create_task","arguments":{"type":"test.smoke","payload":{"message":"deployment test"},"principal_ai":"smoke-agent","agent_id":"smoke-agent","tenant_id":"'"$TENANT_ID"'","idempotency_key":"smoke-test-'"$(date +%s)"'"}}}')

TASK_ID=$(echo "$TASK" | grep -o '"task_id":"[^"]*"' | cut -d'"' -f4)
if [ -n "$TASK_ID" ]; then
    echo "? Task queued: $TASK_ID"
else
    echo "? Task queue failed"
    echo "$TASK"
    exit 1
fi

# Test 4: Get task status
echo "Test 4: Get task status..."
sleep 2
STATUS=$(curl -s -H "$AUTH_HEADER" "$BASE_URL/mcp"   -H "Content-Type: application/json"   -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"asyncgate.get_task","arguments":{"task_id":"'"$TASK_ID"'","tenant_id":"'"$TENANT_ID"'"}}}')
if echo "$STATUS" | grep -q "$TASK_ID"; then
    echo "? Task status retrieved"
else
    echo "? Task status failed"
    echo "$STATUS"
    exit 1
fi

echo
echo "=== All smoke tests passed! ==="
