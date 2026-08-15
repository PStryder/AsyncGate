"""Token verification helpers for MCP and internal auth flows."""

from __future__ import annotations

import secrets

from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from asyncgate.auth.context import AuthContext
from asyncgate.auth.middleware import API_KEY_PREFIX, verify_request_api_key
from asyncgate.config import Environment, settings
from asyncgate.engine.errors import UnauthorizedError

_jwt_key_cache: str | None = None


def _looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2


def _load_jwt_key() -> str:
    global _jwt_key_cache
    if _jwt_key_cache:
        return _jwt_key_cache

    if settings.jwt_public_key_path:
        with open(settings.jwt_public_key_path, encoding="utf-8") as handle:
            _jwt_key_cache = handle.read()
            return _jwt_key_cache

    if settings.jwt_private_key_path:
        with open(settings.jwt_private_key_path, encoding="utf-8") as handle:
            _jwt_key_cache = handle.read()
            return _jwt_key_cache

    raise UnauthorizedError("JWT verification key not configured")


async def verify_auth_token(
    token: str | None,
    session: AsyncSession,
    tenant_id: str | None = None,
    principal_id: str | None = None,
) -> AuthContext:
    """Verify MCP auth token via JWT or API key."""
    if settings.allow_insecure_dev and settings.env == Environment.DEVELOPMENT:
        return AuthContext(user=None, auth_type="insecure_dev", is_internal=False)

    if not token:
        raise UnauthorizedError("Missing authorization token")

    if _looks_like_jwt(token):
        key = _load_jwt_key()
        try:
            payload = jwt.decode(
                token,
                key,
                algorithms=[settings.jwt_algorithm],
                options={"verify_aud": False},
            )
        except JWTError as exc:
            raise UnauthorizedError(f"Invalid JWT: {exc}") from exc

        subject = payload.get("sub")
        tenant_claim = payload.get("tenant_id") or payload.get("tid")

        if principal_id and subject and subject != principal_id:
            raise UnauthorizedError("JWT subject does not match principal")
        if tenant_id and tenant_claim and tenant_claim != tenant_id:
            raise UnauthorizedError("JWT tenant does not match request")

        is_internal = bool(payload.get("is_admin") or payload.get("admin"))
        return AuthContext(user=None, auth_type="jwt", is_internal=is_internal)

    if token.startswith(API_KEY_PREFIX):
        headers = {"authorization": f"Bearer {token}"}
        user = await verify_request_api_key(session, headers)
        if user and user.is_active:
            return AuthContext(
                user=user,
                auth_type="db_api_key",
                is_internal=bool(user.is_admin),
            )
        raise UnauthorizedError("Invalid API key")

    if settings.api_key and secrets.compare_digest(token, settings.api_key):
        return AuthContext(user=None, auth_type="legacy_api_key", is_internal=False)

    raise UnauthorizedError("Invalid API key")
