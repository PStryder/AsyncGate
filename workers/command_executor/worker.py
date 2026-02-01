"""
Command Executor Worker - Reference Implementation

This worker demonstrates the AsyncGate lease protocol by:
1. Polling for command.execute tasks
2. Accepting tasks via receipt emission
3. Executing shell commands
4. Writing results to filesystem
5. Reporting completion via success/failure receipts
"""

import asyncio
import shlex
import subprocess
import json
import sys
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
import httpx
from uuid import uuid4


class CommandExecutorWorker:
    def __init__(
        self,
        asyncgate_url: str,
        api_key: str,
        worker_id: str,
        poll_interval_seconds: int = 1,
        allow_shell: bool = False,
        allowed_commands: Optional[list[str]] = None,
        output_base_dir: str = "./outputs",
        tenant_id: str = "default",
    ):
        self.asyncgate_url = asyncgate_url.rstrip('/')
        self.api_key = api_key
        self.worker_id = worker_id
        self.poll_interval = poll_interval_seconds
        self.capabilities = ["command.execute"]
        self.allow_shell = allow_shell
        self.allowed_commands = [c for c in (allowed_commands or []) if c]
        self.output_base_dir = Path(output_base_dir).resolve()
        self.output_base_dir.mkdir(parents=True, exist_ok=True)
        self.tenant_id = tenant_id
        self.mcp_url = f"{self.asyncgate_url}/mcp"
        
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        self._client = httpx.AsyncClient(timeout=10.0)
    
    def log(self, message: str, level: str = "INFO"):
        """Simple logging"""
        timestamp = datetime.utcnow().isoformat()
        print(f"[{timestamp}] [{level}] [{self.worker_id}] {message}", flush=True)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def _mcp_call(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": f"{tool}",
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        response = await self._client.post(self.mcp_url, json=payload, headers=self.headers)
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise RuntimeError(data["error"])
        return data.get("result", {})

    def _normalize_command(self, command: str) -> str | list[str]:
        """Normalize command execution input and enforce safety."""
        if not command or not command.strip():
            raise ValueError("Command is empty")

        if self.allow_shell:
            return command

        if not self.allowed_commands:
            raise ValueError(
                "No allowed commands configured. Use --allowed-command or --allow-shell."
            )

        args = shlex.split(command)
        if not args:
            raise ValueError("Command is empty after parsing")
        if args[0] not in self.allowed_commands:
            raise ValueError(f"Command not allowed: {args[0]}")
        return args

    def _resolve_output_path(self, output_path: str) -> Path:
        """Resolve output path inside the base directory."""
        candidate = Path(output_path)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.output_base_dir / candidate).resolve()
        try:
            resolved.relative_to(self.output_base_dir)
        except ValueError:
            raise ValueError("output_path must be within the configured output base directory")
        return resolved
    
    async def poll_for_task(self) -> Optional[Dict[str, Any]]:
        """Poll AsyncGate for available tasks matching our capabilities"""
        try:
            data = await self._mcp_call(
                "asyncgate.lease_next",
                {
                    "worker_id": self.worker_id,
                    "capabilities": self.capabilities,
                    "max_tasks": 1,
                    "tenant_id": self.tenant_id,
                },
            )
            tasks = data.get("tasks", [])
            if not tasks:
                return None
            task = tasks[0]
            self.log(f"Received task: {task.get('task_id')}")
            return task
            
        except Exception as e:
            self.log(f"Error polling for task: {e}", "ERROR")
            return None
    
    def execute_command(self, command: str, output_path: str) -> Dict[str, Any]:
        """Execute shell command and write output to file"""
        self.log(f"Executing command: {command}")
        
        try:
            cmd = self._normalize_command(command)
            # Execute command
            result = subprocess.run(
                cmd,
                shell=self.allow_shell,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            # Prepare output data
            output_data = {
                "command": command,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "executed_at": datetime.utcnow().isoformat()
            }
            
            # Write to output path
            output_file = self._resolve_output_path(output_path)
            output_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(output_file, 'w') as f:
                json.dump(output_data, f, indent=2)
            
            self.log(f"Command executed, output written to: {output_path}")
            
            return {
                "success": True,
                "output_path": str(output_file.absolute()),
                "exit_code": result.returncode
            }
            
        except subprocess.TimeoutExpired:
            self.log(f"Command timed out after 300s", "ERROR")
            return {
                "success": False,
                "error": "Command execution timed out after 300 seconds"
            }
        except Exception as e:
            self.log(f"Command execution failed: {e}", "ERROR")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def report_completion(
        self,
        task_id: str,
        lease_id: str,
        execution_result: Dict[str, Any],
    ) -> None:
        """Report task completion or failure to AsyncGate."""
        try:
            if execution_result.get("success"):
                await self._mcp_call(
                    "asyncgate.complete",
                    {
                        "worker_id": self.worker_id,
                        "task_id": task_id,
                        "lease_id": lease_id,
                        "result": {
                            "exit_code": execution_result.get("exit_code"),
                            "output_path": execution_result.get("output_path"),
                        },
                        "artifacts": {
                            "output_path": execution_result.get("output_path"),
                            "content_type": "application/json",
                            "description": "Command execution output",
                        },
                        "tenant_id": self.tenant_id,
                    },
                )
            else:
                await self._mcp_call(
                    "asyncgate.fail",
                    {
                        "worker_id": self.worker_id,
                        "task_id": task_id,
                        "lease_id": lease_id,
                        "error": {"message": execution_result.get("error")},
                        "retryable": False,
                        "tenant_id": self.tenant_id,
                    },
                )
        except Exception as exc:
            self.log(f"Failed to report completion: {exc}", "ERROR")

    async def report_running(self, task_id: str, lease_id: str) -> None:
        """Report task start to AsyncGate."""
        try:
            await self._mcp_call(
                "asyncgate.report_progress",
                {
                    "worker_id": self.worker_id,
                    "task_id": task_id,
                    "lease_id": lease_id,
                    "progress": {"status": "running"},
                    "tenant_id": self.tenant_id,
                },
            )
        except Exception as exc:
            self.log(f"Failed to report running: {exc}", "WARN")
    
    async def process_task(self, task: Dict[str, Any]):
        """Complete task processing workflow"""
        try:
            # Extract task data
            task_id = task.get("task_id")
            lease_id = task.get("lease_id")
            payload = task.get("payload", {})
            command = payload.get("command")
            output_path = payload.get("output_path")
            
            if not task_id or not lease_id:
                self.log("Invalid task payload - missing task_id or lease_id", "ERROR")
                return

            if not command or not output_path:
                self.log("Invalid task payload - missing command or output_path", "ERROR")
                return

            await self.report_running(task_id, lease_id)
            
            # Execute command
            execution_result = self.execute_command(command, output_path)
            
            # Report completion
            await self.report_completion(task_id, lease_id, execution_result)
            
        except Exception as e:
            self.log(f"Error processing task: {e}", "ERROR")
    
    async def run(self):
        """Main worker loop"""
        self.log(f"Starting worker with capabilities: {self.capabilities}")
        self.log(f"Polling AsyncGate at: {self.asyncgate_url}")
        try:
            while True:
                try:
                    # Poll for task
                    task = await self.poll_for_task()

                    if task:
                        # Process task
                        await self.process_task(task)
                    else:
                        # No task available, wait before polling again
                        await asyncio.sleep(self.poll_interval)

                except KeyboardInterrupt:
                    self.log("Shutdown requested")
                    break
                except Exception as e:
                    self.log(f"Unexpected error in main loop: {e}", "ERROR")
                    await asyncio.sleep(self.poll_interval)
        finally:
            await self.close()


def main():
    parser = argparse.ArgumentParser(description="AsyncGate Command Executor Worker")
    parser.add_argument(
        "--asyncgate-url",
        required=True,
        help="AsyncGate base URL (e.g., http://localhost:8000)"
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="AsyncGate API key for authentication"
    )
    parser.add_argument(
        "--worker-id",
        default=f"command-executor-{uuid4().hex[:8]}",
        help="Unique worker identifier (default: auto-generated)"
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=1,
        help="Polling interval in seconds (default: 1)"
    )
    parser.add_argument(
        "--allow-shell",
        action="store_true",
        help="Allow shell execution (unsafe). Default: disabled."
    )
    parser.add_argument(
        "--allowed-command",
        action="append",
        dest="allowed_commands",
        default=[],
        help="Allowlisted command name (repeatable)."
    )
    parser.add_argument(
        "--output-base-dir",
        default="./outputs",
        help="Base directory for output artifacts (default: ./outputs)"
    )
    parser.add_argument(
        "--tenant-id",
        default="default",
        help="Tenant identifier (default: default)"
    )
    
    args = parser.parse_args()
    
    worker = CommandExecutorWorker(
        asyncgate_url=args.asyncgate_url,
        api_key=args.api_key,
        worker_id=args.worker_id,
        poll_interval_seconds=args.poll_interval,
        allow_shell=args.allow_shell,
        allowed_commands=args.allowed_commands,
        output_base_dir=args.output_base_dir,
        tenant_id=args.tenant_id,
    )
    
    asyncio.run(worker.run())


if __name__ == "__main__":
    main()
