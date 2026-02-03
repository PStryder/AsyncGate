#!/usr/bin/env python3
"""
Simple test script to queue a command.execute task to AsyncGate

Usage:
    python test_command_executor.py --asyncgate-url http://localhost:8000 --api-key test-key
"""

import argparse
import requests
from datetime import datetime


def queue_test_task(asyncgate_url: str, api_key: str, output_path: str = None):
    """Queue a simple test command to verify worker is functioning"""
    
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"/tmp/asyncgate_test_{timestamp}.json"
    
    task = {
        "type": "command.execute",
        "payload": {
            "command": "echo 'AsyncGate command executor test' && date && uname -a",
            "output_path": output_path,
        },
        "principal_ai": "command-exec",
        "agent_id": "command-exec",
        "tenant_id": "TENANT_ID",
    }
    
    print(f"Queueing test task to: {asyncgate_url}")
    print(f"Command: {task['payload']['command']}")
    print(f"Output will be written to: {output_path}")
    print()
    
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "asyncgate.create_task",
            "arguments": task,
        },
    }

    response = requests.post(
        f"{asyncgate_url.rstrip('/')}/mcp",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    
    if response.status_code == 200:
        result = response.json().get("result", {})
        task_id = result.get("task_id")
        print(f"✓ Task queued successfully!")
        print(f"  Task ID: {task_id}")
        print()
        print("Next steps:")
        print(f"  1. Ensure worker is running")
        print(f"  2. Check worker logs for task acceptance")
        print(f"  3. Verify output file: cat {output_path}")
        print("  4. Query receipts: asyncgate.list_receipts_ledger (MCP)")
        return task_id
    else:
        print(f"✗ Failed to queue task: {response.status_code}")
        print(f"  Response: {response.text}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Queue test task to AsyncGate command executor")
    parser.add_argument(
        "--asyncgate-url",
        required=True,
        help="AsyncGate base URL (e.g., http://localhost:8000)"
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="AsyncGate API key"
    )
    parser.add_argument(
        "--output-path",
        help="Custom output path (default: /tmp/asyncgate_test_TIMESTAMP.json)"
    )
    
    args = parser.parse_args()
    queue_test_task(args.asyncgate_url, args.api_key, args.output_path)


if __name__ == "__main__":
    main()
