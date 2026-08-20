"""Circuit breaker pattern for external service resilience."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

from asyncgate.utils.time import utc_now

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation, requests pass through
    OPEN = "open"  # Circuit broken, requests fail fast
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker configuration."""

    failure_threshold: int = 5  # Failures before opening
    timeout_seconds: int = 60  # Time before attempting half-open
    half_open_max_calls: int = 3  # Test calls in half-open state
    success_threshold: int = 2  # Successes to close from half-open

    # Exceptions that are answers rather than outages. A dependency that
    # evaluates a request and rejects it is healthy -- it just said no -- so
    # counting that toward the failure threshold trips the breaker on correct
    # behaviour and then degrades every later call. Raised to the caller
    # unchanged; simply not held against the dependency.
    non_failure_exceptions: tuple[type[BaseException], ...] = ()

    # Callback hooks (optional)
    on_open: Callable[[], None] | None = None
    on_close: Callable[[], None] | None = None
    on_half_open: Callable[[], None] | None = None


@dataclass
class CircuitBreakerStats:
    """Circuit breaker statistics."""

    state: CircuitState
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: datetime | None = None
    opened_at: datetime | None = None
    half_open_calls: int = 0
    total_calls: int = 0
    total_failures: int = 0
    total_successes: int = 0


class CircuitBreakerOpen(Exception):
    """Exception raised when circuit breaker is open."""

    def __init__(self, service_name: str, retry_after: int):
        self.service_name = service_name
        self.retry_after = retry_after
        super().__init__(
            f"Circuit breaker open for {service_name}, retry after {retry_after}s"
        )


class CircuitBreaker:
    """
    Generic circuit breaker for external service calls.

    State transitions:
    - CLOSED → OPEN: After failure_threshold consecutive failures
    - OPEN → HALF_OPEN: After timeout_seconds elapsed
    - HALF_OPEN → CLOSED: After success_threshold consecutive successes
    - HALF_OPEN → OPEN: On any failure

    Thread-safe via asyncio locks.
    """

    def __init__(self, name: str, config: CircuitBreakerConfig):
        self.name = name
        self.config = config
        self._state = CircuitState.CLOSED
        self._stats = CircuitBreakerStats(state=self._state)
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Get current circuit state."""
        return self._state

    @property
    def stats(self) -> CircuitBreakerStats:
        """Get current statistics."""
        return CircuitBreakerStats(
            state=self._state,
            failure_count=self._stats.failure_count,
            success_count=self._stats.success_count,
            last_failure_time=self._stats.last_failure_time,
            opened_at=self._stats.opened_at,
            half_open_calls=self._stats.half_open_calls,
            total_calls=self._stats.total_calls,
            total_failures=self._stats.total_failures,
            total_successes=self._stats.total_successes,
        )

    async def call(
        self,
        func: Callable[..., Any],
        *args: Any,
        fallback: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Execute function through circuit breaker.

        Args:
            func: Async function to call
            *args: Positional arguments for func
            fallback: Optional fallback function if circuit is open
            **kwargs: Keyword arguments for func

        Returns:
            Result from func or fallback

        Raises:
            CircuitBreakerOpen: If circuit is open and no fallback provided
            Exception: Any exception from func (after recording)
        """
        async with self._lock:
            self._stats.total_calls += 1

            # Check if circuit should transition from OPEN to HALF_OPEN
            if self._state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    await self._transition_to_half_open()
                else:
                    # Circuit still open, fail fast
                    if fallback:
                        logger.debug(f"Circuit {self.name} open, using fallback")
                        if asyncio.iscoroutinefunction(fallback):
                            return await fallback(*args, **kwargs)
                        return fallback(*args, **kwargs)

                    retry_after = self._seconds_until_half_open()
                    raise CircuitBreakerOpen(self.name, retry_after)

            # Check half-open call limit
            if self._state == CircuitState.HALF_OPEN:
                if self._stats.half_open_calls >= self.config.half_open_max_calls:
                    logger.warning(
                        f"Circuit {self.name} half-open limit reached, "
                        f"rejecting call"
                    )
                    if fallback:
                        if asyncio.iscoroutinefunction(fallback):
                            return await fallback(*args, **kwargs)
                        return fallback(*args, **kwargs)
                    raise CircuitBreakerOpen(
                        self.name,
                        self._seconds_until_half_open(),
                    )
                self._stats.half_open_calls += 1

        # Execute function (outside lock to avoid blocking)
        try:
            result = await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else func(*args, **kwargs)
            await self._on_success()
            return result
        except self.config.non_failure_exceptions:
            # A verdict, not an outage. Deliberately recorded as neither a
            # success nor a failure: it says nothing about reachability.
            raise
        except Exception as e:
            await self._on_failure(e)
            raise

    async def _on_success(self):
        """Record successful call."""
        async with self._lock:
            self._stats.success_count += 1
            self._stats.total_successes += 1
            self._stats.failure_count = 0  # Reset failure counter

            if self._state == CircuitState.HALF_OPEN:
                if self._stats.success_count >= self.config.success_threshold:
                    await self._transition_to_closed()

    async def _on_failure(self, error: Exception):
        """Record failed call."""
        async with self._lock:
            self._stats.failure_count += 1
            self._stats.total_failures += 1
            self._stats.success_count = 0  # Reset success counter
            self._stats.last_failure_time = utc_now()

            logger.warning(
                f"Circuit {self.name} failure ({self._stats.failure_count}/"
                f"{self.config.failure_threshold}): {error}"
            )

            # Transition based on state
            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open immediately reopens
                await self._transition_to_open()
            elif self._state == CircuitState.CLOSED:
                if self._stats.failure_count >= self.config.failure_threshold:
                    await self._transition_to_open()

    async def _transition_to_open(self):
        """Transition to OPEN state."""
        self._state = CircuitState.OPEN
        self._stats.opened_at = utc_now()
        self._stats.half_open_calls = 0
        logger.error(
            f"Circuit {self.name} opened after "
            f"{self._stats.failure_count} failures"
        )
        if self.config.on_open:
            self.config.on_open()

    async def _transition_to_half_open(self):
        """Transition to HALF_OPEN state."""
        self._state = CircuitState.HALF_OPEN
        self._stats.success_count = 0
        self._stats.failure_count = 0
        self._stats.half_open_calls = 0
        logger.info(f"Circuit {self.name} entering half-open state")
        if self.config.on_half_open:
            self.config.on_half_open()

    async def _transition_to_closed(self):
        """Transition to CLOSED state."""
        self._state = CircuitState.CLOSED
        self._stats.failure_count = 0
        self._stats.success_count = 0
        self._stats.opened_at = None
        self._stats.half_open_calls = 0
        logger.info(f"Circuit {self.name} closed after recovery")
        if self.config.on_close:
            self.config.on_close()

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to try half-open."""
        if not self._stats.opened_at:
            return False
        elapsed = (utc_now() - self._stats.opened_at).total_seconds()
        return elapsed >= self.config.timeout_seconds

    def _seconds_until_half_open(self) -> int:
        """Calculate seconds until half-open attempt."""
        if not self._stats.opened_at:
            return self.config.timeout_seconds
        elapsed = (utc_now() - self._stats.opened_at).total_seconds()
        remaining = max(0, self.config.timeout_seconds - elapsed)
        return int(remaining)

    async def reset(self):
        """Manually reset circuit to CLOSED state."""
        async with self._lock:
            logger.info(f"Circuit {self.name} manually reset")
            await self._transition_to_closed()
