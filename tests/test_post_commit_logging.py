"""The ReceiptGate emit-failure handler must not itself abort the transaction.

`_emit_receipt`'s except block logged with structlog-style kwargs against a
stdlib logger. That raises `TypeError`, which propagated out of the handler,
out of the enclosing `async with self.session.begin_nested()`, and rolled back
the task completion the handler was only meant to warn about. A degraded-ledger
warning became a hard failure of the work it was reporting on.

The identical bug was found and fixed thirty lines earlier in the same
function, with a comment explaining it, and left in place here -- which is why
this guard checks the whole module rather than the single line.
"""

from __future__ import annotations

import ast
import inspect
import logging

from asyncgate.engine import core

STDLIB_LOG_METHODS = {"debug", "info", "warning", "error", "critical", "exception"}
# The only keyword arguments stdlib logging accepts.
ALLOWED_LOG_KWARGS = {"extra", "exc_info", "stack_info", "stacklevel"}


def _structlog_style_calls(module) -> list[tuple[int, list[str]]]:
    tree = ast.parse(inspect.getsource(module))
    offenders: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in STDLIB_LOG_METHODS:
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"):
            continue
        bad = [kw.arg for kw in node.keywords if kw.arg not in ALLOWED_LOG_KWARGS]
        if bad:
            offenders.append((node.lineno, bad))
    return offenders


def test_engine_core_uses_stdlib_logging_correctly():
    assert core.logger.__class__ is logging.Logger, (
        "engine.core switched logger implementations; this guard assumes stdlib"
    )
    offenders = _structlog_style_calls(core)
    assert not offenders, (
        f"stdlib logger called with structlog-style kwargs at {offenders}. "
        f"Inside _emit_receipt this raises TypeError from the failure handler, "
        f"which rolls back the enclosing savepoint and fails the completion."
    )


def test_emit_failure_handler_shape_does_not_raise():
    """Execute the handler's log call directly, at WARNING.

    WARNING is enabled by default, so unlike the ReceiptGate INFO case this
    one was live in every deployment the moment a receipt emission failed.
    """
    core.logger.warning(
        "receiptgate_receipt_emit_failed",
        extra={
            "receipt_type": "task.completed",
            "task_id": "00000000-0000-0000-0000-000000000000",
            "error": "connection refused",
        },
    )
