"""Acceptance cannot wait in a queue, and a buffered transition is not committed.

Two rules, both about the difference between "recorded locally" and
"authoritative":

**Acceptance is synchronous and unbufferable.** It is the mutual-exclusion point
-- the moment two candidates resolve to one holder -- and that resolution cannot
be made locally by either candidate. If the notary is unreachable, acceptance
fails; it does not queue. A buffered claim on a contested obligation is a second
custodian waiting to happen.

**A buffered transition must not advance the emitter's state machine.**
Completion and escalation may buffer, because only the established custodian can
issue them, so buffering asserts nothing false about who holds what. But a
worker whose `complete` sits in the outbox has not completed: it may not release
custody, report closure, or treat the obligation as discharged. The ledger
decides when that became true.

Which transitions may buffer is declared in transitions.v1.json, not duplicated
here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from asyncgate.config import settings
from asyncgate.integrations.memorygate_client import (
    ReceiptGateClient,
    ReceiptUnbufferable,
    _phase_may_buffer,
)


@pytest.fixture
def buffered_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ReceiptGateClient:
    monkeypatch.setattr(settings, "receiptgate_endpoint", "http://receiptgate.test")
    monkeypatch.setattr(settings, "receiptgate_auth_token", None)
    monkeypatch.setattr(settings, "receiptgate_circuit_breaker_enabled", False)
    monkeypatch.setattr(
        settings, "receiptgate_emission_buffer_path", str(tmp_path / "buffer.json")
    )
    client = ReceiptGateClient()

    async def unreachable(_payload):
        raise ConnectionError("receiptgate unavailable")

    monkeypatch.setattr(client, "_emit_to_receiptgate", unreachable)
    return client


@pytest.mark.asyncio
async def test_acceptance_fails_rather_than_buffering(buffered_client):
    with pytest.raises(ReceiptUnbufferable):
        await buffered_client.emit_receipt({"receipt_id": "r-a", "phase": "accepted"})

    assert buffered_client._buffered_receipts == [], (
        "an acceptance was queued; a claim that waits locally while the ledger "
        "is unreachable can be granted twice"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["complete", "escalate"])
async def test_single_writer_transitions_may_buffer(buffered_client, phase):
    """The other half: these must still buffer, or an outage loses receipts."""
    result = await buffered_client.emit_receipt({"receipt_id": f"r-{phase}", "phase": phase})
    assert result["status"] == "buffered"
    assert len(buffered_client._buffered_receipts) == 1


@pytest.mark.asyncio
async def test_an_unknown_phase_is_treated_as_unbufferable(buffered_client):
    """Failing loudly on a transition nobody has classified is the safe way."""
    with pytest.raises(ReceiptUnbufferable):
        await buffered_client.emit_receipt({"receipt_id": "r-?", "phase": "invented"})


@pytest.mark.asyncio
async def test_an_open_circuit_does_not_reopen_the_hole(monkeypatch, tmp_path):
    """An open circuit is still an unreachable ledger.

    Guarding only the exception path would leave acceptance bufferable whenever
    the breaker happened to be tripped.
    """
    monkeypatch.setattr(settings, "receiptgate_endpoint", "http://receiptgate.test")
    monkeypatch.setattr(
        settings, "receiptgate_emission_buffer_path", str(tmp_path / "buffer.json")
    )
    client = ReceiptGateClient()

    with pytest.raises(ReceiptUnbufferable):
        await client._fallback_to_buffer({"receipt_id": "r-x", "phase": "accepted"})


def test_bufferability_comes_from_the_model_not_from_a_local_list():
    """A second copy of the judgement in AsyncGate is a second thing to keep true."""
    from legivellum.transitions import may_buffer

    assert _phase_may_buffer("accepted") is may_buffer("ACCEPT") is False
    assert _phase_may_buffer("complete") is may_buffer("COMPLETE") is True
    assert _phase_may_buffer("escalate") is may_buffer("ESCALATE") is True
