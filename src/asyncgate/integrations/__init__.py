"""External service integrations and resilience patterns."""

from asyncgate.integrations.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpen,
    CircuitBreakerStats,
    CircuitState,
)
from asyncgate.integrations.memorygate_client import (
    ReceiptGateClient,
    get_receiptgate_client,
    shutdown_receiptgate_client,
)

from .metagate_client import (
    acknowledge_startup,
    bootstrap_from_metagate,
)

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerOpen",
    "CircuitBreakerStats",
    "CircuitState",
    "ReceiptGateClient",
    "get_receiptgate_client",
    "shutdown_receiptgate_client",
    "acknowledge_startup",
    "bootstrap_from_metagate",
]
