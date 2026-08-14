"""Bootstrap AsyncGate's world-truth from MetaGate.

Every alignment note in the stack lists "bootstrap config from MetaGate" as
required contract behaviour, and until now no gate did it -- each read its
peers' endpoints from environment variables, so the topology a Problemata
described and the topology a component actually used were unrelated.

Two properties matter more than the feature itself:

Never block startup. MetaGate is a describe-only, non-blocking bootstrap
authority; it is not a dependency to be waited on. If it is unreachable, has no
binding for this principal, or returns something unusable, AsyncGate logs and
starts with its configured values. A bootstrap authority that can take the mesh
down is a hidden master, which is precisely what the architecture forbids.

Explicit configuration wins. An operator who sets ASYNCGATE_RECEIPTGATE_ENDPOINT
has said something specific; bootstrap fills gaps, it does not override
intent.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Primitive type -> the setting it resolves. Keyed by type rather than by
# service ref, because refs are Problemata-authored names ("receiptgate-main")
# while types are contract vocabulary.
_RECEIPTGATE_TYPE = "receiptgate"


class BootstrapResult:
    """Outcome of a bootstrap attempt.

    Never raises: callers treat a failed bootstrap as "carry on with configured
    values", so the failure reason is data rather than control flow.
    """

    def __init__(
        self,
        *,
        attempted: bool,
        succeeded: bool,
        reason: Optional[str] = None,
        manifest: Optional[str] = None,
        services: Optional[dict[str, Any]] = None,
        applied: Optional[dict[str, str]] = None,
        startup_id: Optional[str] = None,
    ) -> None:
        self.attempted = attempted
        self.succeeded = succeeded
        self.reason = reason
        self.manifest = manifest
        self.services = services or {}
        self.applied = applied or {}
        self.startup_id = startup_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"BootstrapResult(attempted={self.attempted}, succeeded={self.succeeded}, "
            f"manifest={self.manifest!r}, applied={self.applied!r}, reason={self.reason!r})"
        )


def _endpoint_for_type(services: Any, primitive_type: str) -> Optional[str]:
    """Return the first endpoint whose service declares the given type.

    Tolerates a malformed services block rather than raising: a bootstrap packet
    is external input, and a wrong shape should read as "nothing to apply", not
    as an AttributeError surfacing in the logs as the failure reason.
    """
    if not isinstance(services, dict):
        return None
    for service in services.values():
        if not isinstance(service, dict):
            continue
        if service.get("type") == primitive_type:
            endpoint = service.get("endpoint")
            if isinstance(endpoint, str) and endpoint:
                return endpoint
    return None


async def _mcp_call(
    client: httpx.AsyncClient,
    endpoint: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    api_key: Optional[str],
) -> dict[str, Any]:
    url = endpoint if endpoint.endswith("/mcp") else f"{endpoint.rstrip('/')}/mcp"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    response = await client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers=headers,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"{tool}: {body['error']}")
    result = body.get("result")
    if result is None:
        raise RuntimeError(f"{tool} returned no result")
    return result


async def bootstrap_from_metagate(settings: Any) -> BootstrapResult:
    """Resolve peer endpoints from MetaGate, filling only what is unset.

    Returns a BootstrapResult rather than raising, and mutates `settings` in
    place for the values it resolves.
    """
    endpoint = getattr(settings, "metagate_endpoint", None)
    if not endpoint:
        return BootstrapResult(attempted=False, succeeded=False, reason="metagate_endpoint not configured")

    component_key = getattr(settings, "metagate_component_key", None) or "asyncgate"
    api_key = getattr(settings, "metagate_api_key", None)
    timeout = getattr(settings, "metagate_bootstrap_timeout_seconds", 5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            result = await _mcp_call(
                client,
                endpoint,
                "metagate.bootstrap",
                {"component_key": component_key},
                api_key=api_key,
            )
            packet = result.get("packet", result)
            if not isinstance(packet, dict):
                raise RuntimeError(f"bootstrap packet is not an object: {type(packet).__name__}")
            services = packet.get("services") or {}
            manifest = packet.get("manifest")

            applied: dict[str, str] = {}
            receiptgate_endpoint = _endpoint_for_type(services, _RECEIPTGATE_TYPE)
            if receiptgate_endpoint and not settings.receiptgate_endpoint:
                settings.receiptgate_endpoint = receiptgate_endpoint
                applied["receiptgate_endpoint"] = receiptgate_endpoint
            elif receiptgate_endpoint and settings.receiptgate_endpoint != receiptgate_endpoint:
                # Worth saying out loud: the mesh believes something different
                # from what this component was told, and configuration wins.
                logger.info(
                    "metagate_bootstrap_endpoint_override configured=%s manifest=%s",
                    settings.receiptgate_endpoint,
                    receiptgate_endpoint,
                )

            startup_id = None
            startup = packet.get("startup")
            if isinstance(startup, dict):
                startup_id = startup.get("startup_id")

            logger.info(
                "metagate_bootstrap_ok manifest=%s services=%d applied=%s",
                manifest,
                len(services),
                sorted(applied) or "none",
            )
            return BootstrapResult(
                attempted=True,
                succeeded=True,
                manifest=manifest,
                services=services,
                applied=applied,
                startup_id=startup_id,
            )
    except Exception as exc:  # noqa: BLE001 - bootstrap must never take startup down
        logger.warning(
            "metagate_bootstrap_failed endpoint=%s error=%s; continuing with configured values",
            endpoint,
            exc,
        )
        return BootstrapResult(attempted=True, succeeded=False, reason=str(exc))


async def acknowledge_startup(settings: Any, result: BootstrapResult) -> bool:
    """Close the startup session MetaGate opened during bootstrap.

    perform_bootstrap opens a session with a deadline; never acking leaves it
    open until it expires and makes MetaGate's view of the mesh wrong. Failing
    to ack is not worth taking startup down for either.
    """
    if not result.succeeded or not result.startup_id:
        return False

    endpoint = getattr(settings, "metagate_endpoint", None)
    api_key = getattr(settings, "metagate_api_key", None)
    timeout = getattr(settings, "metagate_bootstrap_timeout_seconds", 5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await _mcp_call(
                client,
                endpoint,
                "metagate.startup_ready",
                {
                    "startup_id": result.startup_id,
                    # Required by the contract: MetaGate records it on the
                    # session and in the startup receipt it emits.
                    "build_version": getattr(settings, "build_version", None) or "0.1.0",
                },
                api_key=api_key,
            )
        logger.info("metagate_startup_acknowledged startup_id=%s", result.startup_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("metagate_startup_ack_failed startup_id=%s error=%s", result.startup_id, exc)
        return False
