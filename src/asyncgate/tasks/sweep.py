"""Lease expiry sweep background task."""

import asyncio
import logging
import random
from typing import Optional

from asyncgate.config import settings
from asyncgate.db.base import get_session
from asyncgate.engine import AsyncGateEngine
from asyncgate.observability.trace import ensure_trace_id

logger = logging.getLogger("asyncgate.sweep")

_sweep_task: Optional[asyncio.Task] = None
_shutdown_event: Optional[asyncio.Event] = None


async def lease_sweep_loop():
    """
    Background loop that expires stale leases and requeues tasks.

    This is NOT polling workers. It is internal state maintenance:
    - Find leases where expires_at < now and task not terminal
    - Mark lease expired
    - Transition task leased -> queued
    - Increment attempt if appropriate
    - Emit lease.expired receipt (to agent; optionally to worker)
    
    Anti-storm protections:
    - Jittered sweep interval (±20% randomization) prevents synchronized sweeps
    - Small batch processing (20 tasks/batch) prevents transaction pile-up
    - Requeue time jitter (0-5s) prevents thundering herd on claim
    """
    base_interval = settings.lease_sweep_interval_seconds
    logger.info(
        f"Lease sweep loop started (base interval: {base_interval}s with ±20% jitter)"
    )

    while not _shutdown_event.is_set():
        try:
            ensure_trace_id()
            async with get_session() as session:
                engine = AsyncGateEngine(session)
                expired_count = await engine.expire_leases(batch_size=20)

                if expired_count > 0:
                    logger.info(f"Expired {expired_count} leases and requeued tasks")

        except Exception as e:
            logger.error(f"Lease sweep error: {e}", exc_info=True)

        # Add jitter to interval: ±20% randomization
        # Prevents multiple instances from sweeping in lockstep
        jitter_factor = random.uniform(0.8, 1.2)
        jittered_interval = base_interval * jitter_factor
        
        # Wait for next sweep interval or shutdown
        try:
            await asyncio.wait_for(
                _shutdown_event.wait(),
                timeout=jittered_interval,
            )
        except asyncio.TimeoutError:
            pass  # Continue loop

    logger.info("Lease sweep loop stopped")


async def start_lease_sweep():
    """Start the lease sweep background task."""
    global _sweep_task, _shutdown_event

    _shutdown_event = asyncio.Event()
    _sweep_task = asyncio.create_task(lease_sweep_loop())


async def stop_lease_sweep():
    """Stop the lease sweep background task."""
    global _sweep_task, _shutdown_event

    if _shutdown_event:
        _shutdown_event.set()

    if _sweep_task:
        try:
            await asyncio.wait_for(_sweep_task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Lease sweep task did not stop gracefully, cancelling")
            _sweep_task.cancel()
            try:
                await _sweep_task
            except asyncio.CancelledError:
                pass

    _sweep_task = None
    _shutdown_event = None
