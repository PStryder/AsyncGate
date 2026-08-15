"""ReceiptGate client with circuit breaker protection and durable buffering."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from asyncgate.config import settings
from asyncgate.integrations.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpen,
)
from asyncgate.utils.time import utc_now

logger = logging.getLogger(__name__)


@dataclass
class BufferedReceipt:
    """Durable receipt buffer item."""

    buffer_id: str
    receipt: dict[str, Any]
    queued_at: datetime
    next_retry_at: datetime
    retry_count: int = 0
    last_error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "buffer_id": self.buffer_id,
            "receipt": self.receipt,
            "queued_at": self.queued_at.isoformat(),
            "next_retry_at": self.next_retry_at.isoformat(),
            "retry_count": self.retry_count,
            "last_error": self.last_error,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> BufferedReceipt:
        return cls(
            buffer_id=str(payload["buffer_id"]),
            receipt=dict(payload["receipt"]),
            queued_at=datetime.fromisoformat(payload["queued_at"]),
            next_retry_at=datetime.fromisoformat(payload["next_retry_at"]),
            retry_count=int(payload.get("retry_count", 0)),
            last_error=payload.get("last_error"),
        )


class ReceiptGateClient:
    """
    Client for ReceiptGate integration with circuit breaker protection.

    If receipt emission fails, receipts are buffered on disk and replayed
    asynchronously using the configured retry policy.
    """

    def __init__(
        self,
        *,
        buffer_path: Path | None = None,
        start_replay_worker: bool = True,
    ):
        self._circuit_breaker: CircuitBreaker | None = None
        self._buffer_path = Path(
            buffer_path or settings.receiptgate_emission_buffer_path
        )
        self._buffer_lock = asyncio.Lock()
        self._buffered_receipts: list[BufferedReceipt] = []
        self._replay_task: asyncio.Task | None = None
        self._stop_replay_event = asyncio.Event()
        self._dropped_count = 0
        self._replayed_success_count = 0
        self._replayed_failure_count = 0
        self._last_replay_error: str | None = None

        self._initialize_circuit_breaker()
        self._load_buffer_from_disk()
        if start_replay_worker:
            self.start_replay_worker()

    def _initialize_circuit_breaker(self):
        """Initialize circuit breaker if enabled."""
        if not settings.receiptgate_circuit_breaker_enabled:
            logger.info("ReceiptGate circuit breaker disabled")
            return

        config = CircuitBreakerConfig(
            failure_threshold=settings.receiptgate_circuit_breaker_failure_threshold,
            timeout_seconds=settings.receiptgate_circuit_breaker_timeout_seconds,
            half_open_max_calls=settings.receiptgate_circuit_breaker_half_open_max_calls,
            success_threshold=settings.receiptgate_circuit_breaker_success_threshold,
            on_open=lambda: logger.error("ReceiptGate circuit breaker opened"),
            on_close=lambda: logger.info("ReceiptGate circuit breaker closed"),
            on_half_open=lambda: logger.info("ReceiptGate circuit breaker half-open"),
        )

        self._circuit_breaker = CircuitBreaker("receiptgate", config)
        logger.info("ReceiptGate circuit breaker initialized")

    def _load_buffer_from_disk(self) -> None:
        """Load persisted buffer entries at startup."""
        if not self._buffer_path.exists():
            return

        try:
            payload = json.loads(self._buffer_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                raw_items = payload.get("items", [])
            elif isinstance(payload, list):
                raw_items = payload
            else:
                raw_items = []

            loaded: list[BufferedReceipt] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                try:
                    loaded.append(BufferedReceipt.from_payload(item))
                except Exception:
                    continue
            max_items = max(1, settings.receiptgate_emission_buffer_size)
            self._buffered_receipts = loaded[-max_items:]
            if self._buffered_receipts:
                logger.info(
                    "Loaded %s buffered ReceiptGate receipts from disk",
                    len(self._buffered_receipts),
                )
        except Exception as exc:
            logger.warning(
                "Failed loading receipt buffer from %s: %s",
                self._buffer_path,
                exc,
            )
            self._buffered_receipts = []

    async def _persist_buffer_locked(self) -> None:
        """Persist current buffer to disk (lock must already be held)."""
        self._buffer_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "items": [item.to_payload() for item in self._buffered_receipts],
        }
        tmp_path = self._buffer_path.with_suffix(f"{self._buffer_path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(payload, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp_path.replace(self._buffer_path)

    def start_replay_worker(self) -> None:
        """Start background replay worker if an event loop is running."""
        if self._replay_task and not self._replay_task.done():
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        self._stop_replay_event.clear()
        self._replay_task = loop.create_task(
            self._replay_loop(),
            name="receiptgate-replay-worker",
        )

    async def close(self) -> None:
        """Stop replay worker and flush in-memory state."""
        self._stop_replay_event.set()
        if self._replay_task and not self._replay_task.done():
            try:
                await asyncio.wait_for(self._replay_task, timeout=2.0)
            except TimeoutError:
                self._replay_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._replay_task
        self._replay_task = None

    async def _emit_with_circuit(
        self,
        receipt_data: dict[str, Any],
        *,
        use_fallback: bool,
    ) -> dict[str, Any]:
        if self._circuit_breaker:
            fallback = self._fallback_to_buffer if use_fallback else None
            return await self._circuit_breaker.call(
                self._emit_to_receiptgate,
                receipt_data,
                fallback=fallback,
            )
        return await self._emit_to_receiptgate(receipt_data)

    async def emit_receipt(self, receipt_data: dict[str, Any]) -> dict[str, Any]:
        """Emit a LegiVellum receipt to ReceiptGate with durable fallback."""
        if not settings.receiptgate_endpoint:
            return {"status": "skipped", "note": "receiptgate_endpoint not configured"}

        self.start_replay_worker()

        try:
            return await self._emit_with_circuit(receipt_data, use_fallback=True)
        except Exception as exc:
            logger.warning(
                "ReceiptGate emission failed, buffering for replay",
                extra={"error": str(exc)},
            )
            return await self._buffer_receipt(receipt_data, error=str(exc))

    async def _emit_to_receiptgate(self, receipt_data: dict[str, Any]) -> dict[str, Any]:
        """POST receipt payload to ReceiptGate via MCP."""
        base_url = settings.receiptgate_endpoint.rstrip("/")
        if not base_url.endswith("/mcp"):
            base_url = f"{base_url}/mcp"

        headers = {"Content-Type": "application/json"}
        if settings.receiptgate_auth_token:
            headers["Authorization"] = f"Bearer {settings.receiptgate_auth_token}"

        timeout = settings.receiptgate_emission_timeout_ms / 1000
        async with httpx.AsyncClient(timeout=timeout) as client:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "receiptgate.submit_receipt",
                    "arguments": {"receipt": receipt_data},
                },
            }
            response = await client.post(base_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise RuntimeError(f"ReceiptGate error: {data['error']}")
            return data.get("result", {})

    def _retry_delay_seconds(self, retry_count: int) -> int:
        """Exponential backoff capped to one hour."""
        base = max(1, settings.receiptgate_emission_retry_interval_seconds)
        return min(base * (2 ** max(0, retry_count - 1)), 3600)

    async def _buffer_receipt(
        self,
        receipt_data: dict[str, Any],
        *,
        error: str | None,
    ) -> dict[str, Any]:
        """Persist a receipt to the durable replay buffer."""
        now = utc_now()
        item = BufferedReceipt(
            buffer_id=str(uuid4()),
            receipt=receipt_data,
            queued_at=now,
            next_retry_at=now,
            retry_count=0,
            last_error=error,
        )

        async with self._buffer_lock:
            max_items = max(1, settings.receiptgate_emission_buffer_size)
            if len(self._buffered_receipts) >= max_items:
                dropped = self._buffered_receipts.pop(0)
                self._dropped_count += 1
                logger.error(
                    "Receipt buffer full; dropping oldest buffered receipt %s",
                    dropped.buffer_id,
                )

            self._buffered_receipts.append(item)
            await self._persist_buffer_locked()
            pending_count = len(self._buffered_receipts)

        self.start_replay_worker()
        return {
            "status": "buffered",
            "buffer_id": item.buffer_id,
            "pending_count": pending_count,
        }

    async def _fallback_to_buffer(self, receipt_data: dict[str, Any]) -> dict[str, Any]:
        """
        Fallback when circuit is open - persist and replay later.
        """
        logger.warning(
            "ReceiptGate circuit open, buffering receipt",
            extra={"phase": receipt_data.get("phase", "unknown")},
        )
        return await self._buffer_receipt(receipt_data, error="circuit_open")

    async def _remove_buffer_item(self, buffer_id: str) -> bool:
        async with self._buffer_lock:
            for idx, item in enumerate(self._buffered_receipts):
                if item.buffer_id == buffer_id:
                    self._buffered_receipts.pop(idx)
                    await self._persist_buffer_locked()
                    return True
        return False

    async def _schedule_retry(self, buffer_id: str, error: str) -> None:
        async with self._buffer_lock:
            for idx, item in enumerate(self._buffered_receipts):
                if item.buffer_id != buffer_id:
                    continue

                max_retries = max(0, settings.receiptgate_emission_max_retries)
                if item.retry_count >= max_retries:
                    self._buffered_receipts.pop(idx)
                    self._dropped_count += 1
                    logger.error(
                        "Dropping buffered receipt %s after %s retries",
                        buffer_id,
                        item.retry_count,
                    )
                else:
                    item.retry_count += 1
                    item.last_error = error
                    item.next_retry_at = utc_now() + timedelta(
                        seconds=self._retry_delay_seconds(item.retry_count)
                    )

                await self._persist_buffer_locked()
                return

    async def replay_buffered_receipts(self, *, limit: int = 50) -> dict[str, int]:
        """Replay due buffered receipts to ReceiptGate."""
        summary = {"replayed": 0, "failed": 0, "blocked": 0, "remaining": 0}
        if not settings.receiptgate_endpoint:
            summary["remaining"] = len(self._buffered_receipts)
            return summary

        now = utc_now()
        async with self._buffer_lock:
            due_items = [
                item
                for item in self._buffered_receipts
                if item.next_retry_at <= now
            ]
            due_items.sort(key=lambda item: item.queued_at)
            due_items = due_items[: max(1, limit)]

        for item in due_items:
            try:
                await self._emit_with_circuit(item.receipt, use_fallback=False)
                removed = await self._remove_buffer_item(item.buffer_id)
                if removed:
                    self._replayed_success_count += 1
                    summary["replayed"] += 1
            except CircuitBreakerOpen as exc:
                self._last_replay_error = str(exc)
                self._replayed_failure_count += 1
                summary["blocked"] += 1
                await self._schedule_retry(item.buffer_id, str(exc))
                break
            except Exception as exc:
                self._last_replay_error = str(exc)
                self._replayed_failure_count += 1
                summary["failed"] += 1
                await self._schedule_retry(item.buffer_id, str(exc))

        summary["remaining"] = len(self._buffered_receipts)
        return summary

    async def _replay_loop(self) -> None:
        """Background replay worker for buffered receipts."""
        interval = max(1, settings.receiptgate_emission_retry_interval_seconds)
        while not self._stop_replay_event.is_set():
            try:
                summary = await self.replay_buffered_receipts()
                if summary["replayed"] > 0:
                    logger.info(
                        "Replayed %s buffered receipts (%s remaining)",
                        summary["replayed"],
                        summary["remaining"],
                    )
            except Exception as exc:
                self._last_replay_error = str(exc)
                logger.warning("Receipt replay worker error: %s", exc)

            try:
                await asyncio.wait_for(self._stop_replay_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    def get_circuit_stats(self) -> dict[str, Any] | None:
        """Get circuit breaker statistics."""
        if not self._circuit_breaker:
            return None

        stats = self._circuit_breaker.stats
        return {
            "state": stats.state.value,
            "failure_count": stats.failure_count,
            "success_count": stats.success_count,
            "total_calls": stats.total_calls,
            "total_failures": stats.total_failures,
            "total_successes": stats.total_successes,
            "opened_at": stats.opened_at.isoformat() if stats.opened_at else None,
            "last_failure_time": (
                stats.last_failure_time.isoformat() if stats.last_failure_time else None
            ),
        }

    def get_buffer_stats(self) -> dict[str, Any]:
        """Get durable buffer health and replay metrics."""
        now = utc_now()
        due_count = sum(
            1 for item in self._buffered_receipts if item.next_retry_at <= now
        )
        next_retry = min(
            (item.next_retry_at for item in self._buffered_receipts),
            default=None,
        )
        return {
            "path": str(self._buffer_path),
            "pending_count": len(self._buffered_receipts),
            "due_count": due_count,
            "dropped_count": self._dropped_count,
            "replayed_success_count": self._replayed_success_count,
            "replayed_failure_count": self._replayed_failure_count,
            "last_replay_error": self._last_replay_error,
            "next_retry_at": next_retry.isoformat() if next_retry else None,
            "worker_running": bool(self._replay_task and not self._replay_task.done()),
        }

    async def reset_circuit(self):
        """Manually reset circuit breaker."""
        if self._circuit_breaker:
            await self._circuit_breaker.reset()


# Singleton instance
_receiptgate_client: ReceiptGateClient | None = None


def get_receiptgate_client() -> ReceiptGateClient:
    """Get or create ReceiptGate client singleton."""
    global _receiptgate_client
    if _receiptgate_client is None:
        _receiptgate_client = ReceiptGateClient()
    return _receiptgate_client


async def shutdown_receiptgate_client() -> None:
    """Shutdown and clear singleton client state."""
    global _receiptgate_client
    if _receiptgate_client is not None:
        await _receiptgate_client.close()
    _receiptgate_client = None
