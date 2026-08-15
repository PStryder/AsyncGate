"""Adapter for rendering AsyncGate receipts in LegiVellum receipt schema."""

import json
from typing import Any

# The canonical receipt model is a hard dependency, imported unguarded.
#
# This was previously a path-walking shim wrapped in `except ImportError`,
# which found `LegiVellum/shared` in a source checkout and nothing in a
# container. When it failed, `CanonicalReceipt` became None and this module
# returned the raw dict -- so AsyncGate POSTed unvalidated payloads to the
# ledger in every deployment, and the resulting rejections were logged at
# WARNING and dropped. `legivellum` is now an installed dependency (see
# pyproject) and a missing one must stop the process, not silently disable
# validation.
from legivellum.models import Receipt as CanonicalReceipt
from legivellum.ulid import derive_ulid

from asyncgate.models import Principal, Receipt, ReceiptType, Task
from asyncgate.models.enums import Outcome


def _principal_value(principal: Principal) -> str:
    return principal.id


def _extract_artifact_fields(artifacts: Any) -> dict[str, Any]:
    artifact = None
    if isinstance(artifacts, list) and artifacts:
        artifact = artifacts[0]
    elif isinstance(artifacts, dict):
        artifact = artifacts

    if not isinstance(artifact, dict):
        return {
            "artifact_location": "NA",
            "artifact_pointer": "NA",
            "artifact_checksum": "NA",
            "artifact_size_bytes": 0,
            "artifact_mime": "NA",
        }

    artifact_pointer = (
        artifact.get("url")
        or artifact.get("uri")
        or artifact.get("output_path")
        or artifact.get("pointer")
        or "NA"
    )
    artifact_location = artifact.get("type") or artifact.get("store") or "NA"
    artifact_mime = (
        artifact.get("mime")
        or artifact.get("content_type")
        or artifact.get("artifact_mime")
        or "NA"
    )
    artifact_checksum = artifact.get("checksum") or artifact.get("etag") or "NA"
    artifact_size_bytes = artifact.get("size_bytes") or artifact.get("bytes") or 0

    return {
        "artifact_location": artifact_location,
        "artifact_pointer": artifact_pointer,
        "artifact_checksum": artifact_checksum,
        "artifact_size_bytes": artifact_size_bytes,
        "artifact_mime": artifact_mime,
    }


def _extract_artifact_refs(artifacts: Any) -> list[dict[str, Any]]:
    if not artifacts:
        return []
    if isinstance(artifacts, list):
        return [item for item in artifacts if isinstance(item, dict)]
    if isinstance(artifacts, dict):
        return [artifacts]
    return []


def _derive_phase_and_status(receipt: Receipt, task: Task | None) -> tuple[str, str]:
    if receipt.receipt_type == ReceiptType.TASK_ESCALATED:
        return "escalate", "NA"

    if receipt.receipt_type in {
        ReceiptType.TASK_COMPLETED,
        ReceiptType.TASK_FAILED,
        ReceiptType.TASK_CANCELED,
        ReceiptType.TASK_RESULT_READY,
    }:
        if receipt.receipt_type == ReceiptType.TASK_FAILED:
            return "complete", "failure"
        if receipt.receipt_type == ReceiptType.TASK_CANCELED:
            return "complete", "canceled"
        if receipt.receipt_type == ReceiptType.TASK_RESULT_READY and task and task.result:
            if task.result.outcome == Outcome.FAILED:
                return "complete", "failure"
            if task.result.outcome == Outcome.CANCELED:
                return "complete", "canceled"
            return "complete", "success"
        return "complete", "success"

    return "accepted", "NA"


def _derive_outcome_kind(receipt: Receipt, task: Task | None) -> str:
    body = receipt.body or {}
    artifacts = body.get("artifacts")
    if artifacts is None and task and task.result:
        artifacts = task.result.artifacts
    result_payload = body.get("result_payload")
    error_payload = body.get("error")

    has_artifacts = artifacts is not None
    has_result = result_payload is not None
    has_error = error_payload is not None

    if has_artifacts and has_result:
        return "mixed"
    if has_artifacts:
        return "artifact_pointer"
    if has_result or has_error:
        return "response_text"
    if task and task.result:
        return "response_text"
    return "NA"


def _task_summary(receipt: Receipt, task: Task | None) -> str:
    body = receipt.body or {}
    if receipt.receipt_type == ReceiptType.TASK_ASSIGNED and "instructions" in body:
        return body["instructions"]
    if "result_summary" in body:
        return body["result_summary"]
    if isinstance(body.get("error"), dict) and body["error"].get("message"):
        return body["error"]["message"]
    if task:
        return task.type
    return "NA"


