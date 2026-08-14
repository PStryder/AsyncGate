"""Tests for bootstrapping AsyncGate's world-truth from MetaGate.

The behaviour that matters most here is what happens when MetaGate is *not*
cooperating. A bootstrap authority that can prevent a component from starting
is a hidden master, which the architecture explicitly forbids, so every failure
mode below must degrade to "start with configured values".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from asyncgate.integrations.metagate_client import (
    _endpoint_for_type,
    acknowledge_startup,
    bootstrap_from_metagate,
)


def _settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        metagate_endpoint="http://metagate:8000",
        metagate_api_key="mgk_test",
        metagate_component_key="asyncgate",
        metagate_bootstrap_timeout_seconds=1.0,
        receiptgate_endpoint=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _packet(**overrides: Any) -> dict[str, Any]:
    packet = {
        "manifest": "problemata:demo:0.1.0:manifest",
        "services": {
            "receiptgate-main": {
                "type": "receiptgate",
                "endpoint": "http://receiptgate:8000/mcp",
            },
            "depotgate-main": {"type": "depotgate", "endpoint": "http://depotgate:8000/mcp"},
        },
        "startup": {"startup_id": "11111111-1111-1111-1111-111111111111"},
    }
    packet.update(overrides)
    return packet


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture
def patch_client(monkeypatch: pytest.MonkeyPatch):
    """Replace httpx.AsyncClient with one bound to a mock transport."""

    def _apply(handler):
        real = httpx.AsyncClient

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = _transport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(
            "asyncgate.integrations.metagate_client.httpx.AsyncClient", factory
        )

    return _apply


def _ok(result: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return handler


async def test_resolves_receiptgate_endpoint_from_manifest(patch_client) -> None:
    patch_client(_ok({"packet": _packet()}))
    settings = _settings()

    result = await bootstrap_from_metagate(settings)

    assert result.succeeded
    assert result.manifest == "problemata:demo:0.1.0:manifest"
    assert settings.receiptgate_endpoint == "http://receiptgate:8000/mcp"
    assert result.applied == {"receiptgate_endpoint": "http://receiptgate:8000/mcp"}


async def test_explicit_configuration_is_not_overridden(patch_client) -> None:
    """An operator who set an endpoint said something specific."""
    patch_client(_ok({"packet": _packet()}))
    settings = _settings(receiptgate_endpoint="http://configured:9000/mcp")

    result = await bootstrap_from_metagate(settings)

    assert result.succeeded
    assert settings.receiptgate_endpoint == "http://configured:9000/mcp"
    assert result.applied == {}


async def test_not_attempted_without_endpoint() -> None:
    settings = _settings(metagate_endpoint=None)
    result = await bootstrap_from_metagate(settings)
    assert result.attempted is False
    assert result.succeeded is False
    assert settings.receiptgate_endpoint is None


async def test_unreachable_metagate_does_not_raise(patch_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    patch_client(handler)
    settings = _settings()

    result = await bootstrap_from_metagate(settings)

    assert result.attempted and not result.succeeded
    assert settings.receiptgate_endpoint is None


async def test_timeout_does_not_raise(patch_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    patch_client(handler)
    result = await bootstrap_from_metagate(_settings())
    assert result.attempted and not result.succeeded


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_http_errors_do_not_raise(patch_client, status: int) -> None:
    patch_client(lambda request: httpx.Response(status, json={"detail": "nope"}))
    result = await bootstrap_from_metagate(_settings())
    assert result.attempted and not result.succeeded


async def test_mcp_error_envelope_does_not_raise(patch_client) -> None:
    """No binding for this principal is a normal condition, not a crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": "ERROR", "message": "No active binding"}},
        )

    patch_client(handler)
    result = await bootstrap_from_metagate(_settings())
    assert result.attempted and not result.succeeded
    assert "No active binding" in (result.reason or "")


async def test_malformed_packet_does_not_raise(patch_client) -> None:
    patch_client(_ok({"packet": {"services": "not-a-dict"}}))
    settings = _settings()
    result = await bootstrap_from_metagate(settings)
    # Either it degrades cleanly or reports failure; it must not raise or
    # apply nonsense.
    assert settings.receiptgate_endpoint is None
    assert result.applied == {}


async def test_manifest_without_receiptgate_applies_nothing(patch_client) -> None:
    patch_client(_ok({"packet": _packet(services={"depot": {"type": "depotgate", "endpoint": "http://d/mcp"}})}))
    settings = _settings()

    result = await bootstrap_from_metagate(settings)

    assert result.succeeded
    assert result.applied == {}
    assert settings.receiptgate_endpoint is None


async def test_packet_returned_unwrapped_is_accepted(patch_client) -> None:
    """MetaGate returns the packet directly on some paths."""
    patch_client(_ok(_packet()))
    settings = _settings()
    result = await bootstrap_from_metagate(settings)
    assert result.succeeded
    assert settings.receiptgate_endpoint == "http://receiptgate:8000/mcp"


async def test_startup_is_acknowledged(patch_client) -> None:
    """Leaving the session open makes MetaGate's view of the mesh wrong."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if "metagate.startup_ready" in body:
            seen.append("ready")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"packet": _packet()}})

    patch_client(handler)
    settings = _settings()
    result = await bootstrap_from_metagate(settings)
    assert await acknowledge_startup(settings, result) is True
    assert seen == ["ready"]


async def test_failed_ack_is_not_fatal(patch_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if "metagate.startup_ready" in body:
            raise httpx.ConnectError("gone", request=request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"packet": _packet()}})

    patch_client(handler)
    settings = _settings()
    result = await bootstrap_from_metagate(settings)
    assert await acknowledge_startup(settings, result) is False


async def test_ack_skipped_when_bootstrap_failed() -> None:
    from asyncgate.integrations.metagate_client import BootstrapResult

    settings = _settings()
    failed = BootstrapResult(attempted=True, succeeded=False, reason="unreachable")
    assert await acknowledge_startup(settings, failed) is False


def test_endpoint_lookup_is_by_type_not_ref() -> None:
    """Refs are Problemata-authored names; types are contract vocabulary."""
    services = {
        "anything-at-all": {"type": "receiptgate", "endpoint": "http://r/mcp"},
    }
    assert _endpoint_for_type(services, "receiptgate") == "http://r/mcp"
    assert _endpoint_for_type(services, "depotgate") is None


def test_endpoint_lookup_ignores_malformed_entries() -> None:
    services = {
        "bad": "not-a-dict",
        "empty": {"type": "receiptgate"},
        "good": {"type": "receiptgate", "endpoint": "http://r/mcp"},
    }
    assert _endpoint_for_type(services, "receiptgate") == "http://r/mcp"
