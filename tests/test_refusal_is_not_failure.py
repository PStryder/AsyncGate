"""A ledger refusal is a verdict, not an outage.

The circuit breaker counted governance refusals toward its failure threshold.
Five of them opened the circuit, after which *every* subsequent receipt --
including legitimate ones -- was buffered rather than committed, while AsyncGate
went on returning success to its callers.

That is the failure mode Slice Zero exists to eliminate, reached by treating a
correct answer as a broken dependency. A refusal means the ledger was reached
and said no: retrying sends the identical receipt to the identical rule, and
buffering it stores something that will never commit.
"""

from __future__ import annotations

import pytest

from asyncgate.integrations.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)
from asyncgate.integrations.memorygate_client import (
    GOVERNANCE_REFUSALS,
    ReceiptRefused,
)


def _breaker(**kw) -> CircuitBreaker:
    config = CircuitBreakerConfig(
        failure_threshold=2,
        timeout_seconds=60,
        non_failure_exceptions=(ReceiptRefused,),
        **kw,
    )
    return CircuitBreaker("test", config)


@pytest.mark.asyncio
async def test_refusals_never_open_the_circuit():
    breaker = _breaker()

    async def refuse():
        raise ReceiptRefused("ACTOR_NOT_CUSTODIAN", "not yours to close")

    for _ in range(10):
        with pytest.raises(ReceiptRefused):
            await breaker.call(refuse)

    assert breaker.state is CircuitState.CLOSED, (
        "governance refusals opened the circuit; a ledger that evaluates and "
        "rejects a proposal is healthy, and treating it as unreachable degrades "
        "every later receipt"
    )


@pytest.mark.asyncio
async def test_real_failures_still_open_the_circuit():
    """The breaker must keep doing its job.

    Widen the exemption to bare Exception and this fails: an unreachable ledger
    would never trip the breaker and every call would pay the full timeout.
    """
    breaker = _breaker()

    async def unreachable():
        raise ConnectionError("receiptgate down")

    for _ in range(2):
        with pytest.raises(ConnectionError):
            await breaker.call(unreachable)

    assert breaker.state is CircuitState.OPEN


@pytest.mark.asyncio
async def test_a_refusal_does_not_count_as_success_either():
    """It says nothing about reachability, in either direction."""
    breaker = _breaker()

    async def refuse():
        raise ReceiptRefused("OBLIGATION_ALREADY_ACCEPTED", "already held")

    with pytest.raises(ReceiptRefused):
        await breaker.call(refuse)

    assert breaker.stats.total_successes == 0


def test_the_refusal_codes_are_the_ones_the_ledger_actually_raises():
    """Guards against a typed code being renamed on one side only.

    A code missing here is silently treated as a transport failure: retried,
    buffered, and counted toward opening the circuit.
    """
    from legivellum import transitions

    model = transitions.load_model()
    declared = {
        code
        for transition in model["transitions"]
        for code in (transition.get("errors") or {}).values()
    }
    missing = declared - GOVERNANCE_REFUSALS
    assert not missing, (
        f"{sorted(missing)} are refusal codes in transitions.v1.json that "
        "AsyncGate would treat as transport failures"
    )
