"""API dependencies."""

import logging
import secrets
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from asyncgate.auth.context import AuthContext
from asyncgate.config import Environment, settings
from asyncgate.db.base import async_session_factory

if TYPE_CHECKING:
    pass


logger = logging.getLogger("asyncgate.api")


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Get database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_tenant_id(
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
) -> UUID:
    """
    Extract tenant ID from request.

    In v0, we use a header for simplicity.
    In production, this would be extracted from JWT claims.
    """
    if x_tenant_id:
        try:
            return UUID(x_tenant_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid tenant ID format")

    # Default tenant for development
    if settings.allow_insecure_dev and settings.env == Environment.DEVELOPMENT:
        return UUID("00000000-0000-0000-0000-000000000000")

    raise HTTPException(status_code=401, detail="Missing tenant ID")


async def verify_api_key(
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    session: AsyncSession = Depends(get_db_session),
) -> AuthContext:
    """
    Verify API key authentication.

    Supports two modes:
    1. Database-backed API keys (ag_... prefix) - validates against auth_api_keys table
    2. Legacy shared token (ASYNCGATE_API_KEY env var) - for backward compatibility

    Returns AuthContext on success. Raises HTTPException on failure.

    Security: Fails closed - if neither mode is configured and we're not
    in explicit insecure dev mode, all requests are rejected.
    """
    from asyncgate.auth.middleware import API_KEY_PREFIX, verify_request_api_key

    # Insecure dev mode bypass (must be explicitly enabled)
    if settings.allow_insecure_dev and settings.env == Environment.DEVELOPMENT:
        return AuthContext(user=None, auth_type="insecure_dev", is_internal=False)

    # Extract API key from headers
    api_key = None
    if authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:]
    elif x_api_key:
        api_key = x_api_key

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing authorization. Use Authorization: Bearer <key> or X-API-Key header"
        )

    # Check if it's a database-backed API key (ag_ prefix)
    if api_key.startswith(API_KEY_PREFIX):
        headers = {}
        if authorization:
            headers["authorization"] = authorization
        if x_api_key:
            headers["x-api-key"] = x_api_key

        user = await verify_request_api_key(session, headers)
        if user:
            if not user.is_active:
                raise HTTPException(status_code=403, detail="User account is inactive")
            return AuthContext(
                user=user,
                auth_type="db_api_key",
                is_internal=bool(user.is_admin),
            )
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Fallback: Legacy shared token validation
    if settings.api_key:
        if secrets.compare_digest(api_key, settings.api_key):
            return AuthContext(user=None, auth_type="legacy_api_key", is_internal=False)
        raise HTTPException(status_code=401, detail="Invalid API key")

    # No valid auth method configured
    logger.error(
        "SECURITY VIOLATION: No API key configured (neither database keys nor legacy token). "
        "Set ASYNCGATE_API_KEY or create database API keys."
    )
    raise HTTPException(
        status_code=503,
        detail="Server misconfigured: authentication not properly initialized",
    )


def validate_auth_config() -> None:
    """
    Validate authentication configuration at startup.

    Ensures server cannot start with insecure configuration in non-dev environments.
    This prevents the footgun where missing api_key accidentally runs open.

    Raises:
        RuntimeError: If configuration is insecure for the current environment
    """
    # Insecure dev mode is only allowed in development
    if settings.allow_insecure_dev and settings.env != Environment.DEVELOPMENT:
        raise RuntimeError(
            f"SECURITY ERROR: allow_insecure_dev=true is only permitted in development. "
            f"Current environment: {settings.env.value}. "
            f"Set ASYNCGATE_ALLOW_INSECURE_DEV=false for {settings.env.value}."
        )

    # In non-insecure mode, we need either legacy API key or database auth
    # Note: Database auth doesn't require config - keys are created via admin API
    if not settings.allow_insecure_dev and not settings.api_key:
        logger.info(
            "No legacy ASYNCGATE_API_KEY configured. "
            "Database-backed API keys (ag_...) will be required for authentication."
        )

    # Log security status
    if settings.allow_insecure_dev:
        logger.warning(
            "=" * 80 + "\n"
            "WARNING: Running in INSECURE DEV MODE\n"
            "  - Authentication is DISABLED\n"
            "  - All API requests will be accepted without verification\n"
            "  - This mode is ONLY for local development\n"
            "  - Set ASYNCGATE_ALLOW_INSECURE_DEV=false for any deployment\n"
            + "=" * 80
        )
    elif settings.api_key:
        logger.info(
            f"Authentication enabled: Legacy API key + database keys for {settings.env.value}"
        )
    else:
        logger.info(
            f"Authentication enabled: Database-backed API keys only for {settings.env.value}"
        )
