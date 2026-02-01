"""HTTP MCP (JSON-RPC) interface for AsyncGate."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from asyncgate.mcp.server import TOOL_DEFS, _handle_tool


class MCPRequest(BaseModel):
    """Minimal JSON-RPC request envelope for MCP."""

    jsonrpc: str = Field(default="2.0")
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    id: Any = None


def _jsonrpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: Any, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.post("")
async def mcp_entry(request: MCPRequest, http_request: Request):
    """Handle MCP JSON-RPC requests."""
    if request.method == "tools/list":
        return _jsonrpc_result(
            request.id,
            {"tools": TOOL_DEFS},
        )

    if request.method != "tools/call":
        return _jsonrpc_error(request.id, -32601, f"Method not found: {request.method}")

    params = request.params or {}
    tool_name = params.get("name")
    arguments = params.get("arguments") or {}

    if not tool_name:
        return _jsonrpc_error(request.id, -32602, "Missing tool name")

    # Inject auth token from headers if caller omitted it
    if "auth_token" not in arguments:
        auth_header = http_request.headers.get("authorization")
        api_key_header = http_request.headers.get("x-api-key")
        if auth_header and auth_header.lower().startswith("bearer "):
            arguments["auth_token"] = auth_header.split(" ", 1)[1]
        elif api_key_header:
            arguments["auth_token"] = api_key_header

    try:
        result = await _handle_tool(tool_name, arguments)
        return _jsonrpc_result(request.id, result)
    except Exception as exc:
        return _jsonrpc_error(request.id, getattr(exc, "code", "ERROR"), str(exc))