def to_memorygate_receipt(receipt: Receipt, task: Task | None) -> dict[str, Any]:
    """Convert AsyncGate receipt to MemoryGate receipt schema."""
    body = receipt.body or {}
    phase, status = _derive_phase_and_status(receipt, task)

    outcome_kind = _derive_outcome_kind(receipt, task)
    outcome_text = body.get("result_summary") or "NA"
    if outcome_text == "NA" and isinstance(body.get("error"), dict):
        outcome_text = body["error"].get("message") or "NA"

    artifacts_source = body.get("artifacts")
    if artifacts_source is None and task and task.result:
        artifacts_source = task.result.artifacts
    artifact_refs_source = body.get("artifact_refs")
    if artifact_refs_source is None:
        artifact_refs_source = artifacts_source
    artifact_fields = _extract_artifact_fields(artifacts_source)
    artifact_refs = _extract_artifact_refs(artifact_refs_source)

    expected_outcome_kind = (
        task.expected_outcome_kind if task and task.expected_outcome_kind else "NA"
    )
    expected_artifact_mime = (
        task.expected_artifact_mime if task and task.expected_artifact_mime else "NA"
    )

    created_at = receipt.created_at.isoformat() if receipt.created_at else None
    started_at = task.started_at.isoformat() if task and task.started_at else None
    completed_at = None
    if task and task.result and task.result.completed_at:
        completed_at = task.result.completed_at.isoformat()

    inputs: dict[str, Any] = {}
    task_body = "TBD"
    if task:
        if task.payload_pointer:
            inputs["payload_pointer"] = task.payload_pointer
        if task.payload:
            inputs["payload"] = task.payload
            task_body = json.dumps(task.payload)
        elif task.payload_pointer:
            task_body = task.payload_pointer

    caused_by = str(receipt.parents[0]) if receipt.parents else "NA"

    escalation_fields = {
        "escalation_class": body.get("escalation_class", "NA"),
        "escalation_reason": body.get("escalation_reason", "NA"),
        "escalation_to": body.get("escalation_to", "NA"),
        "retry_requested": body.get("retry_requested", False),
    }

    metadata = {
        "receipt_type": receipt.receipt_type.value,
        "lease_id": str(receipt.lease_id) if receipt.lease_id else "NA",
        "parents": [str(parent) for parent in receipt.parents],
        "from_kind": receipt.from_.kind.value,
        "to_kind": receipt.to_.kind.value,
    }
    if "trace_id" in body:
        metadata["trace_id"] = body["trace_id"]

    if task:
        owner_principal = _principal_value(task.created_by)
        from_principal = owner_principal
        for_principal = owner_principal
        recipient_ai = task.principal_ai or _principal_value(receipt.to_)
        if receipt.receipt_type == ReceiptType.TASK_ACCEPTED:
            recipient_ai = _principal_value(receipt.from_)
    else:
        from_principal = _principal_value(receipt.from_)
        for_principal = _principal_value(receipt.to_)
        recipient_ai = for_principal
    if phase == "escalate":
        escalation_target = escalation_fields.get("escalation_to")
        if escalation_target and escalation_target != "NA":
            recipient_ai = escalation_target

    payload = {
        "schema_version": "1.0",
        "tenant_id": str(receipt.tenant_id),
        "receipt_id": str(receipt.receipt_id),
        "task_id": str(receipt.task_id) if receipt.task_id else "NA",
        # AsyncGate owns one obligation per task, opened by task.assigned and
        # closed by a terminal receipt. Derived rather than minted because the
        # opening and closing receipts are produced by different engine
        # operations; every receipt for a task therefore names the same
        # obligation, and a terminal receipt for a DIFFERENT task can no longer
        # close this one by sharing a task_id.
        "obligation_id": (
            derive_ulid("asyncgate.task", str(receipt.task_id))
            if receipt.task_id
            else derive_ulid("asyncgate.receipt", str(receipt.receipt_id))
        ),
        "parent_task_id": "NA",
        "caused_by_receipt_id": caused_by,
        "dedupe_key": receipt.hash or "NA",
        "attempt": task.attempt if task else 0,
        "from_principal": from_principal,
        "for_principal": for_principal,
        "source_system": "asyncgate",
        "recipient_ai": recipient_ai,
        "trust_domain": "default",
        "phase": phase,
        "status": status,
        "realtime": False,
        "task_type": task.type if task else "NA",
        "task_summary": _task_summary(receipt, task),
        "task_body": task_body,
        "inputs": inputs,
        "expected_outcome_kind": expected_outcome_kind,
        "expected_artifact_mime": expected_artifact_mime,
        "outcome_kind": outcome_kind,
        "outcome_text": outcome_text,
        **artifact_fields,
        **escalation_fields,
        "body": dict(body),
        "artifact_refs": artifact_refs,
        "created_at": created_at,
        "stored_at": created_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "read_at": None,
        "archived_at": None,
        "metadata": metadata,
    }

    return CanonicalReceipt.model_validate(payload).model_dump(mode="json")
