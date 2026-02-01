"""Distributed tracing helpers for AsyncGate."""

import contextvars
from uuid import uuid4

# Context variable to store trace ID for the current request
_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)


def get_trace_id() -> str | None:
    """Get the current trace ID from context."""
    return _trace_id_var.get()


def ensure_trace_id() -> str:
    """Ensure a trace ID exists in context, creating one if needed."""
    trace_id = _trace_id_var.get()
    if not trace_id:
        trace_id = str(uuid4())
        _trace_id_var.set(trace_id)
    return trace_id


def set_trace_id(trace_id: str) -> None:
    """Set the trace ID in context."""
    _trace_id_var.set(trace_id)
