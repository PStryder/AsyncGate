"""Receipt model - immutable contract records."""

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from asyncgate.models.enums import ReceiptType
from asyncgate.models.principal import Principal


class Receipt(BaseModel):
    """Immutable record of obligations and state changes."""

    receipt_id: UUID
    tenant_id: UUID
    receipt_type: ReceiptType
    created_at: datetime

    # Sender and recipient (using trailing underscore to avoid reserved keywords)
    from_: Principal = Field(serialization_alias="from", validation_alias="from")
    to_: Principal = Field(serialization_alias="to", validation_alias="to")

    # Related entities (nullable)
    task_id: UUID | None = None
    lease_id: UUID | None = None
    schedule_id: str | None = None

    # Causal linkage
    parents: list[UUID] = Field(default_factory=list)

    # Type-specific payload
    body: dict[str, Any] = Field(default_factory=dict)

    # Integrity (for deduplication/equivalence, not crypto verification)
    hash: str | None = None

    # AsyncGate instance that owns this receipt's task
    asyncgate_instance: str | None = None

    # Delivery tracking
    delivered_at: datetime | None = None

    model_config = {"populate_by_name": True}


def compute_receipt_hash(
    receipt_type: ReceiptType,
    task_id: UUID | None,
    from_principal: Principal,
    to_principal: Principal,
    lease_id: UUID | None,
    body: dict[str, Any] | None,
    parents: list[UUID] | None,
) -> str:
    """
    Compute hash for receipt deduplication.

    Includes all fields that make a receipt unique:
    - receipt_type, task_id, lease_id
    - from (kind + id)
    - to (kind + id)
    - parents (sorted list of UUID strings)
    - body (canonical JSON)
    """
    body_hash = None
    if body:
        body_canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        body_hash = hashlib.sha256(body_canonical.encode()).hexdigest()

    data = {
        "receipt_type": receipt_type.value,
        "task_id": str(task_id) if task_id else None,
        "from_kind": from_principal.kind.value,
        "from_id": from_principal.id,
        "to_kind": to_principal.kind.value,
        "to_id": to_principal.id,
        "lease_id": str(lease_id) if lease_id else None,
        "parents": sorted([str(p) for p in (parents or [])]),
        "body_hash": body_hash,
    }
    content = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


class ReceiptBody:
    """Standard receipt body schemas."""

    @staticmethod
    def task_assigned(
        instructions: str,
        requirements: dict | None = None,
        success_criteria: str | None = None,
        result_delivery: str = "return_payload",
        timeouts: dict | None = None,
    ) -> dict[str, Any]:
        """Body for task.assigned receipt."""
        return {
            "instructions": instructions,
            "requirements": requirements or {},
            "success_criteria": success_criteria,
            "result_delivery": result_delivery,
            "timeouts": timeouts or {},
        }

    @staticmethod
    def task_accepted(
        worker_capabilities: list[str],
        expected_duration: int | None = None,
    ) -> dict[str, Any]:
        """Body for task.accepted receipt."""
        return {
            "worker_capabilities": worker_capabilities,
            "expected_duration": expected_duration,
        }

    @staticmethod
    def task_started(
        started_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Body for task.started receipt."""
        return {
            "started_at": started_at.isoformat() if started_at else None,
        }

    @staticmethod
    def task_completed(
        result_summary: str,
        result_payload: dict | None = None,
        artifacts: list[dict] | None = None,
        delivery_proof: dict | None = None,
        completion_metadata: dict | None = None,
    ) -> dict[str, Any]:
        """
        Body for task.completed receipt.

        Locatability requirement: Must provide EITHER artifacts OR delivery_proof.

        Args:
            result_summary: Human-readable summary of completion
            result_payload: Optional structured result data
            artifacts: List of store pointers (work product locations)
                Examples: [
                    {"type": "s3", "url": "s3://bucket/key", "etag": "..."},
                    {"type": "db", "table": "results", "row_id": 123},
                    {"type": "drive", "file_id": "...", "share_url": "..."}
                ]
            delivery_proof: Push delivery confirmation (unopinionated)
                Schema: {
                    "mode": "push" | "store",
                    "target": {...},  # endpoint spec or pointer
                    "status": "succeeded" | "failed",
                    "at": "timestamp",
                    "proof": {...}  # request_id, etag, row_id, http_status, etc.
                }
            completion_metadata: Additional context

        Returns:
            Receipt body dict
        """
        return {
            "result_summary": result_summary,
            "result_payload": result_payload,
            "artifacts": artifacts,
            "delivery_proof": delivery_proof,
            "completion_metadata": completion_metadata or {},
        }

    @staticmethod
    def task_failed(
        error: dict[str, Any],
        retry_recommended: bool = False,
        retry_after_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Body for task.failed receipt."""
        return {
            "error": error,
            "retry_recommended": retry_recommended,
            "retry_after_seconds": retry_after_seconds,
        }

    @staticmethod
    def task_result_ready(
        status: str,
        result_payload: dict | None = None,
        error: dict | None = None,
        artifacts: dict | list[dict] | None = None,
        how_to_retrieve: str | None = None,
    ) -> dict[str, Any]:
        """Body for task.result_ready receipt."""
        return {
            "status": status,
            "result_payload": result_payload,
            "error": error,
            "artifacts": artifacts,
            "how_to_retrieve": how_to_retrieve,
        }

    @staticmethod
    def lease_expired(
        task_id: UUID,
        previous_worker_id: str,
        attempt: int,
        requeued: bool = True,
    ) -> dict[str, Any]:
        """Body for lease.expired receipt."""
        return {
            "task_id": str(task_id),
            "previous_worker_id": previous_worker_id,
            "attempt": attempt,
            "requeued": requeued,
        }

    @staticmethod
    def task_escalated(
        escalation_class: str,
        escalation_reason: str,
        escalation_to: str,
        expected_outcome_kind: str | None = None,
        expected_artifact_mime: str | None = None,
    ) -> dict[str, Any]:
        """Body for task.escalated receipt."""
        return {
            "escalation_class": escalation_class,
            "escalation_reason": escalation_reason,
            "escalation_to": escalation_to,
            "expected_outcome_kind": expected_outcome_kind,
            "expected_artifact_mime": expected_artifact_mime,
        }

    @staticmethod
    def system_anomaly(
        kind: str,
        details: dict[str, Any],
        recommended_action: str | None = None,
    ) -> dict[str, Any]:
        """Body for system.anomaly receipt."""
        return {
            "kind": kind,
            "details": details,
            "recommended_action": recommended_action,
        }
