"""Trace ID middleware for request correlation."""

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from asyncgate.observability.trace import ensure_trace_id, set_trace_id


async def trace_id_middleware(request: Request, call_next):
    """
    Middleware to extract or generate a trace ID for each request.
    
    Checks for X-Trace-Id header, otherwise generates a new trace ID.
    Sets the trace ID in context for use throughout the request lifecycle.
    """
    # Try to get trace ID from header
    trace_id = request.headers.get("X-Trace-Id")
    
    if trace_id:
        # Use provided trace ID
        set_trace_id(trace_id)
    else:
        # Generate new trace ID
        trace_id = ensure_trace_id()
    
    # Process request
    response = await call_next(request)
    
    # Add trace ID to response headers
    response.headers["X-Trace-Id"] = trace_id
    
    return response
