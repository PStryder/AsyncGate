"""
Contract tests for the AsyncGate -> ReceiptGate receipt adapter.

`to_memorygate_receipt` mints every receipt AsyncGate emits. Nothing else
validated its output against the canonical LegiVellum receipt schema, which is
how the `body` field became required in the schema (2026-02-03) without any
AsyncGate test noticing.

These tests validate real adapter output against the canonical schema, using
the same artifact-reference shape the problemata demo sends. Note that the
adapter's artifact extractor reads `type`/`uri`/`mime`/`checksum`, not
DepotGate's raw `location`/`artifact_id`/`content_hash` keys -- the demo's
`_build_artifact_ref` performs that translation. Fixtures here must use the
translated shape or they will not exercise the real path.

Schema resolution order:
  1. LEGIVELLUM_RECEIPT_SCHEMA (absolute path)
  2. ../LegiVellum/docs/canonical/receipt.schema.v1.json (sibling checkout)
  3. skip
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

jsonschema = pytest.importorskip("jsonschema", reason="jsonschema required for contract tests")

from asyncgate.models.enums import Outcome, PrincipalKind, ReceiptType
from asyncgate.models.principal import Principal
from asyncgate.models.receipt import Receipt
from asyncgate.models.task import Task, TaskResult
from asyncgate.receipts.memorygate_adapter import to_memorygate_receipt

CANONICAL_SCHEMA_RELPATH = Path("docs/canonical/receipt.schema.v1.json")


def _resolve_schema_path() -> Path | None:
    override = os.environ.get("LEGIVELLUM_RECEIPT_SCHEMA")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None

    repo_root = Path(__file__).resolve().parents[1]
    sibling = repo_root.parent / "LegiVellum" / CANONICAL_SCHEMA_RELPATH
    return sibling if sibling.is_file() else None


@pytest.fixture(scope="module")
def validator() -> Any:
    schema_path = _resolve_schema_path()
    if schema_path is None:
        pytest.skip(
            "Canonical receipt schema not found. Set LEGIVELLUM_RECEIPT_SCHEMA or "
            "check out LegiVellum as a sibling directory."
        )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _errors(validator: Any, payload: dict[str, Any]) -> list[str]:
    return [
        f"{list(e.path) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(payload), key=lambda e: str(e.path))
    ]


NOW = datetime(2026, 2, 23, 12, 0, 0, tzinfo=timezone.utc)
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TASK_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

SERVICE = Principal(kind=PrincipalKind.SERVICE, id="asyncgate")
AGENT = Principal(kind=PrincipalKind.AGENT, id="demo")

# Mirrors problemata_demo/golden_path.py::_build_artifact_ref -- the shape the
# adapter's `_extract_artifact_fields` actually understands.
ARTIFACT_REF = {
    "type": "depotgate",
    "uri": "depotgate://artifact-1",
    "mime": "text/plain",
    "size_bytes": 42,
    "checksum": "sha256:abc123",
    "location": "shipped",
}


def _task(*, result: TaskResult | None = None, started: bool = True) -> Task:
    return Task(
        task_id=TASK_ID,
        tenant_id=TENANT_ID,
        type="demo.task",
        payload={"message": "Generate a short demo artifact."},
        created_by=AGENT,
        principal_ai="agent:demo",
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW + timedelta(seconds=1) if started else None,
        expected_outcome_kind="artifact_pointer",
        expected_artifact_mime="text/plain",
        result=result,
    )


def _terminal_result(outcome: Outcome, *, artifacts: Any = None) -> TaskResult:
    return TaskResult(
        outcome=outcome,
        result={"summary": "done"},
        artifacts=artifacts,
        completed_at=NOW + timedelta(seconds=5),
    )


def _receipt(receipt_type: ReceiptType, *, body: dict[str, Any] | None = None) -> Receipt:
    return Receipt(
        receipt_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        receipt_type=receipt_type,
        created_at=NOW,
        task_id=TASK_ID,
        body=body or {},
        **{"from": SERVICE, "to": AGENT},
    )


def _accepted() -> tuple[Receipt, Task]:
    return _receipt(ReceiptType.TASK_ACCEPTED), _task()


def _completed_with_artifact() -> tuple[Receipt, Task]:
    receipt = _receipt(
        ReceiptType.TASK_COMPLETED,
        body={"result_summary": "Golden path success", "artifacts": [ARTIFACT_REF]},
    )
    return receipt, _task(result=_terminal_result(Outcome.SUCCEEDED, artifacts=[ARTIFACT_REF]))


def _failed() -> tuple[Receipt, Task]:
    receipt = _receipt(
        ReceiptType.TASK_FAILED,
        body={"error": {"message": "Worker exhausted retries"}},
    )
    return receipt, _task(result=_terminal_result(Outcome.FAILED))


def _canceled() -> tuple[Receipt, Task]:
    receipt = _receipt(ReceiptType.TASK_CANCELED, body={"result_summary": "Canceled by owner"})
    return receipt, _task(result=_terminal_result(Outcome.CANCELED))


def _escalated() -> tuple[Receipt, Task]:
    receipt = _receipt(
        ReceiptType.TASK_ESCALATED,
        body={
            "escalation_class": "capability",
            "escalation_reason": "No worker advertises the required capability.",
            "escalation_to": "agent:supervisor",
        },
    )
    return receipt, _task()


ALL_CASES = {
    "accepted": _accepted,
    "complete_success_with_artifact": _completed_with_artifact,
    "complete_failure": _failed,
    "complete_canceled": _canceled,
    "escalate": _escalated,
}


@pytest.mark.parametrize("case_name", sorted(ALL_CASES))
def test_emitted_receipt_matches_canonical_schema(validator: Any, case_name: str) -> None:
    """Every receipt AsyncGate emits must validate against the canonical schema."""
    receipt, task = ALL_CASES[case_name]()
    payload = to_memorygate_receipt(receipt, task)
    errors = _errors(validator, payload)
    assert not errors, f"{case_name} produced an invalid receipt:\n  " + "\n  ".join(errors)


@pytest.mark.parametrize("case_name", sorted(ALL_CASES))
def test_emitted_receipt_always_carries_body(case_name: str) -> None:
    """Regression guard: `body` is a required schema field and must be an object.

    This is the exact defect that broke every golden path in the stack for six
    months -- the schema gained a required `body` and no emitter test caught it.
    """
    receipt, task = ALL_CASES[case_name]()
    payload = to_memorygate_receipt(receipt, task)
    assert "body" in payload, f"{case_name}: emitted receipt is missing `body`"
    assert isinstance(payload["body"], dict), f"{case_name}: `body` must be a JSON object"


@pytest.mark.parametrize("case_name", sorted(ALL_CASES))
def test_emitted_receipt_has_every_required_field(validator: Any, case_name: str) -> None:
    """Guards against a required field being added to the schema and never emitted."""
    receipt, task = ALL_CASES[case_name]()
    payload = to_memorygate_receipt(receipt, task)
    missing = [field for field in validator.schema.get("required", []) if field not in payload]
    assert not missing, f"{case_name}: adapter omits required schema field(s): {missing}"


def test_escalation_receipt_preserves_routing_invariant() -> None:
    """Core invariant: recipient_ai == escalation_to when phase == 'escalate'.

    ReceiptGate rejects violations, so an emitter that got this wrong would fail
    only at runtime against a live ledger.
    """
    receipt, task = _escalated()
    payload = to_memorygate_receipt(receipt, task)
    assert payload["phase"] == "escalate"
    assert payload["recipient_ai"] == payload["escalation_to"] == "agent:supervisor"


def test_accepted_receipt_is_not_prematurely_terminal() -> None:
    """An accepted receipt opens an obligation; it must not carry completion state."""
    receipt, task = _accepted()
    payload = to_memorygate_receipt(receipt, task)
    assert payload["phase"] == "accepted"
    assert payload["status"] == "NA"
    assert payload["completed_at"] is None
    assert payload["outcome_kind"] == "NA"


def test_completed_receipt_externalizes_artifact_by_pointer() -> None:
    """Artifacts travel as pointers/hashes, never inline blobs (DepotGate contract)."""
    receipt, task = _completed_with_artifact()
    payload = to_memorygate_receipt(receipt, task)
    assert payload["phase"] == "complete"
    assert payload["status"] == "success"
    assert payload["outcome_kind"] in {"artifact_pointer", "mixed"}
    assert payload["artifact_pointer"] == ARTIFACT_REF["uri"]
    assert payload["artifact_checksum"] == ARTIFACT_REF["checksum"]
    assert payload["artifact_mime"] == ARTIFACT_REF["mime"]
    assert payload["artifact_refs"] == [ARTIFACT_REF]


# --- who each receipt says is responsible ----------------------------------
#
# `for_principal` is the executor (taskee) and `from_principal` the principal
# performing the transition. ReceiptGate derives custody from `for_principal`
# and checks the actor against `from_principal`, so getting these wrong does not
# produce a schema error -- it produces an obligation held by the wrong party
# and completions refused ACTOR_NOT_CUSTODIAN.
#
# Both were previously set to the task owner. That is survivable only while
# every identity in a flow is the same principal; escalation to a real worker
# broke it, and the escalation demo could never complete.

WORKER = Principal(kind=PrincipalKind.WORKER, id="worker-1")


def _from_worker(receipt_type: ReceiptType, **kw: Any) -> Receipt:
    """A receipt whose `from` is the worker, as the engine emits these."""
    return Receipt(
        receipt_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        receipt_type=receipt_type,
        created_at=NOW,
        task_id=TASK_ID,
        body=kw.pop("body", {}) or {},
        **{"from": WORKER, "to": AGENT},
    )


def test_acceptance_makes_the_worker_the_executor():
    """Custody must land on the worker, not on the requester."""
    payload = to_memorygate_receipt(_from_worker(ReceiptType.TASK_ACCEPTED), _task())
    assert payload["for_principal"] == "worker-1"
    # The requester proposes the acceptance; the executor is who takes it on.
    assert payload["from_principal"] == "demo"


def test_completion_is_performed_by_the_executor():
    """The actor on a completion must be the principal that holds it.

    ReceiptGate resolves the transition actor from `from_principal`. If that is
    the task owner while custody sits with the worker, every completion is
    refused -- which is exactly what happened once escalation moved custody.
    """
    result = _terminal_result(Outcome.SUCCEEDED)
    payload = to_memorygate_receipt(
        _from_worker(ReceiptType.TASK_COMPLETED), _task(result=result)
    )
    assert payload["for_principal"] == "worker-1"
    assert payload["from_principal"] == "worker-1"


def test_escalation_is_issued_by_the_current_holder():
    """Only the custodian may hand an obligation on."""
    payload = to_memorygate_receipt(
        _from_worker(
            ReceiptType.TASK_ESCALATED,
            body={
                "escalation_class": "policy",
                "escalation_reason": "lease_expired",
                "escalation_to": "fallback-worker",
            },
        ),
        _task(),
    )
    assert payload["from_principal"] == "worker-1", "the holder escalates, not the service"
    assert payload["escalation_to"] == "fallback-worker"
    assert payload["recipient_ai"] == "fallback-worker", "routing invariant"


def test_an_offer_is_not_forwarded_as_a_governance_transition():
    """task.assigned is the OFFER operational event, not an ACCEPT.

    Forwarding it made every task propose ACCEPT twice against one
    obligation_id -- once for the offer, once for the worker's acceptance -- and
    the second was always refused.
    """
    from asyncgate.engine import core

    source = Path(core.__file__).read_text(encoding="utf-8")
    start = source.index("eligible = {")
    block = source[start : source.index("}", start)]
    assert "TASK_ASSIGNED" not in block, (
        "task.assigned is forwarded to the ledger again; it is an offer, and "
        "an offer does not create an obligation"
    )
    assert "TASK_ACCEPTED" in block, "acceptance must still be forwarded"
