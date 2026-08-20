"""Tenancy comes from the credential, never from the request body.

`tenant_id` was a required argument on every one of AsyncGate's tools, with
nothing checking it against the credential. Any holder of a key could act in any
tenant by typing a different value: create tasks there, claim leases there, read
another tenant's obligations. The isolation the rest of the stack assumes was a
convention the caller was trusted to follow.

Now the credential decides. A JWT names its tenant; deployments whose
credentials name none are single-tenant and resolve to a configured default. A
claim that contradicts the resolved tenant is refused rather than silently
overwritten, so a caller learns its claim was wrong instead of believing it was
honoured.

Known limitation, deliberately not papered over: the `User` model has no tenant
column, so a DB API key cannot name a tenant. Those deployments are
single-tenant. Giving keys their own tenant needs a schema change, not a
different value in `_resolve_tenant`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from asyncgate.auth.token import _resolve_tenant
from asyncgate.config import settings
from asyncgate.engine.errors import UnauthorizedError

SERVER = Path(__file__).resolve().parents[1] / "src" / "asyncgate" / "mcp" / "server.py"


def test_a_contradicting_claim_is_refused():
    with pytest.raises(UnauthorizedError) as exc:
        _resolve_tenant("tenant-a", "tenant-b")
    assert "tenant-a" in str(exc.value) and "tenant-b" in str(exc.value)


def test_a_matching_claim_is_allowed():
    """Redundant, not wrong. Existing callers that send it keep working."""
    assert _resolve_tenant("tenant-a", "tenant-a") == "tenant-a"


def test_the_credential_wins_when_no_claim_is_made():
    assert _resolve_tenant("tenant-a", None) == "tenant-a"


def test_an_unnamed_credential_falls_back_to_the_configured_tenant():
    assert _resolve_tenant(None, None) == settings.default_tenant_id


def test_the_fallback_never_overrides_a_named_tenant():
    """The default is for credentials that name no tenant, not an override."""
    assert _resolve_tenant("tenant-a", None) == "tenant-a"
    assert _resolve_tenant("tenant-a", None) != settings.default_tenant_id


def test_no_tool_accepts_tenant_id_as_an_argument():
    """The schema must not advertise something the server refuses to honour.

    Leaving it in the input schema would keep telling callers they choose their
    tenant while the server ignored them -- worse than either behaviour alone,
    because the request looks accepted.
    """
    source = SERVER.read_text(encoding="utf-8")
    assert '"tenant_id": {"type": "string"' not in source, (
        "a tool still declares tenant_id as an input property"
    )
    for required in re.findall(r'"required": \[([^\]]*)\]', source):
        assert '"tenant_id"' not in required, (
            f"a tool still requires tenant_id: {required}"
        )


def test_no_handler_reads_the_tenant_from_the_arguments():
    """Every tool must act in the tenant the credential resolved to.

    One handler still reading `arguments["tenant_id"]` is one tool through which
    the whole isolation boundary can be crossed.
    """
    source = SERVER.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if 'arguments["tenant_id"]' in line
    ]
    assert not offenders, offenders

    # It is read exactly once, before verification, so it can be checked
    # against the credential and refused if it disagrees.
    claims = [
        line.strip()
        for line in source.splitlines()
        if 'arguments.get("tenant_id")' in line
    ]
    assert len(claims) == 1, (
        f"expected the claim to be read once for checking, found {len(claims)}"
    )
