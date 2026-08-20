"""P1.4 tests: durable ReceiptGate buffering and replay.

These use `complete`, not `accepted`. Acceptance is not bufferable: it is the
mutual-exclusion point, and a claim that waits in a local queue while the ledger
is unreachable is a second custodian waiting to happen. `transitions.v1.json`
declares which transitions may buffer, and the client reads it from there.

Completion is single-writer -- only the established custodian can issue one --
so buffering it asserts nothing false about who holds what.
"""

from pathlib import Path

import pytest

from asyncgate.config import settings
from asyncgate.integrations.memorygate_client import ReceiptGateClient


@pytest.fixture
def receiptgate_buffer_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Apply deterministic receipt buffering settings for tests."""
    buffer_path = tmp_path / "receiptgate_buffer.json"
    monkeypatch.setattr(settings, "receiptgate_endpoint", "http://receiptgate.test")
    monkeypatch.setattr(settings, "receiptgate_auth_token", None)
    monkeypatch.setattr(settings, "receiptgate_circuit_breaker_enabled", False)
    monkeypatch.setattr(settings, "receiptgate_emission_buffer_size", 10)
    monkeypatch.setattr(settings, "receiptgate_emission_retry_interval_seconds", 1)
    monkeypatch.setattr(settings, "receiptgate_emission_max_retries", 2)
    monkeypatch.setattr(settings, "receiptgate_emission_buffer_path", str(buffer_path))
    return buffer_path


@pytest.mark.asyncio
async def test_receipt_emission_failure_is_durably_buffered(
    monkeypatch: pytest.MonkeyPatch,
    receiptgate_buffer_settings: Path,
):
    """Failed sends should be persisted for replay, not dropped."""
    client = ReceiptGateClient(
        buffer_path=receiptgate_buffer_settings,
        start_replay_worker=False,
    )

    async def _failing_emit(receipt_data):
        raise RuntimeError("receiptgate unavailable")

    monkeypatch.setattr(client, "_emit_to_receiptgate", _failing_emit)

    result = await client.emit_receipt({"receipt_id": "r-001", "phase": "complete"})
    stats = client.get_buffer_stats()

    assert result["status"] == "buffered"
    assert stats["pending_count"] == 1
    assert receiptgate_buffer_settings.exists()

    await client.close()


@pytest.mark.asyncio
async def test_buffer_reloads_after_client_restart(
    monkeypatch: pytest.MonkeyPatch,
    receiptgate_buffer_settings: Path,
):
    """A new client instance should load previously buffered receipts from disk."""
    client = ReceiptGateClient(
        buffer_path=receiptgate_buffer_settings,
        start_replay_worker=False,
    )

    async def _failing_emit(receipt_data):
        raise RuntimeError("receiptgate unavailable")

    monkeypatch.setattr(client, "_emit_to_receiptgate", _failing_emit)
    await client.emit_receipt({"receipt_id": "r-002", "phase": "complete"})
    await client.close()

    restarted_client = ReceiptGateClient(
        buffer_path=receiptgate_buffer_settings,
        start_replay_worker=False,
    )
    stats = restarted_client.get_buffer_stats()

    assert stats["pending_count"] == 1
    assert stats["due_count"] == 1

    await restarted_client.close()


@pytest.mark.asyncio
async def test_replay_flushes_buffer_when_receiptgate_recovers(
    monkeypatch: pytest.MonkeyPatch,
    receiptgate_buffer_settings: Path,
):
    """Replay should remove buffered receipts after successful resend."""
    client = ReceiptGateClient(
        buffer_path=receiptgate_buffer_settings,
        start_replay_worker=False,
    )

    async def _failing_emit(receipt_data):
        raise RuntimeError("receiptgate unavailable")

    monkeypatch.setattr(client, "_emit_to_receiptgate", _failing_emit)
    await client.emit_receipt({"receipt_id": "r-003", "phase": "complete"})

    async def _successful_emit(receipt_data):
        return {"status": "ok", "receipt_id": receipt_data["receipt_id"]}

    monkeypatch.setattr(client, "_emit_to_receiptgate", _successful_emit)

    summary = await client.replay_buffered_receipts(limit=10)
    stats = client.get_buffer_stats()

    assert summary["replayed"] == 1
    assert stats["pending_count"] == 0
    assert stats["replayed_success_count"] >= 1

    await client.close()
