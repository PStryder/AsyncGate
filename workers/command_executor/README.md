# Command Executor Worker

**Reference implementation demonstrating AsyncGate's lease protocol**

## What It Does

This worker:
1. Polls AsyncGate for `command.execute` tasks
2. Accepts tasks by emitting an `accepted` receipt
3. Executes shell commands via `subprocess`
4. Writes execution results (stdout/stderr/exit_code) to specified filesystem path
5. Reports completion via `success` or `failure` receipt with artifact pointer

## Architecture

This is a **reference artifact** - intentionally simple to prove AsyncGate's coordination layer works end-to-end. It demonstrates:

- **Lease protocol**: Worker polls, AsyncGate offers tasks based on capabilities
- **Receipt chains**: queued → accepted → success/failure
- **Artifact locatability**: Output files referenced in success receipts
- **Autonomous operation**: Worker self-manages, AsyncGate doesn't spawn/monitor

## Usage

### Installation

```bash
cd workers/command_executor
pip install -r requirements.txt
```

### Running the Worker

```bash
python worker.py \
  --asyncgate-url http://localhost:8000 \
  --api-key your-api-key-here \
  --worker-id command-exec-1 \
  --poll-interval 1
```

### Environment Variables (Alternative)

```bash
export ASYNCGATE_URL=http://localhost:8000
export ASYNCGATE_API_KEY=your-api-key
python worker.py
```

### Queueing a Test Task

```bash
curl -s http://localhost:8000/mcp \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"asyncgate.create_task","arguments":{"type":"command.execute","payload":{"command":"echo \"Hello from AsyncGate\"","output_path":"/tmp/asyncgate_test_output.json"},"principal_ai":"command-exec","agent_id":"command-exec","tenant_id":"TENANT_ID"}}}'
```

### Expected Output File

```json
{
  "command": "echo \"Hello from AsyncGate\"",
  "exit_code": 0,
  "stdout": "Hello from AsyncGate\n",
  "stderr": "",
  "executed_at": "2026-01-06T18:30:00.000000"
}
```

## Task Schema

```json
{
  "task_type": "command.execute",
  "payload": {
    "command": "string (required) - Shell command to execute",
    "output_path": "string (required) - Where to write JSON output"
  }
}
```

## Receipt Flow

1. **AsyncGate creates task** → queued receipt emitted
2. **Worker polls** → receives task offer via `asyncgate.lease_next`
3. **Worker accepts** → emits `accepted` receipt (parent: queued)
4. **Worker executes** → runs command, writes to filesystem
5. **Worker reports** → emits `success` receipt (parent: accepted) with artifact pointer

## Security Warnings

⚠️ **THIS IS A REFERENCE IMPLEMENTATION - NOT PRODUCTION READY**

This worker:
- Executes arbitrary shell commands without validation
- Has no sandboxing or resource limits
- Trusts all task payloads
- Should only be used for testing AsyncGate coordination

For production use, implement:
- Command whitelisting
- Sandboxing (containers, VMs)
- Resource limits (CPU, memory, disk)
- Input validation and sanitization
- Proper authentication between worker and AsyncGate

## Testing the Full Stack

1. **Start AsyncGate**:
   ```bash
   cd ../../
   uvicorn src.asyncgate.main:app --reload
   ```

2. **Start Worker**:
   ```bash
   python workers/command_executor/worker.py \
     --asyncgate-url http://localhost:8000 \
     --api-key test-key
   ```

3. **Queue Task**:
   ```bash
   curl -s http://localhost:8000/mcp \
     -H "Authorization: Bearer test-key" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"asyncgate.create_task","arguments":{"type":"command.execute","payload":{"command":"ls -la /tmp","output_path":"/tmp/ls_output.json"},"principal_ai":"command-exec","agent_id":"command-exec","tenant_id":"TENANT_ID"}}}'
   ```

4. **Verify**:
   - Check worker logs for task acceptance
   - Check `/tmp/ls_output.json` for command output
   - Query AsyncGate for receipts to verify chain

## Development Notes

This worker demonstrates the **minimal viable implementation** of the AsyncGate worker protocol:

- **No worker registration** - capabilities declared on each poll
- **No process management** - worker runs independently
- **Stateless protocol** - each lease poll is independent
- **Pure coordination** - AsyncGate doesn't know/care about execution details

Remote workers follow identical protocol - just different `--asyncgate-url`.

## License

Part of AsyncGate - see parent repository for license.
